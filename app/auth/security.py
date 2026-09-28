"""Credential primitives: password hashing, JWTs, API keys, login throttling."""

import hashlib
import hmac
import secrets
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from pwdlib import PasswordHash

ISSUER = "wage-catalog-service"
AUDIENCE = "wage-catalog-api"
API_KEY_PREFIX = "wtc"

_hasher = PasswordHash.recommended()  # Argon2id
# Verified when the username doesn't exist, so response timing doesn't reveal which usernames are real.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


# ---------- passwords ----------

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> tuple[bool, str | None]:
    """Returns (valid, rehashed); `rehashed` is set when the stored hash uses outdated parameters."""
    if password_hash is None:
        _hasher.verify(password, _DUMMY_HASH)
        return False, None
    return _hasher.verify_and_update(password, password_hash)


# ---------- access tokens ----------

def create_access_token(*, username: str, role: str, token_version: int, secret: str,
                        algorithm: str, minutes: int) -> tuple[str, int]:
    now = datetime.now(UTC)
    payload = {
        "sub": username, "role": role, "ver": token_version,
        "iat": now, "nbf": now, "exp": now + timedelta(minutes=minutes),
        "jti": uuid.uuid4().hex, "iss": ISSUER, "aud": AUDIENCE,
    }
    return jwt.encode(payload, secret, algorithm=algorithm), minutes * 60


def decode_access_token(token: str, *, secret: str, algorithm: str) -> dict:
    return jwt.decode(
        token, secret, algorithms=[algorithm], issuer=ISSUER, audience=AUDIENCE,
        options={"require": ["sub", "exp", "iat", "ver", "aud", "iss"]},
    )


# ---------- API keys ----------
# Format: wtc_<key_id>_<secret>. key_id is a public lookup handle; only SHA-256(secret) is stored.
# A fast hash is appropriate here (unlike passwords): the secret is 256 bits of randomness.

def new_api_key() -> tuple[str, str, str]:
    """Returns (key_id, full_key_to_show_once, secret_hash_to_store)."""
    key_id, secret = secrets.token_hex(6), secrets.token_urlsafe(32)
    return key_id, f"{API_KEY_PREFIX}_{key_id}_{secret}", _sha256(secret)


def split_api_key(key: str) -> tuple[str, str] | None:
    parts = key.split("_", 2)
    if len(parts) != 3 or parts[0] != API_KEY_PREFIX or len(parts[1]) != 12:
        return None
    return parts[1], parts[2]


def api_key_matches(secret: str, stored_hash: str) -> bool:
    return hmac.compare_digest(_sha256(secret), stored_hash)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ---------- login throttling ----------

class LoginThrottle:
    """Sliding-window lockout keyed on (username, client IP), plus a looser per-IP cap.

    Keying on the pair means an attacker can't lock a real user out just by knowing their username;
    the per-IP cap stops one client from spraying many usernames. State is per process — behind several
    workers/replicas, move it to Redis or enforce it at the gateway.
    """

    def __init__(self, max_per_user_ip: int, max_per_ip: int, window_seconds: int):
        self.max_per_user_ip = max_per_user_ip
        self.max_per_ip = max_per_ip
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _count(self, key: str, now: float) -> int:
        recent = [t for t in self._hits.get(key, ()) if now - t < self.window]
        if recent:
            self._hits[key] = recent
        else:
            self._hits.pop(key, None)
        return len(recent)

    def retry_after(self, username: str, ip: str) -> int | None:
        """Seconds until the caller may try again, or None if not locked."""
        now = time.monotonic()
        with self._lock:
            limits = ((f"u:{username.casefold()}|{ip}", self.max_per_user_ip), (f"ip:{ip}", self.max_per_ip))
            locked_keys = [k for k, limit in limits if self._count(k, now) >= limit]
            if not locked_keys:
                return None
            oldest = min(self._hits[k][0] for k in locked_keys)
            return max(1, int(self.window - (now - oldest)))

    def record_failure(self, username: str, ip: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in (f"u:{username.casefold()}|{ip}", f"ip:{ip}"):
                self._hits.setdefault(key, []).append(now)

    def reset(self, username: str, ip: str) -> None:
        with self._lock:
            self._hits.pop(f"u:{username.casefold()}|{ip}", None)

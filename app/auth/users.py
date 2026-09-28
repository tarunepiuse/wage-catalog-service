"""SQLite-backed store for users and API keys. Only hashes are stored, never secrets."""

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.auth.security import api_key_matches, hash_password, new_api_key

Role = Literal["admin", "user"]
ROLES = ("admin", "user")

# Append-only: each entry upgrades the schema by one version (tracked in PRAGMA user_version).
_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS users (
        username      TEXT PRIMARY KEY COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL CHECK (role IN ('admin', 'user')),
        is_active     INTEGER NOT NULL DEFAULT 1,
        token_version INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE api_keys (
        key_id       TEXT PRIMARY KEY,
        username     TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
        name         TEXT NOT NULL,
        secret_hash  TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        expires_at   TEXT,
        last_used_at TEXT,
        revoked_at   TEXT
    );
    CREATE INDEX ix_api_keys_username ON api_keys(username);
    """,
]

_LAST_USED_RESOLUTION = timedelta(minutes=1)  # avoid a DB write on every API-key request


def _now() -> datetime:
    return datetime.now(UTC)


class User(BaseModel):
    username: str
    role: Role
    is_active: bool
    created_at: datetime


class UserRecord(User):
    password_hash: str
    token_version: int


class ApiKeyInfo(BaseModel):
    key_id: str
    username: str
    name: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked: bool


class UserStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer across workers
            version = c.execute("PRAGMA user_version").fetchone()[0]
            for i, ddl in enumerate(_MIGRATIONS[version:], start=version + 1):
                c.executescript(ddl)
                c.execute(f"PRAGMA user_version = {i}")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:  # commits on success, rolls back on error
                yield conn
        finally:
            conn.close()

    def ping(self) -> None:
        with self._conn() as c:
            c.execute("SELECT 1").fetchone()

    # ---------- users ----------

    @staticmethod
    def _user(r: sqlite3.Row) -> UserRecord:
        return UserRecord(
            username=r["username"], role=r["role"], is_active=bool(r["is_active"]),
            created_at=r["created_at"], password_hash=r["password_hash"], token_version=r["token_version"],
        )

    def get(self, username: str) -> UserRecord | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return self._user(r) if r else None

    def list(self) -> list[User]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM users ORDER BY username").fetchall()
        return [User.model_validate(self._user(r).model_dump()) for r in rows]

    def create(self, username: str, password: str, role: Role = "user") -> User:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        created = _now()
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                    (username, hash_password(password), role, created.isoformat()),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"user '{username}' already exists") from None
        return User(username=username, role=role, is_active=True, created_at=created)

    def set_password(self, username: str, password: str) -> bool:
        """Also bumps token_version, which revokes every access token issued before the change."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE users SET password_hash = ?, token_version = token_version + 1 WHERE username = ?",
                (hash_password(password), username),
            )
        return cur.rowcount == 1

    def set_active(self, username: str, active: bool) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE users SET is_active = ?, token_version = token_version + 1 WHERE username = ?",
                (int(active), username),
            )
        return cur.rowcount == 1

    def revoke_tokens(self, username: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE users SET token_version = token_version + 1 WHERE username = ?", (username,))

    def update_hash(self, username: str, new_hash: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE users SET password_hash = ? WHERE username = ?", (new_hash, username))

    # ---------- API keys ----------

    @staticmethod
    def _key(r: sqlite3.Row) -> ApiKeyInfo:
        return ApiKeyInfo(
            key_id=r["key_id"], username=r["username"], name=r["name"], created_at=r["created_at"],
            expires_at=r["expires_at"], last_used_at=r["last_used_at"], revoked=r["revoked_at"] is not None,
        )

    def create_api_key(self, username: str, name: str, expires_in_days: int | None) -> tuple[ApiKeyInfo, str]:
        key_id, full_key, secret_hash = new_api_key()
        created = _now()
        expires = created + timedelta(days=expires_in_days) if expires_in_days else None
        with self._conn() as c:
            c.execute(
                "INSERT INTO api_keys (key_id, username, name, secret_hash, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (key_id, username, name, secret_hash, created.isoformat(), expires.isoformat() if expires else None),
            )
        info = ApiKeyInfo(key_id=key_id, username=username, name=name, created_at=created,
                          expires_at=expires, last_used_at=None, revoked=False)
        return info, full_key

    def list_api_keys(self, username: str | None = None) -> list[ApiKeyInfo]:
        sql, args = "SELECT * FROM api_keys", ()
        if username is not None:
            sql, args = sql + " WHERE username = ?", (username,)
        with self._conn() as c:
            rows = c.execute(sql + " ORDER BY created_at DESC", args).fetchall()
        return [self._key(r) for r in rows]

    def get_api_key(self, key_id: str) -> ApiKeyInfo | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM api_keys WHERE key_id = ?", (key_id,)).fetchone()
        return self._key(r) if r else None

    def revoke_api_key(self, key_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute("UPDATE api_keys SET revoked_at = ? WHERE key_id = ? AND revoked_at IS NULL",
                            (_now().isoformat(), key_id))
        return cur.rowcount == 1

    def authenticate_api_key(self, key_id: str, secret: str) -> UserRecord | None:
        """Returns the owning user if the key is valid, unexpired, unrevoked and the user is active."""
        with self._conn() as c:
            r = c.execute("SELECT * FROM api_keys WHERE key_id = ?", (key_id,)).fetchone()
            if r is None or r["revoked_at"] is not None or not api_key_matches(secret, r["secret_hash"]):
                return None
            now = _now()
            if r["expires_at"] and datetime.fromisoformat(r["expires_at"]) <= now:
                return None
            last = r["last_used_at"]
            if last is None or now - datetime.fromisoformat(last) > _LAST_USED_RESOLUTION:
                c.execute("UPDATE api_keys SET last_used_at = ? WHERE key_id = ?", (now.isoformat(), key_id))
            u = c.execute("SELECT * FROM users WHERE username = ?", (r["username"],)).fetchone()
        return self._user(u) if u and u["is_active"] else None

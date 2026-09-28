"""Authentication dependencies. Accepts a bearer JWT (people) or an X-API-Key (systems)."""

from dataclasses import dataclass
from typing import Annotated, Literal

import jwt
from fastapi import Depends, Request
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer

from app.auth.security import decode_access_token, split_api_key
from app.auth.users import UserStore
from app.core.errors import ApiError

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/token", auto_error=False)
api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False, description="Key from POST /v1/auth/api-keys")

_BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}


@dataclass(frozen=True)
class Principal:
    username: str
    role: str
    method: Literal["token", "api_key"]
    key_id: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _unauthorized(detail: str = "Missing, invalid or expired credentials") -> ApiError:
    return ApiError(401, "unauthorized", detail, headers=_BEARER_CHALLENGE)


def get_user_store(request: Request) -> UserStore:
    return request.app.state.user_store


def get_principal(
    request: Request,
    store: Annotated[UserStore, Depends(get_user_store)],
    token: Annotated[str | None, Depends(oauth2_scheme)],
    api_key: Annotated[str | None, Depends(api_key_scheme)],
) -> Principal:
    if token and api_key:
        raise ApiError(400, "ambiguous_credentials", "Send either a bearer token or an API key, not both")

    if api_key:
        parts = split_api_key(api_key)
        user = store.authenticate_api_key(*parts) if parts else None
        if user is None:
            raise _unauthorized("Invalid, expired or revoked API key")
        principal = Principal(user.username, user.role, "api_key", key_id=parts[0])
    elif token:
        settings = request.app.state.settings
        try:
            claims = decode_access_token(token, secret=settings.jwt_secret, algorithm=settings.jwt_algorithm)
        except jwt.PyJWTError:
            raise _unauthorized() from None
        user = store.get(claims["sub"])
        # token_version mismatch = password changed, logout, or user disabled after the token was issued.
        if user is None or not user.is_active or user.token_version != claims["ver"]:
            raise _unauthorized()
        principal = Principal(user.username, user.role, "token")
    else:
        raise _unauthorized()

    request.state.principal = principal.username  # picked up by the access log
    return principal


def require_session(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    """Credential and user management needs an interactive login: a leaked API key must not be able
    to mint more keys, change the password or create users."""
    if principal.method != "token":
        raise ApiError(403, "session_required", "This operation requires a user login token, not an API key")
    return principal


def require_admin(principal: Annotated[Principal, Depends(require_session)]) -> Principal:
    if not principal.is_admin:
        raise ApiError(403, "forbidden", "Admin role required")
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
SessionPrincipal = Annotated[Principal, Depends(require_session)]
AdminPrincipal = Annotated[Principal, Depends(require_admin)]
Store = Annotated[UserStore, Depends(get_user_store)]

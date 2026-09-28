"""Auth endpoints: login, session management, API keys, and admin user management."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field

from app.auth.deps import AdminPrincipal, CurrentPrincipal, SessionPrincipal, Store
from app.auth.security import create_access_token, verify_password
from app.auth.users import ApiKeyInfo, Role, User
from app.core.errors import ApiError
from app.core.logging import audit

router = APIRouter(prefix="/v1/auth", tags=["auth"])

USERNAME_PATTERN = r"^[A-Za-z0-9._@-]{3,64}$"
Password = Annotated[str, Field(min_length=12, max_length=256)]


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 token type, not a secret
    expires_in: int


class Me(BaseModel):
    username: str
    role: Role
    auth_method: str
    api_key_id: str | None = None


class NewUser(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    password: Password
    role: Role = "user"


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: Password


class PasswordReset(BaseModel):
    new_password: Password


class NewApiKey(BaseModel):
    name: str = Field(min_length=1, max_length=80, description="What the key is for, e.g. 'SuccessFactors nightly job'")
    expires_in_days: int | None = Field(90, ge=1, description="Omit or null for the configured maximum")


class ApiKeyCreated(ApiKeyInfo):
    api_key: str = Field(description="Shown once. Store it in a secret manager; it cannot be retrieved again.")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _not_found(what: str) -> ApiError:
    return ApiError(404, f"{what}_not_found", f"{what.replace('_', ' ').capitalize()} not found")


# ---------- session ----------

@router.post("/token", response_model=Token, summary="Log in (OAuth2 password flow) and get a bearer token")
def login(request: Request, form: Annotated[OAuth2PasswordRequestForm, Depends()], store: Store) -> Token:
    settings = request.app.state.settings
    throttle = request.app.state.login_throttle
    ip = _client_ip(request)

    if (wait := throttle.retry_after(form.username, ip)) is not None:
        audit("login_throttled", username=form.username, ip=ip)
        raise ApiError(429, "rate_limited", "Too many failed login attempts; try again later",
                       headers={"Retry-After": str(wait)})

    user = store.get(form.username)
    valid, rehashed = verify_password(form.password, user.password_hash if user else None)
    if not valid or user is None or not user.is_active:
        throttle.record_failure(form.username, ip)
        audit("login_failed", username=form.username, ip=ip,
              reason="disabled" if valid and user and not user.is_active else "bad_credentials")
        raise ApiError(401, "invalid_credentials", "Incorrect username or password",
                       headers={"WWW-Authenticate": "Bearer"})

    throttle.reset(form.username, ip)
    if rehashed:
        store.update_hash(user.username, rehashed)
    token, expires_in = create_access_token(
        username=user.username, role=user.role, token_version=user.token_version,
        secret=settings.jwt_secret, algorithm=settings.jwt_algorithm, minutes=settings.access_token_minutes,
    )
    audit("login_succeeded", username=user.username, ip=ip)
    return Token(access_token=token, expires_in=expires_in)


@router.get("/me", response_model=Me)
def me(principal: CurrentPrincipal) -> Me:
    return Me(username=principal.username, role=principal.role, auth_method=principal.method,
              api_key_id=principal.key_id)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT,
             summary="Revoke all of your access tokens (every session, every device)")
def logout(principal: SessionPrincipal, store: Store) -> None:
    store.revoke_tokens(principal.username)
    audit("tokens_revoked", username=principal.username)


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT,
             summary="Change your password (revokes your existing tokens)")
def change_password(body: PasswordChange, principal: SessionPrincipal, store: Store) -> None:
    user = store.get(principal.username)
    valid, _ = verify_password(body.current_password, user.password_hash)
    if not valid:
        raise ApiError(400, "invalid_current_password", "Current password is incorrect")
    store.set_password(principal.username, body.new_password)
    audit("password_changed", username=principal.username)


# ---------- API keys ----------

@router.post("/api-keys", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED,
             summary="Create an API key for system-to-system calls")
def create_api_key(body: NewApiKey, request: Request, principal: SessionPrincipal, store: Store) -> ApiKeyCreated:
    max_days = request.app.state.settings.api_key_max_days
    days = body.expires_in_days or max_days
    if days > max_days:
        raise ApiError(422, "invalid_input", f"expires_in_days may not exceed {max_days}")
    info, key = store.create_api_key(principal.username, body.name, days)
    audit("api_key_created", username=principal.username, key_id=info.key_id, expires_at=info.expires_at)
    return ApiKeyCreated(**info.model_dump(), api_key=key)


@router.get("/api-keys", response_model=list[ApiKeyInfo], summary="List API keys (admins see everyone's)")
def list_api_keys(principal: SessionPrincipal, store: Store) -> list[ApiKeyInfo]:
    return store.list_api_keys(None if principal.is_admin else principal.username)


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Revoke an API key")
def revoke_api_key(key_id: str, principal: SessionPrincipal, store: Store) -> None:
    info = store.get_api_key(key_id)
    if info is None or (info.username != principal.username and not principal.is_admin):
        raise _not_found("api_key")
    store.revoke_api_key(key_id)
    audit("api_key_revoked", username=principal.username, key_id=key_id, owner=info.username)


# ---------- user administration ----------

@router.get("/users", response_model=list[User])
def list_users(_: AdminPrincipal, store: Store) -> list[User]:
    return store.list()


@router.post("/users", response_model=User, status_code=status.HTTP_201_CREATED)
def create_user(body: NewUser, admin: AdminPrincipal, store: Store) -> User:
    try:
        user = store.create(body.username, body.password, body.role)
    except ValueError as e:
        raise ApiError(409, "user_exists", str(e)) from None
    audit("user_created", by=admin.username, username=user.username, role=user.role)
    return user


@router.post("/users/{username}/disable", status_code=status.HTTP_204_NO_CONTENT,
             summary="Disable a user (revokes their tokens and blocks their API keys)")
def disable_user(username: str, admin: AdminPrincipal, store: Store) -> None:
    if username.casefold() == admin.username.casefold():
        raise ApiError(400, "cannot_disable_self", "You cannot disable your own account")
    if not store.set_active(username, False):
        raise _not_found("user")
    audit("user_disabled", by=admin.username, username=username)


@router.post("/users/{username}/enable", status_code=status.HTTP_204_NO_CONTENT)
def enable_user(username: str, admin: AdminPrincipal, store: Store) -> None:
    if not store.set_active(username, True):
        raise _not_found("user")
    audit("user_enabled", by=admin.username, username=username)


@router.post("/users/{username}/password", status_code=status.HTTP_204_NO_CONTENT,
             summary="Reset a user's password (revokes their tokens)")
def reset_password(username: str, body: PasswordReset, admin: AdminPrincipal, store: Store) -> None:
    if not store.set_password(username, body.new_password):
        raise _not_found("user")
    audit("password_reset", by=admin.username, username=username)

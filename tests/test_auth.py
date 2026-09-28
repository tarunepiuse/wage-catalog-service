import sqlite3
from contextlib import closing

from app.auth.security import LoginThrottle
from tests.conftest import PASSWORD, login, upload_xlsx


def _key(client, headers, name="nightly job", **extra) -> dict:
    r = client.post("/v1/auth/api-keys", headers=headers, json={"name": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()


# ---------- tokens ----------

def test_protected_endpoints_require_credentials(client):
    for method, path in (("post", "/v1/catalog/jobs"), ("get", "/v1/catalog/jobs"),
                         ("get", "/v1/catalog/jobs/x/download"), ("get", "/v1/auth/me")):
        r = getattr(client, method)(path)
        assert r.status_code == 401, path
        assert r.headers["www-authenticate"] == "Bearer"
        assert r.json()["code"] == "unauthorized"


def test_garbage_and_foreign_tokens_rejected(client):
    import jwt
    forged = jwt.encode({"sub": "root", "ver": 0, "iat": 0, "exp": 9999999999, "aud": "wage-catalog-api",
                         "iss": "wage-catalog-service"}, "a-different-secret-of-sufficient-length!!")
    for token in ("not-a-jwt", forged):
        assert client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_login_is_case_insensitive_on_username(client):
    r = client.get("/v1/auth/me", headers=login(client, "ALICE"))
    assert r.json() == {"username": "alice", "role": "user", "auth_method": "token", "api_key_id": None}


def test_wrong_password_then_lockout_with_retry_after(client):
    for _ in range(5):
        r = client.post("/v1/auth/token", data={"username": "alice", "password": "wrong-password"})
        assert r.status_code == 401 and r.json()["code"] == "invalid_credentials"
    r = client.post("/v1/auth/token", data={"username": "alice", "password": PASSWORD})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


def test_unknown_user_gets_same_error_as_bad_password(client):
    r = client.post("/v1/auth/token", data={"username": "nobody", "password": "whatever-password"})
    assert r.status_code == 401 and r.json()["code"] == "invalid_credentials"


def test_throttle_is_per_user_and_ip_so_victims_are_not_locked_out():
    t = LoginThrottle(max_per_user_ip=3, max_per_ip=10, window_seconds=60)
    for _ in range(3):
        t.record_failure("alice", "6.6.6.6")
    assert t.retry_after("alice", "6.6.6.6")
    assert t.retry_after("alice", "10.0.0.1") is None  # the real alice, elsewhere, can still log in
    for i in range(10):
        t.record_failure(f"user{i}", "7.7.7.7")
    assert t.retry_after("someone-new", "7.7.7.7")  # password spraying from one IP is capped


def test_disable_revokes_tokens(client):
    h = login(client)
    client.app.state.user_store.set_active("alice", False)
    assert client.get("/v1/auth/me", headers=h).status_code == 401


def test_password_change_revokes_tokens(client):
    h = login(client)
    r = client.post("/v1/auth/me/password", headers=h,
                    json={"current_password": PASSWORD, "new_password": "a-brand-new-password"})
    assert r.status_code == 204
    assert client.get("/v1/auth/me", headers=h).status_code == 401
    login(client, "alice", "a-brand-new-password")


def test_logout_revokes_all_tokens(client):
    h1, h2 = login(client), login(client)
    assert client.post("/v1/auth/logout", headers=h1).status_code == 204
    assert client.get("/v1/auth/me", headers=h2).status_code == 401


# ---------- admin ----------

def test_user_management_is_admin_only(client):
    new = {"username": "carol", "password": "long-enough-password"}
    r = client.post("/v1/auth/users", headers=login(client, "alice"), json=new)
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    admin = login(client, "root")
    assert client.post("/v1/auth/users", headers=admin, json=new).status_code == 201
    assert client.post("/v1/auth/users", headers=admin, json=new).status_code == 409
    login(client, "carol", "long-enough-password")


def test_admin_password_reset_and_self_disable_guard(client):
    admin = login(client, "root")
    assert client.post("/v1/auth/users/alice/password", headers=admin,
                       json={"new_password": "reset-by-admin-123"}).status_code == 204
    login(client, "alice", "reset-by-admin-123")
    assert client.post("/v1/auth/users/root/disable", headers=admin).json()["code"] == "cannot_disable_self"
    assert client.post("/v1/auth/users/ghost/disable", headers=admin).status_code == 404


def test_weak_password_rejected_with_validation_problem(client):
    r = client.post("/v1/auth/users", headers=login(client, "root"), json={"username": "dave", "password": "short"})
    assert r.status_code == 422 and r.json()["code"] == "validation_failed"
    assert r.json()["errors"][0]["loc"] == ["body", "password"]


# ---------- API keys ----------

def test_api_key_full_flow(client):
    key = _key(client, login(client))
    assert key["api_key"].startswith(f"wtc_{key['key_id']}_")
    k = {"X-API-Key": key["api_key"]}

    assert client.get("/v1/auth/me", headers=k).json()["auth_method"] == "api_key"
    job = upload_xlsx(client, k).json()
    assert client.get(job["download_url"], headers=k).status_code == 200

    listed = client.get("/v1/auth/api-keys", headers=login(client)).json()
    assert listed[0]["key_id"] == key["key_id"] and "api_key" not in listed[0]
    assert listed[0]["last_used_at"] is not None

    assert client.delete(f"/v1/auth/api-keys/{key['key_id']}", headers=login(client)).status_code == 204
    assert client.get("/v1/auth/me", headers=k).status_code == 401


def test_api_key_cannot_manage_credentials(client):
    k = {"X-API-Key": _key(client, login(client, "root"))["api_key"]}
    for path, body in (("/v1/auth/api-keys", {"name": "x"}),
                       ("/v1/auth/users", {"username": "eve", "password": "x" * 12}),
                       ("/v1/auth/me/password", {"current_password": PASSWORD, "new_password": "y" * 12})):
        r = client.post(path, headers=k, json=body)
        assert r.status_code == 403 and r.json()["code"] == "session_required", path


def test_expired_revoked_and_disabled_owner_keys_fail(client):
    key = _key(client, login(client))
    k = {"X-API-Key": key["api_key"]}
    db = client.app.state.settings.user_db_path
    with closing(sqlite3.connect(db)) as c, c:  # closing() closes; the inner `c` commits
        c.execute("UPDATE api_keys SET expires_at = '2000-01-01T00:00:00+00:00'")
    assert client.get("/v1/auth/me", headers=k).status_code == 401

    key2 = {"X-API-Key": _key(client, login(client))["api_key"]}
    client.app.state.user_store.set_active("alice", False)
    assert client.get("/v1/auth/me", headers=key2).status_code == 401

    tampered = key["api_key"][:-2] + ("aa" if not key["api_key"].endswith("aa") else "bb")
    assert client.get("/v1/auth/me", headers={"X-API-Key": tampered}).status_code == 401


def test_users_cannot_revoke_each_others_keys(client):
    key = _key(client, login(client, "alice"))
    assert client.delete(f"/v1/auth/api-keys/{key['key_id']}", headers=login(client, "bob")).status_code == 404
    assert client.delete(f"/v1/auth/api-keys/{key['key_id']}", headers=login(client, "root")).status_code == 204


def test_api_key_expiry_is_capped(client):
    r = client.post("/v1/auth/api-keys", headers=login(client), json={"name": "x", "expires_in_days": 5000})
    assert r.status_code == 422


def test_both_credentials_is_ambiguous(client):
    h = {**login(client), "X-API-Key": _key(client, login(client))["api_key"]}
    assert client.get("/v1/auth/me", headers=h).json()["code"] == "ambiguous_credentials"


# ---------- cross-cutting ----------

def test_request_id_echoed_or_generated(client):
    assert client.get("/health/live", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"] == "abc-123"
    generated = client.get("/health/live", headers={"X-Request-ID": "bad id with spaces"}).headers["x-request-id"]
    assert generated != "bad id with spaces" and len(generated) == 32


def test_security_headers_present(client):
    h = client.get("/health/live").headers
    assert h["x-content-type-options"] == "nosniff" and h["cache-control"] == "no-store"


def test_readiness(client):
    assert client.get("/health/ready").json()["status"] == "ok"

from tests.conftest import register


def test_register_returns_user_and_token(client):
    resp = client.post(
        "/auth/register", json={"email": "Alice@Example.com", "password": "supersecret123"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["expires_in"] == 60 * 60 * 24
    assert body["user"]["email"] == "alice@example.com"
    assert "hashed_password" not in body["user"]


def test_register_rejects_duplicate_email(client):
    payload = {"email": "dupe@example.com", "password": "supersecret123"}
    assert client.post("/auth/register", json=payload).status_code == 201
    resp = client.post("/auth/register", json=payload)
    assert resp.status_code == 409


def test_register_rejects_short_password_and_bad_email(client):
    assert client.post(
        "/auth/register", json={"email": "a@example.com", "password": "short"}
    ).status_code == 422
    assert client.post(
        "/auth/register", json={"email": "not-an-email", "password": "supersecret123"}
    ).status_code == 422


def test_login_success_and_failure(client):
    client.post(
        "/auth/register", json={"email": "bob@example.com", "password": "supersecret123"}
    )

    ok = client.post(
        "/auth/login", json={"email": "bob@example.com", "password": "supersecret123"}
    )
    assert ok.status_code == 200
    assert ok.json()["access_token"]

    bad_pw = client.post(
        "/auth/login", json={"email": "bob@example.com", "password": "wrongpassword"}
    )
    assert bad_pw.status_code == 401

    missing = client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": "supersecret123"}
    )
    assert missing.status_code == 401


def test_me_requires_valid_token(client):
    headers, user = register(client)

    me = client.get("/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["id"] == user["id"]

    assert client.get("/auth/me").status_code == 401
    assert client.get(
        "/auth/me", headers={"Authorization": "Bearer garbage.token.here"}
    ).status_code == 401
    assert client.get("/auth/me", headers={"Authorization": "Basic abc"}).status_code == 401


def test_expired_token_is_rejected(client, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from jose import jwt

    from app.config import get_settings

    headers, user = register(client)
    settings = get_settings()
    expired = jwt.encode(
        {
            "sub": user["id"],
            "exp": int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401


def test_token_signed_with_wrong_secret_is_rejected(client):
    from jose import jwt

    forged = jwt.encode({"sub": "00000000-0000-0000-0000-000000000001"}, "not-the-secret")
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


def test_protected_endpoints_reject_anonymous(client):
    for method, path in [
        ("get", "/roadmaps"),
        ("post", "/roadmaps"),
        ("get", "/dashboard"),
    ]:
        resp = getattr(client, method)(path, **({"json": {}} if method == "post" else {}))
        assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"

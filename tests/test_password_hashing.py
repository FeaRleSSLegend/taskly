"""Direct coverage of the bcrypt helpers, which replaced passlib."""

import bcrypt

from app.security import hash_password, verify_password


def test_hash_is_a_real_bcrypt_hash_and_is_salted():
    first = hash_password("supersecret123")
    second = hash_password("supersecret123")

    assert first.startswith("$2b$")
    assert first != second, "each hash must use a fresh salt"
    assert bcrypt.checkpw(b"supersecret123", first.encode())


def test_verify_accepts_correct_and_rejects_wrong_password():
    hashed = hash_password("supersecret123")
    assert verify_password("supersecret123", hashed) is True
    assert verify_password("supersecret124", hashed) is False
    assert verify_password("", hashed) is False


def test_verify_handles_unicode_passwords():
    hashed = hash_password("pässwörd-ünïcode")
    assert verify_password("pässwörd-ünïcode", hashed) is True
    assert verify_password("passwörd-ünïcode", hashed) is False


def test_verify_returns_false_instead_of_raising_on_a_malformed_hash():
    assert verify_password("supersecret123", "not-a-bcrypt-hash") is False


def test_register_rejects_password_over_72_bytes(client):
    # 40 two-byte characters = 80 bytes, but only 40 characters.
    resp = client.post(
        "/auth/register", json={"email": "long@example.com", "password": "é" * 40}
    )
    assert resp.status_code == 422


def test_full_register_then_login_round_trip(client):
    creds = {"email": "roundtrip@example.com", "password": "supersecret123"}
    assert client.post("/auth/register", json=creds).status_code == 201

    login = client.post("/auth/login", json=creds)
    assert login.status_code == 200

    me = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["email"] == "roundtrip@example.com"

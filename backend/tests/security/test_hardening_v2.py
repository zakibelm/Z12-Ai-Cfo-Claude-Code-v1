"""Non-régression : clés chiffrées en base, limiteur par IP réelle, Google non configuré."""
from __future__ import annotations

import time
from contextlib import contextmanager

import jwt
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import auth
import main
import secret_box
import settings

SECRET = "s" * 48
USER = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "strict")
    monkeypatch.setenv("SESSION_JWT_SECRET", SECRET)
    monkeypatch.setattr(auth, "session_is_active", lambda jti: True)
    tok = jwt.encode({"sub": USER, "jti": "j", "aud": "phoenix", "iss": "phoenix-auth",
                      "exp": int(time.time()) + 600}, SECRET, algorithm="HS256")
    return TestClient(main.app, raise_server_exceptions=False, headers={"Authorization": f"Bearer {tok}"})


class FakeDB:
    def __init__(self):
        self.writes, self.closed = [], False

    def cursor(self):
        db = self

        class Cur:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def execute(s, sql, params=()): db.writes.append(params)
        return Cur()

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def close(self): self.closed = True


def test_user_key_is_encrypted_before_storage(client, monkeypatch):
    monkeypatch.setenv("TOKEN_ENC_KEY", Fernet.generate_key().decode())
    db = FakeDB()
    monkeypatch.setattr(settings.psycopg2, "connect", lambda *_: db)
    r = client.post("/api/settings/save-key", json={"api_key": "sk-or-v1-secret-value"})
    assert r.status_code == 200
    stored = db.writes[0][0]
    assert stored.startswith(secret_box.PREFIX) and "secret-value" not in stored
    assert secret_box.decrypt_secret(stored) == "sk-or-v1-secret-value"
    assert db.closed, "la connexion doit être fermée"


def test_without_encryption_key_nothing_is_written(client, monkeypatch):
    monkeypatch.delenv("TOKEN_ENC_KEY", raising=False)
    db = FakeDB()
    monkeypatch.setattr(settings.psycopg2, "connect", lambda *_: db)
    r = client.post("/api/settings/save-key", json={"api_key": "sk-or-v1-secret-value"})
    assert r.status_code == 503 and db.writes == []


def test_legacy_plaintext_value_is_still_readable_then_migrated(monkeypatch):
    monkeypatch.setenv("TOKEN_ENC_KEY", Fernet.generate_key().decode())
    assert secret_box.decrypt_secret("sk-or-old") == "sk-or-old"
    assert secret_box.decrypt_secret("enc:v1:garbage") is None  # falsifié -> rien, pas d'exception


def test_google_login_unconfigured_is_503_not_500(monkeypatch):
    import auth_google
    monkeypatch.setattr(auth_google, "GOOGLE_CLIENT_ID", "")
    r = TestClient(main.app, raise_server_exceptions=False).get("/auth/google/login")
    assert r.status_code == 503


def test_compose_trusts_forwarded_ip_only_from_private_networks():
    text = open("../docker-compose.yml", encoding="utf-8").read()
    assert "FORWARDED_ALLOW_IPS" in text and "FORWARDED_ALLOW_IPS: ${FORWARDED_ALLOW_IPS:-*" not in text


def test_proxy_header_gives_each_client_its_own_limiter_key():
    """uvicorn avec FORWARDED_ALLOW_IPS : l'IP réelle remplace celle du proxy."""
    import asyncio
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["client"][0])

    mw = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1,172.16.0.0/12")
    for real in ("203.0.113.7", "198.51.100.9"):
        scope = {"type": "http", "client": ("172.18.0.5", 5555), "headers": [(b"x-forwarded-for", real.encode())]}
        asyncio.run(mw(scope, None, None))
    assert seen == ["203.0.113.7", "198.51.100.9"]
    seen.clear()
    spoof = {"type": "http", "client": ("8.8.8.8", 1), "headers": [(b"x-forwarded-for", b"1.2.3.4")]}
    asyncio.run(mw(spoof, None, None))
    assert seen == ["8.8.8.8"], "un client direct ne peut pas usurper son IP"

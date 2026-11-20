"""Non-régression des correctifs issus de l'audit / red team du 20 novembre 2026."""
from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient

import auth
import auth_google
import auth_local
import main
import security_limits

SECRET = "s" * 48
USER = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _reset():
    security_limits.auth_limiter.reset()
    yield


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "strict")
    monkeypatch.setenv("SESSION_JWT_SECRET", SECRET)
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    monkeypatch.delenv("INTERNAL_SERVICE_SECRET", raising=False)


def _session_jwt(jti="j1", iss="phoenix-auth"):
    return jwt.encode({"sub": USER, "jti": jti, "aud": "phoenix", "iss": iss,
                       "exp": int(time.time()) + 600}, SECRET, algorithm="HS256")


class TestBearerSessionRevocation:
    def test_revoked_session_refused_as_bearer(self, strict, monkeypatch):
        monkeypatch.setattr(auth, "session_is_active", lambda jti: False)
        r = TestClient(main.app).get("/api/memory/list", headers={"Authorization": f"Bearer {_session_jwt()}"})
        assert r.status_code == 401

    def test_active_session_accepted_as_bearer(self, strict, monkeypatch):
        monkeypatch.setattr(auth, "session_is_active", lambda jti: True)
        r = TestClient(main.app).get("/api/memory/list", headers={"Authorization": f"Bearer {_session_jwt()}"})
        assert r.status_code != 401

    def test_bearer_without_jti_refused(self, strict, monkeypatch):
        monkeypatch.setattr(auth, "session_is_active", lambda jti: True)
        token = jwt.encode({"sub": USER, "aud": "phoenix", "iss": "phoenix-auth",
                            "exp": int(time.time()) + 600}, SECRET, algorithm="HS256")
        r = TestClient(main.app).get("/api/memory/list", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401


class TestGoogleSignupGate:
    class _Cur:
        def __init__(self, exists): self.exists = exists
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchone(self): return {"x": 1} if self.exists else None

    class _Conn(_Cur):
        def cursor(self): return self

    def _patch(self, monkeypatch, exists):
        monkeypatch.setattr(auth_google, "_conn", lambda: self._Conn(exists))

    def test_existing_user_can_login(self, monkeypatch):
        self._patch(monkeypatch, True)
        assert auth_google._google_signup_allowed("sub", "a@x.com")

    def test_new_user_refused_in_invite_mode(self, monkeypatch):
        self._patch(monkeypatch, False)
        monkeypatch.setenv("REGISTRATION_MODE", "invite")
        monkeypatch.delenv("GOOGLE_ALLOWED_EMAILS", raising=False)
        assert not auth_google._google_signup_allowed("sub", "a@x.com")

    def test_new_user_allowed_when_listed_or_open(self, monkeypatch):
        self._patch(monkeypatch, False)
        monkeypatch.setenv("REGISTRATION_MODE", "invite")
        monkeypatch.setenv("GOOGLE_ALLOWED_EMAILS", "Boss@x.com, other@x.com")
        assert auth_google._google_signup_allowed("sub", "boss@x.com")
        monkeypatch.delenv("GOOGLE_ALLOWED_EMAILS")
        monkeypatch.setenv("REGISTRATION_MODE", "open")
        assert auth_google._google_signup_allowed("sub", "anyone@x.com")

    def test_database_error_fails_closed(self, monkeypatch):
        def boom(): raise RuntimeError("db down")
        monkeypatch.setattr(auth_google, "_conn", boom)
        assert not auth_google._google_signup_allowed("sub", "a@x.com")


class TestSessionTokenNotStored:
    def test_fingerprint_is_not_the_token(self):
        token = _session_jwt()
        fp = auth_local._token_fingerprint(token)
        assert fp != token and len(fp) == 64 and token not in fp


class TestResetPasswordRateLimited:
    def test_reset_password_is_throttled(self, strict, monkeypatch):
        monkeypatch.setattr(auth_local, "_conn", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
        client = TestClient(main.app, raise_server_exceptions=False)
        codes = [client.post("/api/auth/local/reset-password",
                             json={"token": "x", "password": "a-long-enough-pass"}).status_code
                 for _ in range(security_limits.auth_limiter.max_calls + 3)]
        assert 429 in codes


class TestLogout:
    def test_get_logout_removed(self):
        assert TestClient(main.app).get("/api/auth/logout").status_code == 405

    def test_post_logout_ok_without_session(self):
        assert TestClient(main.app).post("/api/auth/logout").status_code == 200

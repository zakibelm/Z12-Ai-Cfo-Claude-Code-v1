"""Inscription sur invitation, contre un vrai PostgreSQL.

Lancés seulement si Z12_TEST_DATABASE_URL est défini (la CI fournit une base).
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

TEST_URL = os.getenv("Z12_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="Z12_TEST_DATABASE_URL non défini")

ADMIN, USER = {"X-User-Id": "9"}, {"X-User-Id": "12"}
PASSWORD = "Un-mot-de-passe-solide-2026!"


@pytest.fixture
def api(monkeypatch):
    import psycopg

    import auth_local
    import init_db_rag
    import invitations
    import main
    import security_limits

    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        for table in ("cfo_invitations", "user_sessions", "local_users"):
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        for statement in init_db_rag.STATEMENTS:
            if "local_users" in statement or "user_sessions" in statement:
                conn.execute(statement)
    monkeypatch.setattr(auth_local, "DATABASE_URL", TEST_URL)
    monkeypatch.setenv("ADMIN_USER_IDS", "9")
    for name in ("SESSION_JWT_SECRET", "SUPABASE_JWT_SECRET", "JWT_SECRET"):
        monkeypatch.setenv(name, "secret-de-test-" + "x" * 48)
    monkeypatch.delenv("REGISTRATION_MODE", raising=False)
    monkeypatch.setattr(security_limits.auth_limiter, "max_calls", 10_000)
    assert invitations.ensure_schema() is True
    yield TestClient(main.app)
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        for table in ("cfo_invitations", "user_sessions", "local_users"):
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


def _register(api, email, code=None):
    body = {"email": email, "password": PASSWORD}
    if code is not None:
        body["invite_code"] = code
    return api.post("/api/auth/local/register", json=body)


def _invite(api, **body):
    created = api.post("/api/admin/invitations", headers=ADMIN, json=body)
    assert created.status_code == 201
    return created.json()


def _count(table):
    import psycopg

    with psycopg.connect(TEST_URL) as conn:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_registration_requires_an_invitation_by_default(api):
    assert api.get("/api/auth/local/registration-mode").json() == {"mode": "invite"}
    for code in (None, "", "ABCD-EFGH-JKLM", "x", "%' OR 1=1 --"):
        refused = _register(api, "intrus@exemple.ca", code)
        assert refused.status_code == 403 and "invitation" in refused.json()["detail"]
    assert _count("local_users") == 0


def test_a_code_works_once_and_only_once(api):
    invitation = _invite(api, label="  Cabinet   Tremblay ", days=7)
    assert invitation["label"] == "Cabinet Tremblay" and invitation["status"] == "active"
    code = invitation["code"]
    assert len(code) == 14 and code.count("-") == 2

    # La saisie tolère les minuscules, les espaces et l'absence de tirets.
    ok = _register(api, "client@exemple.ca", f"  {code.lower().replace('-', ' ')} ")
    assert ok.status_code == 200 and ok.cookies.get("phoenix_session")
    again = _register(api, "autre@exemple.ca", code)
    assert again.status_code == 403
    assert _count("local_users") == 1

    listed = api.get("/api/admin/invitations", headers=ADMIN).json()["invitations"]
    assert listed[0]["status"] == "used" and listed[0]["used_by"] == "client@exemple.ca"
    # Le code n'est jamais renvoyé après sa création, ni stocké en clair.
    assert "code" not in listed[0]
    import psycopg

    with psycopg.connect(TEST_URL) as conn:
        stored = conn.execute("SELECT code_hash FROM cfo_invitations").fetchone()[0]
    assert code not in stored and len(stored) == 64


def test_a_failed_signup_does_not_burn_the_code(api):
    first = _invite(api)
    assert _register(api, "client@exemple.ca", first["code"]).status_code == 200
    second = _invite(api)
    # Courriel déjà pris : le compte n'est pas créé, et le code reste utilisable.
    assert _register(api, "client@exemple.ca", second["code"]).status_code == 409
    weak = api.post("/api/auth/local/register", json={"email": "b@exemple.ca", "password": "court", "invite_code": second["code"]})
    assert weak.status_code == 400
    assert _register(api, "nouveau@exemple.ca", second["code"]).status_code == 200


def test_expired_and_revoked_codes_are_refused(api):
    import psycopg

    expired = _invite(api, label="expirée")
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        conn.execute("UPDATE cfo_invitations SET expires_at = NOW() - interval '1 minute' WHERE id = %s", (expired["id"],))
    assert _register(api, "a@exemple.ca", expired["code"]).status_code == 403

    revoked = _invite(api, label="révoquée")
    assert api.delete(f"/api/admin/invitations/{revoked['id']}", headers=ADMIN).status_code == 200
    assert api.delete(f"/api/admin/invitations/{revoked['id']}", headers=ADMIN).status_code == 404
    assert _register(api, "b@exemple.ca", revoked["code"]).status_code == 403

    statuses = {i["label"]: i["status"] for i in api.get("/api/admin/invitations", headers=ADMIN).json()["invitations"]}
    assert statuses == {"expirée": "expired", "révoquée": "revoked"}
    assert _count("local_users") == 0


def test_only_the_administrator_manages_invitations(api):
    assert api.post("/api/admin/invitations", headers=USER, json={}).status_code == 403
    assert api.get("/api/admin/invitations", headers=USER).status_code == 403
    invitation = _invite(api)
    assert api.delete(f"/api/admin/invitations/{invitation['id']}", headers=USER).status_code == 403
    for days in (0, -3, 91):
        assert api.post("/api/admin/invitations", headers=ADMIN, json={"days": days}).status_code == 400
    assert _register(api, "client@exemple.ca", invitation["code"]).status_code == 200


def test_open_and_closed_modes(api, monkeypatch):
    monkeypatch.setenv("REGISTRATION_MODE", "open")
    assert api.get("/api/auth/local/registration-mode").json() == {"mode": "open"}
    assert _register(api, "libre@exemple.ca").status_code == 200

    monkeypatch.setenv("REGISTRATION_MODE", "closed")
    invitation = _invite(api)
    closed = _register(api, "ferme@exemple.ca", invitation["code"])
    assert closed.status_code == 403 and "fermées" in closed.json()["detail"]

    # Une valeur inconnue retombe sur le mode le plus sûr qui reste utilisable.
    monkeypatch.setenv("REGISTRATION_MODE", "n-importe-quoi")
    assert api.get("/api/auth/local/registration-mode").json() == {"mode": "invite"}
    assert _register(api, "x@exemple.ca").status_code == 403

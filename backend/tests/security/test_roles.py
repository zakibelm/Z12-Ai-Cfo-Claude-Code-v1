"""Rôles : un client n'atteint que sa liste blanche ; aucun rôle ne s'accorde par erreur."""
from __future__ import annotations

import re
import time

import jwt
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import auth
import main
import roles

SECRET = "s" * 48
CLIENT, CPA, SUPER, MEMBER = ("00000000-0000-0000-0000-00000000000%d" % i for i in (1, 2, 3, 4))


def _tok(uid):
    return jwt.encode({"sub": uid, "jti": "j" + uid, "aud": "phoenix", "iss": "phoenix-auth",
                       "exp": int(time.time()) + 600}, SECRET, algorithm="HS256")


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "strict")
    monkeypatch.setenv("SESSION_JWT_SECRET", SECRET)
    monkeypatch.setenv("SUPERADMIN_USER_IDS", SUPER)
    monkeypatch.setenv("ADMIN_USER_IDS", CPA)
    monkeypatch.setattr(auth, "session_is_active", lambda jti: True)
    table = {CLIENT: {"role": "client", "managed_by": CPA, "client_folder_id": "f1"}}
    monkeypatch.setattr(roles, "_row", lambda uid: table.get(uid))

    def client(uid):
        return TestClient(main.app, raise_server_exceptions=False, headers={"Authorization": f"Bearer {_tok(uid)}"})
    return client


def test_role_resolution(world):
    assert roles.role_of(SUPER) == "superadmin"
    assert roles.role_of(CPA) == "cpa_admin"
    assert roles.role_of(CLIENT) == "client"
    assert roles.role_of(MEMBER) == "member"


def test_client_can_only_reach_the_allowlist_across_every_route(world):
    """Énumère TOUTES les routes : hors liste blanche, un client reçoit 403 (jamais un 200 ni une erreur serveur)."""
    c = world(CLIENT)
    checked = leaked = 0
    for r in main.app.routes:
        if not isinstance(r, APIRoute):
            continue
        for method in r.methods - {"HEAD", "OPTIONS"}:
            path = re.sub(r"\{[^}]+\}", "x", r.path)
            if path.startswith(("/api/auth/", "/auth/", "/health")) or path == "/":
                continue  # publiques ou gérées par leur propre session
            checked += 1
            code = c.request(method, path, json={}).status_code
            if roles.is_client_path_allowed(method, path):
                assert code != 403, (method, path)
            else:
                assert code == 403, f"{method} {path} accessible à un client ({code})"
                leaked += 0
    assert checked > 40


def test_client_allowlist_does_not_open_neighbours(world):
    assert roles.is_client_path_allowed("GET", "/api/knowledge/list")
    assert not roles.is_client_path_allowed("POST", "/api/knowledge/reindex-all")
    assert not roles.is_client_path_allowed("GET", "/api/knowledge/abc/chunks")
    assert not roles.is_client_path_allowed("PATCH", "/api/knowledge/abc")
    assert not roles.is_client_path_allowed("POST", "/api/chat")
    assert not roles.is_client_path_allowed("GET", "/api/knowledge/images/9")


def test_member_and_cpa_keep_their_existing_access(world):
    for uid in (MEMBER, CPA, SUPER):
        assert world(uid).get("/api/knowledge/list").status_code == 200
    assert world(MEMBER).post("/api/chat", json={}).status_code != 403


def test_client_cannot_grant_roles_nor_read_admin_api(world):
    c = world(CLIENT)
    assert c.get("/api/admin/roles").status_code == 403
    assert c.put(f"/api/admin/roles/{MEMBER}", json={"role": "cpa_admin"}).status_code == 403
    assert world(MEMBER).get("/api/admin/roles").status_code == 403


def test_privilege_escalation_rules(world, monkeypatch):
    monkeypatch.setattr(roles, "_db_configured", lambda: False)  # atteint la règle avant l'écriture
    cpa, sup = world(CPA), world(SUPER)
    assert cpa.put("/api/admin/roles/00000000-0000-0000-0000-0000000000bb", json={"role": "superadmin"}).status_code == 400   # jamais par l'API
    assert cpa.put("/api/admin/roles/00000000-0000-0000-0000-0000000000bb", json={"role": "cpa_admin"}).status_code == 403    # réservé au superadmin
    assert cpa.put(f"/api/admin/roles/{CPA}", json={"role": "member"}).status_code == 403        # pas soi-même
    assert sup.put(f"/api/admin/roles/{CPA}", json={"role": "member"}).status_code == 403        # défini par la config
    assert cpa.put("/api/admin/roles/00000000-0000-0000-0000-0000000000bb", json={"role": "client"}).status_code == 503       # règle OK, base absente


def test_database_error_never_downgrades_to_member(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    roles.clear_cache()

    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(roles, "_conn", boom)
    with pytest.raises(Exception) as e:
        roles.role_of("00000000-0000-0000-0000-0000000000aa")
    assert getattr(e.value, "status_code", None) == 503


def test_admin_ids_include_database_cpa_admins(monkeypatch):
    import security_limits
    monkeypatch.setenv("ADMIN_USER_IDS", "a")
    monkeypatch.setenv("SUPERADMIN_USER_IDS", "s")
    monkeypatch.setattr(roles, "ids_with_role", lambda r: {"db-cpa"} if r == "cpa_admin" else set())
    assert security_limits.admin_ids() == {"a", "s", "db-cpa"}

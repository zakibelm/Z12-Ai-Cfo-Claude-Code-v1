"""Rôles de l'installation : superadmin · cpa_admin · member · client.

- superadmin : Zaki. Configure l'installation (SUPERADMIN_USER_IDS, variable d'environnement : jamais
  accordé par l'API). Ne lit PAS les documents des clients : leur lecture suit le propriétaire.
- cpa_admin  : le CPA du cabinet (ADMIN_USER_IDS conservé pour compatibilité, ou table cfo_user_roles).
- member     : comportement historique d'un compte (défaut).
- client     : client du CPA. REFUS PAR DÉFAUT : seules les routes de CLIENT_ALLOWED lui sont ouvertes,
  contrôlées au point unique d'identification (auth.get_current_user_id) — une route oubliée reste fermée.
"""
from __future__ import annotations

import os
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user_id

ROLES = ("superadmin", "cpa_admin", "member", "client")
GRANTABLE = ("cpa_admin", "member", "client")  # superadmin : uniquement par SUPERADMIN_USER_IDS
CACHE_TTL = 15.0

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS cfo_user_roles (
        user_id VARCHAR(255) PRIMARY KEY,
        role VARCHAR(20) NOT NULL CHECK (role IN ('cpa_admin', 'member', 'client')),
        managed_by VARCHAR(255),
        client_folder_id VARCHAR(255),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS idx_cfo_user_roles_role ON cfo_user_roles (role)",
]

# (méthode, expression sur le chemin exact) — tout le reste est refusé à un client.
CLIENT_ALLOWED: list[tuple[str, re.Pattern]] = [(m, re.compile(p)) for m, p in [
    ("GET", r"^/api/auth/local/me$"), ("GET", r"^/auth/me$"),
    ("POST", r"^/api/auth/local/logout$"), ("POST", r"^/api/auth/logout$"), ("POST", r"^/auth/logout$"),
    ("GET", r"^/api/knowledge/list$"), ("GET", r"^/api/knowledge/content-types$"),
    ("POST", r"^/api/knowledge/ingest$"),
    ("GET", r"^/api/knowledge/[^/]+$"), ("DELETE", r"^/api/knowledge/[^/]+$"),
    ("GET", r"^/api/privacy/my-data$"),
]]

_cache: dict[str, tuple[float, Optional[dict]]] = {}
_role_ids_cache: dict[str, tuple[float, set[str]]] = {}


def _split_env(name: str) -> set[str]:
    return {p.strip() for p in os.getenv(name, "").split(",") if p.strip()}


def superadmin_ids() -> set[str]:
    return _split_env("SUPERADMIN_USER_IDS")


def _db_configured() -> bool:
    return bool(os.getenv("DATABASE_URL", "").strip())


def _conn():
    import auth_local

    return auth_local._conn()


def _is_missing_table(exc: Exception) -> bool:
    return exc.__class__.__name__ == "UndefinedTable"


def ensure_schema() -> bool:
    if not _db_configured():
        return False
    try:
        with _conn() as c, c.cursor() as cur:
            for statement in SCHEMA:
                cur.execute(statement)
            c.commit()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Table des rôles indisponible : {type(e).__name__}")
        return False


def clear_cache() -> None:
    _cache.clear()
    _role_ids_cache.clear()


def _row(user_id: str) -> Optional[dict]:
    """Ligne de rôle en base. None = aucun rôle enregistré. Erreur de base = 503 (jamais « membre » par défaut)."""
    if not _db_configured():
        return None
    now = time.monotonic()
    hit = _cache.get(user_id)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT role, managed_by, client_folder_id FROM cfo_user_roles WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        if _is_missing_table(e):
            row = None
        else:
            print(f"[ERROR] rôles : {type(e).__name__}")
            raise HTTPException(503, "Service d'autorisation indisponible") from e
    row = dict(row) if row else None
    _cache[user_id] = (now, row)
    return row


def role_of(user_id: str | None) -> str:
    if not user_id:
        return "member"
    uid = str(user_id)
    if uid in superadmin_ids():
        return "superadmin"
    if uid in _split_env("ADMIN_USER_IDS"):
        return "cpa_admin"
    row = _row(uid)
    return row["role"] if row else "member"


def profile_of(user_id: str) -> dict:
    row = _row(str(user_id)) or {}
    return {"role": role_of(user_id), "managed_by": row.get("managed_by"),
            "client_folder_id": row.get("client_folder_id")}


def ids_with_role(role: str) -> set[str]:
    """Identifiants portant un rôle en base (tolérant : sans base ou en cas d'erreur, ensemble vide)."""
    if not _db_configured():
        return set()
    now = time.monotonic()
    hit = _role_ids_cache.get(role)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT user_id FROM cfo_user_roles WHERE role = %s", (role,))
            ids = {str(r["user_id"]) for r in cur.fetchall()}
    except Exception:  # noqa: BLE001
        ids = set()
    _role_ids_cache[role] = (now, ids)
    return ids


def is_client_path_allowed(method: str, path: str) -> bool:
    path = path.rstrip("/") or "/"
    return any(m == method.upper() and rx.match(path) for m, rx in CLIENT_ALLOWED)


def enforce_client_scope(user_id: str, method: str, path: str) -> None:
    """Appelé à chaque identification : un client n'atteint que la liste blanche."""
    if role_of(user_id) == "client" and not is_client_path_allowed(method, path):
        raise HTTPException(403, "Accès réservé au cabinet")


# ─── API d'administration des rôles ─────────────────────────────────────────

router = APIRouter(tags=["roles"])


def _caller_role(user_id: str) -> str:
    return role_of(user_id)


async def _admin(user_id: str = Depends(get_current_user_id)) -> str:
    if _caller_role(user_id) not in ("superadmin", "cpa_admin"):
        raise HTTPException(403, "Réservé à l'administrateur")
    return user_id


class RoleBody(BaseModel):
    role: str
    client_folder_id: str | None = None


@router.get("/api/admin/roles")
async def list_roles(caller: str = Depends(_admin)):
    rows = []
    if _db_configured():
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT user_id, role, managed_by, client_folder_id, updated_at FROM cfo_user_roles ORDER BY updated_at DESC")
            rows = [{**dict(r), "updated_at": str(r["updated_at"])} for r in cur.fetchall()]
    return {"roles": rows, "superadmins": sorted(superadmin_ids()) if _caller_role(caller) == "superadmin" else [],
            "env_admins": sorted(_split_env("ADMIN_USER_IDS"))}


@router.put("/api/admin/roles/{target_id}")
async def set_role(target_id: str, body: RoleBody, caller: str = Depends(_admin)):
    caller_role = _caller_role(caller)
    if body.role not in GRANTABLE:
        raise HTTPException(400, "Rôle invalide (le super administrateur se définit par configuration serveur)")
    if target_id == caller:
        raise HTTPException(403, "Impossible de modifier son propre rôle")
    target_role = role_of(target_id)
    if target_id in superadmin_ids() or target_id in _split_env("ADMIN_USER_IDS"):
        raise HTTPException(403, "Ce compte est défini par la configuration du serveur")
    if caller_role == "cpa_admin" and (body.role == "cpa_admin" or target_role == "cpa_admin"):
        raise HTTPException(403, "Seul le super administrateur gère les administrateurs du cabinet")
    if not _db_configured():
        raise HTTPException(503, "Base de données indisponible")
    folder = body.client_folder_id if body.role == "client" else None
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """INSERT INTO cfo_user_roles (user_id, role, managed_by, client_folder_id, updated_at)
               VALUES (%s, %s, %s, %s, NOW())
               ON CONFLICT (user_id) DO UPDATE SET role = EXCLUDED.role, managed_by = EXCLUDED.managed_by,
                 client_folder_id = EXCLUDED.client_folder_id, updated_at = NOW()""",
            (target_id, body.role, caller, folder))
        c.commit()
    clear_cache()
    return {"user_id": target_id, "role": body.role, "client_folder_id": folder}


@router.delete("/api/admin/roles/{target_id}")
async def reset_role(target_id: str, caller: str = Depends(_admin)):
    if target_id in superadmin_ids() or target_id in _split_env("ADMIN_USER_IDS") or target_id == caller:
        raise HTTPException(403, "Ce compte est défini par la configuration du serveur ou est le vôtre")
    if _caller_role(caller) == "cpa_admin" and role_of(target_id) == "cpa_admin":
        raise HTTPException(403, "Seul le super administrateur gère les administrateurs du cabinet")
    if not _db_configured():
        raise HTTPException(503, "Base de données indisponible")
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM cfo_user_roles WHERE user_id = %s", (target_id,))
        c.commit()
    clear_cache()
    return {"user_id": target_id, "role": "member"}

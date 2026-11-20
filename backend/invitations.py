"""Inscription sur invitation.

Par défaut, un compte ne se crée qu'avec un code d'invitation émis par un administrateur :
la plateforme traite des données financières et chaque compte consomme le crédit du
fournisseur de modèles. Le mode se règle sans changer le code :

    REGISTRATION_MODE = invite  (défaut)  code obligatoire, à usage unique, avec échéance
                        open              inscription libre
                        closed            aucune inscription

Un code n'est jamais stocké en clair : seule son empreinte l'est. Il est affiché une seule
fois, à sa création.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from security_limits import admin_user

MODES = ("invite", "open", "closed")
DEFAULT_VALIDITY_DAYS = 14
MAX_VALIDITY_DAYS = 90
MAX_LABEL_CHARS = 120
INVALID_CODE = "Code d'invitation invalide, expiré ou déjà utilisé"
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sans 0/O ni 1/I, pour la dictée et la recopie

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS cfo_invitations (
        id          BIGSERIAL PRIMARY KEY,
        code_hash   TEXT NOT NULL UNIQUE,
        label       TEXT,
        created_by  TEXT NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at  TIMESTAMPTZ NOT NULL,
        used_at     TIMESTAMPTZ,
        used_by     TEXT,
        revoked_at  TIMESTAMPTZ
    )""",
]


def registration_mode() -> str:
    mode = os.getenv("REGISTRATION_MODE", "invite").strip().lower()
    return mode if mode in MODES else "invite"


def normalize_code(code: str | None) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def hash_code(code: str | None) -> str:
    return hashlib.sha256(normalize_code(code).encode()).hexdigest()


def generate_code() -> str:
    """Code de 12 caractères (60 bits), présenté en trois groupes : ABCD-EFGH-JKLM."""
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def _conn():
    import auth_local

    return auth_local._conn()


def ensure_schema() -> bool:
    try:
        with _conn() as c, c.cursor() as cur:
            for statement in SCHEMA:
                cur.execute(statement)
            c.commit()
        return True
    except Exception as e:  # noqa: BLE001 — sans base, l'inscription sur invitation reste fermée
        print(f"[WARN] Table des invitations indisponible : {type(e).__name__}")
        return False


def consume(cur, code: str | None, used_by: str) -> bool:
    """Marque le code comme utilisé, dans la transaction de création du compte. Une seule
    requête : deux inscriptions simultanées ne peuvent pas utiliser le même code."""
    if len(normalize_code(code)) < 8:
        return False
    cur.execute(
        """UPDATE cfo_invitations SET used_at = NOW(), used_by = %s
           WHERE code_hash = %s AND used_at IS NULL AND revoked_at IS NULL AND expires_at > NOW()
           RETURNING id""",
        (used_by, hash_code(code)),
    )
    return cur.fetchone() is not None


def _status(row: dict, now: dt.datetime) -> str:
    if row.get("revoked_at"):
        return "revoked"
    if row.get("used_at"):
        return "used"
    return "expired" if row["expires_at"] <= now else "active"


def _public(row: dict) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    return {
        "id": row["id"], "label": row.get("label"), "status": _status(row, now),
        "created_at": row["created_at"].isoformat(), "expires_at": row["expires_at"].isoformat(),
        "used_at": row["used_at"].isoformat() if row.get("used_at") else None,
        "used_by": row.get("used_by"),
    }


def create_invitation(created_by: str, label: str | None, days: int) -> dict:
    days = max(1, min(int(days), MAX_VALIDITY_DAYS))
    label = " ".join((label or "").split())[:MAX_LABEL_CHARS] or None
    code = generate_code()
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """INSERT INTO cfo_invitations (code_hash, label, created_by, expires_at)
               VALUES (%s, %s, %s, NOW() + make_interval(days => %s)) RETURNING *""",
            (hash_code(code), label, created_by, days),
        )
        row = cur.fetchone()
        c.commit()
    return {**_public(row), "code": code}


def list_invitations() -> list[dict]:
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT * FROM cfo_invitations ORDER BY created_at DESC LIMIT 200")
        return [_public(row) for row in cur.fetchall()]


def revoke_invitation(invitation_id: int) -> bool:
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE cfo_invitations SET revoked_at = NOW() WHERE id = %s AND used_at IS NULL AND revoked_at IS NULL RETURNING id",
            (invitation_id,),
        )
        done = cur.fetchone() is not None
        c.commit()
    return done


# ─── Routes ──────────────────────────────────────────────────────────────────

router = APIRouter(tags=["invitations"])


class InvitationBody(BaseModel):
    label: str | None = None
    days: int = DEFAULT_VALIDITY_DAYS


@router.get("/api/auth/local/registration-mode")
async def get_registration_mode():
    """Public : l'écran d'inscription sait s'il doit demander un code."""
    return {"mode": registration_mode()}


@router.get("/api/admin/invitations")
async def invitations_list(_: str = Depends(admin_user)):
    return {"mode": registration_mode(), "invitations": list_invitations()}


@router.post("/api/admin/invitations", status_code=201)
async def invitations_create(body: InvitationBody, user_id: str = Depends(admin_user)):
    if body.days < 1 or body.days > MAX_VALIDITY_DAYS:
        raise HTTPException(400, f"Validité entre 1 et {MAX_VALIDITY_DAYS} jours")
    return create_invitation(user_id, body.label, body.days)


@router.delete("/api/admin/invitations/{invitation_id}")
async def invitations_revoke(invitation_id: int, _: str = Depends(admin_user)):
    if not revoke_invitation(invitation_id):
        raise HTTPException(404, "Invitation introuvable ou déjà utilisée")
    return {"revoked": invitation_id}

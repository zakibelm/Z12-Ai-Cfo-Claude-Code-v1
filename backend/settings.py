import os
from contextlib import contextmanager
import psycopg2
from fastapi import APIRouter, Request, HTTPException, Header
from pydantic import BaseModel
from typing import Optional
import secret_box
from auth import get_current_user_id

router = APIRouter()
DATABASE_URL = os.environ.get("DATABASE_URL", "")

@contextmanager
def _db():
    """Connexion fermée à coup sûr ; validée (commit) en cas de succès, annulée sinon.
    (Avec psycopg2, « with connect() » seul ne ferme pas la connexion.)"""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


class ApiKeyPayload(BaseModel):
    api_key: str

@router.post("/save-key")
async def save_api_key(
    payload: ApiKeyPayload,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_user_id: Optional[str] = Header(None),
    x_internal_service: Optional[str] = Header(None),
):
    """Save OpenRouter API key for the authenticated user."""
    user_id = await get_current_user_id(request, authorization, x_user_id, x_internal_service)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    key = payload.api_key.strip()
    if not key.startswith("sk-or-") or len(key) > 300:
        raise HTTPException(status_code=400, detail="Invalid OpenRouter key format")
    try:
        stored = secret_box.encrypt_secret(key)
    except secret_box.SecretBoxUnavailable:
        # Jamais de clé en clair : on refuse plutôt que de dégrader la protection.
        raise HTTPException(status_code=503, detail="Stockage sécurisé des clés non configuré (TOKEN_ENC_KEY)")
    try:
        with _db() as conn, conn.cursor() as cur:
            cur.execute("UPDATE local_users SET openrouter_key = %s WHERE id::text = %s", (stored, str(user_id)))
        return {"status": "saved"}
    except Exception as e:
        print(f"[ERROR] settings: {type(e).__name__}")
        raise HTTPException(status_code=500, detail="Erreur serveur")


def encrypt_legacy_keys() -> int:
    """Chiffre les clés enregistrées en clair avant ce correctif. Idempotent ; retourne le nombre migré."""
    if not DATABASE_URL:
        return 0
    n = 0
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id::text, openrouter_key FROM local_users "
                    "WHERE openrouter_key IS NOT NULL AND openrouter_key <> '' AND openrouter_key NOT LIKE %s",
                    (secret_box.PREFIX + "%",))
        for uid, plain in cur.fetchall():
            cur.execute("UPDATE local_users SET openrouter_key = %s WHERE id::text = %s",
                        (secret_box.encrypt_secret(plain), uid))
            n += 1
    return n


@router.get("/get-key-status")
async def get_key_status(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_user_id: Optional[str] = Header(None),
    x_internal_service: Optional[str] = Header(None),
):
    """Check if user has a saved OpenRouter key (without revealing it)."""
    user_id = await get_current_user_id(request, authorization, x_user_id, x_internal_service)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    try:
        with _db() as conn, conn.cursor() as cur:
            cur.execute("SELECT openrouter_key IS NOT NULL AND openrouter_key <> '' FROM local_users WHERE id::text = %s",
                        (str(user_id),))
            row = cur.fetchone()
        return {"has_key": bool(row and row[0])}
    except Exception as e:
        print(f"[ERROR] settings: {type(e).__name__}")
        raise HTTPException(status_code=500, detail="Erreur serveur")

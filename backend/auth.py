"""
auth.py — ZAKI OS Z12 AI CFO Suite
─────────────────────────────────────────────────────────────────────────────
Dependency FastAPI qui résout l'utilisateur courant.

Sources d'identité acceptées, dans l'ordre :
1. Secret de service interne (header X-Internal-Service == INTERNAL_SERVICE_SECRET)
2. Authorization: Bearer <jwt> signé (Supabase ou session maison)
3. Cookie phoenix_session signé ET dont le jti est actif en base
4. AUTH_MODE=dev uniquement : header X-User-Id ou utilisateur par défaut

Règles de sécurité (audit 2026-11) :
- AUTH_MODE vaut "strict" par défaut. Le mode "dev" doit être demandé explicitement.
- Aucun secret par défaut : sans secret configuré, le mode strict refuse tout.
- Aucun JWT n'est accepté sans vérification de signature en mode strict.
- Un nom de service ("whatsapp") n'est jamais une preuve d'identité.

Variables d'env :
    AUTH_MODE                  → "strict" (défaut) | "dev"
    SESSION_JWT_SECRET         → signe les cookies de session
    SUPABASE_JWT_SECRET        → valide les tokens Supabase
    INTERNAL_SERVICE_SECRET    → appels service-à-service (WhatsApp)
    DEV_DEFAULT_USER_ID        → utilisateur des requêtes non authentifiées en dev
"""

from __future__ import annotations

import hmac
import os
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

from observability import get_logger, set_request_id

log = get_logger(__name__)

INTERNAL_SERVICE_USER_ID = "whatsapp-service-user"
COOKIE_NAME = "phoenix_session"

# Valeurs connues pour avoir circulé (exemples, anciens défauts) : refusées.
_FORBIDDEN_SECRETS = {
    "phoenix-local-secret",
    "change-me-random-256bit-secret",
    "REMPLACER_PAR_openssl_rand_hex_32",
    "REMPLACER_openssl_rand_hex_32",
    "secret",
    "changeme",
}
_MIN_SECRET_LENGTH = 32


# ─────────────────────────────────────────────────────────────────────────────
# Configuration (lue à chaque appel : testable, et pas de valeur figée à l'import)
# ─────────────────────────────────────────────────────────────────────────────

def auth_mode() -> str:
    """Mode d'authentification. Tout ce qui n'est pas exactement "dev" est strict."""
    return "dev" if os.getenv("AUTH_MODE", "strict").strip().lower() == "dev" else "strict"


def _usable_secret(value: str | None) -> str | None:
    """Retourne le secret s'il est acceptable, sinon None."""
    value = (value or "").strip()
    if not value or value in _FORBIDDEN_SECRETS:
        return None
    if auth_mode() == "strict" and len(value) < _MIN_SECRET_LENGTH:
        return None
    return value


def get_session_secret() -> str | None:
    """Secret de signature des sessions. None si non configuré (jamais de défaut)."""
    return _usable_secret(os.getenv("SESSION_JWT_SECRET")) or _usable_secret(os.getenv("SUPABASE_JWT_SECRET"))


def require_session_secret() -> str:
    """Secret de session, ou 503 : on ne signe jamais avec une valeur par défaut."""
    secret = get_session_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="Authentification non configurée sur le serveur")
    return secret


def get_internal_service_secret() -> str | None:
    return _usable_secret(os.getenv("INTERNAL_SERVICE_SECRET"))


def assert_production_config() -> list[str]:
    """Vérifie la configuration au démarrage.

    En mode strict, lève RuntimeError si un secret obligatoire manque ou est faible :
    mieux vaut un backend qui ne démarre pas qu'un backend qui signe avec un défaut.
    Retourne la liste des avertissements (mode dev).
    """
    problems: list[str] = []
    if not get_session_secret():
        problems.append(
            f"SESSION_JWT_SECRET absent, connu ou trop court (< {_MIN_SECRET_LENGTH} caractères) "
            "— générer avec : openssl rand -hex 32"
        )
    if auth_mode() == "strict" and problems:
        raise RuntimeError("Configuration d'authentification invalide : " + " ; ".join(problems))
    if auth_mode() == "dev":
        problems.append("AUTH_MODE=dev : authentification désactivée, ne jamais utiliser en production")
    return problems


# ─────────────────────────────────────────────────────────────────────────────
# Vérifications
# ─────────────────────────────────────────────────────────────────────────────

def is_internal_service_call(header_value: str | None) -> bool:
    """True si le header porte le secret de service (comparaison à temps constant)."""
    secret = get_internal_service_secret()
    if not secret or not header_value:
        return False
    return hmac.compare_digest(header_value.encode(), secret.encode())


def _decode_bearer(token: str) -> dict | None:
    """Décode un JWT Bearer signé. Retourne le payload ou None si invalide."""
    try:
        import jwt  # type: ignore
    except ImportError:
        log.error("auth.jwt.pyjwt_missing")
        return None

    supabase_secret = _usable_secret(os.getenv("SUPABASE_JWT_SECRET"))
    session_secret = _usable_secret(os.getenv("SESSION_JWT_SECRET"))

    attempts: list[tuple[str, dict]] = []
    if supabase_secret:
        attempts.append((supabase_secret, {"options": {"verify_aud": False}}))
    if session_secret and session_secret != supabase_secret:
        attempts.append((session_secret, {"audience": "phoenix"}))

    for secret, kwargs in attempts:
        try:
            return jwt.decode(token, secret, algorithms=["HS256"], **kwargs)
        except Exception as e:  # noqa: BLE001 — toute erreur JWT = token refusé
            log.debug("auth.jwt.decode_failed", error=str(e))

    # Décodage sans signature : uniquement en dev ET sans aucun secret configuré.
    if auth_mode() == "dev" and not attempts:
        try:
            return jwt.decode(token, options={"verify_signature": False})
        except Exception as e:  # noqa: BLE001
            log.debug("auth.jwt.unsigned_decode_failed", error=str(e))
    return None


def _is_valid_user_id(s: str) -> bool:
    """Accepte les identifiants entiers (auth locale) ou UUID (Supabase / Google)."""
    if s and s.isdigit():
        return True
    try:
        uuid.UUID(s)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def session_is_active(jti: str) -> bool:
    """Vérifie en base que la session n'est ni révoquée ni expirée.

    Tolère les deux schémas de `user_sessions` (is_active / revoked_at).
    Toute erreur = session refusée (fail closed).
    """
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url or not jti:
        return False
    try:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(database_url, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_jsonb(s) AS row FROM user_sessions s WHERE s.jti = %s LIMIT 1", (jti,))
            found = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        log.warning("auth.session.lookup_failed", error=str(e))
        return False

    if not found or not found.get("row"):
        return False
    row = found["row"]
    if row.get("is_active") is False or row.get("revoked_at"):
        return False
    expires_at = row.get("expires_at")
    if expires_at:
        try:
            expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= datetime.now(timezone.utc):
                return False
        except ValueError:
            return False
    return True


def _user_from_cookie(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    secret = get_session_secret()
    if not token or not secret:
        return None
    try:
        import jwt  # type: ignore

        payload = jwt.decode(token, secret, algorithms=["HS256"], audience="phoenix")
    except Exception as e:  # noqa: BLE001
        log.debug("auth.cookie.invalid", error=str(e))
        return None

    uid = str(payload.get("sub") or payload.get("user_id") or "")
    jti = payload.get("jti")
    if not uid or not _is_valid_user_id(uid) or not jti:
        return None
    # Une signature valide ne suffit pas : la session doit exister et être active.
    if not session_is_active(str(jti)):
        log.info("auth.cookie.session_inactive", user_id=uid)
        return None
    return uid


# ─────────────────────────────────────────────────────────────────────────────
# Dependency
# ─────────────────────────────────────────────────────────────────────────────

async def _resolve_user_id(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[str | None, Header()] = None,
    x_internal_service: Annotated[str | None, Header()] = None,
) -> str:
    """Retourne le user_id courant, ou lève 401.

    - AUTH_MODE=strict : identité prouvée obligatoire.
    - AUTH_MODE=dev    : retombe sur X-User-Id puis DEV_DEFAULT_USER_ID.
    """
    set_request_id(request.headers.get("X-Request-Id") or None)

    # 1. Appel de service interne : prouvé par un secret partagé, jamais par un nom.
    if is_internal_service_call(x_internal_service):
        return INTERNAL_SERVICE_USER_ID

    # 2. Bearer JWT
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(None, 1)[1].strip()
        payload = _decode_bearer(token)
        # Audit 2026-11 : un JWT émis par notre propre service d'auth (iss=phoenix-auth) doit
        # correspondre à une session active, comme le cookie : logout et changement de mot de
        # passe le révoquent ainsi aussi lorsqu'il est présenté en Bearer.
        if payload and payload.get("iss") == "phoenix-auth":
            jti = payload.get("jti")
            if not jti or not session_is_active(str(jti)):
                payload = None
        if payload:
            uid = str(payload.get("sub") or payload.get("user_id") or "")
            if uid and _is_valid_user_id(uid):
                return uid
        if auth_mode() == "strict":
            raise HTTPException(status_code=401, detail="JWT invalide")

    # 3. Cookie de session (signature + session active en base)
    uid = _user_from_cookie(request)
    if uid:
        return uid

    # 4. Strict : rien de prouvé → 401
    if auth_mode() == "strict":
        raise HTTPException(status_code=401, detail="Authentification requise")

    # 5. Dev uniquement
    if x_user_id and _is_valid_user_id(x_user_id):
        return x_user_id
    return os.getenv("DEV_DEFAULT_USER_ID", "00000000-0000-0000-0000-000000000001")


async def get_current_user_id(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[str | None, Header()] = None,
    x_internal_service: Annotated[str | None, Header()] = None,
) -> str:
    """Identité + refus par défaut des comptes « client » hors de leur liste blanche (voir roles.py)."""
    uid = await _resolve_user_id(request, authorization, x_user_id, x_internal_service)
    if uid != INTERNAL_SERVICE_USER_ID:
        import roles

        roles.enforce_client_scope(uid, request.method, request.url.path)
    return uid


# Alias pour annotation plus claire côté endpoints
CurrentUserId = Annotated[str, Depends(get_current_user_id)]

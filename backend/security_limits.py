"""
security_limits.py — ZAKI OS Z12 AI CFO Suite
─────────────────────────────────────────────────────────────────────────────
Garde-fous transverses introduits par l'audit 2026-11 :

- dossier d'uploads unique et cloisonné par utilisateur
- limites de taille / nombre de fichiers
- limitation de débit par utilisateur (en mémoire, par processus)
- plafonds sur les paramètres LLM fournis par le client

Tout est configurable par variable d'environnement, avec des défauts prudents.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, HTTPException

from auth import get_current_user_id

# ─────────────────────────────────────────────────────────────────────────────
# Uploads
# ─────────────────────────────────────────────────────────────────────────────

UPLOAD_ROOT = Path(os.getenv("UPLOAD_DIR", str(Path(__file__).parent / "uploads"))).resolve()
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE_MB", "25")) * 1024 * 1024
MAX_FILES_PER_REQUEST = int(os.getenv("MAX_FILES_PER_REQUEST", "10"))
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_MB", "100")) * 1024 * 1024
# Dépôt dans le RAG : le fichier est écrit sur disque par blocs, puis indexé en arrière-plan,
# ce qui autorise des documents bien plus gros que les pièces jointes du chat (MAX_FILE_SIZE).
MAX_KNOWLEDGE_FILE_SIZE = int(os.getenv("MAX_KNOWLEDGE_FILE_MB", "500")) * 1024 * 1024
MAX_KNOWLEDGE_REQUEST_BYTES = int(os.getenv("MAX_KNOWLEDGE_REQUEST_MB", "1024")) * 1024 * 1024
MAX_PDF_PAGES_OCR = int(os.getenv("MAX_PDF_PAGES_OCR", "40"))
MAX_IMAGE_PIXELS = int(os.getenv("MAX_IMAGE_PIXELS", str(50_000_000)))

_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_\-]")


def user_upload_dir(user_id: str) -> Path:
    """Dossier d'uploads propre à un utilisateur (créé au besoin).

    Le user_id vient toujours de la session ; il est tout de même réduit à un
    segment de chemin sûr pour qu'aucune valeur ne puisse sortir de UPLOAD_ROOT.
    """
    segment = _SAFE_SEGMENT_RE.sub("_", str(user_id))[:64] or "unknown"
    path = (UPLOAD_ROOT / segment).resolve()
    if UPLOAD_ROOT not in path.parents:
        raise HTTPException(status_code=400, detail="Identifiant utilisateur invalide")
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_within(base_dir: Path, target: Path) -> bool:
    """True si target est strictement contenu dans base_dir (pas un simple préfixe de chaîne)."""
    try:
        base = base_dir.resolve()
        resolved = target.resolve()
    except (OSError, ValueError):
        return False
    return base == resolved or base in resolved.parents


# ─────────────────────────────────────────────────────────────────────────────
# Limitation de débit
# ─────────────────────────────────────────────────────────────────────────────

class RateLimiter:
    """Fenêtre glissante en mémoire, par clé (utilisateur).

    Suffisant pour un backend mono-processus ; à remplacer par Redis dès qu'il y a
    plusieurs workers (chaque worker a aujourd'hui son propre compteur).
    """

    def __init__(self, max_calls: int, window_seconds: float) -> None:
        self.max_calls = max_calls
        self.window = window_seconds
        self._calls: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            calls = self._calls[key]
            while calls and now - calls[0] > self.window:
                calls.popleft()
            if len(calls) >= self.max_calls:
                retry_after = max(1, int(self.window - (now - calls[0])))
                raise HTTPException(
                    status_code=429,
                    detail="Trop de requêtes. Réessayez dans quelques instants.",
                    headers={"Retry-After": str(retry_after)},
                )
            calls.append(now)

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()


llm_limiter = RateLimiter(int(os.getenv("LLM_RATE_PER_MINUTE", "30")), 60.0)
upload_limiter = RateLimiter(int(os.getenv("UPLOAD_RATE_PER_MINUTE", "20")), 60.0)
auth_limiter = RateLimiter(int(os.getenv("AUTH_RATE_PER_MINUTE", "10")), 60.0)


# ─────────────────────────────────────────────────────────────────────────────
# Administrateurs
# ─────────────────────────────────────────────────────────────────────────────
# Le RAG métier (sources de connaissance) est commun à toute la plateforme : seuls les
# administrateurs le voient, l'enrichissent et le modifient. Les autres utilisateurs n'ont
# accès qu'à leurs dossiers client. Sans ADMIN_USER_IDS, personne n'est administrateur.

def admin_ids() -> set[str]:
    """Identifiants des comptes administrateurs (ADMIN_USER_IDS, séparés par des virgules)."""
    return {part.strip() for part in os.getenv("ADMIN_USER_IDS", "").split(",") if part.strip()}


def is_admin(user_id: str | None) -> bool:
    return bool(user_id) and str(user_id) in admin_ids()


async def admin_user(user_id: str = Depends(get_current_user_id)) -> str:
    """Utilisateur authentifié et administrateur, sinon 403."""
    if not is_admin(user_id):
        raise HTTPException(403, "Réservé à l'administrateur")
    return user_id


async def llm_user(user_id: str = Depends(get_current_user_id)) -> str:
    """Utilisateur authentifié + quota d'appels LLM + budget de consommation."""
    llm_limiter.check(user_id)
    import budget

    budget.check(user_id)            # 402 si le budget du compte ou de la plateforme est atteint
    budget.current_user.set(user_id)  # les appels au modèle de cette requête lui sont imputés
    return user_id


async def upload_user(user_id: str = Depends(get_current_user_id)) -> str:
    """Utilisateur authentifié + quota d'uploads."""
    upload_limiter.check(user_id)
    return user_id


# ─────────────────────────────────────────────────────────────────────────────
# Paramètres LLM fournis par le client
# ─────────────────────────────────────────────────────────────────────────────

# Longueur maximale d'une réponse. Pour un modèle « à réflexion », elle couvre la réflexion ET
# le texte de la réponse : trop basse, le modèle réfléchit et n'écrit rien.
MAX_TOKENS_CAP = int(os.getenv("MAX_TOKENS_CAP", "16000"))
MAX_MESSAGES = int(os.getenv("MAX_CHAT_MESSAGES", "60"))
# Taille totale d'une conversation envoyée au modèle. Elle doit laisser la place au texte des
# pièces jointes du chat (40 000 caractères par message côté interface).
MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "120000"))
ALLOWED_PLUGIN_IDS = {"web"}
RUN_BUDGET_CAP = float(os.getenv("RUN_BUDGET_CAP", "25"))
RUN_PAGES_CAP = int(os.getenv("RUN_PAGES_CAP", "60"))


def allowed_model_prefixes() -> list[str]:
    """Préfixes de modèles autorisés (CHAT_ALLOWED_MODEL_PREFIXES, séparés par des virgules).

    Vide = aucun filtrage. À renseigner en production pour borner le coût par requête.
    """
    raw = os.getenv("CHAT_ALLOWED_MODEL_PREFIXES", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def check_model(model: str) -> str:
    prefixes = allowed_model_prefixes()
    if prefixes and not any(model.startswith(p) for p in prefixes):
        raise HTTPException(status_code=400, detail="Modèle non autorisé sur cette instance")
    return model


def clamp_max_tokens(value: int | None) -> int:
    try:
        value = int(value or 800)
    except (TypeError, ValueError):
        value = 800
    return max(1, min(value, MAX_TOKENS_CAP))


def sanitize_plugins(plugins: list | None) -> list:
    """Ne garde que les plugins connus, avec des paramètres bornés."""
    out = []
    for p in plugins or []:
        if isinstance(p, dict) and p.get("id") in ALLOWED_PLUGIN_IDS:
            try:
                max_results = int(p.get("max_results", 5))
            except (TypeError, ValueError):
                max_results = 5
            out.append({"id": p["id"], "max_results": max(1, min(max_results, 5))})
    return out


def validate_messages(messages: list) -> list:
    """Valide la forme et la taille de l'historique envoyé par le client."""
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=422, detail="messages requis")
    if len(messages) > MAX_MESSAGES:
        raise HTTPException(status_code=413, detail=f"Trop de messages (max {MAX_MESSAGES})")
    total = 0
    clean = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant"):
            raise HTTPException(status_code=422, detail="Message invalide")
        content = m.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        total += len(content)
        clean.append({"role": m["role"], "content": content})
    if total > MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=413, detail="Conversation trop longue")
    return clean

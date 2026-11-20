"""
webhook.py — Reception des messages WhatsApp entrants (Twilio)

Securite (audit 2026-11) :
- chaque requete doit porter une signature Twilio valide (X-Twilio-Signature) ;
- sans liste blanche de numeros, le webhook refuse tout (fail closed) ;
- le texte passe par le masquage PII avant tout envoi au cloud ;
- debit limite par numero.
"""
import logging
import os

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from auth import auth_mode, get_current_user_id
from security_limits import RateLimiter
from security_pii import ScrubLevel, scrub_text

from .sender import send_message

logger = logging.getLogger(__name__)
router = APIRouter()

DATABASE_URL = os.environ.get("DATABASE_URL", "")
WHATSAPP_MODEL = os.getenv("WHATSAPP_MODEL", "deepseek/deepseek-v4-pro")
MAX_INBOUND_CHARS = 2000

_whatsapp_limiter = RateLimiter(int(os.getenv("WHATSAPP_RATE_PER_MINUTE", "6")), 60.0)


def get_whitelist() -> set[str]:
    return {n.strip() for n in os.getenv("WHATSAPP_WHITELIST", "").split(",") if n.strip()}


def get_openrouter_key() -> str:
    """Cle OpenRouter du serveur. (L'ancien repli sur la cle d'un utilisateur en base a ete
    retire : un message WhatsApp ne doit pas consommer la cle personnelle de quelqu'un.)"""
    return os.getenv("OPENROUTER_API_KEY", "")


def _public_webhook_url(request: Request) -> str:
    """URL exacte que Twilio a signee. Derriere un proxy, la fixer via WHATSAPP_WEBHOOK_URL."""
    configured = os.getenv("WHATSAPP_WEBHOOK_URL", "").strip()
    return configured or str(request.url)


def verify_twilio_signature(request: Request, form: dict) -> bool:
    """Valide X-Twilio-Signature avec TWILIO_AUTH_TOKEN. Toute erreur = refus."""
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    signature = request.headers.get("X-Twilio-Signature", "")
    if not token or not signature:
        return False
    try:
        from twilio.request_validator import RequestValidator

        return bool(RequestValidator(token).validate(_public_webhook_url(request), form, signature))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Twilio signature check failed: {e}")
        return False


async def handle_text_message(phone: str, text: str):
    """Reponse courte par OpenRouter, apres masquage PII."""
    openrouter_key = get_openrouter_key()
    if not openrouter_key:
        logger.warning("[WhatsApp] Cle OpenRouter manquante")
        await send_message(phone, "Z12 CFO: service non configure. Contactez votre administrateur.")
        return
    import budget

    if not budget.platform_open():
        await send_message(phone, "Z12 CFO: service momentanement suspendu. Reessayez plus tard.")
        return
    text, _ = scrub_text(text[:MAX_INBOUND_CHARS], level=ScrubLevel.MEDIUM, skip_types={"DATE_OF_BIRTH"})
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {openrouter_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": WHATSAPP_MODEL,
                    "messages": [
                        {"role": "system", "content": "Tu es Sophie, CPA virtuelle specialisee en fiscalite canadienne et quebecoise. Reponds en francais, de facon concise."},
                        {"role": "user", "content": text},
                    ],
                    "max_tokens": 500,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                budget.meter(data, WHATSAPP_MODEL, "whatsapp", "whatsapp")
                reply = data["choices"][0]["message"]["content"]
                reply = reply[:1500] + ("..." if len(reply) > 1500 else "")
                await send_message(phone, f"Z12 CFO\n{reply}")
            else:
                logger.error(f"OpenRouter error: {resp.status_code} {resp.text[:200]}")
                await send_message(phone, "Z12 - Service temporairement indisponible.")
    except Exception as e:  # noqa: BLE001
        logger.error(f"handle_text_message error: {e}")
        await send_message(phone, "Z12 - Erreur technique. Reessayez sur cfo.optigenius.pro")


@router.post("/api/whatsapp/webhook")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    # Twilio envoie du form-urlencoded
    try:
        form = dict(await request.form())
    except Exception as e:  # noqa: BLE001
        logger.error(f"Form parse error: {e}")
        raise HTTPException(status_code=400, detail="Invalid form data") from e

    # 1. La requete vient-elle vraiment de Twilio ? (le mode dev permet les tests locaux)
    if auth_mode() == "strict" and not verify_twilio_signature(request, form):
        raise HTTPException(status_code=403, detail="Signature invalide")

    phone = str(form.get("From", "")).replace("whatsapp:", "").strip()
    text = str(form.get("Body", ""))

    # 2. Liste blanche obligatoire : vide = tout refuser.
    whitelist = get_whitelist()
    if not whitelist or phone not in whitelist:
        logger.info("Ignored non-whitelisted sender")
        return {"status": "ignored"}

    # 3. Debit par numero
    try:
        _whatsapp_limiter.check(phone)
    except HTTPException:
        return {"status": "rate_limited"}

    if text:
        background_tasks.add_task(handle_text_message, phone, text)

    return {"status": "ok"}


@router.get("/api/whatsapp/status")
async def whatsapp_status(user_id: str = Depends(get_current_user_id)):
    return {
        "bridge_running": False,
        "bridge_connected": False,
        "mode": "twilio_api",
        "whitelist_count": len(get_whitelist()),
        "twilio_from": os.getenv("TWILIO_WHATSAPP_FROM", ""),
    }

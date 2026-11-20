"""Modèles retirés par le fournisseur : bascule automatique sur un modèle de repli.

Un modèle peut disparaître du catalogue du fournisseur du jour au lendemain (réponse 404
« no longer available »). Sans repli, tout agent réglé sur ce modèle répond « requête
refusée », y compris pour les utilisateurs qui l'ont enregistré dans leurs réglages.

- un modèle signalé indisponible est mémorisé pour ce processus : les appels suivants vont
  directement au modèle de repli, sans payer un aller-retour inutile ;
- le modèle de repli se règle avec FALLBACK_MODEL ;
- au démarrage, les modèles configurés sont comparés au catalogue public du fournisseur et
  ceux qui manquent sont signalés dans les journaux.
"""
from __future__ import annotations

import os

import httpx

MODELS_URL = "https://openrouter.ai/api/v1/models"
_unavailable: set[str] = set()


def fallback_model() -> str:
    return os.getenv("FALLBACK_MODEL", "").strip() or "deepseek/deepseek-v4-pro"


def is_unavailable_response(status_code: int, text: str) -> bool:
    """Vrai si la réponse du fournisseur dit que le modèle n'existe pas ou n'est plus servi."""
    lowered = (text or "").lower()
    if status_code == 404:
        return True
    return status_code == 400 and any(
        marker in lowered for marker in ("not a valid model", "no longer available", "no endpoints found", "model not found")
    )


def mark_unavailable(model: str) -> None:
    if model and model != fallback_model():
        if model not in _unavailable:
            print(f"[WARN] Modèle indisponible chez le fournisseur : {model} -> repli sur {fallback_model()}")
        _unavailable.add(model)


def resolve(model: str | None) -> str:
    """Modèle à appeler : celui demandé, ou le repli s'il est connu comme indisponible."""
    if not model or model in _unavailable:
        return fallback_model()
    return model


def can_fall_back(model: str) -> bool:
    return bool(model) and model != fallback_model()


def known_unavailable() -> list[str]:
    return sorted(_unavailable)


def reset() -> None:
    _unavailable.clear()


def configured_models() -> list[str]:
    """Modèles réglés côté serveur (agents, WhatsApp, images, repli)."""
    models = {fallback_model()}
    try:
        import z_kernel

        models.update(z_kernel.AGENT_DEFAULT_MODEL.values())
        for name in ("EVV_VALIDATOR_MODEL", "ESCALATION_MODEL", "BUDGET_SAVE_MODEL", "FAST_MODEL", "MEDIUM_MODEL"):
            models.add(getattr(z_kernel, name, ""))
    except Exception:  # noqa: BLE001
        pass
    for name in ("WHATSAPP_MODEL", "IMAGE_DESCRIPTION_MODEL", "DEFAULT_MODEL"):
        models.add(os.getenv(name, "").strip())
    return sorted(m for m in models if m)


async def check_catalog() -> list[str]:
    """Compare les modèles configurés au catalogue public. Retourne ceux qui manquent
    (liste vide si le catalogue n'a pas pu être lu). Ne lève jamais."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(MODELS_URL)
        if response.status_code != 200:
            return []
        available = {entry.get("id") for entry in response.json().get("data", []) if isinstance(entry, dict)}
        if not available:
            return []
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Catalogue des modèles illisible : {type(e).__name__}")
        return []
    missing = [m for m in configured_models() if m not in available]
    for model in missing:
        mark_unavailable(model)
    if fallback_model() not in available:
        print(f"[ERROR] Le modèle de repli {fallback_model()} n'est pas au catalogue : régler FALLBACK_MODEL")
    return missing

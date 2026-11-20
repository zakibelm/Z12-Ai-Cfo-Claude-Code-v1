"""Ce qu'une question du chat contient vraiment.

Le Studio joint le texte des pièces jointes à la question, entre des marqueurs. Pour chercher
dans les documents, pour la mémoire et pour le cache, seule la question compte : le texte
d'un fichier joint n'est ni une requête de recherche, ni quelque chose à conserver.
"""
from __future__ import annotations

import hashlib
import json
import re

# [Pièce jointe « nom » …]\n texte \n[Fin de « nom »]   (et la forme anglaise)
_BLOCK = re.compile(
    r"\[(?:Pièce jointe|Attachment) [«\"][^\n]*?[»\"][^\n\]]*\]\n.*?\n\[(?:Fin de|End of) [«\"][^\n]*?[»\"]\]\n?",
    re.DOTALL,
)
# [Pièce jointe « nom » : non lue — raison]
_UNREAD = re.compile(r"\[(?:Pièce jointe|Attachment) [«\"][^\n]*?[»\"] ?: (?:non lue|not read)[^\n]*\]\n?")
# Ancien format du Studio : [📎 nom]\n---\n…\n---\n
_LEGACY = re.compile(r"\[📎 [^\n\]]*\]\n---\n.*?\n---\n", re.DOTALL)

ROUTING_MARKER = "retourne le plan de workflow JSON"
PURPOSES = ("agent", "routing", "synthesis")


def has_attachments(text: str) -> bool:
    return bool(_BLOCK.search(text or "") or _UNREAD.search(text or "") or _LEGACY.search(text or ""))


def question_only(text: str) -> str:
    """La question telle que l'utilisateur l'a tapée, sans le texte des pièces jointes."""
    text = text or ""
    for pattern in (_BLOCK, _UNREAD, _LEGACY):
        text = pattern.sub("", text)
    return text.strip()


def purpose_of(declared: str | None, last_user_text: str) -> str:
    """Nature de l'appel : réponse d'un spécialiste, plan de l'orchestrateur ou synthèse.

    Une ancienne interface ne déclare rien : la demande de plan se reconnaît à sa consigne.
    """
    if declared in PURPOSES and declared != "agent":
        return declared
    if ROUTING_MARKER in (last_user_text or ""):
        return "routing"
    return "agent"


def conversation_key(messages: list) -> str:
    """Empreinte de toute la conversation (hors prompt système) : une réponse en cache n'est
    resservie que si l'échange entier est identique, pas seulement le début de la question."""
    payload = [[m.get("role"), m.get("content")] for m in messages if m.get("role") != "system"]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


# ─── Réflexion du modèle ─────────────────────────────────────────────────────
# Un modèle « à réflexion » dépense la longueur autorisée d'abord à réfléchir, puis à écrire.
# Le plan de l'orchestrateur est un simple tri : il se fait sans réflexion. Un spécialiste
# garde la réflexion, avec assez de longueur pour rédiger ensuite.
NO_REASONING = {"effort": "none"}


def reasoning_for(purpose: str) -> dict | None:
    """Réglage de réflexion à envoyer au fournisseur pour cet appel, ou None pour son défaut."""
    return dict(NO_REASONING) if purpose == "routing" else None


def answer_text(data: dict | None) -> str:
    try:
        return str((data or {})["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


def cut_before_answering(data: dict | None) -> bool:
    """Vrai si le modèle a atteint la limite de longueur sans avoir écrit de réponse."""
    try:
        return not answer_text(data) and (data or {})["choices"][0].get("finish_reason") == "length"
    except (KeyError, IndexError, TypeError, AttributeError):
        return False

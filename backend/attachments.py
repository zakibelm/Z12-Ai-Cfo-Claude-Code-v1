"""Pièces jointes du chat : lecture du texte d'un fichier, sans rien enregistrer.

Un fichier joint à une question (étude de cas, feuille de calcul, document Word) est lu ici
puis son texte est joint à la question. Il n'entre pas dans le RAG : rien n'est écrit dans la
base ni dans le dossier des documents, et le texte ne vit que dans la conversation.

La lecture passe par le même convertisseur que le RAG (Markdown, tableaux conservés, OCR des
pages numérisées).
"""
from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from security_limits import MAX_FILE_SIZE, MAX_FILES_PER_REQUEST, upload_user
from services.markdown_converter import convert_to_markdown
from utils.security import is_allowed_file_type, sanitize_filename

# Texte renvoyé par fichier, au plus. L'interface répartit ensuite son propre budget par message.
ATTACHMENT_MAX_CHARS = int(os.getenv("ATTACHMENT_MAX_CHARS", "200000"))

router = APIRouter(prefix="/api/attachments", tags=["attachments"])


def _failed(name: str, size: int, reason: str) -> dict:
    return {"filename": name, "size": size, "text": "", "chars": 0, "truncated": False, "error": reason}


@router.post("/extract")
async def extract_attachments(files: list[UploadFile] = File(...), _user: str = Depends(upload_user)):
    """Texte de chaque fichier joint. Un fichier illisible est signalé, sans faire échouer les autres."""
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(400, f"Trop de fichiers (max {MAX_FILES_PER_REQUEST})")
    results = []
    for upload in files:
        name = sanitize_filename(upload.filename or "sans-nom")
        if not is_allowed_file_type(name):
            results.append(_failed(name, 0, "type de fichier non accepté"))
            continue
        content = await upload.read(MAX_FILE_SIZE + 1)
        size = len(content)
        if size > MAX_FILE_SIZE:
            results.append(_failed(name, size, f"fichier trop volumineux pour le chat (max {MAX_FILE_SIZE // 1024 // 1024} Mo) "
                                               "— déposez-le dans Documents"))
            continue
        if not size:
            results.append(_failed(name, 0, "fichier vide"))
            continue
        try:
            conversion = await asyncio.to_thread(convert_to_markdown, content, name)
        except Exception as e:  # noqa: BLE001 — un fichier abîmé ne fait pas échouer les autres
            print(f"[WARN] Pièce jointe illisible ({type(e).__name__})")
            results.append(_failed(name, size, "lecture impossible"))
            continue
        finally:
            del content
        text = conversion.markdown.strip()
        if not text:
            reason = "; ".join(conversion.warnings)[:300] or "aucun texte lisible"
            results.append(_failed(name, size, reason))
            continue
        results.append({
            "filename": name, "size": size,
            "text": text[:ATTACHMENT_MAX_CHARS], "chars": len(text),
            "truncated": len(text) > ATTACHMENT_MAX_CHARS,
            "pages_read": conversion.pages_read or None, "pages_total": conversion.total_pages,
            "method": "+".join(sorted(conversion.methods)) or "native",
            "warnings": conversion.warnings[:5], "error": None,
        })
    return {"files": results}

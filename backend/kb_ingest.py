"""
kb_ingest.py
─────────────────────────────────────────────────────────────────────────────
Logique d'ingestion des documents dans la Knowledge Base.

Pipeline :
1. Validation + sanitisation du nom de fichier
2. Sauvegarde physique dans uploads/
3. Extraction de texte (réutilise services/text_extractor.py existant)
4. Embedding via Hugging Face Inference API (intfloat/multilingual-e5-large)
5. Insertion en DB via kb_storage

Fallbacks :
- Si HF_API_TOKEN absent → on skip l'embedding (le doc reste indexé sans vecteur)
- Si SUPABASE absent → stockage local JSON via kb_storage
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from pathlib import Path

import httpx

from kb_storage import (
    FolderError,
    insert_kb_doc,
    new_id,
    replace_chunks,
    replace_images,
    resolve_placement,
    set_kb_doc_system_fields,
    SCOPE_METIER,
    doc_keywords_of,
    get_kb_doc,
    rag_scope,
    set_doc_index_terms,
)
from ollama_client import check_ollama_alive, generate_embedding_ollama
from security_pii import ScrubLevel, scrub_text
from services.chunker import (
    chunk_pages,
    describe_document,
    doc_terms,
    extract_keywords,
    normalize_content_type,
    search_meta,
)
from services import image_description
import budget
from services.image_extraction import extract_pdf_images
from services.markdown_converter import convert_to_markdown
from security_limits import is_within, user_upload_dir
from utils.security import is_allowed_file_type, sanitize_filename

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

HF_API_TOKEN = os.getenv("HF_API_TOKEN", "").strip()
HF_EMBED_URL = "https://api-inference.huggingface.co/models/intfloat/multilingual-e5-large"


# ─────────────────────────────────────────────────────────────────────────────
# Embedding via Hugging Face
# ─────────────────────────────────────────────────────────────────────────────

_OLLAMA_CHECK_TTL = 30.0
_ollama_state: tuple[float, bool] | None = None


async def _ollama_available() -> bool:
    """Disponibilité d'Ollama, mémorisée 30 secondes : la vérifier pour chacun des milliers de
    segments d'un manuel coûtait plusieurs minutes quand Ollama n'est pas installé."""
    global _ollama_state
    now = time.monotonic()
    if _ollama_state and now - _ollama_state[0] < _OLLAMA_CHECK_TTL:
        return _ollama_state[1]
    try:
        alive = bool(await check_ollama_alive())
    except Exception:  # noqa: BLE001 — une vérification en panne vaut « indisponible »
        alive = False
    _ollama_state = (now, alive)
    return alive


async def generate_embedding(text: str, sensibilite: str = "professionnel") -> list[float] | None:
    """Génère un embedding 1024d.
    Règles Loi 25 :
    - confidentiel-client → Ollama local OBLIGATOIRE (pas d'envoi cloud)
    - professionnel → cloud OK mais scrubbing préalable
    """
    if not text:
        return None

    # 1. Mode CONFIDENTIEL-CLIENT → Ollama obligatoire
    if sensibilite == "confidentiel-client":
        if await _ollama_available():
            return await generate_embedding_ollama(text)
        print("[WARN] Embedding confidentiel impossible : Ollama local indisponible.")
        return None

    # 2. Mode PROFESSIONNEL/PUBLIC → Cloud HF (avec scrubbing si pro)
    if sensibilite == "professionnel":
        text, _ = scrub_text(text, level=ScrubLevel.MEDIUM)

    if not HF_API_TOKEN:
        # Fallback local si pas de token cloud
        if await _ollama_available():
            return await generate_embedding_ollama(text)
        return None

    # Tronquer pour l'API
    text = text[:2000]
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                HF_EMBED_URL,
                headers={"Authorization": f"Bearer {HF_API_TOKEN}"},
                json={"inputs": text, "options": {"wait_for_model": True}},
            )
            if resp.status_code != 200:
                print(f"[WARN] HF embedding failed: {resp.status_code} {resp.text[:200]}")
                # Dernier recours : local si HF fail
                if await _ollama_available():
                    return await generate_embedding_ollama(text)
                return None
            data = resp.json()
            if isinstance(data, list) and data and isinstance(data[0], list):
                return data[0]
            if isinstance(data, list) and data and isinstance(data[0], int | float):
                return data
            return None
    except (httpx.HTTPError, ValueError, KeyError) as e:
        print(f"[WARN] Embedding error: {e}")
        if await _ollama_available():
            return await generate_embedding_ollama(text)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion principale
# ─────────────────────────────────────────────────────────────────────────────

EMBED_CONCURRENCY = int(os.getenv("EMBED_CONCURRENCY", "4"))


async def _embed_all(texts: list[str], sensibilite: str) -> list[list[float] | None]:
    """Embeddings de plusieurs segments, avec une concurrence bornée."""
    semaphore = asyncio.Semaphore(EMBED_CONCURRENCY)

    async def one(text: str) -> list[float] | None:
        async with semaphore:
            try:
                return await generate_embedding(text, sensibilite=sensibilite)
            except Exception as e:  # noqa: BLE001 — un segment sans embedding reste trouvable par mots-clés
                print(f"[WARN] Embedding d'un segment : {e}")
                return None

    return await asyncio.gather(*(one(t) for t in texts))


def _merge_meta(bulk_metadata: dict | list, file_refinement: dict | None) -> dict:
    # Normalise bulk_metadata: accepte liste [{...}] ou dict {...}
    if isinstance(bulk_metadata, list):
        meta = bulk_metadata[0].copy() if bulk_metadata else {}
    else:
        meta = dict(bulk_metadata)
    for k, v in (file_refinement or {}).items():
        if v is not None:
            meta[k] = v
    return meta


async def index_document(
    doc_id: str,
    owner_id: str,
    file_bytes: bytes,
    filename: str,
    *,
    content_type: str,
    sensibilite: str,
    client_id: str | None,
    image_dir: Path,
    project_id: str | None = None,
) -> dict:
    """Convertit, découpe et indexe un document. Utilisé à l'ingestion et à la réindexation.

    Retourne {markdown, chunks_count, images_count, extraction_method, warnings}.
    """
    conversion = await asyncio.to_thread(convert_to_markdown, file_bytes, filename)
    chunks = chunk_pages(conversion.pages)

    # Description et mots-clés du document : ceux saisis par l'administrateur sont conservés ;
    # sinon ils sont tirés du document lui-même (aucun appel externe).
    stored = get_kb_doc(doc_id, owner_id) or {}
    doc_description = (stored.get("description") or "").strip() or describe_document(conversion.markdown, filename)
    doc_keywords = doc_keywords_of(stored) or extract_keywords(conversion.markdown, 15)
    set_doc_index_terms(doc_id, owner_id, doc_description, doc_keywords)

    # Images incluses dans le document (PDF) : fichier conservé + texte lu en local.
    images = []
    if filename.lower().endswith(".pdf"):
        ocr_pages = {p.page for p in conversion.pages if p.method == "ocr" and p.page}
        images = await asyncio.to_thread(extract_pdf_images, file_bytes, image_dir, doc_id, ocr_pages)

    rows = [
        {
            "chunk_id": new_id(), "client_id": client_id, "project_id": project_id,
            "content": c.content, "heading": c.heading,
            "source_page": c.page, "keywords": c.keywords, "content_type": content_type,
            "sensibilite": sensibilite, "extraction_method": c.method, "verification_status": "unverified",
        }
        for c in chunks
    ]
    # Description des images par un modèle de vision : sources métier non confidentielles
    # seulement. Les images des dossiers client ne quittent jamais le serveur.
    describe = (
        image_description.enabled()
        and rag_scope(content_type) == SCOPE_METIER
        and sensibilite != "confidentiel-client"
        and budget.allowed(owner_id)  # plafond de la plateforme
    )
    described = 0
    image_rows = []
    for image in images:
        vision = None
        if describe and described < image_description.max_images_per_doc():
            vision = await image_description.describe_image(image.storage_path, owner_id)
            described += 1
        description = vision["description"] if vision else None
        keywords = list(dict.fromkeys([*(vision["keywords"] if vision else []), *image.keywords]))[:15]
        chunk_id = None
        if description or len(image.text) >= 20:
            chunk_id = new_id()
            body = [f"[Image du document, page {image.page}]"]
            if description:
                body.append(f"Description : {description}")
            if len(image.text) >= 20:
                body.append(f"Texte lu dans l'image : {image.text}" if description else image.text)
            rows.append({
                "chunk_id": chunk_id, "client_id": client_id, "project_id": project_id,
                "content": "\n".join(body),
                "heading": None, "source_page": image.page, "keywords": keywords,
                "content_type": content_type, "sensibilite": sensibilite,
                "extraction_method": "vision+ocr" if description else "ocr", "verification_status": "unverified",
            })
        image_rows.append({
            "image_id": new_id(), "chunk_id": chunk_id, "storage_path": image.storage_path,
            "image_hash": image.image_hash, "page_or_slide": image.page, "width": image.width,
            "height": image.height, "extraction_method": "vision+ocr" if description else "ocr",
            "extraction_model": image_description.vision_model() if description else "tesseract",
            "prompt_version": image_description.PROMPT_VERSION if description else None,
            "extracted_text": image.text, "extracted_data": None, "description": description,
            "keywords": keywords, "extraction_confidence": image.extraction_confidence,
            "verification_status": "unverified",
        })

    # Index : titre et mots-clés du segment (poids fort), description et mots-clés du document (poids faible).
    terms = doc_terms(doc_description, doc_keywords)
    for row in rows:
        row["search_meta"] = search_meta(row.get("heading"), row.get("keywords"))
        row["doc_terms"] = terms

    embeddings = await _embed_all([r["content"] for r in rows], sensibilite) if rows else []
    for i, row in enumerate(rows):
        row["chunk_index"], row["total_chunks"], row["embedding"] = i, len(rows), embeddings[i]

    replace_chunks(doc_id, owner_id, rows)
    replace_images(doc_id, owner_id, image_rows)

    methods = conversion.methods | ({"ocr"} if image_rows else set())
    warnings = list(conversion.warnings)
    partial = conversion.partial
    if rows and not conversion.pages:
        # Seul le texte lu dans des images a été indexé : le corps du document manque.
        partial = True
        warnings.insert(0, "texte du document non lu : seul le texte de quelques images est indexé")
    return {
        "partial": partial,
        "pages_total": conversion.total_pages,
        "pages_read": conversion.pages_read,
        "markdown": conversion.markdown,
        "chunks_count": len(rows),
        "images_count": len(image_rows),
        "images_described": sum(1 for r in image_rows if r.get("description")),
        "description": doc_description,
        "keywords": doc_keywords,
        "embedded_count": sum(1 for e in embeddings if e),
        "extraction_method": "+".join(sorted(methods)) or "none",
        "first_embedding": next((e for e in embeddings if e), None),
        "warnings": warnings,
    }


def final_status(indexed: dict) -> str:
    """Statut d'un document après indexation : « indexed » seulement s'il a été lu en entier.

    - « error » : rien n'est cherchable ;
    - « partial » : une partie seulement du document a été lue (le message dit laquelle) ;
    - « indexed » : le document est lu et cherchable.
    """
    if not indexed.get("chunks_count"):
        return "error"
    return "partial" if indexed.get("partial") else "indexed"


def _file_sha256(path: Path) -> str:
    """Empreinte du fichier, lue par blocs : un gros fichier n'est pas chargé en mémoire pour cela."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def register_file(
    file_path: Path,
    original_filename: str,
    bulk_metadata: dict,
    file_refinement: dict | None = None,
    owner_id: str | None = None,
) -> dict:
    """
    Enregistre un fichier déjà écrit sur disque : crée son document avec le statut
    « processing ». L'indexation (conversion, segments, embeddings, images) vient ensuite,
    par `index_registered`.
    """
    if not owner_id:
        raise ValueError("owner_id requis")
    meta = _merge_meta(bulk_metadata, file_refinement)
    content_type = normalize_content_type(meta.get("content_type"))
    sensibilite = meta.get("sensibilite", "professionnel")
    # RAG métier : aucun client. RAG client : dossier client obligatoire, projet facultatif.
    client_id, project_id = resolve_placement(owner_id, content_type, meta.get("client_id"), meta.get("project_id"))

    try:
        size = file_path.stat().st_size
        file_hash = _file_sha256(file_path) if size else None
    except OSError as e:
        print(f"[WARN] Lecture de {original_filename} impossible : {e}")
        size, file_hash = 0, None

    doc_id = str(uuid.uuid4())
    ext = file_path.suffix.lower().lstrip(".")
    doc = {
        "doc_id": doc_id,
        "filename": original_filename,
        "filepath": str(file_path),
        "file_size_bytes": size,
        "file_type": ext,
        "mime_type": {
            "pdf": "application/pdf",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "csv": "text/csv",
            "txt": "text/plain",
            "md": "text/markdown",
        }.get(ext, "application/octet-stream"),
        "domaine": meta.get("domaine", "Financial"),
        "fiscal_year": meta.get("fiscal_year", meta.get("year")),
        "sensibilite": sensibilite,
        "doc_type": meta.get("doc_type", "autre"),
        "regulatory_refs": meta.get("regulatory_refs", []),
        "tags": meta.get("tags", []),
        "agents_assigned": meta.get("agents_assigned", []),
        "client_id": client_id,
        "project_id": project_id,
        "content_type": content_type,
        "file_hash": file_hash,
        "status": "processing",
        "status_message": None,
        "text_content": "",
        "text_excerpt": "",
        "embedding": None,
        "version": 1,
        "owner_id": owner_id,
    }
    # Le document existe avant ses segments : ceux-ci s'y rattachent par doc_id.
    insert_kb_doc(doc)
    return doc


async def index_registered(doc: dict) -> dict:
    """Indexe un document déjà enregistré (statut « processing ») et fixe son statut final."""
    doc_id, owner_id = doc["doc_id"], doc["owner_id"]
    file_path = Path(doc["filepath"])
    try:
        file_bytes = await asyncio.to_thread(file_path.read_bytes)
    except OSError as e:
        print(f"[WARN] Lecture de {doc.get('filename')} impossible : {e}")
        file_bytes = b""

    indexed = {"markdown": "", "chunks_count": 0, "images_count": 0, "extraction_method": "none",
               "first_embedding": None, "warnings": ["fichier illisible"], "embedded_count": 0}
    if file_bytes:
        indexed = await index_document(
            doc_id, owner_id, file_bytes, doc["filename"],
            content_type=doc["content_type"], sensibilite=doc["sensibilite"], client_id=doc["client_id"],
            project_id=doc["project_id"], image_dir=file_path.parent / "images",
        )

    markdown = indexed["markdown"]
    patch = {
        "text_content": markdown,
        "text_excerpt": (markdown[:500] + "…") if len(markdown) > 500 else markdown,
        "status": final_status(indexed),
        "status_message": "; ".join(indexed["warnings"])[:500] or None,
        "extraction_method": indexed["extraction_method"],
        "chunks_count": indexed["chunks_count"],
        "embedding": indexed["first_embedding"],
    }
    set_kb_doc_system_fields(doc_id, owner_id, patch)
    return {
        **doc, **patch, "embedding": None,
        "description": indexed.get("description"), "keywords": indexed.get("keywords", []),
        "images_count": indexed["images_count"], "embedded_count": indexed["embedded_count"],
    }


async def ingest_file(
    file_path: Path,
    original_filename: str,
    bulk_metadata: dict,
    file_refinement: dict | None = None,
    owner_id: str | None = None,
) -> dict:
    """Enregistre puis indexe un fichier, en attendant la fin de l'indexation."""
    doc = register_file(file_path, original_filename, bulk_metadata, file_refinement, owner_id=owner_id)
    return await index_registered(doc)


# ─── Indexation en arrière-plan ──────────────────────────────────────────────
# Un gros document (ou un document numérisé) met plusieurs minutes à indexer : la requête
# d'envoi répond dès que le fichier est enregistré, et l'indexation se poursuit ici. Le
# statut du document (« processing », puis « indexed » ou « error ») se lit par l'API.

INTERRUPTED_MESSAGE = "Indexation interrompue par un redémarrage du serveur — relancez « Réindexer »."
_background_tasks: set[asyncio.Task] = set()
_index_slots: asyncio.Semaphore | None = None


def _slots() -> asyncio.Semaphore:
    """Nombre d'indexations simultanées (INGEST_CONCURRENCY, 1 par défaut) : un document est
    chargé en entier en mémoire pendant sa conversion."""
    global _index_slots
    if _index_slots is None:
        try:
            limit = max(1, int(os.getenv("INGEST_CONCURRENCY", "1")))
        except ValueError:
            limit = 1
        _index_slots = asyncio.Semaphore(limit)
    return _index_slots


async def _run_in_background(doc_id: str, owner_id: str, work) -> None:
    """Exécute une indexation ; quoi qu'il arrive, le document ne reste pas « processing »."""
    try:
        async with _slots():
            await work()
    except Exception as e:  # noqa: BLE001 — toute panne devient un statut lisible
        print(f"[ERROR] Indexation en arrière-plan de {doc_id} : {type(e).__name__}: {e}")
        set_kb_doc_system_fields(doc_id, owner_id, {
            "status": "error", "chunks_count": 0,
            "status_message": f"Indexation interrompue ({type(e).__name__}) — relancez « Réindexer »."[:500],
        })


def schedule(doc_id: str, owner_id: str, work) -> asyncio.Task:
    """Lance `work()` (coroutine sans argument) en arrière-plan pour ce document."""
    task = asyncio.create_task(_run_in_background(doc_id, owner_id, work))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def schedule_indexing(doc: dict) -> asyncio.Task:
    return schedule(doc["doc_id"], doc["owner_id"], lambda: index_registered(doc))


async def wait_for_background() -> None:
    """Attend la fin des indexations en cours (tests, arrêt propre)."""
    while _background_tasks:
        await asyncio.gather(*list(_background_tasks), return_exceptions=True)


def recover_interrupted() -> int:
    """Au démarrage : un document resté « processing » a été interrompu par un redémarrage.
    Il est marqué en erreur, avec la marche à suivre, au lieu de rester « en cours » pour toujours."""
    from kb_storage import mark_interrupted_docs

    return mark_interrupted_docs(INTERRUPTED_MESSAGE)


def store_path(owner_id: str, original_filename: str) -> Path | None:
    """Chemin où enregistrer un fichier envoyé, ou None si son type n'est pas accepté."""
    upload_dir = user_upload_dir(owner_id)
    safe_name = sanitize_filename(original_filename)
    if not is_allowed_file_type(safe_name):
        return None
    path = upload_dir / f"{uuid.uuid4().hex[:12]}-{safe_name}"
    return path if is_within(upload_dir, path) else None


async def ingest_files(
    files_and_names: list[tuple[bytes, str]],
    bulk_metadata: dict,
    per_file_refinements: dict,
    owner_id: str | None = None,
) -> list[dict]:
    """
    Traite une liste de fichiers en série.
    files_and_names : liste de tuples (bytes, original_filename)
    """
    if not owner_id:
        raise ValueError("owner_id requis : aucun document n'est ingéré sans propriétaire")
    upload_dir = user_upload_dir(owner_id)
    results = []
    for idx, (content, name) in enumerate(files_and_names):
        safe_name = sanitize_filename(name)
        if not is_allowed_file_type(safe_name):
            print(f"[WARN] Skip {name} — type non autorisé")
            continue

        # Générer un nom unique
        unique_name = f"{uuid.uuid4().hex[:12]}-{safe_name}"
        file_path = upload_dir / unique_name

        if not is_within(upload_dir, file_path):
            print(f"[WARN] Skip {name} — path traversal détecté")
            continue

        try:
            file_path.write_bytes(content)
        except OSError as e:
            print(f"[ERROR] Impossible d'écrire {file_path}: {e}")
            continue

        refinement = per_file_refinements.get(f"{name}-{idx}")
        try:
            doc = await ingest_file(file_path, name, bulk_metadata, refinement, owner_id=owner_id)
            results.append(doc)
        except FolderError:
            file_path.unlink(missing_ok=True)
            raise  # emplacement invalide : l'appelant doit le savoir, rien n'est ingéré
        except (OSError, ValueError, RuntimeError) as e:
            print(f"[ERROR] Ingest failed for {name}: {e}")
            continue

    return results

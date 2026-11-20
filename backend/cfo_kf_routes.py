"""
cfo_kf_routes.py
─────────────────────────────────────────────────────────────────────────────
Router FastAPI pour tous les endpoints CFO Knowledge Factory + Knowledge Base.

À monter dans main.py :
    from cfo_kf_routes import router as cfo_kf_router
    app.include_router(cfo_kf_router)

Endpoints exposés :
    Knowledge Base :
      POST   /api/knowledge/ingest
      GET    /api/knowledge/list
      GET    /api/knowledge/{doc_id}
      PATCH  /api/knowledge/{doc_id}
      DELETE /api/knowledge/{doc_id}
      POST   /api/knowledge/start-factory-run
      GET    /api/knowledge/{doc_id}/runs

    Factory :
      POST   /api/cfo-kf/launch
      GET    /api/cfo-kf/runs
      GET    /api/cfo-kf/runs/{run_id}
      POST   /api/cfo-kf/checkpoint
"""

import asyncio
import json
import os
from pathlib import Path
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from auth import get_current_user_id
from kb_ingest import (
    final_status,
    index_document,
    index_registered,
    register_file,
    schedule,
    schedule_indexing,
    store_path,
)
from services.chunker import CONTENT_TYPES, normalize_content_type
from kb_storage import (
    SCOPE_CLIENT,
    SCOPE_METIER,
    SENSIBILITE_ORDER,
    FolderError,
    create_folder,
    delete_doc_children,
    delete_folder,
    folder_tree,
    move_doc_chunks,
    rag_scope,
    rename_folder,
    resolve_placement,
    delete_kb_doc,
    get_image,
    get_kb_doc,
    list_chunks,
    list_images,
    set_kb_doc_system_fields,
    clean_keywords,
    refresh_search_meta,
    get_run,
    get_run_for,
    list_kb_docs,
    list_runs_for,
    update_kb_doc,
)
from security_limits import (
    MAX_FILES_PER_REQUEST,
    MAX_KNOWLEDGE_FILE_SIZE,
    MAX_KNOWLEDGE_REQUEST_BYTES,
    RUN_BUDGET_CAP,
    RUN_PAGES_CAP,
    is_admin,
    is_within,
    llm_user,
    upload_user,
    user_upload_dir,
)
from observability import get_logger, get_metrics_snapshot
from z_kernel import CFOZKernel, get_kernel, register_kernel

_log = get_logger(__name__)

# Auth par défaut sur tout le routeur : une nouvelle route est protégée sans y penser.
router = APIRouter(
    prefix="/api",
    tags=["CFO Knowledge Factory"],
    dependencies=[Depends(get_current_user_id)],
)


def _remove_user_file(path: str | None, user_id: str) -> None:
    """Efface un fichier, seulement s'il se trouve dans le dossier de l'utilisateur."""
    if not path:
        return
    target = Path(path)
    if is_within(user_upload_dir(user_id), target):
        target.unlink(missing_ok=True)


def background_indexing() -> bool:
    """Indexation en arrière-plan (défaut). INGEST_BACKGROUND=0 la fait dans la requête."""
    return os.getenv("INGEST_BACKGROUND", "1").strip().lower() not in ("0", "false", "no", "off")


def _public_doc(doc: dict) -> dict:
    """Document tel que renvoyé après un dépôt : sans le texte complet ni le vecteur."""
    return {k: v for k, v in doc.items() if k not in ("embedding", "text_content", "filepath")}


async def _save_upload(upload: UploadFile, path: Path, limit: int) -> int:
    """Écrit un fichier envoyé sur disque, par blocs. 413 (et fichier retiré) au-delà de `limit`."""
    size = 0
    try:
        with path.open("wb") as out:
            while True:
                block = await upload.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > limit:
                    raise HTTPException(413, f"Fichier trop volumineux (max {limit // 1024 // 1024} Mo)")
                await asyncio.to_thread(out.write, block)
    except HTTPException:
        path.unlink(missing_ok=True)
        raise
    except OSError as e:
        path.unlink(missing_ok=True)
        print(f"[ERROR] Impossible d'écrire {path.name}: {e}")
        raise HTTPException(507, "Le serveur n'a pas pu enregistrer le fichier (espace disque).") from e
    return size


def _start_reindex(doc: dict, user_id: str) -> dict:
    """Réindexe un document : en arrière-plan (statut « processing ») sauf fichier d'origine absent."""
    path = Path(doc.get("filepath") or "")
    if not path.is_file() or not is_within(user_upload_dir(user_id), path):
        return {"doc_id": doc["doc_id"], "filename": doc.get("filename"), "status": "skipped",
                "reason": "fichier d'origine introuvable — téléverser le document de nouveau"}
    set_kb_doc_system_fields(doc["doc_id"], user_id, {"status": "processing", "status_message": None})
    schedule(doc["doc_id"], user_id, lambda: _reindex(doc, user_id))
    return {"doc_id": doc["doc_id"], "filename": doc.get("filename"), "status": "processing"}


async def _reindex(doc: dict, user_id: str) -> dict:
    """Reconstruit segments et images d'un document à partir de son fichier d'origine."""
    path = Path(doc.get("filepath") or "")
    if not path.is_file() or not is_within(user_upload_dir(user_id), path):
        return {"doc_id": doc["doc_id"], "filename": doc.get("filename"), "status": "skipped",
                "reason": "fichier d'origine introuvable — téléverser le document de nouveau"}
    for image_path in delete_doc_children(doc["doc_id"], user_id):
        _remove_user_file(image_path, user_id)
    indexed = await index_document(
        doc["doc_id"], user_id, path.read_bytes(), doc.get("filename") or path.name,
        content_type=normalize_content_type(doc.get("content_type")),
        sensibilite=doc.get("sensibilite") or "professionnel",
        client_id=doc.get("client_id"), project_id=doc.get("project_id"), image_dir=path.parent / "images",
    )
    markdown = indexed["markdown"]
    set_kb_doc_system_fields(doc["doc_id"], user_id, {
        "text_content": markdown,
        "text_excerpt": (markdown[:500] + "…") if len(markdown) > 500 else markdown,
        "status": final_status(indexed),
        "status_message": "; ".join(indexed["warnings"])[:500] or None,
        "extraction_method": indexed["extraction_method"],
        "chunks_count": indexed["chunks_count"],
        "embedding": indexed["first_embedding"],
    })
    return {"doc_id": doc["doc_id"], "filename": doc.get("filename"), "status": "reindexed",
            "partial": bool(indexed.get("partial")), "pages_read": indexed.get("pages_read"),
            "pages_total": indexed.get("pages_total"),
            "chunks": indexed["chunks_count"], "images": indexed["images_count"],
            "embedded": indexed["embedded_count"], "extraction_method": indexed["extraction_method"]}


def _bounded_run_config(config: dict, owner_id: str) -> dict:
    """Borne côté serveur les paramètres de coût d'un run et y inscrit son propriétaire."""
    config = dict(config)
    try:
        config["budget_max_eur"] = max(0.0, min(float(config.get("budget_max_eur", 15)), RUN_BUDGET_CAP))
        config["nb_pages_cible"] = max(1, min(int(config.get("nb_pages_cible", 30)), RUN_PAGES_CAP))
    except (TypeError, ValueError) as err:
        raise HTTPException(400, "budget_max_eur / nb_pages_cible invalides") from err
    config["owner_id"] = owner_id  # jamais fourni par le client
    return config


# ═════════════════════════════════════════════════════════════════════════════
# SSE STREAMING — progression live d'un run
# ═════════════════════════════════════════════════════════════════════════════

async def _run_event_stream(run_id: str) -> AsyncIterator[str]:
    """Génère un flux SSE pour suivre un run en temps réel.

    Polle la DB chaque 2s, émet un event à chaque changement d'état ou de
    métrique. Termine quand status ∈ {COMPLETED, COMPLETED_WITH_WARNINGS, FAILED}.
    """
    last_payload: dict[str, Any] = {}
    terminal = {"COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED"}

    for _ in range(1800):  # max ~1h (1800 × 2s)
        run = get_run(run_id)
        if not run:
            yield f"event: error\ndata: {json.dumps({'error': 'run not found'})}\n\n"
            return

        payload = {
            "run_id": run_id,
            "status": run.get("status"),
            "pages_created": run.get("pages_created", 0),
            "pages_verified": run.get("pages_verified", 0),
            "pages_to_verify": run.get("pages_to_verify", 0),
            "budget_used_eur": float(run.get("budget_used_eur") or 0),
            "budget_max_eur": float(run.get("budget_max_eur") or 0),
            "avg_quality_score": run.get("avg_quality_score"),
        }
        if payload != last_payload:
            yield f"event: update\ndata: {json.dumps(payload, default=str)}\n\n"
            last_payload = payload

        if payload["status"] in terminal:
            yield f"event: done\ndata: {json.dumps(payload, default=str)}\n\n"
            return

        await asyncio.sleep(2)
    # Timeout 1h : ferme proprement
    yield "event: timeout\ndata: {}\n\n"


@router.get("/cfo-kf/runs/{run_id}/stream")
async def stream_run(run_id: str, user_id: str = Depends(get_current_user_id)) -> StreamingResponse:
    """Server-Sent Events pour suivre la progression d'un run.

    Client React : `new EventSource('/api/cfo-kf/runs/{id}/stream')`
    """
    if not get_run_for(run_id, user_id):
        raise HTTPException(404, "Run non trouvé")
    return StreamingResponse(
        _run_event_stream(run_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx : désactive le buffering
        },
    )


# ═════════════════════════════════════════════════════════════════════════════
# METRICS — pour dashboard observabilité
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/cfo-kf/metrics")
async def cfo_kf_metrics() -> dict[str, Any]:
    """Snapshot des métriques in-memory (LLM calls, coûts, latences par phase)."""
    return get_metrics_snapshot()


# ═════════════════════════════════════════════════════════════════════════════
# KNOWLEDGE BASE
# ═════════════════════════════════════════════════════════════════════════════

def _require_admin_for_metier(user_id: str, *content_types) -> None:
    """Refuse (403) qu'un non-administrateur crée ou reclasse un document en source métier."""
    if is_admin(user_id):
        return
    for value in content_types:
        if value is not None and rag_scope(normalize_content_type(value)) == SCOPE_METIER:
            raise HTTPException(403, "Les sources de connaissance métier sont réservées à l'administrateur")


@router.post("/knowledge/ingest")
async def knowledge_ingest(
    files: list[UploadFile] = File(...),
    bulk_metadata: str = Form("{}"),
    per_file_refinements: str = Form("{}"),
    user_id: str = Depends(upload_user),
):
    """Ingest multi-fichiers avec métadonnées en lot + overrides par fichier."""
    try:
        bulk = json.loads(bulk_metadata)
    except json.JSONDecodeError as err:
        raise HTTPException(400, "bulk_metadata invalide (JSON requis)") from err
    try:
        refinements = json.loads(per_file_refinements)
    except json.JSONDecodeError:
        refinements = {}

    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(400, f"Trop de fichiers (max {MAX_FILES_PER_REQUEST})")

    # Le RAG métier est réservé à l'administrateur : vérifié avant toute écriture.
    declared = [bulk.get("content_type")] + [
        r.get("content_type") for r in refinements.values() if isinstance(r, dict) and "content_type" in r
    ] if isinstance(bulk, dict) and isinstance(refinements, dict) else []
    _require_admin_for_metier(user_id, *declared)

    # Chaque fichier est écrit sur disque par blocs : un gros document n'est pas chargé en
    # mémoire pour être reçu. Au moindre refus, rien de cette requête n'est conservé.
    saved: list[tuple[Path, str, int]] = []
    skipped: list[str] = []
    total = 0

    def discard() -> None:
        for path, _, _ in saved:
            path.unlink(missing_ok=True)

    for idx, f in enumerate(files):
        name = f.filename or "sans-nom"
        path = store_path(user_id, name)
        if path is None:
            print(f"[WARN] Skip {name} — type non autorisé")
            skipped.append(name)
            continue
        try:
            size = await _save_upload(f, path, MAX_KNOWLEDGE_FILE_SIZE)
        except HTTPException:
            discard()
            raise
        saved.append((path, name, idx))
        total += size
        if total > MAX_KNOWLEDGE_REQUEST_BYTES:
            discard()
            raise HTTPException(413, "Envoi trop volumineux")

    docs: list[dict] = []
    try:
        for path, name, idx in saved:
            docs.append(register_file(path, name, bulk, refinements.get(f"{name}-{idx}"), owner_id=user_id))
    except FolderError as e:
        discard()
        for doc in docs:
            delete_kb_doc(doc["doc_id"], user_id)
        raise HTTPException(400, str(e)) from e

    # L'indexation se poursuit en arrière-plan : la réponse part dès que les fichiers sont
    # enregistrés, avec le statut « processing ». Le statut final se lit par GET /knowledge/{id}.
    results = []
    for doc in docs:
        if background_indexing():
            schedule_indexing(doc)
            results.append(_public_doc(doc))
        else:
            results.append(_public_doc(await index_registered(doc)))
    return {"documents": results, "count": len(results), "skipped": skipped}


@router.get("/knowledge/list")
async def knowledge_list(
    q: str | None = None,
    domaine: str | None = None,
    sensibilite: str | None = None,
    agent: str | None = None,
    year: int | None = None,
    content_type: str | None = None,
    scope: str | None = None,
    client_id: str | None = None,
    project_id: str | None = None,
    unfiled: bool = False,
    user_id: str = Depends(get_current_user_id),
):
    """Liste les docs de la KB avec filtres optionnels."""
    filters: dict[str, Any] = {}
    if q:
        filters["q"] = q
    if domaine:
        filters["domaine"] = domaine
    if sensibilite:
        filters["sensibilite"] = sensibilite
    if agent:
        filters["agent"] = agent
    if year:
        filters["year"] = year

    docs = list_kb_docs(filters, owner_id=user_id)
    # Les sources métier ne sont montrées qu'à l'administrateur.
    if not is_admin(user_id):
        if scope == SCOPE_METIER:
            raise HTTPException(403, "Réservé à l'administrateur")
        docs = [d for d in docs if rag_scope(d.get("content_type")) == SCOPE_CLIENT]
    if content_type:
        if content_type not in CONTENT_TYPES:
            raise HTTPException(400, "content_type invalide")
        docs = [d for d in docs if (d.get("content_type") or "client_data") == content_type]
    # RAG métier ou RAG client, puis dossier client et projet.
    if scope:
        if scope not in (SCOPE_METIER, SCOPE_CLIENT):
            raise HTTPException(400, "scope invalide (metier | client)")
        docs = [d for d in docs if rag_scope(d.get("content_type")) == scope]
    if client_id:
        docs = [d for d in docs if d.get("client_id") == client_id]
    if project_id:
        docs = [d for d in docs if d.get("project_id") == project_id]
    if unfiled:  # documents client sans dossier (antérieurs aux dossiers)
        docs = [d for d in docs if rag_scope(d.get("content_type")) == SCOPE_CLIENT and not d.get("client_id")]
    for d in docs:
        d["scope"] = rag_scope(d.get("content_type"))
    # Strip heavy fields
    for d in docs:
        d.pop("text_content", None)
        d.pop("embedding", None)
    return {"documents": docs}


# ─── Dossiers du RAG client : un dossier par client, des sous-dossiers par projet ───

@router.get("/clients")
async def clients_tree(user_id: str = Depends(get_current_user_id)):
    """Arborescence du RAG client, avec le nombre de documents à chaque niveau."""
    docs = list_kb_docs(owner_id=user_id)
    return {
        "clients": folder_tree(user_id),
        "unfiled": sum(1 for d in docs if rag_scope(d.get("content_type")) == SCOPE_CLIENT and not d.get("client_id")),
        "metier_documents": sum(1 for d in docs if rag_scope(d.get("content_type")) == SCOPE_METIER),
    }


@router.post("/clients", status_code=201)
async def clients_create(payload: dict = Body(...), user_id: str = Depends(get_current_user_id)):
    try:
        folder = create_folder(user_id, str(payload.get("name", "")))
    except FolderError as e:
        raise HTTPException(400, str(e)) from e
    return {"client_id": folder["folder_id"], "name": folder["name"], "documents": 0, "projects": []}


@router.post("/clients/{client_id}/projects", status_code=201)
async def projects_create(client_id: str, payload: dict = Body(...), user_id: str = Depends(get_current_user_id)):
    try:
        folder = create_folder(user_id, str(payload.get("name", "")), parent_id=client_id)
    except FolderError as e:
        raise HTTPException(400, str(e)) from e
    return {"project_id": folder["folder_id"], "client_id": client_id, "name": folder["name"], "documents": 0}


@router.patch("/folders/{folder_id}")
async def folders_rename(folder_id: str, payload: dict = Body(...), user_id: str = Depends(get_current_user_id)):
    try:
        folder = rename_folder(folder_id, user_id, str(payload.get("name", "")))
    except FolderError as e:
        raise HTTPException(400, str(e)) from e
    if not folder:
        raise HTTPException(404, "Dossier non trouvé")
    return {"folder_id": folder["folder_id"], "name": folder["name"], "kind": folder["kind"]}


@router.delete("/folders/{folder_id}")
async def folders_delete(folder_id: str, user_id: str = Depends(get_current_user_id)):
    try:
        deleted = delete_folder(folder_id, user_id)
    except FolderError as e:
        raise HTTPException(409, str(e)) from e
    if not deleted:
        raise HTTPException(404, "Dossier non trouvé")
    return {"deleted": True}


@router.get("/knowledge/content-types")
async def knowledge_content_types(user_id: str = Depends(get_current_user_id)):
    """Types de contenu utilisables : dossiers client pour tous, sources métier pour l'administrateur."""
    types = list(CONTENT_TYPES) if is_admin(user_id) else [t for t in CONTENT_TYPES if rag_scope(t) == SCOPE_CLIENT]
    return {"content_types": types, "default": "client_data"}


@router.post("/knowledge/reindex-all")
async def knowledge_reindex_all(user_id: str = Depends(upload_user)):
    """Réindexe tous les documents de l'utilisateur (après le passage à l'ingestion v2)."""
    docs = list_kb_docs(owner_id=user_id)
    if background_indexing():
        results = [_start_reindex(doc, user_id) for doc in docs]
    else:
        results = [await _reindex(doc, user_id) for doc in docs]
    return {"count": len(results), "results": results}


@router.get("/knowledge/images/{image_id}")
async def knowledge_image_file(image_id: str, user_id: str = Depends(get_current_user_id)):
    """Fichier d'une image extraite, pour l'afficher dans une citation."""
    image = get_image(image_id, user_id)
    path = Path(image["storage_path"]) if image else None
    if not path or not path.is_file() or not is_within(user_upload_dir(user_id), path):
        raise HTTPException(404, "Image non trouvée")
    return FileResponse(path, media_type="image/png")


@router.get("/knowledge/{doc_id}/chunks")
async def knowledge_chunks(doc_id: str, user_id: str = Depends(get_current_user_id)):
    if not get_kb_doc(doc_id, user_id):
        raise HTTPException(404, "Document non trouvé")
    chunks = list_chunks(doc_id, user_id)
    return {"count": len(chunks), "chunks": chunks}


@router.get("/knowledge/{doc_id}/images")
async def knowledge_images(doc_id: str, user_id: str = Depends(get_current_user_id)):
    if not get_kb_doc(doc_id, user_id):
        raise HTTPException(404, "Document non trouvé")
    images = [{k: v for k, v in img.items() if k != "storage_path"} | {"url": f"/api/knowledge/images/{img['image_id']}"}
              for img in list_images(doc_id, user_id)]
    return {"count": len(images), "images": images}


@router.post("/knowledge/{doc_id}/reindex")
async def knowledge_reindex(doc_id: str, user_id: str = Depends(upload_user)):
    doc = get_kb_doc(doc_id, user_id)
    if not doc:
        raise HTTPException(404, "Document non trouvé")
    if background_indexing():
        return _start_reindex(doc, user_id)
    return await _reindex(doc, user_id)


@router.get("/knowledge/{doc_id}")
async def knowledge_detail(doc_id: str, user_id: str = Depends(get_current_user_id)):
    doc = get_kb_doc(doc_id, user_id)
    if not doc:
        raise HTTPException(404, "Document non trouvé")
    doc.pop("embedding", None)
    return doc


@router.patch("/knowledge/{doc_id}")
async def knowledge_update(doc_id: str, patch: dict = Body(...), user_id: str = Depends(get_current_user_id)):
    # Les champs modifiables sont une liste blanche côté stockage (KB_EDITABLE_FIELDS).
    current = get_kb_doc(doc_id, user_id)
    if not current:
        raise HTTPException(404, "Document non trouvé")
    if "sensibilite" in patch:
        new_level = SENSIBILITE_ORDER.get(patch["sensibilite"])
        if new_level is None:
            raise HTTPException(400, "sensibilite invalide")
        # La sensibilité pilote le routage local/cloud : on peut la durcir, pas l'abaisser.
        if new_level < SENSIBILITE_ORDER.get(current.get("sensibilite", "professionnel"), 1):
            raise HTTPException(403, "La sensibilité d'un document ne peut pas être abaissée")
    if "content_type" in patch and patch["content_type"] not in CONTENT_TYPES:
        raise HTTPException(400, f"content_type invalide (attendu : {', '.join(CONTENT_TYPES)})")
    _require_admin_for_metier(user_id, patch.get("content_type"))
    # Déplacement ou reclassement : l'emplacement final est validé comme à l'ingestion,
    # puis reporté sur les segments pour que la recherche suive immédiatement.
    moving = {"content_type", "client_id", "project_id"} & patch.keys()
    if moving:
        target_type = patch.get("content_type", current.get("content_type") or "client_data")
        try:
            client_id, project_id = resolve_placement(
                user_id, target_type,
                patch.get("client_id", current.get("client_id")),
                patch["project_id"] if "project_id" in patch else (None if "client_id" in patch else current.get("project_id")),
            )
        except FolderError as e:
            raise HTTPException(400, str(e)) from e
        patch.update(content_type=target_type, client_id=client_id, project_id=project_id)
    # Description et mots-clés saisis à la main : nettoyés, puis repris dans l'index de recherche.
    if "description" in patch:
        if patch["description"] is not None and not isinstance(patch["description"], str):
            raise HTTPException(400, "description invalide")
        patch["description"] = " ".join((patch["description"] or "").split())[:600] or None
    if "keywords" in patch:
        if not isinstance(patch["keywords"], list):
            raise HTTPException(400, "keywords doit être une liste")
        patch["keywords"] = clean_keywords(patch["keywords"])
    updated = update_kb_doc(doc_id, patch, user_id)
    if updated and ("description" in patch or "keywords" in patch):
        refresh_search_meta(doc_id, user_id)
    if moving and updated:
        move_doc_chunks(doc_id, user_id, patch["client_id"], patch["project_id"], patch["content_type"])
    if not updated:
        raise HTTPException(404, "Document non trouvé")
    updated.pop("embedding", None)
    updated.pop("text_content", None)
    return updated


@router.delete("/knowledge/{doc_id}")
async def knowledge_delete(doc_id: str, user_id: str = Depends(get_current_user_id)):
    if not get_kb_doc(doc_id, user_id):
        raise HTTPException(404, "Document non trouvé")
    # Segments, lignes d'images et fichiers image disparaissent avec le document.
    for image_path in delete_doc_children(doc_id, user_id):
        _remove_user_file(image_path, user_id)
    ok = delete_kb_doc(doc_id, user_id)
    if not ok:
        raise HTTPException(404, "Document non trouvé")
    return {"deleted": doc_id}


@router.post("/knowledge/start-factory-run")
async def knowledge_start_factory_run(payload: dict = Body(...), user_id: str = Depends(llm_user)):
    """Bridge : depuis la KB, démarrer un run Factory avec les docs sélectionnés."""
    doc_ids = payload.get("doc_ids", [])
    run_config = payload.get("run_config", {})
    if not doc_ids:
        raise HTTPException(400, "doc_ids requis")

    # Résout les docs pour dériver domaine et sensibilité
    docs = [get_kb_doc(did, user_id) for did in doc_ids]
    docs = [d for d in docs if d]
    if not docs:
        raise HTTPException(404, "Aucun document trouvé")

    # Sensibilité max
    sens_order = {"public": 0, "professionnel": 1, "confidentiel-client": 2}
    sens_max = max(docs, key=lambda d: sens_order.get(d.get("sensibilite", "public"), 0))["sensibilite"]

    # Domaine dominant
    domaines = [d.get("domaine", "Multi-domaine") for d in docs]
    domaine = domaines[0] if len(set(domaines)) == 1 else "Multi-domaine"

    config = {
        "mode": "client",
        "domaine": domaine,
        "mandat": run_config.get("mandat", ""),
        "description": run_config.get("description", ""),
        "nb_pages_cible": run_config.get("nb_pages_cible", 30),
        "budget_max_eur": run_config.get("budget_max_eur", 15),
        "niveau_rigueur": run_config.get("niveau_rigueur", "professionnel"),
        "sensibilite": sens_max,
        "documents_sources": [d["doc_id"] for d in docs],
        "temps_max_min": 90,
    }

    kernel = CFOZKernel(_bounded_run_config(config, user_id))
    run = kernel.start()
    register_kernel(kernel)
    return {"run_id": run["run_id"]}


@router.get("/knowledge/{doc_id}/runs")
async def knowledge_doc_runs(doc_id: str, user_id: str = Depends(get_current_user_id)):
    """Liste les runs Factory ayant consommé ce document."""
    doc = get_kb_doc(doc_id, user_id)
    if not doc:
        raise HTTPException(404)
    run_ids = doc.get("used_in_runs", [])
    runs = [get_run_for(rid, user_id) for rid in run_ids]
    return {"runs": [r for r in runs if r]}


# ═════════════════════════════════════════════════════════════════════════════
# FACTORY
# ═════════════════════════════════════════════════════════════════════════════

@router.post("/cfo-kf/launch")
async def cfo_kf_launch(payload: dict = Body(...), user_id: str = Depends(llm_user)):
    """Lance un nouveau run Factory (ou Client) depuis la page Factory."""
    required = ["mode", "domaine", "mandat", "nb_pages_cible", "budget_max_eur"]
    for k in required:
        if k not in payload:
            raise HTTPException(400, f"Champ obligatoire manquant : {k}")

    kernel = CFOZKernel(_bounded_run_config(payload, user_id))
    run = kernel.start()
    register_kernel(kernel)
    return {"run_id": run["run_id"], "status": run["status"]}


@router.get("/cfo-kf/runs")
async def cfo_kf_runs(limit: int = 50, user_id: str = Depends(get_current_user_id)):
    """Liste les runs — résumés pour la vue Factory."""
    runs = list_runs_for(user_id, limit=limit)
    # Projette un résumé minimal pour le front
    summaries = []
    for r in runs:
        summaries.append({
            "run_id": r.get("run_id"),
            "mandat": r.get("mandat", ""),
            "mode": r.get("mode", "factory"),
            "domaine": r.get("domaine", ""),
            "description": r.get("description"),
            "status": r.get("status"),
            "nb_pages_cible": r.get("nb_pages_cible", 0),
            "pages_created": r.get("pages_created", 0),
            "pages_verified": r.get("pages_verified", 0),
            "pages_to_verify": r.get("pages_to_verify", 0),
            "budget_max_eur": r.get("budget_max_eur", 0),
            "budget_used_eur": r.get("budget_used_eur", 0),
            "temps_max_min": r.get("temps_max_min", 90),
            "niveau_rigueur": r.get("niveau_rigueur", "professionnel"),
            "sensibilite": r.get("sensibilite", "professionnel"),
            "avg_quality_score": r.get("avg_quality_score"),
            "started_at": r.get("started_at"),
            "ended_at": r.get("ended_at"),
            "client_id": r.get("client_id"),
            "total_cost_eur": r.get("budget_used_eur", 0),
            "wall_time_minutes": 0,  # calculé côté back en prod
            "docs_used": r.get("config_snapshot", {}).get("documents_sources", []),
        })
    return {"runs": summaries}


@router.get("/cfo-kf/runs/{run_id}")
async def cfo_kf_run_detail(run_id: str, user_id: str = Depends(get_current_user_id)):
    run = get_run_for(run_id, user_id)
    if not run:
        raise HTTPException(404, "Run non trouvé")
    return run


@router.post("/cfo-kf/checkpoint")
async def cfo_kf_checkpoint(payload: dict = Body(...), user_id: str = Depends(get_current_user_id)):
    """Réponse CPA à un checkpoint CP1 ou CP2 — décision GO/CORRIGE/AJUSTE/STOP."""
    run_id = payload.get("run_id")
    checkpoint = payload.get("checkpoint")  # "CP1" or "CP2"
    decision = payload.get("decision")       # "GO" | "CORRIGE" | "AJUSTE" | "STOP"

    if not all([run_id, checkpoint, decision]):
        raise HTTPException(400, "run_id, checkpoint, decision requis")

    if decision not in ("GO", "CORRIGE", "AJUSTE", "STOP") or checkpoint not in ("CP1", "CP2"):
        raise HTTPException(400, "checkpoint ou decision invalide")
    run = get_run_for(run_id, user_id)
    if not run:
        raise HTTPException(404, "Run non trouvé")

    kernel = get_kernel(run_id)
    if not kernel:
        # Reconstruct kernel from DB (permet reprise après restart back)
        kernel = CFOZKernel(run.get("config_snapshot", {}))
        kernel.run_id = run_id
        register_kernel(kernel)

    import asyncio
    asyncio.create_task(kernel.resume_after_checkpoint(checkpoint, decision))
    return {"accepted": True}

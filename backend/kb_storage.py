"""
kb_storage.py
─────────────────────────────────────────────────────────────────────────────
Couche de persistence pour la Knowledge Base et les runs Factory.
Migration 100% Locale (Postgres + PgVector via psycopg)
"""

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

try:
    import psycopg
    from psycopg.rows import dict_row
    _HAS_PSYCOPG = True
except ImportError:
    psycopg = None
    dict_row = None
    _HAS_PSYCOPG = False

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://ai_cfo:ai_cfo_password@localhost:5432/ai_cfo_db")

# Fallback local (JSON) au cas où la BD ne répond vraiment pas
_DATA_DIR = Path(__file__).parent / "data" / "cfo_kf"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_DOCS_FILE = _DATA_DIR / "knowledge_docs.json"
_RUNS_FILE = _DATA_DIR / "runs.json"
_STEPS_FILE = _DATA_DIR / "steps.json"
_PAGES_FILE = _DATA_DIR / "pages.json"

_lock = threading.RLock()

def _load(path: Path) -> list:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[WARN] kb_storage._load failed ({path}): {e}")
        return []

def _save(path: Path, data: list) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

def get_conn():
    if not _HAS_PSYCOPG:
        return None
    try:
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    except psycopg.Error as e:
        print(f"[ERROR] Connexion PostgreSQL echouee: {e}")
        return None

# ─────────────────────────────────────────────────────────────────────────────
# CRUD Knowledge Docs
# ─────────────────────────────────────────────────────────────────────────────

def vector_search(embedding: list[float], limit: int = 5, min_score: float = 0.5,
                  owner_id: str | None = None) -> list[dict]:
    """Recherche sémantique via pgvector, limitée aux documents du propriétaire.

    Sans owner_id, la recherche ne retourne rien : il n'existe pas de recherche
    "tous utilisateurs confondus".
    """
    if not owner_id:
        return []
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                # pgvector <=> est la distance cosinus (1 - similarity)
                # On veut similarity > min_score <=> 1 - distance > min_score <=> distance < 1 - min_score
                max_dist = 1.0 - min_score
                cur.execute(
                    """
                    SELECT doc_id, filename, filepath, domaine, fiscal_year, sensibilite, doc_type, tags, text_excerpt,
                           (1 - (embedding <=> %s::vector)) as similarity
                    FROM cfo_knowledge_docs
                    WHERE embedding IS NOT NULL AND owner_id = %s
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (str(embedding), owner_id, str(embedding), limit)
                )
                results = cur.fetchall()
                # Filtrer par score minimum si nécessaire
                return [r for r in results if r.get("similarity", 0) >= min_score]
        except Exception as e:
            print(f"[WARN] Vector search failed: {e}")
        finally:
            conn.close()

    # Fallback : recherche par mots-clés simple sur le JSON local (très basique)
    with _lock:
        docs = _load(_DOCS_FILE)
        # On ne peut pas vraiment faire de sémantique sans vecteur en local
        # On retourne juste les derniers pour ne pas bloquer l'UI
        return [d for d in docs if d.get("owner_id") == owner_id][:limit]

def insert_kb_doc(doc: dict) -> dict:
    doc.setdefault("doc_id", str(uuid.uuid4()))
    doc.setdefault("uploaded_at", datetime.utcnow().isoformat())
    doc.setdefault("status", "indexed")
    doc.setdefault("use_count", 0)
    doc.setdefault("used_in_runs", [])
    doc.setdefault("regulatory_refs", [])
    doc.setdefault("tags", [])
    doc.setdefault("agents_assigned", [])
    doc.setdefault("version", 1)

    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                # Filtrer les clés pour ne garder que celles qui existent dans le schéma
                allowed = ["doc_id", "filename", "filepath", "file_size_bytes", "file_type", "mime_type",
                           "domaine", "fiscal_year", "sensibilite", "doc_type", "regulatory_refs", "tags",
                           "agents_assigned", "client_id", "status", "status_message", "text_content",
                           "text_excerpt", "embedding", "version", "uploaded_at", "owner_id",
                           "content_type", "file_hash", "chunks_count", "extraction_method", "project_id"]
                insert_data = {k: v for k, v in doc.items() if k in allowed}
                # Les colonnes JSONB reçoivent du JSON, pas un tableau PostgreSQL : sans cela, une
                # liste non vide faisait échouer l'insertion et le document partait dans le repli local.
                for json_col in ("regulatory_refs", "tags", "agents_assigned"):
                    if json_col in insert_data and not isinstance(insert_data[json_col], str):
                        insert_data[json_col] = json.dumps(insert_data[json_col])

                cols = ", ".join(insert_data.keys())
                vals = ", ".join([f"%({k})s" for k in insert_data])
                # Special cast for vector
                if "embedding" in insert_data and insert_data["embedding"] is not None:
                    insert_data["embedding"] = str(insert_data["embedding"])

                cur.execute(f"INSERT INTO cfo_knowledge_docs ({cols}) VALUES ({vals}) RETURNING *", insert_data)
                res = cur.fetchone()
                conn.commit()
                return res or doc
        except psycopg.Error as e:
            print(f"[WARN] Postgres insert failed, fallback to local: {e}")
            conn.rollback()
        finally:
            conn.close()

    with _lock:
        docs = _load(_DOCS_FILE)
        docs.insert(0, doc)
        _save(_DOCS_FILE, docs)
    return doc

def list_kb_docs(filters: dict | None = None, owner_id: str | None = None) -> list[dict]:
    filters = filters or {}
    if not owner_id:
        return []
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                query = "SELECT * FROM cfo_knowledge_docs WHERE owner_id = %(owner_id)s"
                params = {"owner_id": owner_id}
                if filters.get("domaine"):
                    query += " AND domaine = %(domaine)s"
                    params["domaine"] = filters["domaine"]
                if filters.get("sensibilite"):
                    query += " AND sensibilite = %(sensibilite)s"
                    params["sensibilite"] = filters["sensibilite"]
                if filters.get("year"):
                    query += " AND fiscal_year = %(year)s"
                    params["year"] = int(filters["year"])
                if filters.get("agent"):
                    query += " AND %(agent)s = ANY(agents_assigned)"
                    params["agent"] = filters["agent"]

                query += " ORDER BY uploaded_at DESC"
                cur.execute(query, params)
                docs = cur.fetchall()

                if filters.get("q"):
                    qstr = filters["q"].lower()
                    docs = [d for d in docs if qstr in (d.get("filename", "")).lower()
                            or qstr in (d.get("text_excerpt") or "").lower()]
                return docs
        except psycopg.Error as e:
            print(f"[WARN] Postgres list failed, fallback: {e}")
        finally:
            conn.close()

    with _lock:
        docs = _load(_DOCS_FILE)

    out = []
    for d in docs:
        if d.get("owner_id") != owner_id:
            continue
        if filters.get("domaine") and d.get("domaine") != filters["domaine"]:

            continue
        if filters.get("sensibilite") and d.get("sensibilite") != filters["sensibilite"]:

            continue
        if filters.get("year") and d.get("fiscal_year") != int(filters["year"]):

            continue
        if filters.get("agent") and filters["agent"] not in (d.get("agents_assigned") or []):

            continue
        if filters.get("q"):
            qstr = filters["q"].lower()
            if qstr not in (d.get("filename", "")).lower() and qstr not in (d.get("text_excerpt") or "").lower():
                continue
        out.append(d)
    return out

def get_kb_doc(doc_id: str, owner_id: str | None = None) -> dict | None:
    """Retourne le document s'il appartient à owner_id (None sinon, sans distinction)."""
    if not owner_id:
        return None
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM cfo_knowledge_docs WHERE doc_id = %s AND owner_id = %s",
                            (doc_id, owner_id))
                return cur.fetchone()
        except psycopg.Error as _e:
            pass  # fallback silencieux (DB down → JSON local)
        finally:
            conn.close()
    with _lock:
        docs = _load(_DOCS_FILE)
    return next((d for d in docs if d.get("doc_id") == doc_id and d.get("owner_id") == owner_id), None)


# Seules colonnes modifiables par l'API. Les noms viennent de cette liste, jamais du
# client : c'est ce qui ferme l'injection SQL par nom de colonne et l'affectation de masse.
KB_EDITABLE_FIELDS: tuple[str, ...] = (
    "filename", "domaine", "fiscal_year", "sensibilite", "doc_type",
    "regulatory_refs", "tags", "agents_assigned", "client_id", "content_type",
    "description", "keywords",
)
_KB_JSON_FIELDS = {"regulatory_refs", "tags", "agents_assigned", "keywords"}
SENSIBILITE_ORDER = {"public": 0, "professionnel": 1, "confidentiel-client": 2}


def update_kb_doc(doc_id: str, patch: dict, owner_id: str | None = None) -> dict | None:
    if not owner_id:
        return None
    patch = {k: v for k, v in patch.items() if k in KB_EDITABLE_FIELDS}
    if not patch:
        return get_kb_doc(doc_id, owner_id)
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                set_clauses = ", ".join([f"{k} = %({k})s" for k in patch])
                params = {k: (json.dumps(v) if k in _KB_JSON_FIELDS and not isinstance(v, str) else v)
                          for k, v in patch.items()}
                params["_doc_id"] = doc_id
                params["_owner_id"] = owner_id
                cur.execute(
                    f"UPDATE cfo_knowledge_docs SET {set_clauses} "
                    "WHERE doc_id = %(_doc_id)s AND owner_id = %(_owner_id)s RETURNING *",
                    params,
                )
                res = cur.fetchone()
                conn.commit()
                return res
        except psycopg.Error as _e:
            pass  # fallback silencieux (DB down → JSON local)
        finally:
            conn.close()
    with _lock:
        docs = _load(_DOCS_FILE)
        for i, d in enumerate(docs):
            if d.get("doc_id") == doc_id and d.get("owner_id") == owner_id:
                docs[i] = {**d, **patch}
                _save(_DOCS_FILE, docs)
                return docs[i]
    return None

def delete_kb_doc(doc_id: str, owner_id: str | None = None) -> bool:
    if not owner_id:
        return False
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM cfo_knowledge_docs WHERE doc_id = %s AND owner_id = %s",
                            (doc_id, owner_id))
                conn.commit()
                return cur.rowcount > 0
        except psycopg.Error as _e:
            pass  # fallback silencieux (DB down → JSON local)
        finally:
            conn.close()
    with _lock:
        docs = _load(_DOCS_FILE)
        new = [d for d in docs if not (d.get("doc_id") == doc_id and d.get("owner_id") == owner_id)]
        if len(new) != len(docs):
            _save(_DOCS_FILE, new)
            return True
    return False

# ─────────────────────────────────────────────────────────────────────────────
# CRUD Runs
# ─────────────────────────────────────────────────────────────────────────────

def insert_run(run: dict) -> dict:
    run.setdefault("run_id", f"cfokf-{datetime.utcnow().strftime('%Y-%m-%d')}-{uuid.uuid4().hex[:8]}")
    run.setdefault("started_at", datetime.utcnow().isoformat())
    run.setdefault("status", "PLANNED")
    run.setdefault("budget_used_eur", 0.0)
    run.setdefault("pages_created", 0)
    run.setdefault("pages_verified", 0)
    run.setdefault("pages_to_verify", 0)

    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                allowed = ["run_id", "module", "mode", "domaine", "mandat", "description", "client_id",
                           "parent_run_id", "run_index", "status", "budget_max_eur", "temps_max_min",
                           "nb_pages_cible", "batch_size", "pages_pilote", "niveau_rigueur", "objectif_vault",
                           "sensibilite", "seuil_qualite", "seuil_to_verify", "chemin_vault", "mode_sortie",
                           "started_at", "config_snapshot"]
                insert_data = {k: v for k, v in run.items() if k in allowed}
                if "config_snapshot" in insert_data and isinstance(insert_data["config_snapshot"], dict):
                    insert_data["config_snapshot"] = json.dumps(insert_data["config_snapshot"])

                cols = ", ".join(insert_data.keys())
                vals = ", ".join([f"%({k})s" for k in insert_data])
                cur.execute(f"INSERT INTO cfo_runs ({cols}) VALUES ({vals}) RETURNING *", insert_data)
                res = cur.fetchone()
                conn.commit()
                return res or run
        except psycopg.Error as _e:
            conn.rollback()  # DB erreur -> fallback JSON
        finally:
            conn.close()

    with _lock:
        runs = _load(_RUNS_FILE)
        runs.insert(0, run)
        _save(_RUNS_FILE, runs)
    return run

def list_runs(limit: int = 50) -> list[dict]:
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM cfo_runs ORDER BY started_at DESC LIMIT %s", (limit,))
                return cur.fetchall()
        except psycopg.Error as _e:
            pass  # fallback silencieux (DB down → JSON local)
        finally:
            conn.close()
    with _lock:
        return _load(_RUNS_FILE)[:limit]

def get_run(run_id: str) -> dict | None:
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM cfo_runs WHERE run_id = %s", (run_id,))
                return cur.fetchone()
        except psycopg.Error as _e:
            pass  # fallback silencieux (DB down → JSON local)
        finally:
            conn.close()
    with _lock:
        runs = _load(_RUNS_FILE)
    return next((r for r in runs if r.get("run_id") == run_id), None)

_KB_SYSTEM_FIELDS = (
    "text_content", "text_excerpt", "status", "status_message", "extraction_method",
    "chunks_count", "embedding", "content_type", "file_hash",
)


def set_kb_doc_system_fields(doc_id: str, owner_id: str, patch: dict) -> None:
    """Mise à jour interne (pipeline d'ingestion) : champs que l'API ne laisse pas modifier."""
    patch = {k: v for k, v in patch.items() if k in _KB_SYSTEM_FIELDS}
    if not patch or not owner_id:
        return
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                sets = ", ".join(f"{k} = %({k})s::vector" if k == "embedding" else f"{k} = %({k})s" for k in patch)
                params = {k: (str(v) if k == "embedding" and v is not None else v) for k, v in patch.items()}
                params.update(_doc_id=doc_id, _owner_id=owner_id)
                cur.execute(
                    f"UPDATE cfo_knowledge_docs SET {sets} WHERE doc_id = %(_doc_id)s AND owner_id = %(_owner_id)s",
                    params,
                )
                conn.commit()
            return
        except psycopg.Error as e:
            print(f"[WARN] set_kb_doc_system_fields failed: {e}")
            conn.rollback()
        finally:
            conn.close()
    with _lock:
        docs = _load(_DOCS_FILE)
        for i, d in enumerate(docs):
            if d.get("doc_id") == doc_id and d.get("owner_id") == owner_id:
                docs[i] = {**d, **patch}
        _save(_DOCS_FILE, docs)


def mark_interrupted_docs(message: str) -> int:
    """Passe en erreur tous les documents restés « processing » (tous comptes confondus).
    Appelé au démarrage : aucune indexation ne survit à un redémarrage du serveur."""
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE cfo_knowledge_docs SET status = 'error', status_message = %s WHERE status = 'processing'",
                    (message,),
                )
                count = cur.rowcount
                conn.commit()
            return max(0, count)
        except psycopg.Error as e:
            print(f"[WARN] mark_interrupted_docs failed: {e}")
            conn.rollback()
            return 0
        finally:
            conn.close()
    with _lock:
        docs = _load(_DOCS_FILE)
        count = 0
        for i, d in enumerate(docs):
            if d.get("status") == "processing":
                docs[i] = {**d, "status": "error", "status_message": message}
                count += 1
        if count:
            _save(_DOCS_FILE, docs)
    return count


def run_owner(run: dict | None) -> str | None:
    """Propriétaire d'un run (stocké dans config_snapshot.owner_id)."""
    if not run:
        return None
    snapshot = run.get("config_snapshot") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except json.JSONDecodeError:
            snapshot = {}
    return snapshot.get("owner_id") if isinstance(snapshot, dict) else None


def get_run_for(run_id: str, owner_id: str | None) -> dict | None:
    """Run s'il appartient à owner_id, None sinon."""
    run = get_run(run_id)
    return run if owner_id and run_owner(run) == owner_id else None


def list_runs_for(owner_id: str | None, limit: int = 50) -> list[dict]:
    if not owner_id:
        return []
    limit = max(1, min(int(limit), 200))
    return [r for r in list_runs(limit=1000) if run_owner(r) == owner_id][:limit]


def delete_owner_data(owner_id: str) -> dict:
    """Droit à l'effacement : supprime les documents KB d'un utilisateur. Retourne les compteurs."""
    deleted = {"knowledge_docs": 0}
    if not owner_id:
        return deleted
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM cfo_knowledge_docs WHERE owner_id = %s", (owner_id,))
                deleted["knowledge_docs"] = cur.rowcount
                conn.commit()
                # Segments et images (tables de l'ingestion v2) : effacés avec le compte.
                for table in ("cfo_knowledge_chunks", "cfo_document_images", "cfo_folders"):
                    try:
                        cur.execute(f"DELETE FROM {table} WHERE owner_id = %s", (owner_id,))
                        conn.commit()
                    except psycopg.Error:
                        conn.rollback()
        except psycopg.Error as e:
            print(f"[WARN] delete_owner_data failed: {e}")
            conn.rollback()
        finally:
            conn.close()
    with _lock:
        docs = _load(_DOCS_FILE)
        kept = [d for d in docs if d.get("owner_id") != owner_id]
        if len(kept) != len(docs):
            deleted["knowledge_docs"] += len(docs) - len(kept)
            _save(_DOCS_FILE, kept)
        for path in (_chunks_file(), _images_file(), _folders_file()):
            rows = _load(path)
            if any(r.get("owner_id") == owner_id for r in rows):
                _save(path, [r for r in rows if r.get("owner_id") != owner_id])
    return deleted


def ensure_security_schema() -> bool:
    """Ajoute la colonne owner_id (idempotent). Appelé au démarrage de l'app."""
    conn = get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE cfo_knowledge_docs ADD COLUMN IF NOT EXISTS owner_id VARCHAR(255)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cfo_kb_owner ON cfo_knowledge_docs (owner_id)")
            conn.commit()
        return True
    except psycopg.Error as e:
        print(f"[WARN] ensure_security_schema failed: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def update_run(run_id: str, patch: dict) -> dict | None:
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                if "config_snapshot" in patch and isinstance(patch["config_snapshot"], dict):
                    patch["config_snapshot"] = json.dumps(patch["config_snapshot"])
                set_clauses = ", ".join([f"{k} = %({k})s" for k in patch])
                patch["_run_id"] = run_id
                cur.execute(f"UPDATE cfo_runs SET {set_clauses} WHERE run_id = %(_run_id)s RETURNING *", patch)
                res = cur.fetchone()
                conn.commit()
                return res
        except psycopg.Error as _e:
            conn.rollback()  # DB erreur -> fallback JSON
        finally:
            conn.close()
    with _lock:
        runs = _load(_RUNS_FILE)
        for i, r in enumerate(runs):
            if r.get("run_id") == run_id:
                runs[i] = {**r, **patch}
                _save(_RUNS_FILE, runs)
                return runs[i]
    return None

# ─────────────────────────────────────────────────────────────────────────────
# CRUD Steps + Pages
# ─────────────────────────────────────────────────────────────────────────────

def insert_step(step: dict) -> dict:
    step.setdefault("step_id", str(uuid.uuid4()))
    step.setdefault("started_at", datetime.utcnow().isoformat())
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                allowed = ["step_id", "run_id", "phase_name", "agent_name", "agent_role_kvf", "status",
                           "model_used", "input_summary", "output_summary", "duration_sec", "started_at"]
                ins = {k: v for k, v in step.items() if k in allowed}
                cols = ", ".join(ins.keys())
                vals = ", ".join([f"%({k})s" for k in ins])
                cur.execute(f"INSERT INTO cfo_steps ({cols}) VALUES ({vals}) RETURNING *", ins)
                res = cur.fetchone()
                conn.commit()
                return res or step
        except psycopg.Error as _e:
            conn.rollback()  # DB erreur -> fallback JSON
        finally:
            conn.close()
    with _lock:
        steps = _load(_STEPS_FILE)
        steps.insert(0, step)
        _save(_STEPS_FILE, steps)
    return step

def insert_page(page: dict) -> dict:
    page.setdefault("page_id", str(uuid.uuid4()))
    page.setdefault("created_at", datetime.utcnow().isoformat())
    page.setdefault("status", "draft")
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                allowed = ["page_id", "run_id", "title", "type", "status", "word_count",
                           "quality_score", "filepath", "created_at"]
                ins = {k: v for k, v in page.items() if k in allowed}
                cols = ", ".join(ins.keys())
                vals = ", ".join([f"%({k})s" for k in ins])
                cur.execute(f"INSERT INTO cfo_pages ({cols}) VALUES ({vals}) RETURNING *", ins)
                res = cur.fetchone()
                conn.commit()
                return res or page
        except psycopg.Error as _e:
            conn.rollback()  # DB erreur -> fallback JSON
        finally:
            conn.close()
    with _lock:
        pages = _load(_PAGES_FILE)
        pages.insert(0, page)
        _save(_PAGES_FILE, pages)
    return page


async def search_kb_qc_by_text(query: str, limit: int = 3) -> list[dict]:
    """Embed query via OpenRouter then search KB QC chunks."""
    import os, httpx
    api_key = os.environ.get('OPENROUTER_API_KEY', '')
    if not api_key:
        print("[Patrick KB] OPENROUTER_API_KEY not set")
        return []
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/embeddings",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": "intfloat/multilingual-e5-large", "input": query[:2000]}
            )
        if resp.status_code != 200:
            print(f"[Patrick KB] Embedding API error: {resp.status_code} {resp.text[:100]}")
            return []
        data = resp.json()
        embedding = data["data"][0]["embedding"]
        return search_kb_qc(embedding, limit=limit)
    except Exception as e:
        print(f"[Patrick KB] search_kb_qc_by_text error: {e}")
        return []


# ═════════════════════════════════════════════════════════════════════════════
# Ingestion v2 : segments (chunks) et images de documents
# ═════════════════════════════════════════════════════════════════════════════
#
# Un document = plusieurs segments, chacun avec sa page, son titre, ses mots-clés,
# son type de contenu et sa méthode d'extraction. La recherche se fait sur les segments.
# Les images extraites sont rangées dans `cfo_document_images` : référence du fichier,
# texte lu dans l'image, mots-clés, et lien vers le segment qui porte ce texte.

import math
import re as _re
import uuid as _uuid



def _chunks_file() -> Path:
    """Fichier de repli des segments, toujours à côté de celui des documents."""
    return _DOCS_FILE.parent / "chunks.json"


def _images_file() -> Path:
    return _DOCS_FILE.parent / "images.json"

INGESTION_V2_SCHEMA = [
    "ALTER TABLE cfo_knowledge_docs ADD COLUMN IF NOT EXISTS content_type VARCHAR(50) DEFAULT 'client_data'",
    "ALTER TABLE cfo_knowledge_docs ADD COLUMN IF NOT EXISTS file_hash VARCHAR(64)",
    "ALTER TABLE cfo_knowledge_docs ADD COLUMN IF NOT EXISTS chunks_count INTEGER DEFAULT 0",
    "ALTER TABLE cfo_knowledge_docs ADD COLUMN IF NOT EXISTS extraction_method VARCHAR(50)",
    """CREATE TABLE IF NOT EXISTS cfo_knowledge_chunks (
        chunk_id VARCHAR(64) PRIMARY KEY,
        doc_id VARCHAR(255) NOT NULL,
        owner_id VARCHAR(255) NOT NULL,
        client_id VARCHAR(255),
        chunk_index INTEGER NOT NULL,
        total_chunks INTEGER NOT NULL,
        content TEXT NOT NULL,
        heading TEXT,
        source_page INTEGER,
        keywords JSONB DEFAULT '[]',
        content_type VARCHAR(50) NOT NULL DEFAULT 'client_data',
        sensibilite VARCHAR(50) NOT NULL DEFAULT 'professionnel',
        extraction_method VARCHAR(50) NOT NULL DEFAULT 'native',
        verification_status VARCHAR(30) NOT NULL DEFAULT 'unverified',
        embedding vector(1024),
        search_text tsvector GENERATED ALWAYS AS (to_tsvector('french', content)) STORED,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_cfo_chunks_owner ON cfo_knowledge_chunks (owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_cfo_chunks_doc ON cfo_knowledge_chunks (doc_id)",
    "CREATE INDEX IF NOT EXISTS idx_cfo_chunks_search ON cfo_knowledge_chunks USING GIN (search_text)",
    "CREATE INDEX IF NOT EXISTS idx_cfo_chunks_keywords ON cfo_knowledge_chunks USING GIN (keywords)",
    """CREATE TABLE IF NOT EXISTS cfo_document_images (
        image_id VARCHAR(64) PRIMARY KEY,
        doc_id VARCHAR(255) NOT NULL,
        owner_id VARCHAR(255) NOT NULL,
        chunk_id VARCHAR(64),
        storage_path TEXT NOT NULL,
        image_hash VARCHAR(64),
        page_or_slide INTEGER,
        width INTEGER,
        height INTEGER,
        extraction_method VARCHAR(50) NOT NULL DEFAULT 'ocr',
        extraction_model VARCHAR(100),
        prompt_version VARCHAR(20),
        extracted_text TEXT,
        extracted_data JSONB,
        keywords JSONB DEFAULT '[]',
        extraction_confidence VARCHAR(10),
        verification_status VARCHAR(30) NOT NULL DEFAULT 'unverified',
        verified_at TIMESTAMP,
        supersedes_id VARCHAR(64),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_cfo_images_owner ON cfo_document_images (owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_cfo_images_doc ON cfo_document_images (doc_id)",
]

_CHUNK_COLUMNS = (
    "chunk_id", "doc_id", "owner_id", "client_id", "chunk_index", "total_chunks", "content", "heading",
    "source_page", "keywords", "content_type", "sensibilite", "extraction_method", "verification_status", "embedding",
)
_IMAGE_COLUMNS = (
    "image_id", "doc_id", "owner_id", "chunk_id", "storage_path", "image_hash", "page_or_slide", "width", "height",
    "extraction_method", "extraction_model", "prompt_version", "extracted_text", "extracted_data", "keywords",
    "extraction_confidence", "verification_status",
)
# Vecteur de recherche : titre et mots-clés du segment (A), son texte (B), puis la
# description et les mots-clés de son document (C).
_SEARCH_VECTOR = (
    "(setweight(to_tsvector('french', coalesce(c.search_meta, '')), 'A') || setweight(c.search_text, 'B') "
    "|| setweight(to_tsvector('french', coalesce(c.doc_terms, '')), 'C'))"
)
_CHUNK_SELECT = (
    "c.chunk_id, c.doc_id, c.chunk_index, c.total_chunks, c.content, c.heading, c.source_page, c.keywords, "
    "c.content_type, c.sensibilite, c.extraction_method, c.verification_status, c.client_id, c.project_id, d.filename"
)


def ensure_ingestion_schema() -> bool:
    """Crée les tables de l'ingestion v2 (idempotent). Appelé au démarrage."""
    conn = get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            for statement in INGESTION_V2_SCHEMA:
                cur.execute(statement)
            conn.commit()
        return True
    except psycopg.Error as e:
        print(f"[WARN] ensure_ingestion_schema failed: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def new_id() -> str:
    return _uuid.uuid4().hex


def _row_params(row: dict, columns: tuple[str, ...], json_fields: set[str]) -> dict:
    params = {}
    for col in columns:
        value = row.get(col)
        if col in json_fields and value is not None and not isinstance(value, str):
            value = json.dumps(value)
        elif col == "embedding" and value is not None:
            value = str(value)
        params[col] = value
    return params


def replace_chunks(doc_id: str, owner_id: str, chunks: list[dict]) -> int:
    """Remplace tous les segments d'un document (réindexation incluse). Retourne leur nombre."""
    if not owner_id:
        return 0
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM cfo_knowledge_chunks WHERE doc_id = %s AND owner_id = %s", (doc_id, owner_id))
                placeholders = ", ".join(
                    f"%({c})s::vector" if c == "embedding" else f"%({c})s" for c in _CHUNK_COLUMNS
                )
                for chunk in chunks:
                    cur.execute(
                        f"INSERT INTO cfo_knowledge_chunks ({', '.join(_CHUNK_COLUMNS)}) VALUES ({placeholders})",
                        _row_params({**chunk, "doc_id": doc_id, "owner_id": owner_id}, _CHUNK_COLUMNS, {"keywords"}),
                    )
                cur.execute(
                    "UPDATE cfo_knowledge_docs SET chunks_count = %s WHERE doc_id = %s AND owner_id = %s",
                    (len(chunks), doc_id, owner_id),
                )
                conn.commit()
            return len(chunks)
        except psycopg.Error as e:
            print(f"[WARN] replace_chunks failed: {e}")
            conn.rollback()
        finally:
            conn.close()
    with _lock:
        stored = [c for c in _load(_chunks_file()) if c.get("doc_id") != doc_id]
        stored.extend({**c, "doc_id": doc_id, "owner_id": owner_id} for c in chunks)
        _save(_chunks_file(), stored)
        docs = _load(_DOCS_FILE)
        for d in docs:
            if d.get("doc_id") == doc_id and d.get("owner_id") == owner_id:
                d["chunks_count"] = len(chunks)
        _save(_DOCS_FILE, docs)
    return len(chunks)


def list_chunks(doc_id: str, owner_id: str | None) -> list[dict]:
    if not owner_id:
        return []
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_CHUNK_SELECT} FROM cfo_knowledge_chunks c "
                    "JOIN cfo_knowledge_docs d ON d.doc_id = c.doc_id "
                    "WHERE c.doc_id = %s AND c.owner_id = %s ORDER BY c.chunk_index",
                    (doc_id, owner_id),
                )
                return cur.fetchall()
        except psycopg.Error as e:
            print(f"[WARN] list_chunks failed: {e}")
        finally:
            conn.close()
    with _lock:
        rows = [c for c in _load(_chunks_file()) if c.get("doc_id") == doc_id and c.get("owner_id") == owner_id]
    return sorted(({k: v for k, v in c.items() if k != "embedding"} for c in rows), key=lambda c: c.get("chunk_index", 0))


_WORD_RE = _re.compile(r"[a-zà-ÿ0-9][a-zà-ÿ0-9&'-]{1,}", _re.IGNORECASE)
_STOPWORDS = frozenset(
    "les des une que qui dans pour par sur avec est sont aux ces ses leur leurs mais donc car pas plus "
    "the and for with that this from are was what how quel quelle quels quelles comment pourquoi est-ce".split()
)


def _tokens(text: str) -> list[str]:
    return [w for w in (m.group(0).lower() for m in _WORD_RE.finditer(text or "")) if w not in _STOPWORDS]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def search_chunks(
    query: str,
    embedding: list[float] | None,
    owner_id: str | None,
    limit: int = 6,
    min_score: float = 0.5,
    content_types: list[str] | None = None,
    client_id: str | None = None,
    project_id: str | None = None,
    scope: str | None = None,
    metier_owner_ids: list[str] | None = None,
) -> list[dict]:
    """Recherche dans les segments accessibles à `owner_id`.

    Périmètre :
    - le RAG métier (tout sauf `client_data`) est toujours consulté ; il appartient aux
      comptes `metier_owner_ids` (les administrateurs) et sert à tous les utilisateurs.
      Par défaut (None), ce sont les sources métier de `owner_id` lui-même ;
    - les données client sont toujours celles de `owner_id`, jamais d'un autre compte ;
    - le RAG client n'est consulté que pour le dossier `client_id` ; avec `project_id`, on
      retient les documents de ce projet et ceux placés à la racine du dossier client ;
    - sans `client_id`, aucune donnée client ne sort ;
    - `scope` = "metier" ou "client" restreint à une seule des deux bases.

    Deux voies, fusionnées : sémantique (pgvector) quand un embedding est disponible, et
    plein texte français + mots-clés dans tous les cas. La seconde garantit que la recherche
    fonctionne même sans fournisseur d'embeddings configuré.
    Sans owner_id, ne retourne rien.
    """
    if not owner_id or not (query or embedding):
        return []
    limit = max(1, min(limit, 20))
    metier_owners = [owner_id] if metier_owner_ids is None else [str(o) for o in metier_owner_ids if o]
    conn = get_conn()
    if conn:
        try:
            filters, params = [], {"owner": owner_id, "limit": limit, "metier_owners": metier_owners}
            if content_types:
                filters.append("c.content_type = ANY(%(types)s)")
                params["types"] = list(content_types)
            metier = "(c.content_type <> 'client_data' AND c.owner_id = ANY(%(metier_owners)s))"
            if client_id and project_id:
                client = ("(c.content_type = 'client_data' AND c.owner_id = %(owner)s AND c.client_id = %(client)s "
                          "AND (c.project_id = %(project)s OR c.project_id IS NULL))")
                params.update(client=client_id, project=project_id)
            elif client_id:
                client = "(c.content_type = 'client_data' AND c.owner_id = %(owner)s AND c.client_id = %(client)s)"
                params["client"] = client_id
            else:
                client = "FALSE"
            if scope == SCOPE_METIER:
                filters.append(metier)
            elif scope == SCOPE_CLIENT:
                filters.append(client)
            else:
                filters.append(f"({metier} OR {client})")
            where = " AND ".join(filters)
            base = f"FROM cfo_knowledge_chunks c JOIN cfo_knowledge_docs d ON d.doc_id = c.doc_id WHERE {where}"
            results: dict[str, dict] = {}
            with conn.cursor() as cur:
                if embedding:
                    cur.execute(
                        f"SELECT {_CHUNK_SELECT}, 1 - (c.embedding <=> %(emb)s::vector) AS score, 'semantic' AS match "
                        f"{base} AND c.embedding IS NOT NULL ORDER BY c.embedding <=> %(emb)s::vector LIMIT %(limit)s",
                        {**params, "emb": str(embedding)},
                    )
                    for row in cur.fetchall():
                        if row["score"] is not None and row["score"] >= min_score:
                            results[row["chunk_id"]] = row
                if query and len(results) < limit:
                    cur.execute(
                        f"SELECT {_CHUNK_SELECT}, "
                        f"ts_rank({_SEARCH_VECTOR}, websearch_to_tsquery('french', %(q)s)) AS score, 'keyword' AS match "
                        f"{base} AND {_SEARCH_VECTOR} @@ websearch_to_tsquery('french', %(q)s) "
                        "ORDER BY score DESC LIMIT %(limit)s",
                        {**params, "q": " or ".join(dict.fromkeys(_tokens(query))) or query},
                    )
                    for row in cur.fetchall():
                        results.setdefault(row["chunk_id"], row)
            return list(results.values())[:limit]
        except psycopg.Error as e:
            print(f"[WARN] search_chunks failed: {e}")
        finally:
            conn.close()

    # Repli JSON (développement et tests)
    with _lock:
        rows = _load(_chunks_file())
        names = {d.get("doc_id"): d.get("filename") for d in _load(_DOCS_FILE)}
    if content_types:
        rows = [c for c in rows if c.get("content_type") in content_types]

    def in_scope(c: dict) -> bool:
        if c.get("content_type") != "client_data":
            return scope != SCOPE_CLIENT and c.get("owner_id") in metier_owners
        if c.get("owner_id") != owner_id:
            return False
        if scope == SCOPE_METIER or not client_id or c.get("client_id") != client_id:
            return False
        return not project_id or c.get("project_id") in (project_id, None)

    rows = [c for c in rows if in_scope(c)]
    query_tokens = set(_tokens(query))
    scored = []
    for c in rows:
        semantic = _cosine(embedding, c.get("embedding")) if embedding and c.get("embedding") else 0.0
        priority = set(_tokens(c.get("search_meta") or "")) | {k.lower() for k in c.get("keywords") or []}
        haystack = set(_tokens(c.get("content", ""))) | priority
        lexical = len(query_tokens & haystack) / len(query_tokens) if query_tokens else 0.0
        # Un terme trouvé dans les mots-clés, le titre ou la description pèse davantage.
        if query_tokens:
            lexical += 0.5 * len(query_tokens & priority) / len(query_tokens)
            # La description et les mots-clés du document aident à trouver, avec un poids faible.
            lexical += 0.1 * len(query_tokens & set(_tokens(c.get("doc_terms") or ""))) / len(query_tokens)
        if semantic >= min_score:
            scored.append((1 + semantic, "semantic", semantic, c))
        elif lexical > 0:
            scored.append((lexical, "keyword", lexical, c))
    scored.sort(key=lambda item: -item[0])
    return [
        {**{k: v for k, v in c.items() if k != "embedding"}, "filename": names.get(c.get("doc_id")), "score": score, "match": match}
        for _, match, score, c in scored[:limit]
    ]


def delete_doc_children(doc_id: str, owner_id: str | None = None) -> list[str]:
    """Supprime segments et images d'un document. Retourne les chemins des fichiers image à effacer."""
    paths: list[str] = []
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                owner_clause = " AND owner_id = %s" if owner_id else ""
                args = (doc_id, owner_id) if owner_id else (doc_id,)
                cur.execute(f"SELECT storage_path FROM cfo_document_images WHERE doc_id = %s{owner_clause}", args)
                paths = [r["storage_path"] for r in cur.fetchall()]
                cur.execute(f"DELETE FROM cfo_document_images WHERE doc_id = %s{owner_clause}", args)
                cur.execute(f"DELETE FROM cfo_knowledge_chunks WHERE doc_id = %s{owner_clause}", args)
                conn.commit()
        except psycopg.Error as e:
            print(f"[WARN] delete_doc_children failed: {e}")
            conn.rollback()
        finally:
            conn.close()
    with _lock:
        images = _load(_images_file())
        paths += [i["storage_path"] for i in images if i.get("doc_id") == doc_id and i.get("storage_path")]
        _save(_images_file(), [i for i in images if i.get("doc_id") != doc_id])
        _save(_chunks_file(), [c for c in _load(_chunks_file()) if c.get("doc_id") != doc_id])
    return list(dict.fromkeys(paths))


def replace_images(doc_id: str, owner_id: str, images: list[dict]) -> int:
    if not owner_id:
        return 0
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM cfo_document_images WHERE doc_id = %s AND owner_id = %s", (doc_id, owner_id))
                placeholders = ", ".join(f"%({c})s" for c in _IMAGE_COLUMNS)
                for image in images:
                    cur.execute(
                        f"INSERT INTO cfo_document_images ({', '.join(_IMAGE_COLUMNS)}) VALUES ({placeholders})",
                        _row_params({**image, "doc_id": doc_id, "owner_id": owner_id}, _IMAGE_COLUMNS, {"keywords", "extracted_data"}),
                    )
                conn.commit()
            return len(images)
        except psycopg.Error as e:
            print(f"[WARN] replace_images failed: {e}")
            conn.rollback()
        finally:
            conn.close()
    with _lock:
        stored = [i for i in _load(_images_file()) if i.get("doc_id") != doc_id]
        stored.extend({**i, "doc_id": doc_id, "owner_id": owner_id} for i in images)
        _save(_images_file(), stored)
    return len(images)


def list_images(doc_id: str, owner_id: str | None) -> list[dict]:
    if not owner_id:
        return []
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM cfo_document_images WHERE doc_id = %s AND owner_id = %s ORDER BY page_or_slide, image_id",
                    (doc_id, owner_id),
                )
                return cur.fetchall()
        except psycopg.Error as e:
            print(f"[WARN] list_images failed: {e}")
        finally:
            conn.close()
    with _lock:
        return [i for i in _load(_images_file()) if i.get("doc_id") == doc_id and i.get("owner_id") == owner_id]


def get_image(image_id: str, owner_id: str | None) -> dict | None:
    if not owner_id:
        return None
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM cfo_document_images WHERE image_id = %s AND owner_id = %s", (image_id, owner_id))
                return cur.fetchone()
        except psycopg.Error as e:
            print(f"[WARN] get_image failed: {e}")
        finally:
            conn.close()
    with _lock:
        return next((i for i in _load(_images_file()) if i.get("image_id") == image_id and i.get("owner_id") == owner_id), None)


# ═════════════════════════════════════════════════════════════════════════════
# RAG métier / RAG client : dossiers de clients et sous-dossiers de projets
# ═════════════════════════════════════════════════════════════════════════════
#
# - RAG métier : sources de connaissance (méthodes, normes, guides). Communes à tous les
#   dossiers d'un même utilisateur ; aucun rattachement à un client.
# - RAG client : données réelles. Chaque document vit dans un dossier client, et
#   facultativement dans un sous-dossier de projet de ce client.
#
# Règle de recherche : les données d'un client ne sortent que si ce client est le dossier
# actif de la conversation. Les chiffres du client A ne nourrissent jamais l'analyse du client B.

SCOPE_METIER = "metier"
SCOPE_CLIENT = "client"

FOLDERS_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS cfo_folders (
        folder_id VARCHAR(64) PRIMARY KEY,
        owner_id VARCHAR(255) NOT NULL,
        parent_id VARCHAR(64),
        kind VARCHAR(20) NOT NULL CHECK (kind IN ('client', 'project')),
        name VARCHAR(200) NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        CHECK ((kind = 'client' AND parent_id IS NULL) OR (kind = 'project' AND parent_id IS NOT NULL))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_cfo_folders_owner ON cfo_folders (owner_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_cfo_folders_name ON cfo_folders (owner_id, COALESCE(parent_id, ''), lower(name))",
    "ALTER TABLE cfo_knowledge_docs ADD COLUMN IF NOT EXISTS project_id VARCHAR(64)",
    "ALTER TABLE cfo_knowledge_chunks ADD COLUMN IF NOT EXISTS project_id VARCHAR(64)",
    "CREATE INDEX IF NOT EXISTS idx_cfo_chunks_client ON cfo_knowledge_chunks (owner_id, client_id, project_id)",
]
INGESTION_V2_SCHEMA.extend(FOLDERS_SCHEMA)
_CHUNK_COLUMNS = (*_CHUNK_COLUMNS, "project_id")
# ─────────────────────────────────────────────────────────────────────────────
# Index par description et mots-clés
# ─────────────────────────────────────────────────────────────────────────────
# - document : une description et des mots-clés (générés à l'indexation, modifiables) ;
# - segment  : search_meta = titre + mots-clés du segment (poids fort, avant le texte) ;
#   doc_terms = description + mots-clés du document (poids faible, après le texte) ;
# - image    : une description (modèle de vision, RAG métier seulement) en plus du texte OCR.
SEARCH_INDEX_SCHEMA = [
    "ALTER TABLE cfo_knowledge_docs ADD COLUMN IF NOT EXISTS description TEXT",
    "ALTER TABLE cfo_knowledge_docs ADD COLUMN IF NOT EXISTS keywords JSONB DEFAULT '[]'",
    "ALTER TABLE cfo_knowledge_chunks ADD COLUMN IF NOT EXISTS search_meta TEXT",
    "ALTER TABLE cfo_knowledge_chunks ADD COLUMN IF NOT EXISTS doc_terms TEXT",
    "ALTER TABLE cfo_document_images ADD COLUMN IF NOT EXISTS description TEXT",
    """CREATE INDEX IF NOT EXISTS idx_cfo_chunks_weighted ON cfo_knowledge_chunks USING GIN (
        (setweight(to_tsvector('french', coalesce(search_meta, '')), 'A') || setweight(search_text, 'B')
         || setweight(to_tsvector('french', coalesce(doc_terms, '')), 'C')))""",
]
INGESTION_V2_SCHEMA.extend(SEARCH_INDEX_SCHEMA)
_CHUNK_COLUMNS = (*_CHUNK_COLUMNS, "search_meta", "doc_terms")
_IMAGE_COLUMNS = (*_IMAGE_COLUMNS, "description")
MAX_DOC_KEYWORDS = 30


def clean_keywords(values) -> list[str]:
    """Liste de mots-clés propre : chaînes courtes, sans doublon (casse ignorée), bornée."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values if isinstance(values, (list, tuple)) else []:
        word = " ".join(str(value).split())[:60]
        if word and word.lower() not in seen:
            seen.add(word.lower())
            cleaned.append(word)
    return cleaned[:MAX_DOC_KEYWORDS]


def doc_keywords_of(doc: dict | None) -> list[str]:
    value = (doc or {}).get("keywords") or []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = []
    return clean_keywords(value)


def set_doc_index_terms(doc_id: str, owner_id: str, description: str | None, keywords: list[str]) -> None:
    """Enregistre la description et les mots-clés d'un document (pipeline d'indexation)."""
    if not owner_id:
        return
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE cfo_knowledge_docs SET description = %s, keywords = %s WHERE doc_id = %s AND owner_id = %s",
                    (description, json.dumps(keywords), doc_id, owner_id),
                )
                conn.commit()
            return
        except psycopg.Error as e:
            print(f"[WARN] set_doc_index_terms failed: {e}")
            conn.rollback()
        finally:
            conn.close()
    with _lock:
        docs = _load(_DOCS_FILE)
        for d in docs:
            if d.get("doc_id") == doc_id and d.get("owner_id") == owner_id:
                d["description"], d["keywords"] = description, keywords
        _save(_DOCS_FILE, docs)


def refresh_search_meta(doc_id: str, owner_id: str) -> int:
    """Reporte sur les segments la description et les mots-clés du document après leur
    modification. Retourne le nombre de segments mis à jour."""
    from services.chunker import doc_terms

    doc = get_kb_doc(doc_id, owner_id)
    if not doc:
        return 0
    terms = doc_terms(doc.get("description"), doc_keywords_of(doc))
    updates = [(terms, c["chunk_id"]) for c in list_chunks(doc_id, owner_id)]
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                for meta, chunk_id in updates:
                    cur.execute(
                        "UPDATE cfo_knowledge_chunks SET doc_terms = %s WHERE chunk_id = %s AND owner_id = %s",
                        (meta, chunk_id, owner_id),
                    )
                conn.commit()
            return len(updates)
        except psycopg.Error as e:
            print(f"[WARN] refresh_search_meta failed: {e}")
            conn.rollback()
        finally:
            conn.close()
    with _lock:
        stored = _load(_chunks_file())
        by_id = {chunk_id: meta for meta, chunk_id in updates}
        for c in stored:
            if c.get("chunk_id") in by_id and c.get("owner_id") == owner_id:
                c["doc_terms"] = by_id[c["chunk_id"]]
        _save(_chunks_file(), stored)
    return len(updates)
KB_EDITABLE_FIELDS = (*KB_EDITABLE_FIELDS, "project_id")


def rag_scope(content_type: str | None) -> str:
    """Un document de données client relève du RAG client ; tout le reste, du RAG métier."""
    return SCOPE_CLIENT if (content_type or "client_data") == "client_data" else SCOPE_METIER


class FolderError(ValueError):
    """Dossier invalide : nom vide, doublon, parent introuvable ou dossier non vide."""


def _folders_file() -> Path:
    return _DOCS_FILE.parent / "folders.json"


def _clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name or len(name) > 200:
        raise FolderError("Le nom du dossier doit contenir entre 1 et 200 caractères")
    return name


def get_folder(folder_id: str | None, owner_id: str | None) -> dict | None:
    if not folder_id or not owner_id:
        return None
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM cfo_folders WHERE folder_id = %s AND owner_id = %s", (folder_id, owner_id))
                return cur.fetchone()
        except psycopg.Error as e:
            print(f"[WARN] get_folder failed: {e}")
        finally:
            conn.close()
    with _lock:
        return next((f for f in _load(_folders_file()) if f.get("folder_id") == folder_id and f.get("owner_id") == owner_id), None)


def create_folder(owner_id: str, name: str, parent_id: str | None = None) -> dict:
    """Crée un dossier client (sans parent) ou un sous-dossier de projet (parent = dossier client)."""
    if not owner_id:
        raise FolderError("Propriétaire requis")
    name = _clean_name(name)
    kind = "client"
    if parent_id:
        parent = get_folder(parent_id, owner_id)
        if not parent or parent.get("kind") != "client":
            raise FolderError("Un projet doit être créé dans un dossier client existant")
        kind = "project"
    folder = {"folder_id": new_id(), "owner_id": owner_id, "parent_id": parent_id, "kind": kind,
              "name": name, "created_at": datetime.utcnow().isoformat()}
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO cfo_folders (folder_id, owner_id, parent_id, kind, name) "
                    "VALUES (%(folder_id)s, %(owner_id)s, %(parent_id)s, %(kind)s, %(name)s) RETURNING *",
                    folder,
                )
                row = cur.fetchone()
                conn.commit()
                return row
        except psycopg.errors.UniqueViolation as e:
            conn.rollback()
            raise FolderError("Un dossier porte déjà ce nom à cet endroit") from e
        except psycopg.Error as e:
            print(f"[WARN] create_folder failed: {e}")
            conn.rollback()
        finally:
            conn.close()
    with _lock:
        folders = _load(_folders_file())
        if any(f["owner_id"] == owner_id and f.get("parent_id") == parent_id and f["name"].lower() == name.lower() for f in folders):
            raise FolderError("Un dossier porte déjà ce nom à cet endroit")
        folders.append(folder)
        _save(_folders_file(), folders)
    return folder


def rename_folder(folder_id: str, owner_id: str, name: str) -> dict | None:
    name = _clean_name(name)
    current = get_folder(folder_id, owner_id)
    if not current:
        return None
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE cfo_folders SET name = %s WHERE folder_id = %s AND owner_id = %s RETURNING *",
                            (name, folder_id, owner_id))
                row = cur.fetchone()
                conn.commit()
                return row
        except psycopg.errors.UniqueViolation as e:
            conn.rollback()
            raise FolderError("Un dossier porte déjà ce nom à cet endroit") from e
        except psycopg.Error as e:
            print(f"[WARN] rename_folder failed: {e}")
            conn.rollback()
        finally:
            conn.close()
    with _lock:
        folders = _load(_folders_file())
        if any(f["owner_id"] == owner_id and f.get("parent_id") == current.get("parent_id")
               and f["name"].lower() == name.lower() and f["folder_id"] != folder_id for f in folders):
            raise FolderError("Un dossier porte déjà ce nom à cet endroit")
        for f in folders:
            if f["folder_id"] == folder_id and f["owner_id"] == owner_id:
                f["name"] = name
                current = f
        _save(_folders_file(), folders)
    return current


def list_folders(owner_id: str | None) -> list[dict]:
    if not owner_id:
        return []
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM cfo_folders WHERE owner_id = %s ORDER BY lower(name)", (owner_id,))
                return cur.fetchall()
        except psycopg.Error as e:
            print(f"[WARN] list_folders failed: {e}")
        finally:
            conn.close()
    with _lock:
        return sorted((f for f in _load(_folders_file()) if f.get("owner_id") == owner_id), key=lambda f: f["name"].lower())


def folder_tree(owner_id: str | None) -> list[dict]:
    """Dossiers client, chacun avec ses projets et le nombre de documents à chaque niveau."""
    folders = list_folders(owner_id)
    docs = [d for d in list_kb_docs(owner_id=owner_id) if rag_scope(d.get("content_type")) == SCOPE_CLIENT]
    by_client: dict[str, int] = {}
    by_project: dict[str, int] = {}
    for d in docs:
        if d.get("client_id"):
            by_client[d["client_id"]] = by_client.get(d["client_id"], 0) + 1
        if d.get("project_id"):
            by_project[d["project_id"]] = by_project.get(d["project_id"], 0) + 1
    tree = []
    for client in (f for f in folders if f["kind"] == "client"):
        projects = [
            {"project_id": p["folder_id"], "name": p["name"], "documents": by_project.get(p["folder_id"], 0)}
            for p in folders if p["kind"] == "project" and p.get("parent_id") == client["folder_id"]
        ]
        tree.append({"client_id": client["folder_id"], "name": client["name"],
                     "documents": by_client.get(client["folder_id"], 0), "projects": projects})
    return tree


def delete_folder(folder_id: str, owner_id: str) -> bool:
    """Supprime un dossier vide. Refuse s'il contient des documents ou des projets."""
    folder = get_folder(folder_id, owner_id)
    if not folder:
        return False
    column = "client_id" if folder["kind"] == "client" else "project_id"
    if any(d.get(column) == folder_id for d in list_kb_docs(owner_id=owner_id)):
        raise FolderError("Le dossier contient des documents : déplacez-les ou supprimez-les d'abord")
    if folder["kind"] == "client" and any(f.get("parent_id") == folder_id for f in list_folders(owner_id)):
        raise FolderError("Le dossier contient des projets : supprimez-les d'abord")
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM cfo_folders WHERE folder_id = %s AND owner_id = %s", (folder_id, owner_id))
                conn.commit()
                return True
        except psycopg.Error as e:
            print(f"[WARN] delete_folder failed: {e}")
            conn.rollback()
        finally:
            conn.close()
    with _lock:
        _save(_folders_file(), [f for f in _load(_folders_file()) if f.get("folder_id") != folder_id])
    return True


def resolve_placement(owner_id: str, content_type: str | None, client_id: str | None, project_id: str | None) -> tuple[str | None, str | None]:
    """Valide l'emplacement d'un document et retourne (client_id, project_id).

    - RAG métier : jamais rattaché à un client.
    - RAG client : dossier client obligatoire, appartenant à l'utilisateur ; le projet,
      s'il est donné, doit être un sous-dossier de ce client.
    """
    if rag_scope(content_type) == SCOPE_METIER:
        return None, None
    client = get_folder(client_id, owner_id)
    if not client or client.get("kind") != "client":
        raise FolderError("Un document client doit être placé dans un dossier client")
    if project_id:
        project = get_folder(project_id, owner_id)
        if not project or project.get("kind") != "project" or project.get("parent_id") != client["folder_id"]:
            raise FolderError("Ce projet n'appartient pas à ce dossier client")
        return client["folder_id"], project["folder_id"]
    return client["folder_id"], None


def move_doc_chunks(doc_id: str, owner_id: str, client_id: str | None, project_id: str | None, content_type: str | None = None) -> None:
    """Reporte sur les segments le dossier (et le type) d'un document déplacé ou reclassé."""
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE cfo_knowledge_chunks SET client_id = %s, project_id = %s, "
                    "content_type = COALESCE(%s, content_type) WHERE doc_id = %s AND owner_id = %s",
                    (client_id, project_id, content_type, doc_id, owner_id),
                )
                conn.commit()
        except psycopg.Error as e:
            print(f"[WARN] move_doc_chunks failed: {e}")
            conn.rollback()
        finally:
            conn.close()
    with _lock:
        chunks = _load(_chunks_file())
        for c in chunks:
            if c.get("doc_id") == doc_id and c.get("owner_id") == owner_id:
                c["client_id"], c["project_id"] = client_id, project_id
                if content_type:
                    c["content_type"] = content_type
        _save(_chunks_file(), chunks)


def delete_owner_folders(owner_id: str) -> None:
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM cfo_folders WHERE owner_id = %s", (owner_id,))
                conn.commit()
        except psycopg.Error:
            conn.rollback()
        finally:
            conn.close()
    with _lock:
        folders = _load(_folders_file())
        if any(f.get("owner_id") == owner_id for f in folders):
            _save(_folders_file(), [f for f in folders if f.get("owner_id") != owner_id])

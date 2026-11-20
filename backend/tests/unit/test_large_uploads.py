"""Gros documents : envoi écrit sur disque par blocs, indexation en arrière-plan, limite de 500 Mo."""
from __future__ import annotations

import asyncio
import io
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cfo_kf_routes
import kb_ingest
import kb_storage
import main
import security_limits

FIXTURES = Path(__file__).parent.parent / "fixtures"
STATEMENTS = (FIXTURES / "etats_financiers.pdf").read_bytes()
ADMIN = "11111111-1111-1111-1111-111111111111"
H = {"X-User-Id": ADMIN}
METIER = {"bulk_metadata": json.dumps({"content_type": "internal_knowledge"})}


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(kb_storage, "get_conn", lambda: None)
    monkeypatch.setattr(kb_storage, "_DOCS_FILE", tmp_path / "docs.json")
    monkeypatch.setattr(security_limits, "UPLOAD_ROOT", tmp_path / "uploads")
    (tmp_path / "uploads").mkdir()
    monkeypatch.setenv("ADMIN_USER_IDS", ADMIN)
    monkeypatch.setenv("INGEST_BACKGROUND", "1")
    monkeypatch.setattr(kb_ingest, "_index_slots", None)

    async def no_embedding(*_a, **_k):
        return None

    monkeypatch.setattr(kb_ingest, "generate_embedding", no_embedding)
    monkeypatch.setattr(main, "generate_embedding", no_embedding)
    for limiter in (security_limits.llm_limiter, security_limits.upload_limiter):
        limiter.reset()
    # Le client reste ouvert : les tâches d'arrière-plan vivent dans sa boucle d'événements.
    with TestClient(main.app) as client:
        yield client


def _post(api, name="etats.pdf", content=STATEMENTS, data=METIER):
    return api.post("/api/knowledge/ingest", headers=H, data=data,
                    files=[("files", (name, io.BytesIO(content), "application/pdf"))])


def _wait(api, doc_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        doc = api.get(f"/api/knowledge/{doc_id}", headers=H).json()
        if doc["status"] != "processing":
            return doc
        time.sleep(0.05)
    raise AssertionError("indexation non terminée")


def test_default_limits_allow_500_mb_per_file_for_the_rag_only():
    assert security_limits.MAX_KNOWLEDGE_FILE_SIZE == 500 * 1024 * 1024
    assert security_limits.MAX_KNOWLEDGE_REQUEST_BYTES >= security_limits.MAX_KNOWLEDGE_FILE_SIZE
    assert security_limits.MAX_FILE_SIZE == 25 * 1024 * 1024        # pièces jointes du chat : inchangé


class TestBackgroundIndexing:
    def test_upload_answers_at_once_then_the_document_becomes_indexed(self, api, monkeypatch):
        gate = asyncio.Event()
        real = kb_ingest.index_document

        async def slow(*args, **kwargs):
            await gate.wait()                                  # l'indexation dure aussi longtemps qu'on veut
            return await real(*args, **kwargs)

        monkeypatch.setattr(kb_ingest, "index_document", slow)
        answer = _post(api)
        assert answer.status_code == 200
        doc = answer.json()["documents"][0]
        assert doc["status"] == "processing"
        assert "text_content" not in doc and "filepath" not in doc and "embedding" not in doc
        # Pendant l'indexation, le document est visible « en cours » et le serveur répond.
        assert api.get(f"/api/knowledge/{doc['doc_id']}", headers=H).json()["status"] == "processing"
        assert api.get("/api/knowledge/list", headers=H).json()["documents"][0]["status"] == "processing"
        api.portal.call(gate.set)
        done = _wait(api, doc["doc_id"])
        assert done["status"] == "indexed" and done["chunks_count"] > 0

    def test_a_crash_during_indexing_never_leaves_the_document_processing(self, api, monkeypatch):
        async def boom(*_a, **_k):
            raise MemoryError("plus de mémoire")

        monkeypatch.setattr(kb_ingest, "index_document", boom)
        doc = _post(api).json()["documents"][0]
        done = _wait(api, doc["doc_id"])
        assert done["status"] == "error" and done["chunks_count"] == 0
        assert "MemoryError" in done["status_message"] and "Réindexer" in done["status_message"]

    def test_documents_are_indexed_one_at_a_time_by_default(self, api, monkeypatch):
        running, peak = 0, 0
        real = kb_ingest.index_document

        async def tracked(*args, **kwargs):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            try:
                return await real(*args, **kwargs)
            finally:
                running -= 1

        monkeypatch.setattr(kb_ingest, "index_document", tracked)
        ids = [_post(api, name=f"doc{i}.pdf").json()["documents"][0]["doc_id"] for i in range(3)]
        assert [_wait(api, i)["status"] for i in ids] == ["indexed"] * 3
        assert peak == 1

    def test_reindex_runs_in_the_background_too(self, api):
        doc = _post(api).json()["documents"][0]
        _wait(api, doc["doc_id"])
        started = api.post(f"/api/knowledge/{doc['doc_id']}/reindex", headers=H).json()
        assert started == {"doc_id": doc["doc_id"], "filename": "etats.pdf", "status": "processing"}
        assert _wait(api, doc["doc_id"])["status"] == "indexed"
        everything = api.post("/api/knowledge/reindex-all", headers=H).json()
        assert [r["status"] for r in everything["results"]] == ["processing"]
        assert _wait(api, doc["doc_id"])["chunks_count"] > 0

    def test_reindex_reports_a_missing_original_file_at_once(self, api):
        doc = _post(api).json()["documents"][0]
        _wait(api, doc["doc_id"])
        for stored in security_limits.UPLOAD_ROOT.rglob("*.pdf"):
            stored.unlink()
        answer = api.post(f"/api/knowledge/{doc['doc_id']}/reindex", headers=H).json()
        assert answer["status"] == "skipped" and "téléverser" in answer["reason"]
        assert api.get(f"/api/knowledge/{doc['doc_id']}", headers=H).json()["status"] == "indexed"   # statut intact


class TestRestartRecovery:
    def test_documents_left_processing_by_a_restart_are_flagged(self, api, monkeypatch):
        async def never(*_a, **_k):
            await asyncio.Event().wait()

        monkeypatch.setattr(kb_ingest, "index_document", never)
        doc = _post(api).json()["documents"][0]
        assert kb_ingest.recover_interrupted() == 1               # ce que fait le serveur à son démarrage
        flagged = api.get(f"/api/knowledge/{doc['doc_id']}", headers=H).json()
        assert flagged["status"] == "error" and "redémarrage" in flagged["status_message"]
        assert kb_ingest.recover_interrupted() == 0


class TestStreamedUpload:
    def test_a_file_over_the_limit_is_refused_and_leaves_nothing_behind(self, api, monkeypatch):
        monkeypatch.setattr(cfo_kf_routes, "MAX_KNOWLEDGE_FILE_SIZE", 3 * 1024 * 1024)
        refused = _post(api, content=b"%PDF-1.4\n" + b"0" * (4 * 1024 * 1024))
        assert refused.status_code == 413 and "max 3 Mo" in refused.json()["detail"]
        assert not list(security_limits.UPLOAD_ROOT.rglob("*.pdf"))
        assert kb_storage.list_kb_docs(owner_id=ADMIN) == []

    def test_a_file_larger_than_the_old_25_mb_limit_is_now_stored(self, api, monkeypatch):
        async def stub(*_a, **_k):                                # la conversion n'est pas l'objet de ce test
            return {"markdown": "texte", "chunks_count": 1, "images_count": 0, "extraction_method": "text",
                    "first_embedding": None, "warnings": [], "embedded_count": 0}

        monkeypatch.setattr(kb_ingest, "index_document", stub)
        size = 30 * 1024 * 1024
        doc = _post(api, name="gros.pdf", content=b"%PDF-1.4\n" + b"0" * size).json()["documents"][0]
        assert doc["file_size_bytes"] == size + 9 and len(doc["file_hash"]) == 64
        assert _wait(api, doc["doc_id"])["status"] == "indexed"
        stored = list(security_limits.UPLOAD_ROOT.rglob("*gros.pdf"))
        assert len(stored) == 1 and stored[0].stat().st_size == size + 9

    def test_an_unsupported_type_is_reported_as_skipped(self, api):
        answer = _post(api, name="video.mp4", content=b"0000").json()
        assert answer == {"documents": [], "count": 0, "skipped": ["video.mp4"]}

    def test_the_business_rag_stays_admin_only_and_nothing_is_written(self, api):
        refused = api.post("/api/knowledge/ingest", headers={"X-User-Id": "22222222-2222-2222-2222-222222222222"},
                           data=METIER, files=[("files", ("x.pdf", io.BytesIO(STATEMENTS), "application/pdf"))])
        assert refused.status_code == 403
        assert not list(security_limits.UPLOAD_ROOT.rglob("*.pdf"))

    def test_an_invalid_folder_keeps_neither_file_nor_document(self, api):
        refused = api.post("/api/knowledge/ingest", headers=H, data={"bulk_metadata": json.dumps({"client_id": "inconnu"})},
                           files=[("files", ("x.pdf", io.BytesIO(STATEMENTS), "application/pdf"))])
        assert refused.status_code == 400
        assert not list(security_limits.UPLOAD_ROOT.rglob("*.pdf"))
        assert kb_storage.list_kb_docs(owner_id=ADMIN) == []

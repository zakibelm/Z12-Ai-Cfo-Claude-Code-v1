"""Benchmark RAG (hors ligne, recherche plein texte du stockage JSON).

Mesure : rappel@3 sur des questions types, et étanchéité entre dossiers clients.
"""
import json
from pathlib import Path

import pytest

import kb_storage

DATA = json.loads((Path(__file__).parent / "rag_dataset.json").read_text(encoding="utf-8"))
USER = "11111111-1111-1111-1111-111111111111"
RECALL_THRESHOLD = 0.8


@pytest.fixture(autouse=True)
def _corpus(monkeypatch, tmp_path):
    monkeypatch.setattr(kb_storage, "get_conn", lambda: None)
    monkeypatch.setattr(kb_storage, "_DOCS_FILE", tmp_path / "docs.json")
    monkeypatch.setattr(kb_storage, "_RUNS_FILE", tmp_path / "runs.json")
    kb_storage._save(kb_storage._DOCS_FILE, [{"doc_id": d["id"], "filename": f"{d['id']}.pdf"} for d in DATA["docs"]])
    for d in DATA["docs"]:
        kb_storage.replace_chunks(d["id"], USER, [{
            "chunk_id": f"c-{d['id']}", "chunk_index": 0, "content": d["text"], "content_type": "client_data",
            "client_id": d["client"], "source_page": d["page"], "sensibilite": "professionnel",
        }])


def _search(q, client):
    return kb_storage.search_chunks(q, None, USER, limit=3, client_id=client)


def test_recall_at_3():
    hits = 0
    for case in DATA["queries"]:
        found = [r["doc_id"] for r in _search(case["q"], DATA["client_id"])]
        hits += case["expect"] in found
    recall = hits / len(DATA["queries"])
    print(f"\n[benchmark RAG] rappel@3 = {recall:.0%}")
    assert recall >= RECALL_THRESHOLD


def test_no_leak_between_clients():
    for case in DATA["forbidden"]:
        found = [r["doc_id"] for r in _search(case["q"], DATA["client_id"])]
        assert case["must_not_return"] not in found


def test_no_client_data_without_active_client():
    assert kb_storage.search_chunks("TPS TVQ", None, USER, limit=3, client_id=None) == []

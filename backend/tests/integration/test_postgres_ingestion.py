"""Tests d'intégration sur une vraie base PostgreSQL.

Lancés seulement si Z12_TEST_DATABASE_URL est défini (la CI fournit une base pgvector).
Si l'extension pgvector est absente de la base de test, un type de remplacement permet de
vérifier tout le SQL sauf la recherche sémantique, qui est alors ignorée.
"""
from __future__ import annotations

import os

import pytest

import kb_storage

TEST_URL = os.getenv("Z12_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="Z12_TEST_DATABASE_URL non défini")

A, B = "user-a", "user-b"


@pytest.fixture(scope="module")
def has_vector():
    import psycopg

    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        for table in ("cfo_document_images", "cfo_knowledge_chunks", "cfo_knowledge_docs", "cfo_folders"):
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        try:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            return True
        except psycopg.Error:
            conn.execute("DROP DOMAIN IF EXISTS vector CASCADE")
            conn.execute("CREATE DOMAIN vector AS text")
            return False


@pytest.fixture
def db(monkeypatch, has_vector):
    import psycopg

    import init_db_rag

    def adapt(sql: str) -> str:
        return sql if has_vector else sql.replace("vector(1024)", "vector")

    monkeypatch.setattr(kb_storage, "DATABASE_URL", TEST_URL)
    monkeypatch.setattr(kb_storage, "INGESTION_V2_SCHEMA", [adapt(s) for s in kb_storage.INGESTION_V2_SCHEMA])
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        for statement in init_db_rag.STATEMENTS:
            if "cfo_knowledge_docs" in statement and "vector" not in statement.split("(")[0].lower():
                try:
                    conn.execute(adapt(statement))
                except psycopg.Error:
                    pass  # extension ou index sans objet dans la base de test
        for table in ("cfo_document_images", "cfo_knowledge_chunks", "cfo_folders"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("DELETE FROM cfo_knowledge_docs")
    assert kb_storage.ensure_security_schema() is True
    assert kb_storage.ensure_ingestion_schema() is True
    assert kb_storage.ensure_ingestion_schema() is True  # idempotent
    return has_vector


def _doc(owner: str, name: str, **extra) -> dict:
    return kb_storage.insert_kb_doc({
        "filename": name, "filepath": f"/tmp/{name}", "owner_id": owner, "sensibilite": "professionnel",
        "tags": ["états financiers", "2025"], "regulatory_refs": ["NCECF 1400"], "agents_assigned": ["marc"],
        "content_type": "client_data", **extra,
    })


def _chunk(i: int, content: str, **extra) -> dict:
    return {
        "chunk_id": kb_storage.new_id(), "chunk_index": i, "total_chunks": 3, "content": content,
        "heading": "Résultats", "source_page": i + 1, "keywords": ["état des résultats"],
        "content_type": "client_data", "sensibilite": "professionnel", "extraction_method": "native",
        "verification_status": "unverified", "embedding": None, **extra,
    }


def test_document_with_json_lists_lands_in_postgres(db):
    """Régression : une liste non vide faisait échouer l'insertion (repli silencieux sur fichier)."""
    doc = _doc(A, "etats.pdf")
    stored = kb_storage.get_kb_doc(doc["doc_id"], A)
    assert stored["tags"] == ["états financiers", "2025"] and stored["regulatory_refs"] == ["NCECF 1400"]
    assert stored["content_type"] == "client_data" and stored["owner_id"] == A
    assert kb_storage.get_kb_doc(doc["doc_id"], B) is None


def _client(owner: str, name: str = "Boréal") -> str:
    return kb_storage.create_folder(owner, name)["folder_id"]


def test_chunks_roundtrip_and_french_full_text_search(db):
    client = _client(A)
    doc = _doc(A, "etats.pdf", client_id=client)
    count = kb_storage.replace_chunks(doc["doc_id"], A, [
        _chunk(0, "| Bénéfice net | 146 094 $ | 104 325 $ |", client_id=client),
        _chunk(1, "L'encaisse s'établit à 84 500 $ au bilan.", client_id=client),
        _chunk(2, "Les produits sont constatés à la livraison.", content_type="methodology"),
    ])
    assert count == 3
    chunks = kb_storage.list_chunks(doc["doc_id"], A)
    assert [c["chunk_index"] for c in chunks] == [0, 1, 2] and chunks[0]["filename"] == "etats.pdf"
    assert chunks[0]["keywords"] == ["état des résultats"] and "embedding" not in chunks[0]
    assert kb_storage.get_kb_doc(doc["doc_id"], A)["chunks_count"] == 3

    # Racinisation française : le pluriel de la question retrouve le singulier du document.
    found = kb_storage.search_chunks("Quels sont les bénéfices nets ?", None, A, client_id=client)
    assert found and found[0]["chunk_index"] == 0 and found[0]["match"] == "keyword" and found[0]["source_page"] == 1
    # Sans dossier actif, les données du client ne sortent pas ; pour un autre utilisateur, jamais.
    assert kb_storage.search_chunks("bénéfice net", None, A) == []
    assert kb_storage.search_chunks("bénéfice net", None, B, client_id=client) == []
    assert kb_storage.search_chunks("bénéfice net", None, None, client_id=client) == []
    only_method = kb_storage.search_chunks("produits livraison bénéfice", None, A, content_types=["methodology"])
    assert [c["chunk_index"] for c in only_method] == [2]
    assert kb_storage.search_chunks("'; DROP TABLE cfo_knowledge_chunks; --", None, A) == []
    assert kb_storage.list_chunks(doc["doc_id"], A)  # la table est toujours là

    # Réindexation : les segments sont remplacés, pas ajoutés.
    assert kb_storage.replace_chunks(doc["doc_id"], A, [_chunk(0, "Nouveau contenu unique.")]) == 1
    assert len(kb_storage.list_chunks(doc["doc_id"], A)) == 1


def test_images_roundtrip_and_cascade(db):
    doc = _doc(A, "rapport.pdf")
    chunk = _chunk(0, "[Image du document, page 1]\nVentes trimestrielles 2025")
    kb_storage.replace_chunks(doc["doc_id"], A, [chunk])
    image_id = kb_storage.new_id()
    kb_storage.replace_images(doc["doc_id"], A, [{
        "image_id": image_id, "chunk_id": chunk["chunk_id"], "storage_path": "/tmp/x.png", "image_hash": "a" * 64,
        "page_or_slide": 1, "width": 900, "height": 420, "extraction_method": "ocr", "extraction_model": "tesseract",
        "prompt_version": None, "extracted_text": "Ventes trimestrielles 2025", "extracted_data": None,
        "keywords": ["TPS", "TVQ"], "extraction_confidence": "high", "verification_status": "unverified",
    }])
    image = kb_storage.get_image(image_id, A)
    assert image["keywords"] == ["TPS", "TVQ"] and image["chunk_id"] == chunk["chunk_id"]
    assert kb_storage.get_image(image_id, B) is None and kb_storage.list_images(doc["doc_id"], B) == []
    assert kb_storage.delete_doc_children(doc["doc_id"], A) == ["/tmp/x.png"]
    assert kb_storage.list_images(doc["doc_id"], A) == [] and kb_storage.list_chunks(doc["doc_id"], A) == []


def test_system_fields_update_and_editable_whitelist(db):
    doc = _doc(A, "etats.pdf")
    kb_storage.set_kb_doc_system_fields(doc["doc_id"], A, {
        "text_content": "# Markdown", "status": "indexed", "chunks_count": 4, "extraction_method": "native+ocr",
        "embedding": None, "owner_id": B,  # ignoré : pas un champ système
    })
    stored = kb_storage.get_kb_doc(doc["doc_id"], A)
    assert stored["text_content"] == "# Markdown" and stored["chunks_count"] == 4 and stored["owner_id"] == A
    updated = kb_storage.update_kb_doc(doc["doc_id"], {
        "tags": ["bilan"], "content_type": "regulatory_reference", "owner_id": B, "status = 'x' --": 1,
    }, A)
    assert updated["tags"] == ["bilan"] and updated["content_type"] == "regulatory_reference" and updated["owner_id"] == A
    assert kb_storage.update_kb_doc(doc["doc_id"], {"tags": ["x"]}, B) is None


def test_erasure_removes_documents_chunks_and_images(db):
    client_a, client_b = _client(A), _client(B)  # même nom chez deux utilisateurs : permis
    mine, other = _doc(A, "a.pdf", client_id=client_a), _doc(B, "b.pdf", client_id=client_b)
    kb_storage.replace_chunks(mine["doc_id"], A, [_chunk(0, "contenu de A", client_id=client_a)])
    kb_storage.replace_chunks(other["doc_id"], B, [_chunk(0, "contenu de B", client_id=client_b)])
    assert kb_storage.delete_owner_data(A)["knowledge_docs"] == 1
    assert kb_storage.list_kb_docs(owner_id=A) == []
    assert kb_storage.search_chunks("contenu", None, A, client_id=client_a) == []
    assert kb_storage.folder_tree(A) == []
    assert len(kb_storage.search_chunks("contenu", None, B, client_id=client_b)) == 1
    assert len(kb_storage.folder_tree(B)) == 1


def test_semantic_search_with_pgvector(db):
    if not db:
        pytest.skip("extension pgvector absente de la base de test")
    client = _client(A)
    doc = _doc(A, "etats.pdf", client_id=client)
    near, far = [1.0] + [0.0] * 1023, [0.0, 1.0] + [0.0] * 1022
    kb_storage.replace_chunks(doc["doc_id"], A, [
        _chunk(0, "alpha", embedding=near, client_id=client), _chunk(1, "beta", embedding=far, client_id=client)])
    assert kb_storage.search_chunks("", near, A) == []  # aucun dossier actif
    found = kb_storage.search_chunks("", near, A, client_id=client)
    assert found[0]["chunk_index"] == 0 and found[0]["match"] == "semantic" and found[0]["score"] > 0.99
    assert all(c["chunk_index"] != 1 for c in found)  # sous le seuil de similarité
    assert kb_storage.search_chunks("", near, B, client_id=client) == []


def test_client_folders_projects_and_scoped_search(db):
    """RAG métier / RAG client dans PostgreSQL : dossiers, sous-dossiers et cloisonnement."""
    boreal, nordik = _client(A, "Boréal"), _client(A, "Nordik")
    audit = kb_storage.create_folder(A, "Audit 2025", boreal)["folder_id"]
    acquisition = kb_storage.create_folder(A, "Acquisition", boreal)["folder_id"]

    with pytest.raises(kb_storage.FolderError):
        kb_storage.create_folder(A, "  boréal ")                # doublon, casse et espaces ignorés
    with pytest.raises(kb_storage.FolderError):
        kb_storage.create_folder(A, "Sous-projet", audit)       # pas de sous-sous-dossier
    with pytest.raises(kb_storage.FolderError):
        kb_storage.create_folder(B, "Intrus", boreal)           # dossier d'un autre utilisateur
    assert kb_storage.create_folder(A, "Audit 2025", nordik)    # même nom de projet, autre client
    assert kb_storage.get_folder(boreal, B) is None

    assert kb_storage.resolve_placement(A, "methodology", boreal, audit) == (None, None)
    assert kb_storage.resolve_placement(A, "client_data", boreal, audit) == (boreal, audit)
    for bad in [(None, None), ("inconnu", None), (nordik, audit)]:
        with pytest.raises(kb_storage.FolderError):
            kb_storage.resolve_placement(A, "client_data", *bad)
    with pytest.raises(kb_storage.FolderError):
        kb_storage.resolve_placement(B, "client_data", boreal, None)

    def add(name, content_type="client_data", client_id=None, project_id=None):
        doc = _doc(A, name, content_type=content_type, client_id=client_id, project_id=project_id)
        kb_storage.replace_chunks(doc["doc_id"], A, [_chunk(
            0, "Le bénéfice net progresse.", content_type=content_type, client_id=client_id, project_id=project_id)])
        return doc["doc_id"]

    guide = add("guide.pdf", content_type="methodology")
    general = add("statuts.pdf", client_id=boreal)
    audit_doc = add("balance.pdf", client_id=boreal, project_id=audit)
    acq_doc = add("lettre.pdf", client_id=boreal, project_id=acquisition)
    nordik_doc = add("etats_nordik.pdf", client_id=nordik)

    def docs(**scope):
        return {c["doc_id"] for c in kb_storage.search_chunks("bénéfice net", None, A, limit=20, **scope)}

    assert docs() == {guide}
    assert docs(client_id=boreal) == {guide, general, audit_doc, acq_doc}
    assert docs(client_id=boreal, project_id=audit) == {guide, general, audit_doc}
    assert docs(client_id=nordik) == {guide, nordik_doc}
    assert docs(client_id=boreal, scope="client") == {general, audit_doc, acq_doc}
    assert docs(client_id=boreal, scope="metier") == {guide}

    tree = kb_storage.folder_tree(A)
    assert [c["name"] for c in tree] == ["Boréal", "Nordik"]
    assert tree[0]["documents"] == 3
    assert {p["name"]: p["documents"] for p in tree[0]["projects"]} == {"Acquisition": 1, "Audit 2025": 1}

    # Déplacer un document déplace ses segments.
    kb_storage.update_kb_doc(audit_doc, {"client_id": nordik, "project_id": None}, A)
    kb_storage.move_doc_chunks(audit_doc, A, nordik, None)
    assert audit_doc in docs(client_id=nordik) and audit_doc not in docs(client_id=boreal)

    # Un dossier non vide ne se supprime pas ; vide, oui.
    with pytest.raises(kb_storage.FolderError):
        kb_storage.delete_folder(acquisition, A)
    kb_storage.delete_kb_doc(acq_doc, A)
    assert kb_storage.delete_folder(acquisition, A) is True
    assert kb_storage.delete_folder(acquisition, A) is False
    assert kb_storage.rename_folder(audit, A, "Audit 2025-2026")["name"] == "Audit 2025-2026"
    assert kb_storage.rename_folder(audit, B, "x") is None


def test_admin_knowledge_is_shared_but_client_data_is_not(db):
    """RAG métier des administrateurs servi à tous ; données client jamais partagées."""
    admin, user = A, B
    guide = _doc(admin, "guide.pdf", content_type="methodology")
    kb_storage.replace_chunks(guide["doc_id"], admin, [_chunk(0, "Le bénéfice net se calcule ainsi.", content_type="methodology")])
    admin_client = _client(admin, "Client de l'admin")
    private = _doc(admin, "prive.pdf", client_id=admin_client)
    kb_storage.replace_chunks(private["doc_id"], admin, [_chunk(0, "Bénéfice net du client de l'admin.", client_id=admin_client)])
    user_client = _client(user, "Client de B")
    mine = _doc(user, "etats.pdf", client_id=user_client)
    kb_storage.replace_chunks(mine["doc_id"], user, [_chunk(0, "Bénéfice net de mon client.", client_id=user_client)])
    old = _doc(user, "vieux_guide.pdf", content_type="methodology")
    kb_storage.replace_chunks(old["doc_id"], user, [_chunk(0, "Ancien guide sur le bénéfice net.", content_type="methodology")])

    def docs(owner, **kw):
        return {c["doc_id"] for c in kb_storage.search_chunks("bénéfice net", None, owner, limit=20, **kw)}

    shared = {"metier_owner_ids": [admin]}
    assert docs(user, **shared) == {guide["doc_id"]}
    assert docs(user, client_id=user_client, **shared) == {guide["doc_id"], mine["doc_id"]}
    # Même en visant le dossier client de l'administrateur, rien ne sort.
    assert docs(user, client_id=admin_client, **shared) == {guide["doc_id"]}
    assert docs(admin, client_id=admin_client, **shared) == {guide["doc_id"], private["doc_id"]}
    # Aucun administrateur : aucun savoir métier servi.
    assert docs(user, client_id=user_client, metier_owner_ids=[]) == {mine["doc_id"]}


def test_weighted_search_on_keywords_headings_and_document_terms(db):
    """Recherche PostgreSQL : mots-clés et titre du segment d'abord, puis texte, puis termes du document."""
    doc = _doc(A, "guide.pdf", content_type="methodology")
    kb_storage.replace_chunks(doc["doc_id"], A, [
        _chunk(0, "On mentionne un amortissement en passant.", content_type="methodology",
               heading=None, keywords=[], search_meta=""),
        _chunk(1, "Règles de calcul et taux applicables.", content_type="methodology",
               heading="Amortissement", keywords=["amortissement"], search_meta="Amortissement amortissement"),
    ])
    found = kb_storage.search_chunks("amortissement", None, A)
    assert [c["chunk_index"] for c in found] == [1, 0]           # le mot-clé passe devant le simple texte

    # Mot absent de tout le texte : introuvable, puis trouvable une fois saisi comme mot-clé du document.
    assert kb_storage.search_chunks("zébulon", None, A) == []
    kb_storage.update_kb_doc(doc["doc_id"], {"keywords": ["Zébulon"], "description": "Méthode de répartition des frais."}, A)
    assert kb_storage.refresh_search_meta(doc["doc_id"], A) == 2
    assert len(kb_storage.search_chunks("zébulon", None, A)) == 2
    assert kb_storage.search_chunks("répartition des frais", None, A)
    stored = kb_storage.get_kb_doc(doc["doc_id"], A)
    assert stored["keywords"] == ["Zébulon"] and stored["description"].startswith("Méthode")

    kb_storage.set_doc_index_terms(doc["doc_id"], A, "Autre description.", ["alpha"])
    assert kb_storage.get_kb_doc(doc["doc_id"], A)["keywords"] == ["alpha"]


def test_image_description_is_stored(db):
    doc = _doc(A, "guide.pdf", content_type="methodology")
    kb_storage.replace_images(doc["doc_id"], A, [{
        "image_id": kb_storage.new_id(), "chunk_id": None, "storage_path": "/tmp/x.png", "image_hash": "h",
        "page_or_slide": 2, "width": 400, "height": 300, "extraction_method": "vision+ocr",
        "extraction_model": "modele/vision", "prompt_version": "v1", "extracted_text": "T1 T2",
        "extracted_data": None, "description": "Graphique en barres des ventes.", "keywords": ["graphique"],
        "extraction_confidence": "high", "verification_status": "unverified",
    }])
    image = kb_storage.list_images(doc["doc_id"], A)[0]
    assert image["description"] == "Graphique en barres des ventes." and image["keywords"] == ["graphique"]

"""Ingestion v2 : conversion Markdown (MarkItDown), OCR local, segments, images, recherche.

Les fixtures sont de petits PDF générés pour ces tests : des états financiers avec tableaux,
un rapport contenant un graphique en image, et un bilan numérisé (image sans couche texte).
"""
from __future__ import annotations

import io
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import kb_ingest
import kb_storage
import main
import security_limits
from services import markdown_converter as mc
from services.chunker import (
    CONTENT_TYPES,
    Chunk,
    chunk_pages,
    extract_keywords,
    normalize_content_type,
)
from services.markdown_converter import PageText, convert_to_markdown
from services.text_extractor import extract_text_from_file

FIXTURES = Path(__file__).parent.parent / "fixtures"
STATEMENTS = (FIXTURES / "etats_financiers.pdf").read_bytes()
REPORT = (FIXTURES / "rapport_avec_graphique.pdf").read_bytes()
SCAN = (FIXTURES / "bilan_numerise.pdf").read_bytes()
USER_A = "11111111-1111-1111-1111-111111111111"
USER_B = "22222222-2222-2222-2222-222222222222"

needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract non installé")


def _row_intact(text: str) -> bool:
    """Le libellé et ses deux montants doivent rester sur la même ligne."""
    return any("Bénéfice net" in line and "146 094" in line and "104 325" in line for line in text.splitlines())


# ─── Conversion ──────────────────────────────────────────────────────────────

class TestConversion:
    def test_aes_protected_pdf_is_read(self):
        """PDF protégé contre la modification (AES-256, sans mot de passe d'ouverture)."""
        result = convert_to_markdown((FIXTURES / "etats_proteges_aes.pdf").read_bytes(), "etats_proteges_aes.pdf")
        assert "146 094" in result.markdown and _row_intact(result.markdown)
        assert result.warnings == []

    def test_password_locked_pdf_is_reported_clearly(self):
        from services.markdown_converter import PASSWORD_WARNING

        result = convert_to_markdown((FIXTURES / "etats_mot_de_passe.pdf").read_bytes(), "etats_mot_de_passe.pdf")
        assert result.markdown == "" and result.warnings == [PASSWORD_WARNING]

    def test_financial_table_rows_stay_together(self):
        result = convert_to_markdown(STATEMENTS, "etats.pdf")
        assert _row_intact(result.markdown)
        assert "| Poste" in result.markdown  # tableau Markdown, pas du texte à plat

    def test_previous_extractor_broke_the_same_table(self):
        """Le constat qui justifie le changement : PyPDF2 éclate chaque cellule sur sa ligne."""
        assert not _row_intact(extract_text_from_file(STATEMENTS, "etats.pdf"))

    def test_pdf_pages_are_numbered_and_native(self):
        result = convert_to_markdown(STATEMENTS, "etats.pdf")
        assert [p.page for p in result.pages] == [1, 2]
        assert result.methods == {"native"}
        assert "Bilan au 31 décembre 2025" in result.pages[1].markdown

    def test_word_tables_are_kept(self):
        import docx

        document = docx.Document()
        document.add_heading("Lettre de mission", 1)
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text, table.cell(0, 1).text = "Service", "Honoraires"
        table.cell(1, 0).text, table.cell(1, 1).text = "Mission d'examen", "8 500 $"
        buffer = io.BytesIO()
        document.save(buffer)
        markdown = convert_to_markdown(buffer.getvalue(), "mission.docx").markdown
        assert "8 500 $" in markdown and "| Mission d'examen" in markdown
        assert "8 500" not in extract_text_from_file(buffer.getvalue(), "mission.docx")  # perdu auparavant

    def test_excel_sheets_become_tables(self):
        import openpyxl

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Résultats"
        sheet.append(["Poste", "2025"])
        sheet.append(["Bénéfice net", 146094])
        buffer = io.BytesIO()
        workbook.save(buffer)
        markdown = convert_to_markdown(buffer.getvalue(), "etats.xlsx").markdown
        assert "Résultats" in markdown and "| Bénéfice net | 146094 |" in markdown

    def test_no_llm_client_and_no_plugins(self):
        """Aucune image ni aucun texte ne part vers un modèle pendant la conversion."""
        instance = mc._get_markitdown()
        assert getattr(instance, "_llm_client", None) is None
        assert not getattr(instance, "_plugins_enabled", False)

    def test_garbage_never_raises(self):
        result = convert_to_markdown(b"\x00\x01ceci n'est pas un pdf", "faux.pdf")
        assert result.pages == [] or result.markdown
        assert convert_to_markdown(b"", "vide.docx").pages == []

    def test_too_many_pages_falls_back_without_crashing(self, monkeypatch):
        monkeypatch.setattr(mc, "MAX_PDF_PAGES", 1)
        result = convert_to_markdown(STATEMENTS, "etats.pdf")  # 2 pages > limite
        assert isinstance(result.pages, list)

    @needs_tesseract
    def test_scanned_page_is_read_by_local_ocr(self, monkeypatch):
        monkeypatch.setattr(mc, "ocrmypdf_available", lambda: False)  # repli Tesseract
        result = convert_to_markdown(SCAN, "bilan.pdf")
        assert result.methods == {"ocr"}
        assert "Bilan" in result.markdown and "393 650" in result.markdown

    def test_ocrmypdf_output_is_used_when_available(self, monkeypatch):
        """Quand OCRmyPDF est installé, on lit le PDF qu'il a rendu cherchable."""
        monkeypatch.setattr(mc, "_run_ocrmypdf", lambda content: STATEMENTS)
        monkeypatch.setattr(mc, "_tesseract_pdf_page", lambda *a: pytest.fail("Tesseract ne doit pas être appelé"))
        result = convert_to_markdown(SCAN, "bilan.pdf")
        assert result.methods == {"ocr"} and "Chiffre d'affaires" in result.markdown

    def test_ocrmypdf_command_is_local_and_fixed(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            Path(cmd[-1]).write_bytes(b"%PDF-ocr")

        monkeypatch.setattr(mc, "ocrmypdf_available", lambda: True)
        monkeypatch.setattr(mc.subprocess, "run", fake_run)
        assert mc._run_ocrmypdf(SCAN) == b"%PDF-ocr"
        cmd, kwargs = calls[0]
        assert cmd[0] == "ocrmypdf" and "--skip-text" in cmd and "fra+eng" in cmd
        assert kwargs["timeout"] == mc.OCR_TIMEOUT_SECONDS and kwargs.get("shell") is not True


# ─── Découpage ───────────────────────────────────────────────────────────────

class TestChunking:
    def test_chunks_keep_their_page_and_never_span_two(self):
        chunks = chunk_pages(convert_to_markdown(STATEMENTS, "etats.pdf").pages)
        assert {c.page for c in chunks} == {1, 2}
        assert [c.index for c in chunks] == list(range(len(chunks)))
        page_one = "\n".join(c.content for c in chunks if c.page == 1)
        assert "Bénéfice net" in page_one and "Encaisse" not in page_one

    def test_long_table_is_split_by_rows_with_header_repeated(self):
        header = "| Compte | Débit | Crédit |\n| --- | --- | --- |"
        rows = "\n".join(f"| Compte {i:03d} | {i * 10} $ | {i * 7} $ |" for i in range(120))
        chunks = chunk_pages([PageText(markdown=f"{header}\n{rows}", page=3)], max_chars=600)
        assert len(chunks) > 3
        for chunk in chunks:
            lines = chunk.content.splitlines()
            assert lines[0] == "| Compte | Débit | Crédit |" and lines[1].startswith("| ---")
            assert all(line.startswith("|") and line.endswith("|") for line in lines)  # aucune ligne coupée
            assert len(chunk.content) <= 600 + 60
        assert sum(c.content.count("| Compte 0") + c.content.count("| Compte 1") for c in chunks) == 120

    def test_heading_is_tracked(self):
        pages = [PageText(markdown="# Note 3 — Impôts\n\nLa DPA est calculée selon la catégorie 8.\n\n## Détail\n\nTexte.", page=1)]
        chunks = chunk_pages(pages)
        assert chunks[0].heading == "Note 3 — Impôts" and chunks[-1].heading == "Détail"

    def test_long_paragraph_is_split_under_the_limit(self):
        text = " ".join(f"Phrase numéro {i} sur la trésorerie." for i in range(200))
        chunks = chunk_pages([PageText(markdown=text)], max_chars=500)
        assert len(chunks) > 5 and all(len(c.content) <= 500 for c in chunks)

    def test_keywords_come_from_the_finance_lexicon(self):
        keywords = extract_keywords("La TPS et la TVQ sont exclues. Déclaration T2 et CO-17, crédit RS&DE, état des résultats.")
        assert {"TPS", "TVQ", "T2", "CO-17", "RS&DE", "état des résultats"} <= set(keywords)
        assert extract_keywords("Bonjour, comment allez-vous ?") == []

    def test_keywords_ignore_accents_and_case(self):
        assert "flux de trésorerie" in extract_keywords("FLUX DE TRESORERIE")

    def test_content_type_taxonomy(self):
        assert CONTENT_TYPES[0] == "client_data" and len(CONTENT_TYPES) == 6
        assert normalize_content_type("regulatory_reference") == "regulatory_reference"
        assert normalize_content_type("real_data") == "client_data"  # ancienne valeur, refusée
        assert isinstance(Chunk(content="x").keywords, list)


# ─── Ingestion et recherche de bout en bout (stockage de repli) ─────────────

@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(kb_storage, "get_conn", lambda: None)
    monkeypatch.setattr(kb_storage, "_DOCS_FILE", tmp_path / "docs.json")
    monkeypatch.setattr(security_limits, "UPLOAD_ROOT", tmp_path / "uploads")
    (tmp_path / "uploads").mkdir()
    # USER_A administre le RAG métier ; USER_B est un utilisateur ordinaire.
    monkeypatch.setenv("ADMIN_USER_IDS", USER_A)

    async def no_embedding(*_a, **_k):
        return None

    monkeypatch.setattr(kb_ingest, "generate_embedding", no_embedding)
    monkeypatch.setattr(main, "generate_embedding", no_embedding)
    for limiter in (security_limits.llm_limiter, security_limits.upload_limiter):
        limiter.reset()
    return TestClient(main.app)


def _client_folder(client, user, name="Les Entreprises Boréal"):
    """Dossier client de l'utilisateur (créé au besoin)."""
    headers = {"X-User-Id": user}
    created = client.post("/api/clients", headers=headers, json={"name": name})
    if created.status_code == 201:
        return created.json()["client_id"]
    return next(c["client_id"] for c in client.get("/api/clients", headers=headers).json()["clients"] if c["name"] == name)


def _ingest(client, user, content, name, **meta):
    import json

    # Un document de données client vit toujours dans un dossier client.
    is_client = kb_storage.rag_scope(meta.get("content_type") if meta.get("content_type") in CONTENT_TYPES else None) == "client"
    if is_client and "client_id" not in meta:
        meta["client_id"] = _client_folder(client, user)
    response = client.post(
        "/api/knowledge/ingest", headers={"X-User-Id": user},
        files=[("files", (name, io.BytesIO(content), "application/pdf"))],
        data={"bulk_metadata": json.dumps({"domaine": "Financial", **meta})},
    )
    assert response.status_code == 200, response.text
    return response.json()["documents"][0]


class TestEndToEnd:
    def test_document_is_stored_as_markdown_and_chunked(self, api):
        doc = _ingest(api, USER_A, STATEMENTS, "etats.pdf")
        assert doc["status"] == "indexed" and doc["chunks_count"] >= 2
        assert doc["content_type"] == "client_data" and doc["extraction_method"] == "native"
        chunks = api.get(f"/api/knowledge/{doc['doc_id']}/chunks", headers={"X-User-Id": USER_A}).json()["chunks"]
        assert {c["source_page"] for c in chunks} == {1, 2}
        assert any("état des résultats" in c["keywords"] for c in chunks)
        assert "embedding" not in chunks[0]

    def test_search_works_without_any_embedding_provider(self, api):
        """Sans jeton Hugging Face ni Ollama, la recherche par mots-clés doit suffire."""
        doc = _ingest(api, USER_A, STATEMENTS, "etats.pdf")
        found = kb_storage.search_chunks("Quel est le bénéfice net ?", None, USER_A, client_id=doc["client_id"])
        assert found and found[0]["doc_id"] == doc["doc_id"] and found[0]["match"] == "keyword"
        assert found[0]["filename"] == "etats.pdf" and found[0]["source_page"] == 1
        assert "146 094" in found[0]["content"]

    def test_search_is_scoped_to_the_owner(self, api):
        doc = _ingest(api, USER_A, STATEMENTS, "etats.pdf")
        assert kb_storage.search_chunks("bénéfice net", None, USER_A, client_id=doc["client_id"])
        # Même en connaissant l'identifiant du dossier, un autre utilisateur n'obtient rien.
        assert kb_storage.search_chunks("bénéfice net", None, USER_B, client_id=doc["client_id"]) == []
        assert kb_storage.search_chunks("bénéfice net", None, None, client_id=doc["client_id"]) == []

    def test_semantic_match_ranks_first_when_embeddings_exist(self, api, monkeypatch):
        vectors = {"bilan": [1.0, 0.0], "resultats": [0.0, 1.0]}

        async def fake_embedding(text, sensibilite="professionnel"):
            return vectors["bilan"] if "Bilan" in text else vectors["resultats"]

        monkeypatch.setattr(kb_ingest, "generate_embedding", fake_embedding)
        doc = _ingest(api, USER_A, STATEMENTS, "etats.pdf")
        found = kb_storage.search_chunks("", vectors["bilan"], USER_A, client_id=doc["client_id"])
        assert found[0]["match"] == "semantic" and found[0]["source_page"] == 2

    def test_content_type_is_validated_and_filterable(self, api):
        guide = _ingest(api, USER_A, STATEMENTS, "guide_arc.pdf", content_type="regulatory_reference")
        client_doc = _ingest(api, USER_A, STATEMENTS, "dossier.pdf", content_type="n'importe quoi")
        assert guide["content_type"] == "regulatory_reference" and client_doc["content_type"] == "client_data"
        listed = api.get("/api/knowledge/list?content_type=regulatory_reference", headers={"X-User-Id": USER_A}).json()
        assert [d["doc_id"] for d in listed["documents"]] == [guide["doc_id"]]
        assert api.get("/api/knowledge/list?content_type=faux", headers={"X-User-Id": USER_A}).status_code == 400
        only_reference = kb_storage.search_chunks(
            "bénéfice net", None, USER_A, content_types=["regulatory_reference"], client_id=client_doc["client_id"])
        assert {c["doc_id"] for c in only_reference} == {guide["doc_id"]}

    def test_reindex_and_delete_cascade(self, api):
        doc = _ingest(api, USER_A, STATEMENTS, "etats.pdf")
        headers = {"X-User-Id": USER_A}
        before = len(kb_storage.list_chunks(doc["doc_id"], USER_A))
        result = api.post(f"/api/knowledge/{doc['doc_id']}/reindex", headers=headers).json()
        assert result["status"] == "reindexed" and result["chunks"] == before
        assert len(kb_storage.list_chunks(doc["doc_id"], USER_A)) == before  # remplacés, pas doublés
        assert api.post(f"/api/knowledge/{doc['doc_id']}/reindex", headers={"X-User-Id": USER_B}).status_code == 404
        assert api.delete(f"/api/knowledge/{doc['doc_id']}", headers=headers).status_code == 200
        assert kb_storage.search_chunks("bénéfice net", None, USER_A, client_id=doc["client_id"]) == []

    def test_reindex_all_reports_missing_files(self, api):
        doc = _ingest(api, USER_A, STATEMENTS, "etats.pdf")
        Path(kb_storage.get_kb_doc(doc["doc_id"], USER_A)["filepath"]).unlink()
        report = api.post("/api/knowledge/reindex-all", headers={"X-User-Id": USER_A}).json()
        assert report["count"] == 1 and report["results"][0]["status"] == "skipped"
        assert kb_storage.list_chunks(doc["doc_id"], USER_A)  # l'index existant est conservé

    @needs_tesseract
    def test_scanned_document_is_indexed_through_ocr(self, api, monkeypatch):
        monkeypatch.setattr(mc, "ocrmypdf_available", lambda: False)
        doc = _ingest(api, USER_A, SCAN, "bilan_scan.pdf")
        assert doc["extraction_method"] == "ocr"
        found = kb_storage.search_chunks("total de l'actif", None, USER_A, client_id=doc["client_id"])
        assert found and found[0]["extraction_method"] == "ocr"


# ─── Images de documents ─────────────────────────────────────────────────────

class TestDocumentImages:
    def test_image_is_saved_referenced_and_private(self, api, tmp_path):
        doc = _ingest(api, USER_A, REPORT, "rapport.pdf")
        headers = {"X-User-Id": USER_A}
        images = api.get(f"/api/knowledge/{doc['doc_id']}/images", headers=headers).json()["images"]
        assert len(images) == 1
        image = images[0]
        assert image["page_or_slide"] == 1 and image["width"] == 900 and len(image["image_hash"]) == 64
        assert image["verification_status"] == "unverified" and "storage_path" not in image
        # Le fichier est servi à son propriétaire, et à lui seul.
        served = api.get(image["url"], headers=headers)
        assert served.status_code == 200 and served.headers["content-type"] == "image/png"
        assert api.get(image["url"], headers={"X-User-Id": USER_B}).status_code == 404
        stored = kb_storage.list_images(doc["doc_id"], USER_A)[0]["storage_path"]
        assert (tmp_path / "uploads") in Path(stored).parents

    @needs_tesseract
    def test_image_text_and_keywords_are_searchable(self, api):
        doc = _ingest(api, USER_A, REPORT, "rapport.pdf")
        image = kb_storage.list_images(doc["doc_id"], USER_A)[0]
        assert "Ventes trimestrielles" in image["extracted_text"]
        assert {"TPS", "TVQ"} <= set(image["keywords"])
        assert image["extraction_model"] == "tesseract" and image["extracted_data"] is None
        # Le texte de l'image est aussi un segment, relié à l'image.
        chunks = kb_storage.list_chunks(doc["doc_id"], USER_A)
        linked = [c for c in chunks if c["chunk_id"] == image["chunk_id"]]
        assert linked and linked[0]["extraction_method"] == "ocr" and linked[0]["source_page"] == 1
        found = kb_storage.search_chunks("ventes trimestrielles", None, USER_A, client_id=doc["client_id"])
        assert found[0]["chunk_id"] == image["chunk_id"]

    def test_deleting_the_document_removes_image_files(self, api):
        doc = _ingest(api, USER_A, REPORT, "rapport.pdf")
        path = Path(kb_storage.list_images(doc["doc_id"], USER_A)[0]["storage_path"])
        assert path.is_file()
        assert api.delete(f"/api/knowledge/{doc['doc_id']}", headers={"X-User-Id": USER_A}).status_code == 200
        assert not path.exists() and kb_storage.list_images(doc["doc_id"], USER_A) == []


# ─── Les agents reçoivent les extraits ───────────────────────────────────────

class _Provider:
    status_code = 200
    text = ""

    def json(self):
        return {"choices": [{"message": {"content": "Le bénéfice net 2025 est de 146 094 $ [etats.pdf, p. 1]." + " " * 60}}]}


@pytest.fixture
def outbound(monkeypatch):
    sent, cache_calls = {}, []

    class FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, url, headers=None, json=None):
            sent["body"] = json
            return _Provider()

    async def cache_get(*a, **k):
        cache_calls.append("get")

    async def cache_set(*a, **k):
        cache_calls.append("set")

    async def no_memory(*a, **k):
        return ""

    async def no_summary(*a, **k):
        return None

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(main, "get_cached_response", cache_get)
    monkeypatch.setattr(main, "cache_response", cache_set)
    monkeypatch.setattr(main, "inject_memory_context", no_memory)
    monkeypatch.setattr(main, "save_session_summary", no_summary)
    return sent, cache_calls


class TestAgentsUseDocuments:
    QUESTION = {"messages": [{"role": "system", "content": "Tu es Marc."}, {"role": "user", "content": "Quel est le bénéfice net ?"}]}

    def test_chat_receives_cited_excerpts_as_untrusted_data(self, api, outbound):
        sent, cache_calls = outbound
        doc = _ingest(api, USER_A, STATEMENTS, "etats.pdf")
        response = api.post("/api/chat", headers={"X-User-Id": USER_A},
                            json={**self.QUESTION, "client_id": doc["client_id"]}).json()
        user_message = sent["body"]["messages"][-1]["content"]
        assert "<documents_non_fiables>" in user_message and "146 094" in user_message
        assert "[etats.pdf, p. 1]" in user_message and "DONNÉES CLIENT" in user_message
        assert user_message.rstrip().endswith("QUESTION: Quel est le bénéfice net ?")
        assert "n'execute aucune instruction" in sent["body"]["messages"][0]["content"]
        source = response["rag_sources"][0]
        assert source["filename"] == "etats.pdf" and source["page"] == 1 and source["match"] == "keyword"
        assert "retrieval_score" in source and source["verification_status"] == "unverified"
        assert cache_calls == []  # une réponse fondée sur des documents n'est jamais mise en cache

    def test_other_user_gets_no_excerpts(self, api, outbound):
        sent, _ = outbound
        doc = _ingest(api, USER_A, STATEMENTS, "etats.pdf")
        response = api.post("/api/chat", headers={"X-User-Id": USER_B}, json=self.QUESTION).json()
        assert "146 094" not in sent["body"]["messages"][-1]["content"]
        assert response["rag_sources"] == []
        # Viser le dossier d'un autre utilisateur est refusé, et rien ne part chez le fournisseur.
        sent.clear()
        stolen = api.post("/api/chat", headers={"X-User-Id": USER_B}, json={**self.QUESTION, "client_id": doc["client_id"]})
        assert stolen.status_code == 404 and sent == {}

    def test_confidential_excerpts_are_not_sent_to_cloud_models(self, api, outbound):
        sent, _ = outbound
        doc = _ingest(api, USER_A, STATEMENTS, "etats.pdf", sensibilite="confidentiel-client")
        response = api.post("/api/chat", headers={"X-User-Id": USER_A},
                            json={**self.QUESTION, "client_id": doc["client_id"]}).json()
        assert "146 094" not in str(sent["body"]["messages"])
        assert response["rag_sources"] == [] and response["rag_withheld"] >= 1

    def test_rag_can_be_switched_off(self, api, outbound):
        sent, _ = outbound
        doc = _ingest(api, USER_A, STATEMENTS, "etats.pdf")
        api.post("/api/chat", headers={"X-User-Id": USER_A},
                 json={**self.QUESTION, "use_rag": False, "client_id": doc["client_id"]})
        assert "documents_non_fiables" not in str(sent["body"]["messages"])

    async def test_reference_and_client_data_are_labelled_apart(self, api):
        from services.retrieval import retrieve

        _ingest(api, USER_A, STATEMENTS, "guide_ratios.pdf", content_type="example_methodology")
        doc = _ingest(api, USER_A, STATEMENTS, "dossier_boreal.pdf")
        found = await retrieve("bénéfice net", USER_A, client_id=doc["client_id"])
        context = found["context"]
        assert "EXEMPLE DE MÉTHODE (chiffres fictifs)" in context and "DONNÉES CLIENT" in context
        assert context.index("DONNÉES CLIENT") < context.index("EXEMPLE DE MÉTHODE")  # le dossier d'abord
        assert {s["content_type"] for s in found["sources"]} == {"client_data", "example_methodology"}


# ─── RAG métier / RAG client : dossiers de clients et sous-dossiers de projets ───

class TestMetierAndClientRag:
    H = {"X-User-Id": USER_A}

    def _tree(self, api, user=USER_A):
        return api.get("/api/clients", headers={"X-User-Id": user}).json()

    def test_client_folder_with_project_subfolders(self, api):
        boreal = api.post("/api/clients", headers=self.H, json={"name": "  Les Entreprises   Boréal "}).json()
        assert boreal["name"] == "Les Entreprises Boréal"
        audit = api.post(f"/api/clients/{boreal['client_id']}/projects", headers=self.H, json={"name": "Audit 2025"}).json()
        api.post(f"/api/clients/{boreal['client_id']}/projects", headers=self.H, json={"name": "Acquisition Nordik"})
        tree = self._tree(api)
        assert [c["name"] for c in tree["clients"]] == ["Les Entreprises Boréal"]
        assert [p["name"] for p in tree["clients"][0]["projects"]] == ["Acquisition Nordik", "Audit 2025"]
        assert audit["client_id"] == boreal["client_id"]
        # Un autre utilisateur ne voit rien et ne peut rien y créer.
        assert self._tree(api, USER_B)["clients"] == []
        refused = api.post(f"/api/clients/{boreal['client_id']}/projects", headers={"X-User-Id": USER_B}, json={"name": "x"})
        assert refused.status_code == 400

    def test_folder_rules(self, api):
        client_id = api.post("/api/clients", headers=self.H, json={"name": "Boréal"}).json()["client_id"]
        project_id = api.post(f"/api/clients/{client_id}/projects", headers=self.H, json={"name": "Audit 2025"}).json()["project_id"]
        assert api.post("/api/clients", headers=self.H, json={"name": "boréal"}).status_code == 400            # doublon
        assert api.post("/api/clients", headers=self.H, json={"name": "   "}).status_code == 400               # nom vide
        assert api.post(f"/api/clients/{project_id}/projects", headers=self.H, json={"name": "x"}).status_code == 400  # pas de sous-sous-dossier
        assert api.post(f"/api/clients/{client_id}/projects", headers=self.H, json={"name": "AUDIT 2025"}).status_code == 400
        # Le même nom de projet reste permis chez un autre client.
        other = api.post("/api/clients", headers=self.H, json={"name": "Nordik"}).json()["client_id"]
        assert api.post(f"/api/clients/{other}/projects", headers=self.H, json={"name": "Audit 2025"}).status_code == 201
        renamed = api.patch(f"/api/folders/{project_id}", headers=self.H, json={"name": "Audit 2025-2026"})
        assert renamed.json() == {"folder_id": project_id, "name": "Audit 2025-2026", "kind": "project"}
        assert api.patch(f"/api/folders/{project_id}", headers={"X-User-Id": USER_B}, json={"name": "x"}).status_code == 404

    def test_client_document_requires_a_client_folder(self, api):
        import json

        def post(meta):
            return api.post("/api/knowledge/ingest", headers=self.H,
                            files=[("files", ("etats.pdf", io.BytesIO(STATEMENTS), "application/pdf"))],
                            data={"bulk_metadata": json.dumps(meta)})

        assert post({"content_type": "client_data"}).status_code == 400
        assert post({"content_type": "client_data", "client_id": "inconnu"}).status_code == 400
        boreal = _client_folder(api, USER_A, "Boréal")
        nordik = _client_folder(api, USER_A, "Nordik")
        nordik_project = api.post(f"/api/clients/{nordik}/projects", headers=self.H, json={"name": "Audit"}).json()["project_id"]
        # Un projet d'un autre client est refusé.
        assert post({"client_id": boreal, "project_id": nordik_project}).status_code == 400
        # Le dossier d'un autre utilisateur aussi.
        stolen = api.post("/api/knowledge/ingest", headers={"X-User-Id": USER_B},
                          files=[("files", ("x.pdf", io.BytesIO(STATEMENTS), "application/pdf"))],
                          data={"bulk_metadata": json.dumps({"client_id": boreal})})
        assert stolen.status_code == 400
        assert kb_storage.list_kb_docs(owner_id=USER_A) == [] and kb_storage.list_kb_docs(owner_id=USER_B) == []
        assert not list((security_limits.UPLOAD_ROOT).rglob("*.pdf"))  # aucun fichier laissé derrière

    def test_metier_document_is_never_attached_to_a_client(self, api):
        boreal = _client_folder(api, USER_A, "Boréal")
        guide = _ingest(api, USER_A, STATEMENTS, "guide_arc.pdf", content_type="regulatory_reference", client_id=boreal)
        assert guide["client_id"] is None and guide["project_id"] is None
        listed = api.get("/api/knowledge/list?scope=metier", headers=self.H).json()["documents"]
        assert [d["doc_id"] for d in listed] == [guide["doc_id"]] and listed[0]["scope"] == "metier"
        assert api.get("/api/knowledge/list?scope=client", headers=self.H).json()["documents"] == []
        assert api.get("/api/knowledge/list?scope=tout", headers=self.H).status_code == 400

    def test_client_data_never_crosses_client_folders(self, api):
        """Le cœur de la règle : les chiffres de Boréal ne sortent pas dans le dossier Nordik."""
        boreal, nordik = _client_folder(api, USER_A, "Boréal"), _client_folder(api, USER_A, "Nordik")
        guide = _ingest(api, USER_A, STATEMENTS, "guide_ratios.pdf", content_type="methodology")
        boreal_doc = _ingest(api, USER_A, STATEMENTS, "etats_boreal.pdf", client_id=boreal)
        nordik_doc = _ingest(api, USER_A, STATEMENTS, "etats_nordik.pdf", client_id=nordik)

        def docs(**scope):
            return {c["doc_id"] for c in kb_storage.search_chunks("bénéfice net", None, USER_A, limit=20, **scope)}

        assert docs() == {guide["doc_id"]}                                         # sans dossier actif : métier seul
        assert docs(client_id=boreal) == {guide["doc_id"], boreal_doc["doc_id"]}   # métier + ce client
        assert docs(client_id=nordik) == {guide["doc_id"], nordik_doc["doc_id"]}
        assert docs(client_id=boreal, scope="client") == {boreal_doc["doc_id"]}
        assert docs(client_id=boreal, scope="metier") == {guide["doc_id"]}

    def test_project_scope_includes_client_level_documents(self, api):
        boreal = _client_folder(api, USER_A, "Boréal")
        audit = api.post(f"/api/clients/{boreal}/projects", headers=self.H, json={"name": "Audit 2025"}).json()["project_id"]
        acquisition = api.post(f"/api/clients/{boreal}/projects", headers=self.H, json={"name": "Acquisition"}).json()["project_id"]
        general = _ingest(api, USER_A, STATEMENTS, "statuts.pdf", client_id=boreal)
        audit_doc = _ingest(api, USER_A, STATEMENTS, "balance_audit.pdf", client_id=boreal, project_id=audit)
        acq_doc = _ingest(api, USER_A, STATEMENTS, "lettre_intention.pdf", client_id=boreal, project_id=acquisition)

        def docs(**scope):
            return {c["doc_id"] for c in kb_storage.search_chunks("bénéfice net", None, USER_A, limit=20, **scope)}

        assert docs(client_id=boreal) == {general["doc_id"], audit_doc["doc_id"], acq_doc["doc_id"]}
        assert docs(client_id=boreal, project_id=audit) == {general["doc_id"], audit_doc["doc_id"]}
        tree = self._tree(api)["clients"][0]
        assert tree["documents"] == 3
        assert {p["name"]: p["documents"] for p in tree["projects"]} == {"Acquisition": 1, "Audit 2025": 1}
        by_project = api.get(f"/api/knowledge/list?project_id={audit}", headers=self.H).json()["documents"]
        assert [d["doc_id"] for d in by_project] == [audit_doc["doc_id"]]

    def test_moving_a_document_moves_its_segments(self, api):
        boreal, nordik = _client_folder(api, USER_A, "Boréal"), _client_folder(api, USER_A, "Nordik")
        project = api.post(f"/api/clients/{nordik}/projects", headers=self.H, json={"name": "Audit"}).json()["project_id"]
        doc = _ingest(api, USER_A, STATEMENTS, "etats.pdf", client_id=boreal)
        moved = api.patch(f"/api/knowledge/{doc['doc_id']}", headers=self.H, json={"client_id": nordik, "project_id": project})
        assert moved.status_code == 200 and moved.json()["client_id"] == nordik and moved.json()["project_id"] == project
        assert kb_storage.search_chunks("bénéfice net", None, USER_A, client_id=boreal) == []
        assert kb_storage.search_chunks("bénéfice net", None, USER_A, client_id=nordik, project_id=project)
        # Changer de client sans préciser de projet remet le document à la racine du nouveau dossier.
        back = api.patch(f"/api/knowledge/{doc['doc_id']}", headers=self.H, json={"client_id": boreal}).json()
        assert back["client_id"] == boreal and back["project_id"] is None
        # Reclasser en source métier le détache de tout client.
        metier = api.patch(f"/api/knowledge/{doc['doc_id']}", headers=self.H, json={"content_type": "methodology"}).json()
        assert metier["client_id"] is None
        assert kb_storage.search_chunks("bénéfice net", None, USER_A)[0]["content_type"] == "methodology"
        # Et un emplacement invalide est refusé.
        assert api.patch(f"/api/knowledge/{doc['doc_id']}", headers=self.H, json={"content_type": "client_data"}).status_code == 400

    def test_folder_with_content_cannot_be_deleted(self, api):
        boreal = _client_folder(api, USER_A, "Boréal")
        project = api.post(f"/api/clients/{boreal}/projects", headers=self.H, json={"name": "Audit"}).json()["project_id"]
        doc = _ingest(api, USER_A, STATEMENTS, "etats.pdf", client_id=boreal, project_id=project)
        assert api.delete(f"/api/folders/{project}", headers=self.H).status_code == 409   # contient un document
        assert api.delete(f"/api/folders/{boreal}", headers=self.H).status_code == 409    # contient un document et un projet
        assert api.delete(f"/api/folders/{boreal}", headers={"X-User-Id": USER_B}).status_code == 404
        api.delete(f"/api/knowledge/{doc['doc_id']}", headers=self.H)
        assert api.delete(f"/api/folders/{boreal}", headers=self.H).status_code == 409    # reste le projet
        assert api.delete(f"/api/folders/{project}", headers=self.H).status_code == 200
        assert api.delete(f"/api/folders/{boreal}", headers=self.H).status_code == 200
        assert self._tree(api)["clients"] == []

    def test_agents_only_see_the_active_client(self, api, outbound):
        sent, _ = outbound
        boreal, nordik = _client_folder(api, USER_A, "Boréal"), _client_folder(api, USER_A, "Nordik")
        _ingest(api, USER_A, STATEMENTS, "guide_ratios.pdf", content_type="methodology")
        _ingest(api, USER_A, STATEMENTS, "etats_boreal.pdf", client_id=boreal)
        question = TestAgentsUseDocuments.QUESTION

        no_folder = api.post("/api/chat", headers=self.H, json=question).json()
        assert {s["scope"] for s in no_folder["rag_sources"]} == {"metier"}
        assert "etats_boreal.pdf" not in sent["body"]["messages"][-1]["content"]

        in_boreal = api.post("/api/chat", headers=self.H, json={**question, "client_id": boreal}).json()
        assert {s["filename"] for s in in_boreal["rag_sources"]} == {"guide_ratios.pdf", "etats_boreal.pdf"}
        assert {s["scope"] for s in in_boreal["rag_sources"]} == {"metier", "client"}

        in_nordik = api.post("/api/chat", headers=self.H, json={**question, "client_id": nordik}).json()
        assert {s["filename"] for s in in_nordik["rag_sources"]} == {"guide_ratios.pdf"}
        assert "etats_boreal.pdf" not in sent["body"]["messages"][-1]["content"]

    def test_unfiled_legacy_client_documents_are_listed_for_filing(self, api):
        """Documents client antérieurs aux dossiers : visibles dans « À classer », jamais servis aux agents."""
        kb_storage.insert_kb_doc({"filename": "ancien.pdf", "owner_id": USER_A, "content_type": "client_data"})
        tree = self._tree(api)
        assert tree["unfiled"] == 1 and tree["clients"] == []
        unfiled = api.get("/api/knowledge/list?unfiled=true", headers=self.H).json()["documents"]
        assert [d["filename"] for d in unfiled] == ["ancien.pdf"]
        boreal = _client_folder(api, USER_A, "Boréal")
        filed = api.patch(f"/api/knowledge/{unfiled[0]['doc_id']}", headers=self.H, json={"client_id": boreal})
        assert filed.status_code == 200 and self._tree(api)["unfiled"] == 0

    def test_account_erasure_removes_folders(self, api):
        boreal = _client_folder(api, USER_A, "Boréal")
        api.post(f"/api/clients/{boreal}/projects", headers=self.H, json={"name": "Audit"})
        _client_folder(api, USER_B, "Client de B")
        kb_storage.delete_owner_data(USER_A)
        assert self._tree(api)["clients"] == [] and len(self._tree(api, USER_B)["clients"]) == 1


# ─── RAG métier réservé à l'administrateur, partagé avec tous sans être montré ───

class TestMetierIsAdminOnly:
    ADMIN, USER = {"X-User-Id": USER_A}, {"X-User-Id": USER_B}
    QUESTION = {"model": "anthropic/claude-sonnet-4.5", "messages": [
        {"role": "system", "content": "Tu es un CPA."}, {"role": "user", "content": "Quel est le bénéfice net ?"}]}

    def _post(self, api, headers, meta, name="guide.pdf"):
        import json

        return api.post("/api/knowledge/ingest", headers=headers,
                        files=[("files", (name, io.BytesIO(STATEMENTS), "application/pdf"))],
                        data={"bulk_metadata": json.dumps(meta)})

    def test_user_cannot_add_a_knowledge_source(self, api):
        for content_type in ("methodology", "regulatory_reference", "policy", "internal_knowledge", "example_methodology"):
            assert self._post(api, self.USER, {"content_type": content_type}).status_code == 403
        # Y compris par la surcharge propre à un fichier.
        import json

        sneaky = api.post("/api/knowledge/ingest", headers=self.USER,
                          files=[("files", ("guide.pdf", io.BytesIO(STATEMENTS), "application/pdf"))],
                          data={"bulk_metadata": json.dumps({"client_id": _client_folder(api, USER_B)}),
                                "per_file_refinements": json.dumps({"guide.pdf": {"content_type": "methodology"}})})
        assert sneaky.status_code == 403
        assert kb_storage.list_kb_docs(owner_id=USER_B) == []
        assert self._post(api, self.ADMIN, {"content_type": "methodology"}).status_code == 200

    def test_user_cannot_reclassify_a_document_as_knowledge(self, api):
        doc = _ingest(api, USER_B, STATEMENTS, "etats.pdf")
        refused = api.patch(f"/api/knowledge/{doc['doc_id']}", headers=self.USER, json={"content_type": "methodology"})
        assert refused.status_code == 403
        assert kb_storage.get_kb_doc(doc["doc_id"], USER_B)["content_type"] == "client_data"

    def test_user_never_sees_knowledge_sources(self, api):
        guide = _ingest(api, USER_A, STATEMENTS, "guide_interne.pdf", content_type="methodology")
        mine = _ingest(api, USER_B, STATEMENTS, "etats.pdf")
        listed = api.get("/api/knowledge/list", headers=self.USER).json()["documents"]
        assert [d["doc_id"] for d in listed] == [mine["doc_id"]]
        assert api.get("/api/knowledge/list?scope=metier", headers=self.USER).status_code == 403
        for path in ("", "/chunks", "/images"):
            assert api.get(f"/api/knowledge/{guide['doc_id']}{path}", headers=self.USER).status_code == 404
        assert api.delete(f"/api/knowledge/{guide['doc_id']}", headers=self.USER).status_code == 404
        assert api.patch(f"/api/knowledge/{guide['doc_id']}", headers=self.USER, json={"tags": ["x"]}).status_code == 404
        assert api.get("/api/knowledge/content-types", headers=self.USER).json()["content_types"] == ["client_data"]
        assert "methodology" in api.get("/api/knowledge/content-types", headers=self.ADMIN).json()["content_types"]
        # Un ancien document métier d'un utilisateur ordinaire n'est ni listé ni servi.
        kb_storage.insert_kb_doc({"filename": "vieux_guide.pdf", "owner_id": USER_B, "content_type": "methodology"})
        assert len(api.get("/api/knowledge/list", headers=self.USER).json()["documents"]) == 1

    def test_agents_use_admin_knowledge_for_every_user_without_showing_it(self, api, outbound):
        sent, _ = outbound
        _ingest(api, USER_A, STATEMENTS, "guide_interne_confidentiel.pdf", content_type="methodology")
        answer = api.post("/api/chat", headers=self.USER, json=self.QUESTION).json()
        prompt = sent["body"]["messages"][-1]["content"]
        assert "146 094" in prompt                                   # le savoir métier sert bien à l'agent
        assert "guide_interne_confidentiel" not in prompt            # mais son nom n'est jamais transmis
        assert "Base de connaissances Z12" in prompt
        assert answer["rag_sources"] == []                           # et rien n'est montré à l'utilisateur
        assert "guide_interne_confidentiel" not in str(answer)

        as_admin = api.post("/api/chat", headers=self.ADMIN, json=self.QUESTION).json()
        assert {s["filename"] for s in as_admin["rag_sources"]} == {"guide_interne_confidentiel.pdf"}

    def test_user_sees_only_their_client_sources(self, api, outbound):
        _ingest(api, USER_A, STATEMENTS, "guide.pdf", content_type="methodology")
        mine = _ingest(api, USER_B, STATEMENTS, "etats_client.pdf")
        answer = api.post("/api/chat", headers=self.USER, json={**self.QUESTION, "client_id": mine["client_id"]}).json()
        assert {s["filename"] for s in answer["rag_sources"]} == {"etats_client.pdf"}

    def test_admin_client_data_is_never_shared(self, api):
        """Seul le RAG métier de l'administrateur est commun ; ses dossiers client restent privés."""
        admin_doc = _ingest(api, USER_A, STATEMENTS, "client_de_l_admin.pdf")
        found = kb_storage.search_chunks("bénéfice net", None, USER_B, client_id=admin_doc["client_id"],
                                         metier_owner_ids=[USER_A])
        assert found == []

    def test_without_admin_no_knowledge_is_served(self, api, outbound, monkeypatch):
        sent, _ = outbound
        _ingest(api, USER_A, STATEMENTS, "guide.pdf", content_type="methodology")
        monkeypatch.delenv("ADMIN_USER_IDS")
        answer = api.post("/api/chat", headers=self.ADMIN, json=self.QUESTION).json()
        assert answer["rag_sources"] == [] and "146 094" not in sent["body"]["messages"][-1]["content"]
        assert self._post(api, self.ADMIN, {"content_type": "methodology"}).status_code == 403

    def test_admin_ids_parsing(self, monkeypatch):
        monkeypatch.setenv("ADMIN_USER_IDS", " 9 , ,12 ")
        assert security_limits.admin_ids() == {"9", "12"}
        assert security_limits.is_admin("9") and not security_limits.is_admin("1") and not security_limits.is_admin(None)
        monkeypatch.setenv("ADMIN_USER_IDS", "")
        assert not security_limits.is_admin("9") and not security_limits.is_admin("")


# ─── Indexation par description et mots-clés (documents et images) ───────────

class TestDescriptionsAndKeywords:
    ADMIN, USER = {"X-User-Id": USER_A}, {"X-User-Id": USER_B}

    def test_keywords_come_from_the_content_not_only_the_lexicon(self):
        from services.chunker import extract_keywords

        text = ("La méthode Zébulon répartit les frais généraux. La méthode Zébulon s'applique aux ateliers. "
                "Les ateliers déclarent la TPS et la TVQ chaque trimestre.")
        keywords = extract_keywords(text)
        assert "TPS" in keywords and "TVQ" in keywords                 # lexique comptable et fiscal
        assert "zébulon" in keywords and "ateliers" in keywords        # termes propres au document
        assert "avec" not in keywords and "pour" not in keywords
        assert len(extract_keywords(text * 50, limit=5)) == 5
        assert extract_keywords("") == []

    def test_document_description_is_extracted_locally(self):
        from services.chunker import MAX_DESCRIPTION_CHARS, describe_document

        markdown = ("# Guide des ratios financiers\n\n## Liquidité\n\n"
                    "Ce guide présente les ratios utilisés pour évaluer la santé financière d'une PME québécoise.\n\n"
                    "| Ratio | Formule |\n|---|---|\n| Liquidité | Actif / Passif |\n")
        description = describe_document(markdown, "guide.pdf")
        assert description.startswith("Guide des ratios financiers — Liquidité")
        assert "santé financière" in description and "|" not in description
        assert describe_document("", "politique_de_credit.pdf") == "politique de credit"
        assert len(describe_document("# T\n\n" + "Phrase assez longue pour compter comme du texte. " * 40)) <= MAX_DESCRIPTION_CHARS + 1

    def test_ingestion_fills_description_keywords_and_segment_index(self, api):
        doc = _ingest(api, USER_A, STATEMENTS, "etats.pdf")
        stored = kb_storage.get_kb_doc(doc["doc_id"], USER_A)
        assert stored["description"] and stored["keywords"]
        assert doc["description"] == stored["description"]
        listed = api.get("/api/knowledge/list", headers=self.ADMIN).json()["documents"][0]
        assert listed["description"] == stored["description"] and listed["keywords"] == stored["keywords"]
        chunks = kb_storage.list_chunks(doc["doc_id"], USER_A)
        assert all(c.get("doc_terms") for c in chunks)
        assert any(c.get("search_meta") for c in chunks)

    def test_admin_keyword_makes_a_document_findable(self, api):
        """Un mot absent du texte, ajouté à la main, suffit à retrouver le document."""
        doc = _ingest(api, USER_A, STATEMENTS, "guide.pdf", content_type="methodology")
        assert kb_storage.search_chunks("zébulon", None, USER_A) == []
        patched = api.patch(f"/api/knowledge/{doc['doc_id']}", headers=self.ADMIN, json={
            "keywords": ["  Zébulon ", "zébulon", "", "coût de revient"],
            "description": "  Méthode   interne de répartition des frais généraux.  "})
        assert patched.status_code == 200
        assert patched.json()["keywords"] == ["Zébulon", "coût de revient"]
        assert patched.json()["description"] == "Méthode interne de répartition des frais généraux."
        assert kb_storage.search_chunks("zébulon", None, USER_A)
        assert kb_storage.search_chunks("répartition des frais généraux", None, USER_A)
        # Servi aussi aux agents des autres utilisateurs, comme le reste du RAG métier.
        assert kb_storage.search_chunks("zébulon", None, USER_B, metier_owner_ids=[USER_A])

    def test_manual_description_survives_reindexing(self, api):
        doc = _ingest(api, USER_A, STATEMENTS, "guide.pdf", content_type="methodology")
        api.patch(f"/api/knowledge/{doc['doc_id']}", headers=self.ADMIN,
                  json={"description": "Ma description.", "keywords": ["zébulon"]})
        assert api.post(f"/api/knowledge/{doc['doc_id']}/reindex", headers=self.ADMIN).status_code == 200
        stored = kb_storage.get_kb_doc(doc["doc_id"], USER_A)
        assert stored["description"] == "Ma description." and stored["keywords"] == ["zébulon"]
        assert kb_storage.search_chunks("zébulon", None, USER_A)

    def test_keyword_and_description_edits_are_validated(self, api):
        doc = _ingest(api, USER_A, STATEMENTS, "guide.pdf", content_type="methodology")
        url = f"/api/knowledge/{doc['doc_id']}"
        assert api.patch(url, headers=self.ADMIN, json={"keywords": "pas une liste"}).status_code == 400
        assert api.patch(url, headers=self.ADMIN, json={"description": ["x"]}).status_code == 400
        long = api.patch(url, headers=self.ADMIN, json={"keywords": [f"mot{i}" for i in range(80)], "description": "x" * 5000}).json()
        assert len(long["keywords"]) == kb_storage.MAX_DOC_KEYWORDS and len(long["description"]) == 600
        assert api.patch(url, headers=self.USER, json={"keywords": ["x"]}).status_code == 404

    def test_segment_keyword_outranks_plain_text(self, api):
        doc = kb_storage.insert_kb_doc({"filename": "g.pdf", "owner_id": USER_A, "content_type": "methodology"})
        base = {"content_type": "methodology", "sensibilite": "professionnel", "extraction_method": "native",
                "verification_status": "unverified", "embedding": None, "total_chunks": 2, "source_page": 1}
        kb_storage.replace_chunks(doc["doc_id"], USER_A, [
            {**base, "chunk_id": "texte", "chunk_index": 0, "content": "On mentionne un amortissement en passant.",
             "heading": None, "keywords": [], "search_meta": ""},
            {**base, "chunk_id": "cle", "chunk_index": 1, "content": "Règles de calcul et taux applicables.",
             "heading": "Amortissement", "keywords": ["amortissement"], "search_meta": "Amortissement amortissement"},
        ])
        assert [c["chunk_id"] for c in kb_storage.search_chunks("amortissement", None, USER_A)] == ["cle", "texte"]


class TestImageDescriptions:
    """Description des images par un modèle de vision : RAG métier seulement."""

    @pytest.fixture
    def vision(self, monkeypatch):
        from services import image_description

        calls = []

        async def describe(path, user_id=None):
            calls.append(str(path))
            return {"description": "Graphique en barres des ventes trimestrielles, en hausse au quatrième trimestre.",
                    "keywords": ["graphique en barres", "tendance"]}

        monkeypatch.setattr(image_description, "enabled", lambda: True)
        monkeypatch.setattr(image_description, "describe_image", describe)
        monkeypatch.setattr(image_description, "vision_model", lambda: "modele/vision-test")
        return calls

    def test_business_knowledge_images_are_described_and_searchable(self, api, vision):
        doc = _ingest(api, USER_A, REPORT, "guide_graphiques.pdf", content_type="methodology")
        images = kb_storage.list_images(doc["doc_id"], USER_A)
        assert vision and len(vision) == len(images) >= 1
        image = images[0]
        assert image["description"].startswith("Graphique en barres")
        assert image["extraction_model"] == "modele/vision-test" and image["prompt_version"] == "v1"
        assert "graphique en barres" in image["keywords"] and image["storage_path"]
        found = kb_storage.search_chunks("tendance graphique en barres", None, USER_A)
        assert found and found[0]["chunk_id"] == image["chunk_id"]
        assert "Description : Graphique en barres" in found[0]["content"]

    def test_client_images_never_leave_the_server(self, api, vision):
        doc = _ingest(api, USER_A, REPORT, "rapport_client.pdf")
        assert vision == []
        images = kb_storage.list_images(doc["doc_id"], USER_A)
        assert images and all(i.get("description") is None and i["extraction_model"] == "tesseract" for i in images)

    def test_confidential_business_documents_are_not_sent_either(self, api, vision):
        _ingest(api, USER_A, REPORT, "guide_confidentiel.pdf", content_type="methodology", sensibilite="confidentiel-client")
        assert vision == []

    def test_disabled_without_a_configured_model(self, api, monkeypatch):
        from services import image_description

        monkeypatch.delenv("IMAGE_DESCRIPTION_MODEL", raising=False)
        assert image_description.enabled() is False
        doc = _ingest(api, USER_A, REPORT, "guide.pdf", content_type="methodology")
        assert all(i.get("description") is None for i in kb_storage.list_images(doc["doc_id"], USER_A))

    def test_a_failed_description_keeps_the_image_indexed_by_its_text(self, api, monkeypatch):
        from services import image_description

        async def failing(path, user_id=None):
            return None

        monkeypatch.setattr(image_description, "enabled", lambda: True)
        monkeypatch.setattr(image_description, "describe_image", failing)
        doc = _ingest(api, USER_A, REPORT, "guide.pdf", content_type="methodology")
        image = kb_storage.list_images(doc["doc_id"], USER_A)[0]
        assert image["description"] is None and image["extraction_method"] == "ocr" and image["chunk_id"]

    def test_per_document_cap(self, api, vision, monkeypatch):
        monkeypatch.setenv("IMAGE_DESCRIPTION_MAX_PER_DOC", "0")
        _ingest(api, USER_A, REPORT, "guide.pdf", content_type="methodology")
        assert vision == []

    def test_model_answer_is_parsed_defensively(self):
        from services.image_description import MAX_DESCRIPTION_CHARS, MAX_IMAGE_KEYWORDS, parse_answer

        good = parse_answer('Voici : ```json\n{"description": " Un   organigramme. ", "mots_cles": ["Organigramme", "organigramme", 12, ""]}\n```')
        assert good == {"description": "Un organigramme.", "keywords": ["Organigramme", "12"]}
        assert parse_answer("") is None and parse_answer("aucun JSON") is None and parse_answer("{cassé") is None
        assert parse_answer('["liste"]') is None and parse_answer('{"mots_cles": ["x"]}') is None
        assert parse_answer('{"description": "x", "mots_cles": "pas une liste"}') == {"description": "x", "keywords": []}
        big = parse_answer('{"description": "' + "a" * 5000 + '", "mots_cles": ' + str([f"m{i}" for i in range(50)]).replace("'", '"') + "}")
        assert len(big["description"]) == MAX_DESCRIPTION_CHARS and len(big["keywords"]) == MAX_IMAGE_KEYWORDS

    @pytest.mark.asyncio
    async def test_describe_image_never_raises(self, monkeypatch, tmp_path):
        from services import image_description

        assert await image_description.describe_image(tmp_path / "absent.png") is None   # désactivé
        monkeypatch.setenv("IMAGE_DESCRIPTION_MODEL", "modele/vision")
        monkeypatch.setenv("OPENROUTER_API_KEY", "cle-de-test")
        assert await image_description.describe_image(tmp_path / "absent.png") is None   # fichier absent
        big = tmp_path / "gros.png"
        big.write_bytes(b"0" * (image_description.MAX_IMAGE_BYTES + 1))
        assert await image_description.describe_image(big) is None                        # trop lourd

        class Boom:
            def __init__(self, *a, **k): ...
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): raise image_description.httpx.ConnectError("réseau coupé")

        small = tmp_path / "petit.png"
        small.write_bytes(b"png")
        monkeypatch.setattr(image_description.httpx, "AsyncClient", Boom)
        assert await image_description.describe_image(small) is None                      # panne réseau


class TestProtectedPdfIngestion:
    H = {"X-User-Id": USER_A}

    def test_aes_protected_pdf_is_indexed(self, api):
        doc = _ingest(api, USER_A, (FIXTURES / "etats_proteges_aes.pdf").read_bytes(), "etats_proteges_aes.pdf")
        assert doc["status"] == "indexed" and doc["chunks_count"] >= 1
        assert kb_storage.search_chunks("bénéfice net", None, USER_A, client_id=doc["client_id"])

    def test_password_locked_pdf_says_what_to_do(self, api):
        doc = _ingest(api, USER_A, (FIXTURES / "etats_mot_de_passe.pdf").read_bytes(), "etats_mot_de_passe.pdf")
        assert doc["status"] == "error" and doc["chunks_count"] == 0
        assert "mot de passe" in doc["status_message"]

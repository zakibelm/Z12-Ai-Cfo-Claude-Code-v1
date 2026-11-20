"""Gros PDF (manuels de plus de 500 pages) : tout le texte est lu, et le statut dit la vérité."""
from __future__ import annotations

import io
import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

import kb_ingest
import kb_storage
import main
import security_limits
from services import image_extraction
from services import markdown_converter as mc

FIXTURES = Path(__file__).parent.parent / "fixtures"
ADMIN = "11111111-1111-1111-1111-111111111111"
H = {"X-User-Id": ADMIN}
METIER = {"bulk_metadata": json.dumps({"content_type": "internal_knowledge"})}

needs_pdftotext = pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler-utils non installé")
needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None or shutil.which("pdftoppm") is None,
                                     reason="Tesseract ou poppler non installé")


def _repeat(fixture: str, pages: int) -> bytes:
    """PDF de `pages` pages, fait des pages d'une fixture répétées."""
    source = PdfReader(str(FIXTURES / fixture)).pages
    writer = PdfWriter()
    for i in range(pages):
        writer.add_page(source[i % len(source)])
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def manual() -> bytes:
    return _repeat("etats_financiers.pdf", 620)


@pytest.fixture(scope="module")
def scanned_manual() -> bytes:
    return _repeat("bilan_numerise.pdf", 510)


@needs_pdftotext
class TestLargeTextPdf:
    def test_every_page_of_a_620_page_manual_is_read(self, manual):
        started = time.time()
        result = mc.convert_to_markdown(manual, "manuel.pdf")
        assert result.total_pages == 620 and result.pages_read == 620 and not result.partial
        assert result.methods == {"native"} and result.warnings == []
        assert [p.page for p in result.pages] == list(range(1, 621))          # chaque extrait garde sa page
        assert len(result.markdown) > 620 * 200                                # du vrai texte, pas quelques lignes
        assert time.time() - started < 60                                      # lecture en une passe, sans OCR

    def test_page_text_matches_the_small_document_path(self, manual):
        large = mc.convert_to_markdown(manual, "manuel.pdf").pages[0].markdown
        small = mc.convert_to_markdown((FIXTURES / "etats_financiers.pdf").read_bytes(), "etats.pdf").pages[0].markdown
        words = [w for w in small.replace("|", " ").split() if len(w) > 4 and w.isalpha()][:40]
        assert words and sum(w in large for w in words) >= 0.9 * len(words)

    def test_documents_up_to_the_threshold_keep_the_markdown_path(self, monkeypatch):
        called = []
        monkeypatch.setattr(mc, "_convert_large_pdf", lambda *a: called.append(a))
        result = mc.convert_to_markdown((FIXTURES / "etats_financiers.pdf").read_bytes(), "etats.pdf")
        assert not called and result.pages and result.total_pages == len(result.pages)

    def test_without_pdftotext_the_fallback_still_reads_the_pages(self, manual, monkeypatch):
        real = shutil.which
        monkeypatch.setattr(mc.shutil, "which", lambda name: None if name == "pdftotext" else real(name))
        result = mc.convert_to_markdown(_repeat("etats_financiers.pdf", 505), "manuel.pdf")
        assert result.total_pages == 505 and result.pages_read == 505


@needs_pdftotext
@needs_tesseract
class TestLargeScannedPdf:
    def test_scanned_pages_beyond_the_ocr_limit_make_the_document_partial(self, scanned_manual, monkeypatch):
        monkeypatch.setattr(mc, "MAX_PDF_PAGES_OCR", 3)
        result = mc.convert_to_markdown(scanned_manual, "scan.pdf")
        assert result.total_pages == 510 and result.pages_read == 3 and result.partial
        assert result.methods == {"ocr"}
        assert result.warnings[0] == "texte lu sur 3 pages sur 510"
        assert "510 pages numérisées, 3 lues par OCR : 507 restent sans texte" in result.warnings[1]

    def test_the_ocr_time_budget_is_respected(self, scanned_manual, monkeypatch):
        monkeypatch.setattr(mc, "MAX_PDF_PAGES_OCR", 500)
        monkeypatch.setattr(mc, "OCR_TIMEOUT_SECONDS", 0)
        calls = []
        monkeypatch.setattr(mc, "_ocr_page_from_path", lambda path, n: calls.append(n) or "texte")
        result = mc.convert_to_markdown(scanned_manual, "scan.pdf")
        assert len(calls) <= 1 and result.total_pages == 510                   # arrêt dès le budget dépassé


class TestHonestStatus:
    @pytest.fixture
    def api(self, monkeypatch, tmp_path):
        monkeypatch.setattr(kb_storage, "get_conn", lambda: None)
        monkeypatch.setattr(kb_storage, "_DOCS_FILE", tmp_path / "docs.json")
        monkeypatch.setattr(security_limits, "UPLOAD_ROOT", tmp_path / "uploads")
        (tmp_path / "uploads").mkdir()
        monkeypatch.setenv("ADMIN_USER_IDS", ADMIN)

        async def no_embedding(*_a, **_k):
            return None

        monkeypatch.setattr(kb_ingest, "generate_embedding", no_embedding)
        monkeypatch.setattr(main, "generate_embedding", no_embedding)
        for limiter in (security_limits.llm_limiter, security_limits.upload_limiter):
            limiter.reset()
        return TestClient(main.app)

    def _post(self, api, content, name="doc.pdf"):
        answer = api.post("/api/knowledge/ingest", headers=H, data=METIER,
                          files=[("files", (name, io.BytesIO(content), "application/pdf"))])
        assert answer.status_code == 200, answer.text
        return answer.json()["documents"][0]

    def test_status_values(self):
        assert kb_ingest.final_status({"chunks_count": 0, "partial": False}) == "error"
        assert kb_ingest.final_status({"chunks_count": 12, "partial": True}) == "partial"
        assert kb_ingest.final_status({"chunks_count": 12, "partial": False}) == "indexed"

    @needs_pdftotext
    def test_a_fully_read_manual_is_indexed_and_searchable_on_its_last_page(self, api, manual):
        doc = self._post(api, manual, "manuel.pdf")
        assert doc["status"] == "indexed" and doc["status_message"] is None
        chunks = api.get(f"/api/knowledge/{doc['doc_id']}/chunks", headers=H).json()
        chunks = chunks["chunks"] if isinstance(chunks, dict) else chunks
        assert max(c["source_page"] for c in chunks if c.get("source_page")) == 620

    def test_a_document_whose_text_was_not_read_is_never_called_indexed(self, api, monkeypatch):
        """Régression : un manuel de 848 pages sortait « indexé » avec le seul texte de 40 images."""
        monkeypatch.setattr(kb_ingest, "convert_to_markdown",
                            lambda content, name: mc.ConversionResult(warnings=["aucun texte extrait"], total_pages=848))

        def one_image(content, dest_dir, doc_id, skip_pages=None):
            dest_dir.mkdir(parents=True, exist_ok=True)
            path = dest_dir / f"{doc_id}-p3-abc.png"
            path.write_bytes(b"png")
            return [image_extraction.ExtractedImage(page=3, storage_path=str(path), image_hash="h", width=400, height=300,
                                                    text="Tableau des taux d'imposition des sociétés 2024", keywords=["taux"],
                                                    extraction_confidence="high")]

        monkeypatch.setattr(kb_ingest, "extract_pdf_images", one_image)
        doc = self._post(api, (FIXTURES / "etats_financiers.pdf").read_bytes(), "fiscalite.pdf")
        assert doc["status"] == "partial" and doc["chunks_count"] == 1
        assert doc["status_message"].startswith("texte du document non lu")
        assert "aucun texte extrait" in doc["status_message"]

    def test_reindex_reports_coverage(self, api, monkeypatch):
        monkeypatch.setenv("INGEST_BACKGROUND", "0")
        doc = self._post(api, (FIXTURES / "etats_financiers.pdf").read_bytes())
        again = api.post(f"/api/knowledge/{doc['doc_id']}/reindex", headers=H).json()
        assert again["status"] == "reindexed" and again["partial"] is False
        assert again["pages_read"] == again["pages_total"] >= 1


class TestImagesOfLargeDocuments:
    @pytest.mark.skipif(shutil.which("pdfimages") is None, reason="poppler-utils non installé")
    def test_extraction_stops_once_enough_images_are_found(self, monkeypatch, tmp_path):
        """Régression : pdfimages extrayait toutes les images d'un manuel, puis dépassait son délai."""
        windows = []
        real_run = image_extraction.subprocess.run

        def spy(command, **kwargs):
            windows.append((int(command[command.index("-f") + 1]), int(command[command.index("-l") + 1])))
            return real_run(command, **kwargs)

        monkeypatch.setattr(image_extraction.subprocess, "run", spy)
        monkeypatch.setattr(image_extraction, "MAX_IMAGES_PER_DOC", 1)
        monkeypatch.setattr(image_extraction, "IMAGE_PAGE_WINDOW", 10)
        found = image_extraction.extract_pdf_images(_repeat("rapport_avec_graphique.pdf", 60), tmp_path / "img", "doc1")
        assert len(found) == 1
        assert windows == [(1, 10)]                                            # les 50 pages suivantes ne sont pas lues

    @pytest.mark.skipif(shutil.which("pdfimages") is None, reason="poppler-utils non installé")
    def test_a_failing_window_is_skipped_not_fatal(self, monkeypatch, tmp_path):
        real_run = image_extraction.subprocess.run

        def flaky(command, **kwargs):
            if command[command.index("-f") + 1] == "1":
                raise image_extraction.subprocess.TimeoutExpired(command, 60)
            return real_run(command, **kwargs)

        monkeypatch.setattr(image_extraction.subprocess, "run", flaky)
        monkeypatch.setattr(image_extraction, "IMAGE_PAGE_WINDOW", 10)
        found = image_extraction.extract_pdf_images(_repeat("rapport_avec_graphique.pdf", 30), tmp_path / "img", "doc2")
        assert found and min(i.page for i in found) > 10

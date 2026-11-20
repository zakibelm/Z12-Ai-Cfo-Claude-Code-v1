"""Pièces jointes du chat : le texte du fichier est lu, rien n'est enregistré."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import kb_storage
import main
import security_limits

FIXTURES = Path(__file__).parent.parent / "fixtures"
USER = "22222222-2222-2222-2222-222222222222"
H = {"X-User-Id": USER}
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _docx() -> bytes:
    import docx

    document = docx.Document()
    document.add_heading("Étude de cas — Boulangerie Lévesque inc.", level=1)
    document.add_paragraph("Question 1 : calculez la TPS et la TVQ à remettre pour le trimestre.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Ventes taxables", "184 500 $"
    table.cell(1, 0).text, table.cell(1, 1).text = "Achats taxables", "96 200 $"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _xlsx() -> bytes:
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Mon travail"
    for row in (["Poste", "Montant"], ["TPS perçue", 9225], ["TVQ perçue", 18403.88], ["CTI réclamés", 4810]):
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(kb_storage, "get_conn", lambda: None)
    monkeypatch.setattr(kb_storage, "_DOCS_FILE", tmp_path / "docs.json")
    monkeypatch.setattr(security_limits, "UPLOAD_ROOT", tmp_path / "uploads")
    (tmp_path / "uploads").mkdir()
    for limiter in (security_limits.llm_limiter, security_limits.upload_limiter):
        limiter.reset()
    return TestClient(main.app)


def _extract(api, *files, headers=H):
    return api.post("/api/attachments/extract", headers=headers,
                    files=[("files", (name, io.BytesIO(content), mime)) for name, content, mime in files])


def test_pdf_word_and_excel_are_read_in_one_request(api):
    """Régression : le chat joignait « Indexez via Documents. » à la place du contenu des fichiers."""
    answer = _extract(api, ("Etude de cas.pdf", (FIXTURES / "etats_financiers.pdf").read_bytes(), "application/pdf"),
                      ("T4.docx", _docx(), DOCX), ("Mon travail .xlsx", _xlsx(), XLSX))
    assert answer.status_code == 200
    pdf, word, excel = answer.json()["files"]
    assert all(f["error"] is None and f["chars"] > 50 for f in (pdf, word, excel))
    assert pdf["pages_read"] == pdf["pages_total"] >= 1
    assert "Boulangerie Lévesque" in word["text"] and "184 500 $" in word["text"]
    assert "TVQ perçue" in excel["text"] and "18403.88" in excel["text"] and "Mon travail" in excel["text"]


def test_nothing_is_stored(api):
    _extract(api, ("T4.docx", _docx(), DOCX))
    assert kb_storage.list_kb_docs(owner_id=USER) == []
    assert not [p for p in security_limits.UPLOAD_ROOT.rglob("*") if p.is_file()]
    assert api.get("/api/knowledge/list", headers=H).json()["documents"] == []


def test_an_unreadable_file_is_reported_without_failing_the_others(api):
    answer = _extract(api, ("video.mp4", b"0000", "video/mp4"), ("vide.pdf", b"", "application/pdf"),
                      ("abime.pdf", b"ceci n'est pas un PDF", "application/pdf"), ("T4.docx", _docx(), DOCX))
    video, empty, broken, word = answer.json()["files"]
    assert video["error"] == "type de fichier non accepté" and empty["error"] == "fichier vide"
    assert broken["error"] and broken["text"] == ""
    assert word["error"] is None and word["chars"] > 50


def test_a_password_protected_pdf_says_so(api):
    protected = _extract(api, ("etats.pdf", (FIXTURES / "etats_mot_de_passe.pdf").read_bytes(), "application/pdf")).json()["files"][0]
    assert "mot de passe" in protected["error"]


def test_size_and_count_limits(api, monkeypatch):
    import attachments

    monkeypatch.setattr(attachments, "MAX_FILE_SIZE", 1024)
    big = _extract(api, ("gros.pdf", b"%PDF-1.4\n" + b"0" * 4096, "application/pdf")).json()["files"][0]
    assert "trop volumineux pour le chat" in big["error"] and "Documents" in big["error"]
    monkeypatch.setattr(attachments, "MAX_FILES_PER_REQUEST", 1)
    assert _extract(api, ("a.docx", _docx(), DOCX), ("b.docx", _docx(), DOCX)).status_code == 400


def test_very_long_text_is_cut_and_flagged(api, monkeypatch):
    import attachments

    monkeypatch.setattr(attachments, "ATTACHMENT_MAX_CHARS", 80)
    word = _extract(api, ("T4.docx", _docx(), DOCX)).json()["files"][0]
    assert len(word["text"]) == 80 and word["truncated"] is True and word["chars"] > 80


def test_requires_a_signed_in_user(api, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "strict")
    refused = api.post("/api/attachments/extract", files=[("files", ("T4.docx", io.BytesIO(_docx()), DOCX))])
    assert refused.status_code in (401, 403)


def test_a_conversation_has_room_for_attached_text():
    assert security_limits.MAX_MESSAGE_CHARS >= 100_000
    history = [{"role": "user", "content": "x" * 90_000}, {"role": "user", "content": "Analysez ce cas."}]
    assert len(security_limits.validate_messages(history)) == 2

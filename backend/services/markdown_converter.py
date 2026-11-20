"""
markdown_converter.py — conversion de tout document en Markdown, page par page.

Décisions du document « Ingestion documentaire & RAG v2.0 » appliquées ici :
- MarkItDown est la couche de conversion unique (PDF, Word, Excel, PowerPoint, HTML, CSV) ;
- `convert_stream()` uniquement, jamais `convert()` : aucun chemin ni URL fourni par
  l'utilisateur n'est ouvert ;
- plugins désactivés et aucun client LLM : rien ne part vers un modèle cloud pendant la
  lecture d'un document. Les scans sont lus localement (OCRmyPDF, sinon Tesseract) ;
- chaque page porte sa méthode d'extraction (`native` ou `ocr`), reprise dans les citations.

Mesuré sur des états financiers (voir tests/unit/test_ingestion_v2.py) : l'ancienne
extraction PyPDF2 éclatait chaque cellule d'un tableau sur sa propre ligne et ignorait les
tableaux Word ; MarkItDown conserve la ligne « libellé | 2025 | 2024 ».
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from services.text_extractor import extract_text_from_file

# Jusqu'à MAX_PDF_PAGES pages, chaque page est convertie en Markdown (tableaux conservés).
# Au-delà — manuels, recueils — le document est lu par `pdftotext`, page par page, en une seule
# passe : sans cela un document de 800 pages était abandonné et aucun texte n'en sortait.
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "500"))
MAX_PDF_PAGES_OCR = int(os.getenv("MAX_PDF_PAGES_OCR", "40"))
OCR_TIMEOUT_SECONDS = int(os.getenv("OCR_TIMEOUT_SECONDS", "300"))
# En dessous de cette part de pages lues, le document est déclaré partiellement lu.
PARTIAL_COVERAGE = float(os.getenv("PDF_PARTIAL_COVERAGE", "0.9"))
OCR_LANGS = os.getenv("OCR_LANGS", "fra+eng")
# En dessous de ce nombre de caractères, une page PDF est considérée comme une image.
_SCANNED_PAGE_THRESHOLD = 30

_MARKITDOWN_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm", ".csv", ".md", ".txt", ".json", ".xml"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}


@dataclass
class PageText:
    """Texte Markdown d'une page (ou du document entier si `page` vaut None)."""
    markdown: str
    page: int | None = None
    method: str = "native"  # native | ocr


@dataclass
class ConversionResult:
    pages: list[PageText] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total_pages: int | None = None  # nombre de pages du document (PDF), s'il est connu

    @property
    def pages_read(self) -> int:
        """Pages dont du texte a été tiré."""
        return len({p.page for p in self.pages if p.page and p.markdown.strip()})

    @property
    def partial(self) -> bool:
        """Vrai si une part notable des pages n'a donné aucun texte."""
        return bool(self.total_pages) and self.pages_read < PARTIAL_COVERAGE * self.total_pages

    @property
    def markdown(self) -> str:
        return "\n\n".join(p.markdown for p in self.pages if p.markdown.strip())

    @property
    def methods(self) -> set[str]:
        return {p.method for p in self.pages if p.markdown.strip()}


_markitdown = None


def _get_markitdown():
    """Instance unique, sans plugin ni client LLM."""
    global _markitdown
    if _markitdown is None:
        from markitdown import MarkItDown

        _markitdown = MarkItDown(enable_plugins=False)
    return _markitdown


def _markitdown_convert(content: bytes, extension: str) -> str:
    result = _get_markitdown().convert_stream(io.BytesIO(content), file_extension=extension)
    return (result.text_content or "").strip()


# ─────────────────────────────────────────────────────────────────────────────
# PDF
# ─────────────────────────────────────────────────────────────────────────────

def _pdf_page_count(content: bytes) -> int:
    from PyPDF2 import PdfReader

    return len(PdfReader(io.BytesIO(content)).pages)


def _split_pdf_pages(content: bytes) -> list[bytes]:
    """Découpe un PDF en PDF d'une page, pour garder le numéro de page de chaque extrait."""
    from PyPDF2 import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(content))
    if len(reader.pages) > MAX_PDF_PAGES:
        raise ValueError(f"trop de pages ({len(reader.pages)} > {MAX_PDF_PAGES})")
    pages = []
    for page in reader.pages:
        writer = PdfWriter()
        writer.add_page(page)
        buffer = io.BytesIO()
        writer.write(buffer)
        pages.append(buffer.getvalue())
    return pages


PASSWORD_WARNING = "PDF protégé par un mot de passe : retirez la protection, puis déposez-le de nouveau"


def _pdf_password_required(content: bytes) -> bool:
    """Vrai si le PDF ne s'ouvre qu'avec un mot de passe. Un PDF seulement protégé contre la
    modification ou l'impression (mot de passe d'ouverture vide) se lit normalement."""
    from PyPDF2 import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
        return bool(reader.is_encrypted) and not reader.decrypt("")
    except Exception:  # noqa: BLE001 — fichier abîmé ou chiffrement non pris en charge : la suite tranchera
        return False


def ocrmypdf_available() -> bool:
    return shutil.which("ocrmypdf") is not None


def _run_ocrmypdf(content: bytes) -> bytes | None:
    """Ajoute une couche texte à un PDF scanné, en local. None si indisponible ou en échec."""
    if not ocrmypdf_available():
        return None
    with tempfile.TemporaryDirectory(prefix="z12-ocr-") as tmp:
        src, dst = Path(tmp) / "in.pdf", Path(tmp) / "out.pdf"
        src.write_bytes(content)
        try:
            subprocess.run(  # noqa: S603 — arguments fixes, aucun texte utilisateur
                ["ocrmypdf", "--skip-text", "--rotate-pages", "--deskew", "-l", OCR_LANGS,
                 "--output-type", "pdf", "--quiet", str(src), str(dst)],
                check=True, timeout=OCR_TIMEOUT_SECONDS, capture_output=True,
            )
            return dst.read_bytes()
        except (subprocess.SubprocessError, OSError) as e:
            print(f"[WARN] OCRmyPDF a échoué, repli sur Tesseract : {e}")
            return None


def _tesseract_pdf_page(content: bytes, page_number: int) -> str:
    """OCR Tesseract d'une seule page (repli quand OCRmyPDF n'est pas installé)."""
    try:
        import pytesseract
        from pdf2image import convert_from_bytes

        images = convert_from_bytes(content, first_page=page_number, last_page=page_number, dpi=200)
        if not images:
            return ""
        try:
            return pytesseract.image_to_string(images[0], lang=OCR_LANGS).strip()
        except pytesseract.TesseractError:
            return pytesseract.image_to_string(images[0], lang="eng").strip()
    except Exception as e:  # noqa: BLE001 — l'OCR ne doit jamais faire échouer l'ingestion
        print(f"[WARN] OCR Tesseract page {page_number} : {e}")
        return ""


def _page_texts_fast(path: Path, page_count: int) -> list[str]:
    """Texte de chaque page d'un gros PDF, en une passe.

    `pdftotext -layout` (poppler) garde l'alignement des colonnes et lit mille pages en
    quelques secondes. PyPDF2 sert de repli s'il n'est pas installé ou échoue.
    """
    if shutil.which("pdftotext"):
        try:
            done = subprocess.run(  # noqa: S603 — arguments fixes, aucun texte utilisateur
                ["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
                check=True, capture_output=True, timeout=max(120, 2 * page_count),
            )
            texts = done.stdout.decode("utf-8", errors="replace").split("\f")
            if texts and not texts[-1].strip():
                texts.pop()  # pdftotext termine chaque page, dernière comprise, par un saut de page
            texts = [t.strip() for t in texts]
            return (texts + [""] * page_count)[:max(page_count, len(texts))]
        except (subprocess.SubprocessError, OSError) as e:
            print(f"[WARN] pdftotext a échoué, repli sur PyPDF2 : {e}")

    from PyPDF2 import PdfReader

    texts = []
    for page in PdfReader(str(path)).pages:
        try:
            texts.append((page.extract_text() or "").strip())
        except Exception:  # noqa: BLE001 — une page abîmée ne fait pas échouer le document
            texts.append("")
    return texts


def _ocr_page_from_path(path: Path, page_number: int) -> str:
    """OCR d'une page d'un PDF déjà sur disque (le fichier n'est pas recopié à chaque page)."""
    try:
        import pytesseract
        from pdf2image import convert_from_path

        images = convert_from_path(str(path), first_page=page_number, last_page=page_number, dpi=200)
        if not images:
            return ""
        try:
            return pytesseract.image_to_string(images[0], lang=OCR_LANGS).strip()
        except pytesseract.TesseractError:
            return pytesseract.image_to_string(images[0], lang="eng").strip()
    except Exception as e:  # noqa: BLE001 — l'OCR ne doit jamais faire échouer l'ingestion
        print(f"[WARN] OCR Tesseract page {page_number} : {e}")
        return ""


def _convert_large_pdf(content: bytes, page_count: int) -> ConversionResult:
    """Gros PDF (plus de MAX_PDF_PAGES pages) : lecture rapide du texte, puis OCR des pages
    numérisées dans la limite de MAX_PDF_PAGES_OCR pages et de OCR_TIMEOUT_SECONDS."""
    result = ConversionResult(total_pages=page_count)
    with tempfile.TemporaryDirectory(prefix="z12-pdf-") as tmp:
        path = Path(tmp) / "in.pdf"
        path.write_bytes(content)
        texts = _page_texts_fast(path, page_count)
        result.total_pages = max(page_count, len(texts))

        scanned = [i for i, t in enumerate(texts) if len(t) < _SCANNED_PAGE_THRESHOLD]
        ocr_pages: set[int] = set()
        # Quelques pages sans texte dans un manuel sont des pages blanches ou des illustrations :
        # l'OCR n'est tenté que si elles sont nombreuses, ou si tout le document est numérisé.
        if scanned and (len(scanned) > 0.02 * len(texts) or len(scanned) == len(texts)):
            todo = scanned[:MAX_PDF_PAGES_OCR]
            deadline = time.monotonic() + OCR_TIMEOUT_SECONDS
            for i in todo:
                if time.monotonic() > deadline:
                    break
                text = _ocr_page_from_path(path, i + 1)
                if text:
                    texts[i] = text
                    ocr_pages.add(i)
            unread = len(scanned) - len(ocr_pages)
            if unread:
                result.warnings.append(
                    f"{len(scanned)} pages numérisées, {len(ocr_pages)} lues par OCR : {unread} restent sans texte "
                    f"(limites : {MAX_PDF_PAGES_OCR} pages, {OCR_TIMEOUT_SECONDS} s)"
                )

    for i, text in enumerate(texts):
        if text.strip():
            result.pages.append(PageText(markdown=text, page=i + 1, method="ocr" if i in ocr_pages else "native"))
    return result


def _convert_pdf(content: bytes) -> ConversionResult:
    page_count = _pdf_page_count(content)
    if page_count > MAX_PDF_PAGES:
        return _convert_large_pdf(content, page_count)
    result = ConversionResult(total_pages=page_count)
    page_bytes = _split_pdf_pages(content)
    texts = []
    for raw in page_bytes:
        try:
            texts.append(_markitdown_convert(raw, ".pdf"))
        except Exception as e:  # noqa: BLE001
            result.warnings.append(f"page illisible : {e}")
            texts.append("")

    scanned = [i for i, t in enumerate(texts) if len(t) < _SCANNED_PAGE_THRESHOLD]
    ocr_pages: set[int] = set()
    if scanned:
        if len(scanned) > MAX_PDF_PAGES_OCR:
            result.warnings.append(
                f"{len(scanned)} pages numérisées : seules les {MAX_PDF_PAGES_OCR} premières sont lues par OCR"
            )
            scanned = scanned[:MAX_PDF_PAGES_OCR]
        searchable = _run_ocrmypdf(content)
        if searchable:
            try:
                ocr_page_bytes = _split_pdf_pages(searchable)
                for i in scanned:
                    text = _markitdown_convert(ocr_page_bytes[i], ".pdf")
                    if text:
                        texts[i] = text
                        ocr_pages.add(i)
            except Exception as e:  # noqa: BLE001
                result.warnings.append(f"lecture du PDF océrisé : {e}")
        for i in scanned:
            if i not in ocr_pages:
                text = _tesseract_pdf_page(content, i + 1)
                if text:
                    texts[i] = text
                    ocr_pages.add(i)

    for i, text in enumerate(texts):
        if text.strip():
            result.pages.append(PageText(markdown=text, page=i + 1, method="ocr" if i in ocr_pages else "native"))
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Point d'entrée
# ─────────────────────────────────────────────────────────────────────────────

def convert_to_markdown(content: bytes, filename: str) -> ConversionResult:
    """Convertit un fichier en Markdown. Ne lève jamais : en cas d'échec, repli sur
    l'ancienne extraction, puis résultat vide avec un avertissement."""
    extension = Path(filename).suffix.lower()
    total_pages: int | None = None
    try:
        if extension == ".pdf":
            if _pdf_password_required(content):
                return ConversionResult(warnings=[PASSWORD_WARNING])
            result = _convert_pdf(content)
            if result.pages:
                if result.partial:
                    result.warnings.insert(0, f"texte lu sur {result.pages_read} pages sur {result.total_pages}")
                return result
            total_pages = result.total_pages
        elif extension in _IMAGE_EXTENSIONS:
            text = extract_text_from_file(content, filename)
            return ConversionResult(pages=[PageText(markdown=text, method="ocr")] if text.strip() else [])
        elif extension in _MARKITDOWN_EXTENSIONS:
            text = _markitdown_convert(content, extension)
            if text:
                return ConversionResult(pages=[PageText(markdown=text)])
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Conversion Markdown de {filename} : {e} — repli sur l'extraction simple")

    # Repli : formats anciens (.doc, .xls, .ppt) ou échec de MarkItDown
    try:
        text = extract_text_from_file(content, filename)
    except Exception as e:  # noqa: BLE001
        return ConversionResult(warnings=[f"extraction impossible : {e}"], total_pages=total_pages)
    if text.strip() and not text.startswith("[ERREUR]"):
        return ConversionResult(pages=[PageText(markdown=text)], warnings=["extraction simple (sans mise en forme)"])
    return ConversionResult(warnings=["aucun texte extrait"], total_pages=total_pages)

"""
image_extraction.py — images contenues dans un document (graphiques, tableaux en image).

Pour chaque image significative d'un PDF :
- le fichier est conservé dans le dossier de l'utilisateur (référence pour les citations) ;
- son texte est lu en local par Tesseract : aucune image ne part vers un modèle cloud ;
- les mots-clés du lexique métier sont associés à l'image ;
- le texte lu est aussi inséré comme segment, pour que la recherche le retrouve.

L'extraction structurée des graphiques (séries, périodes, devise) par un modèle de vision est
prévue par le document RAG v2.0 mais reste désactivée : elle suppose d'envoyer l'image à un
fournisseur externe, ce qui doit d'abord être décidé par sensibilité. Les colonnes
`extracted_data`, `extraction_model` et `prompt_version` sont prêtes à la recevoir.
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from services.chunker import extract_keywords

MAX_IMAGES_PER_DOC = int(os.getenv("MAX_IMAGES_PER_DOC", "40"))
MIN_IMAGE_SIDE = int(os.getenv("MIN_IMAGE_SIDE_PX", "160"))
OCR_LANGS = os.getenv("OCR_LANGS", "fra+eng")
IMAGE_EXTRACT_TIMEOUT = int(os.getenv("IMAGE_EXTRACT_TIMEOUT_SECONDS", "60"))      # par tranche de pages
IMAGE_EXTRACT_BUDGET = int(os.getenv("IMAGE_EXTRACT_BUDGET_SECONDS", "300"))       # pour tout le document
IMAGE_PAGE_WINDOW = int(os.getenv("IMAGE_PAGE_WINDOW", "25"))


@dataclass
class ExtractedImage:
    page: int
    storage_path: str
    image_hash: str
    width: int
    height: int
    text: str = ""
    keywords: list[str] = field(default_factory=list)
    extraction_confidence: str = "low"


def _ocr(image) -> tuple[str, str]:
    """Texte lu dans l'image et niveau de confiance (high | medium | low)."""
    try:
        import pytesseract

        try:
            data = pytesseract.image_to_data(image, lang=OCR_LANGS, output_type=pytesseract.Output.DICT)
        except pytesseract.TesseractError:
            data = pytesseract.image_to_data(image, lang="eng", output_type=pytesseract.Output.DICT)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] OCR d'image indisponible : {e}")
        return "", "low"

    lines: dict[tuple, list[str]] = {}
    confidences: list[float] = []
    for i, word in enumerate(data.get("text", [])):
        word = (word or "").strip()
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError, KeyError, IndexError):
            conf = -1.0
        if not word or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)
        confidences.append(conf)
    text = "\n".join(" ".join(words) for words in lines.values()).strip()
    if not confidences:
        return "", "low"
    mean = sum(confidences) / len(confidences)
    return text, "high" if mean >= 85 else "medium" if mean >= 65 else "low"


def _page_count(path: Path) -> int:
    from pypdf import PdfReader

    try:
        return len(PdfReader(str(path)).pages)
    except Exception:  # noqa: BLE001
        return 0


def _candidate_images(content: bytes):
    """Produit (page, image PIL) pour chaque image du PDF, au fil de la lecture.

    `pdfimages` (poppler) est préféré : il trouve aussi les images rangées dans des objets
    imbriqués, que PyPDF2 ne parcourt pas. Le document est lu par tranches de pages : l'appelant
    s'arrête dès qu'il a assez d'images, sans extraire celles d'un manuel de mille pages. Chaque
    tranche a son délai, et l'ensemble un budget de temps ; une tranche en échec est sautée.
    PyPDF2 sert de repli si `pdfimages` n'est pas installé.
    """
    from PIL import Image

    if shutil.which("pdfimages"):
        with tempfile.TemporaryDirectory(prefix="z12-img-") as tmp:
            src = Path(tmp) / "in.pdf"
            src.write_bytes(content)
            pages = _page_count(src) or IMAGE_PAGE_WINDOW
            deadline = time.monotonic() + IMAGE_EXTRACT_BUDGET
            failed = 0
            for first in range(1, pages + 1, IMAGE_PAGE_WINDOW):
                if time.monotonic() > deadline:
                    print(f"[WARN] Images : budget de {IMAGE_EXTRACT_BUDGET} s atteint à la page {first}")
                    break
                out = Path(tmp) / f"w{first}"
                out.mkdir()
                try:
                    subprocess.run(  # noqa: S603 — arguments fixes, aucun texte utilisateur
                        ["pdfimages", "-png", "-p", "-f", str(first), "-l", str(min(pages, first + IMAGE_PAGE_WINDOW - 1)),
                         str(src), str(out / "img")],
                        check=True, timeout=IMAGE_EXTRACT_TIMEOUT, capture_output=True,
                    )
                except (subprocess.SubprocessError, OSError) as e:
                    failed += 1
                    if failed == 1:
                        print(f"[WARN] pdfimages a échoué sur les pages {first} et suivantes : {type(e).__name__}")
                    shutil.rmtree(out, ignore_errors=True)
                    continue
                for path in sorted(out.glob("img-*.png")):
                    try:
                        page = int(path.stem.split("-")[1])
                        image = Image.open(path)
                        image.load()
                    except (ValueError, IndexError, OSError) as e:
                        print(f"[WARN] Image illisible ({path.name}) : {e}")
                        continue
                    yield page, image
                shutil.rmtree(out, ignore_errors=True)
        return

    from pypdf import PdfReader

    for page_number, page in enumerate(PdfReader(io.BytesIO(content)).pages, start=1):
        try:
            found = [Image.open(io.BytesIO(raw.data)) for raw in page.images]
            for image in found:
                image.load()
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] Images de la page {page_number} : {e}")
            continue
        for image in found:
            yield page_number, image


def extract_pdf_images(content: bytes, dest_dir: Path, doc_id: str, skip_pages: set[int] | None = None) -> list[ExtractedImage]:
    """Extrait, enregistre et lit les images d'un PDF. Ne lève jamais.

    `skip_pages` : pages numérisées, déjà lues en entier par l'OCR de page.
    """
    skip_pages = skip_pages or set()
    results: list[ExtractedImage] = []
    seen: set[str] = set()
    candidates = _candidate_images(content)
    while len(results) < MAX_IMAGES_PER_DOC:
        try:
            page_number, image = next(candidates)
        except StopIteration:
            break
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] Lecture des images impossible : {e}")
            break
        if page_number in skip_pages:
            continue
        try:
            width, height = image.size
            if min(width, height) < MIN_IMAGE_SIDE:
                continue  # puces, icônes, filets
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            digest = hashlib.sha256(buffer.getvalue()).hexdigest()
            if digest in seen:
                continue  # logo ou filigrane répété sur chaque page
            seen.add(digest)
            dest_dir.mkdir(parents=True, exist_ok=True)
            path = dest_dir / f"{doc_id}-p{page_number}-{digest[:12]}.png"
            path.write_bytes(buffer.getvalue())
            text, confidence = _ocr(image)
            results.append(ExtractedImage(
                page=page_number, storage_path=str(path), image_hash=digest, width=width, height=height,
                text=text, keywords=extract_keywords(text), extraction_confidence=confidence,
            ))
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] Image ignorée (page {page_number}) : {e}")
    candidates.close()  # arrête la lecture du document et retire ses fichiers temporaires
    return results

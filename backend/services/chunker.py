"""
chunker.py — découpage du Markdown en segments indexables.

Principes (document RAG v2.0) :
- on coupe aux frontières naturelles (titres, paragraphes), jamais au milieu d'un tableau :
  un tableau trop long est scindé par lignes, en répétant son en-tête ;
- chaque segment garde sa page, son titre de section et sa méthode d'extraction ;
- les mots-clés viennent d'un lexique métier fixe (fiscalité et comptabilité Québec/Canada),
  sans appel à un modèle : gratuit, déterministe et auditable.
"""
from __future__ import annotations

import re
from pathlib import Path
import unicodedata
from dataclasses import dataclass, field

from services.markdown_converter import PageText

CHUNK_MAX_CHARS = 1400
CHUNK_OVERLAP_CHARS = 150
MAX_KEYWORDS = 12

# Taxonomie canonique du document RAG v2.0.
CONTENT_TYPES = (
    "client_data",            # données réelles d'un client : ce sont des faits à analyser
    "methodology",            # méthode de travail
    "example_methodology",    # exemple chiffré illustrant une méthode : jamais un fait
    "regulatory_reference",   # loi, guide ARC / Revenu Québec, norme
    "policy",                 # politique interne
    "internal_knowledge",     # savoir du cabinet
)
DEFAULT_CONTENT_TYPE = "client_data"
# Types qui décrivent « comment faire », par opposition aux données d'un client.
REFERENCE_CONTENT_TYPES = frozenset(CONTENT_TYPES) - {"client_data"}


def normalize_content_type(value: str | None) -> str:
    return value if value in CONTENT_TYPES else DEFAULT_CONTENT_TYPE


@dataclass
class Chunk:
    content: str
    index: int = 0
    page: int | None = None
    heading: str | None = None
    method: str = "native"
    keywords: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Mots-clés
# ─────────────────────────────────────────────────────────────────────────────

# (mot-clé canonique, motif). Les motifs sont appliqués sur un texte sans accents, en minuscules.
_LEXICON: tuple[tuple[str, str], ...] = (
    ("TPS", r"\btps\b|\bgst\b"), ("TVQ", r"\btvq\b|\bqst\b"), ("TVH", r"\btvh\b|\bhst\b"),
    ("T1", r"\bt1\b"), ("T2", r"\bt2\b"), ("CO-17", r"\bco-?17\b"), ("TP-1", r"\btp-?1\b"),
    ("T4", r"\bt4\b"), ("T5", r"\bt5\b"), ("Relevé 1", r"\breleve 1\b|\brl-?1\b"),
    ("RS&DE", r"\brs&de\b|\brsde\b|\bsr&ed\b"), ("DPA", r"\bdpa\b|deduction pour amortissement|\bcca\b"),
    ("DPE", r"\bdpe\b|deduction pour petite entreprise|small business deduction"),
    ("CII", r"credit d'impot a l'investissement"), ("REER", r"\breer\b|\brrsp\b"), ("CELI", r"\bceli\b|\btfsa\b"),
    ("gain en capital", r"gains? en capital|capital gains?"), ("dividende", r"\bdividendes?\b|\bdividends?\b"),
    ("acomptes provisionnels", r"acomptes? provisionnels?|instalments?"),
    ("retenues à la source", r"retenues? a la source|source deductions?|\bdas\b"),
    ("IFRS", r"\bifrs\b"), ("NCECF", r"\bncecf\b|\baspe\b"), ("NCA", r"\bnca\b|normes canadiennes d'audit|\bcas\b \d"),
    ("bilan", r"\bbilan\b|balance sheet|etat de la situation financiere"),
    ("état des résultats", r"etat des resultats|income statement|etat du resultat"),
    ("flux de trésorerie", r"flux de tresorerie|cash ?flows?"), ("fonds de roulement", r"fonds de roulement|working capital"),
    ("BAIIA", r"\bbaiia\b|\bebitda\b"), ("marge brute", r"marge brute|gross margin"),
    ("comptes clients", r"comptes clients|accounts receivable"), ("comptes fournisseurs", r"comptes fournisseurs|accounts payable"),
    ("stocks", r"\bstocks\b|\binventaires?\b|\binventory\b"), ("amortissement", r"\bamortissements?\b|depreciation"),
    ("rapprochement bancaire", r"rapprochement bancaire|bank reconciliation"),
    ("mission d'examen", r"mission d'examen|review engagement"), ("audit", r"\baudit\b|\bverification\b"),
    ("contrôle interne", r"controles? internes?|internal controls?"), ("anomalie", r"\banomalies?\b|misstatements?"),
    ("Loi 25", r"loi 25|renseignements personnels"), ("LCAP", r"\blcap\b|\bcasl\b"),
    ("ARC", r"\barc\b|agence du revenu du canada|\bcra\b"), ("Revenu Québec", r"revenu quebec"),
    ("subvention", r"\bsubventions?\b|\bgrants?\b"), ("PARI", r"\bpari\b|\birap\b"),
    ("Investissement Québec", r"investissement quebec"), ("CDAE", r"\bcdae\b"), ("BDC", r"\bbdc\b"),
    ("masse salariale", r"masse salariale|payroll"), ("CNESST", r"\bcnesst\b"), ("RRQ", r"\brrq\b|\bqpp\b"),
    ("RQAP", r"\brqap\b|\bqpip\b"), ("FSS", r"\bfss\b|fonds des services de sante"),
    ("acquisition", r"\bacquisitions?\b|fusion"), ("évaluation d'entreprise", r"evaluation d'entreprise|business valuation"),
    ("budget", r"\bbudgets?\b"), ("prévisions", r"\bprevisions?\b|\bforecasts?\b"),
)
_COMPILED_LEXICON = tuple((kw, re.compile(pattern)) for kw, pattern in _LEXICON)


def _fold(text: str) -> str:
    """Minuscules sans accents, pour une recherche tolérante."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


# Mots trop courants pour servir de mot-clé (comparés sans accents).
_COMMON_WORDS = frozenset("""
    avec sans pour dans entre sous vers chez plus moins tres aussi ainsi alors donc mais comme dont
    cette cet ces leur leurs notre nos votre vos elle elles nous vous ils sont etre avoir fait faire
    doit doivent peut peuvent sera seront ete etait avait ont aux des les une que qui quoi quel quelle
    quels quelles tout tous toute toutes autre autres meme chaque selon lors afin deja encore apres avant
    pendant depuis page pages image document total note notes voir exemple figure tableau
    with from that this these those have has been were will shall would should their there which
    """.split())
_TERM_RE = re.compile(r"[a-z][a-z'&-]{3,}")


def content_terms(text: str, limit: int, exclude: set[str] | None = None) -> list[str]:
    """Termes les plus fréquents du texte lui-même (au moins deux occurrences)."""
    exclude = exclude or set()
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for word in re.findall(r"[^\W\d_][\w'&-]{3,}", text or ""):
        key = _fold(word).strip("'-&")
        if len(key) < 4 or key in _COMMON_WORDS or key in exclude or not _TERM_RE.fullmatch(key):
            continue
        counts[key] = counts.get(key, 0) + 1
        display.setdefault(key, word.lower())
    ranked = sorted((k for k, n in counts.items() if n >= 2), key=lambda k: (-counts[k], k))
    return [display[k] for k in ranked[:limit]]


def extract_keywords(text: str, limit: int = MAX_KEYWORDS) -> list[str]:
    """Mots-clés d'un texte : termes du lexique comptable et fiscal d'abord, puis les termes
    propres au contenu, pour qu'une notion absente du lexique reste retrouvable."""
    folded = _fold(text)
    found = [(len(pattern.findall(folded)), kw) for kw, pattern in _COMPILED_LEXICON]
    ranked = sorted(((n, kw) for n, kw in found if n), key=lambda item: (-item[0], item[1]))
    keywords = [kw for _, kw in ranked[:limit]]
    if len(keywords) < limit:
        taken = {_fold(part) for kw in keywords for part in kw.split()}
        keywords += content_terms(text, limit - len(keywords), taken)
    return keywords


MAX_DESCRIPTION_CHARS = 320


def describe_document(markdown: str, filename: str = "") -> str:
    """Description courte, tirée du document lui-même (aucun appel externe) : titres principaux,
    puis premières phrases de texte. L'administrateur peut la remplacer."""
    headings, prose = [], []
    for line in (markdown or "").splitlines():
        line = line.strip()
        match = _HEADING_RE.match(line)
        if match:
            title = match.group(1).strip("# ").strip()
            if title and title not in headings and len(headings) < 3:
                headings.append(title)
        elif line and not line.startswith(("|", "<!--", "![", "---", "```")) and len(line) > 40 and len(prose) < 2:
            prose.append(re.sub(r"[*_`#>]+", "", line))
    parts = [" — ".join(headings)] if headings else []
    parts += prose
    text = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
    if not text:
        text = Path(filename).stem.replace("_", " ").replace("-", " ") if filename else ""
    return text[:MAX_DESCRIPTION_CHARS].rstrip() + ("…" if len(text) > MAX_DESCRIPTION_CHARS else "")


def search_meta(heading: str | None, keywords: list[str] | None) -> str:
    """Texte d'index prioritaire d'un segment : son titre et ses mots-clés."""
    return re.sub(r"\s+", " ", " ".join(p for p in (heading or "", " ".join(keywords or [])) if p)).strip()


def doc_terms(doc_description: str | None, doc_keywords: list[str] | None) -> str:
    """Texte d'index du document, repris sur chacun de ses segments avec un poids faible :
    il aide à retrouver le bon document sans écraser le classement de ses segments."""
    return re.sub(r"\s+", " ", " ".join(p for p in (" ".join(doc_keywords or []), doc_description or "") if p)).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Découpage
# ─────────────────────────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^#{1,6}\s+(.*\S)\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\|\s*:?-{2,}")


def _blocks(markdown: str) -> list[tuple[str, str]]:
    """Découpe en blocs typés : ('heading' | 'table' | 'text', contenu)."""
    blocks: list[tuple[str, str]] = []
    buffer: list[str] = []
    kind = "text"

    def flush() -> None:
        nonlocal buffer
        content = "\n".join(buffer).strip()
        if content:
            blocks.append((kind, content))
        buffer = []

    for line in markdown.splitlines():
        stripped = line.strip()
        is_table = stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1
        if _HEADING_RE.match(stripped):
            flush()
            blocks.append(("heading", stripped))
            kind = "text"
        elif is_table:
            if kind != "table":
                flush()
                kind = "table"
            buffer.append(stripped)
        elif not stripped:
            flush()
            kind = "text"
        else:
            if kind == "table":
                flush()
                kind = "text"
            buffer.append(line.rstrip())
    flush()
    return blocks


def _split_table(table: str, max_chars: int) -> list[str]:
    """Scinde un tableau par lignes en répétant l'en-tête et le séparateur."""
    lines = table.splitlines()
    if len(table) <= max_chars or len(lines) < 4 or not _TABLE_SEPARATOR_RE.match(lines[1]):
        return [table]
    header = lines[:2]
    header_len = sum(len(line) + 1 for line in header)
    parts, current, size = [], [], header_len
    for row in lines[2:]:
        if current and size + len(row) + 1 > max_chars:
            parts.append("\n".join(header + current))
            current, size = [], header_len
        current.append(row)
        size += len(row) + 1
    if current:
        parts.append("\n".join(header + current))
    return parts


def _split_text(text: str, max_chars: int, overlap: int) -> list[str]:
    """Scinde un long paragraphe aux fins de phrase, avec un léger chevauchement."""
    if len(text) <= max_chars:
        return [text]
    parts, start = [], 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            cut = max(text.rfind(". ", start, end), text.rfind("\n", start, end))
            if cut > start + max_chars // 2:
                end = cut + 1
        parts.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [p for p in parts if p]


def chunk_pages(pages: list[PageText], max_chars: int = CHUNK_MAX_CHARS, overlap: int = CHUNK_OVERLAP_CHARS) -> list[Chunk]:
    """Transforme les pages Markdown en segments. Un segment ne chevauche jamais deux pages,
    pour que la citation « page N » soit toujours exacte."""
    chunks: list[Chunk] = []
    heading: str | None = None

    for page in pages:
        current: list[str] = []
        size = 0

        def emit(source: PageText = page) -> None:
            nonlocal current, size
            content = "\n\n".join(current).strip()
            if content:
                chunks.append(Chunk(content=content, page=source.page, heading=heading, method=source.method))
            current, size = [], 0

        for kind, content in _blocks(page.markdown):
            if kind == "heading":
                emit()
                match = _HEADING_RE.match(content)
                heading = match.group(1) if match else content
                current, size = [content], len(content)
                continue
            pieces = _split_table(content, max_chars) if kind == "table" else _split_text(content, max_chars, overlap)
            for piece in pieces:
                if current and size + len(piece) + 2 > max_chars:
                    emit()
                current.append(piece)
                size += len(piece) + 2
        emit()

    for i, chunk in enumerate(chunks):
        chunk.index = i
        chunk.keywords = extract_keywords(f"{chunk.heading or ''}\n{chunk.content}")
    return chunks

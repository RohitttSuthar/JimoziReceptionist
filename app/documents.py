from __future__ import annotations

import asyncio
import hashlib
import json
import difflib
import logging
import mimetypes
import re
import sqlite3
import time
import uuid
from collections import OrderedDict
from pathlib import Path, PureWindowsPath
from typing import Any

import numpy as np
from .config import ROOT, settings
from .prompt_store import PromptError, prompts

log = logging.getLogger(__name__)
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".csv"}
DATA_DIR = ROOT / "data" / "documents"
DB_PATH = ROOT / "data" / "knowledge.sqlite3"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# "internal" documents are stored but never used to answer customers.
AUDIENCES = {"consumer", "chef", "both", "internal"}


def _safe_id(value: str) -> str:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("Invalid product_id")
    return value


def _read_text_file(path: Path) -> str:
    """Read plain text robustly while preserving Unicode/Hindi and line structure."""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp1252"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _pdf_page_text(page: Any) -> str:
    try:
        return page.extract_text(extraction_mode="layout") or page.extract_text() or ""
    except TypeError:  # older pypdf versions
        return page.extract_text() or ""


def _ocr_pdf_pages(path: Path, pages_to_ocr: set[int]) -> dict[int, str]:
    """Optional OCR fallback. Requires PyMuPDF, pytesseract, Tesseract and language packs."""
    if not pages_to_ocr:
        return {}
    try:
        import fitz
        import pytesseract
        from PIL import Image
        import io
    except ImportError:
        log.warning("PDF_OCR_UNAVAILABLE file=%s install pymupdf pytesseract pillow for scanned pages", path.name)
        return {}
    try:
        doc = fitz.open(str(path))
        out: dict[int, str] = {}
        for idx in sorted(pages_to_ocr):
            if idx < 0 or idx >= len(doc):
                continue
            pix = doc[idx].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            image = Image.open(io.BytesIO(pix.tobytes("png")))
            try:
                out[idx] = pytesseract.image_to_string(image, lang="eng+hin", config="--psm 3")
            except Exception as exc:
                log.warning("PDF_OCR_FAILED file=%s page=%d error=%s", path.name, idx + 1, exc)
        doc.close()
        return out
    except Exception as exc:
        log.warning("PDF_OCR_FAILED file=%s error=%s", path.name, exc)
        return {}


def extract_document(path: Path) -> list[tuple[int | None, str]]:
    """Extract ordered, page-aware text from supported knowledge documents.

    OCR is a best-effort fallback for image-only/poor-text PDF pages. For reliable OCR,
    install the optional Python packages and the system Tesseract Hindi+English language packs.
    """
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path), strict=False)
        records: list[tuple[int | None, str]] = []
        needs_ocr: set[int] = set()
        for i, page in enumerate(reader.pages):
            text = _pdf_page_text(page)
            # Sparse text or replacement/control characters commonly indicate scanned pages
            # or broken font maps. OCR can improve these pages when available.
            letters = len(re.findall(r"[A-Za-z\u0900-\u097F]", text))
            bad = len(re.findall(r"\ufffd|[\x00-\x08\x0b\x0c\x0e-\x1f]", text))
            if letters < 25 or (bad and bad / max(1, len(text)) > 0.02):
                needs_ocr.add(i)
            records.append((i + 1, text.strip()))
        ocr = _ocr_pdf_pages(path, needs_ocr)
        for idx, ocr_text in ocr.items():
            old = records[idx][1]
            # Replace extraction only when OCR provides substantially more usable text.
            if len(re.sub(r"\W", "", ocr_text)) > len(re.sub(r"\W", "", old)) * 1.25:
                records[idx] = (idx + 1, ocr_text.strip())
        for page_no, text in records:
            if not text:
                log.warning("PDF_PAGE_NO_TEXT file=%s page=%s; OCR dependencies/system language packs may be missing", path.name, page_no)
        return records
    if ext == ".docx":
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        from docx.oxml.table import CT_Tbl
        from docx.oxml.text.paragraph import CT_P
        doc = Document(str(path))
        chunks: list[str] = []
        # Iterate the underlying body so paragraphs and tables remain in document order.
        for child in doc.element.body.iterchildren():
            if isinstance(child, CT_P):
                paragraph = Paragraph(child, doc)
                value = paragraph.text.strip()
                if value:
                    style = (paragraph.style.name or "") if paragraph.style else ""
                    if style.lower().startswith("heading"):
                        chunks.append(f"\n{value}\n")
                    else:
                        chunks.append(value)
            elif isinstance(child, CT_Tbl):
                table = Table(child, doc)
                rows = []
                for row in table.rows:
                    cells = [re.sub(r"\s+", " ", cell.text).strip() for cell in row.cells]
                    rows.append(" | ".join(cells))
                if rows:
                    chunks.append("\n[TABLE]\n" + "\n".join(rows) + "\n[/TABLE]")
        # Headers/footers often contain relevant company details; include them explicitly.
        for section_no, section in enumerate(doc.sections, start=1):
            for label, part in (("HEADER", section.header), ("FOOTER", section.footer)):
                value = "\n".join(p.text.strip() for p in part.paragraphs if p.text.strip())
                if value:
                    chunks.append(f"\n[{label} section {section_no}]\n{value}")
        return [(None, "\n\n".join(chunks).strip())]
    if ext in {".txt", ".md", ".csv"}:
        return [(None, _read_text_file(path))]
    raise ValueError(f"Unsupported file type: {ext}")



# Lightweight transliteration helps match Hindi-script product names to English
# names in PDFs (e.g. "क्लासिक वेज टिफिन" -> "klasik vej tifin").
# Cross-script search vocabulary is configuration, not response-generation logic.
_RETRIEVAL_LEXICON_PATH = Path(__file__).with_name("retrieval_lexicon.json")
try:
    _RETRIEVAL_LEXICON = json.loads(_RETRIEVAL_LEXICON_PATH.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    log.exception("RETRIEVAL_LEXICON_LOAD_FAILED path=%s", _RETRIEVAL_LEXICON_PATH)
    raise RuntimeError("Retrieval lexicon is missing or invalid") from exc

_DEVANAGARI = _RETRIEVAL_LEXICON["devanagari_transliteration"]
_STOP_WORDS = set(_RETRIEVAL_LEXICON["stop_words"])
_ALIASES = _RETRIEVAL_LEXICON["aliases"]

_VOWELLESS_DIGRAPHS = (("ph", "f"), ("sh", "s"), ("ch", "s"), ("th", "t"), ("dh", "d"), ("kh", "k"), ("gh", "g"),
                       ("bh", "b"), ("jh", "j"), ("ck", "k"))

def _skeleton(latin: str) -> str:
    """Rough consonant skeleton so 'dilivri' ~ 'delivery', 'rifnd' ~ 'refund', 'paikej' ~ 'package'."""
    t = re.sub(r"[^a-z]", "", latin.lower())
    for a, b in _VOWELLESS_DIGRAPHS:
        t = t.replace(a, b)
    t = t.replace("c", "k").replace("q", "k").replace("x", "ks").replace("z", "j").replace("w", "v").replace("j", "g")
    t = re.sub(r"[aeiouyh]", "", t)
    return re.sub(r"(.)\1+", r"\1", t)

def _latinize_token(token: str) -> str:
    out = []
    for char in token.lower():
        if char in _DEVANAGARI:
            out.append(_DEVANAGARI[char])
        elif "\u0900" <= char <= "\u097f":
            # Ignore unhandled Devanagari marks rather than poisoning the token.
            continue
        else:
            out.append(char)
    return "".join(out)

def _stem(token: str) -> str:
    return token[:-1] if len(token) > 3 and token.endswith("s") and not token.endswith("ss") and token.isascii() else token

def _search_tokens(text: str) -> list[str]:
    return [
        _stem(token) for token in re.findall(r"[\w\u0900-\u097F]+", text.lower())
        if token not in _STOP_WORDS and len(token) > 1
    ]

def _fuzzy_equal(a: str, b: str) -> bool:
    """Strict fuzzy match: both >=4 chars, similar length, high similarity."""
    if len(a) < 4 or len(b) < 4 or abs(len(a) - len(b)) > 2:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.84

def _content_forms(content_tokens: list[str]) -> set[str]:
    forms = set(content_tokens)
    for t in content_tokens:
        lat = _latinize_token(t)
        forms.add(lat)
        sk = _skeleton(lat)
        if len(sk) >= 3:
            forms.add("~" + sk)
    return forms

def _token_match(token: str, content_forms: set[str]) -> float:
    """1.0 exact / alias / transliteration, 0.85 same consonant skeleton, 0.7 strict fuzzy, else 0."""
    latin = _latinize_token(token)
    alias = _ALIASES.get(token) or _ALIASES.get(latin)
    forms = {token, latin}
    if any(f and f in content_forms for f in forms):
        return 1.0
    if alias and "+" in alias:
        words = [_stem(w) for w in alias.split("+")]
        return sum(w in content_forms for w in words) / len(words) if any(w in content_forms for w in words) else 0.0
    if alias:
        forms.update(_stem(w) for w in alias.split())
    if any(f and f in content_forms for f in forms):
        return 1.0
    if not token.isascii():  # consonant skeletons are only meant to bridge Devanagari -> English spelling
        for f in list(forms):
            sk = _skeleton(f)
            if len(sk) >= 3 and ("~" + sk) in content_forms:
                return 0.85
    for f in forms:
        if len(f) >= 4 and any(_fuzzy_equal(f, c) for c in content_forms if not c.startswith("~")):
            return 0.7
    return 0.0

def _cross_language_overlap(query_tokens: list[str], content_tokens: list[str]) -> float:
    """Fraction of query terms matched exactly, by Hindi-to-Latin transliteration, or strict fuzzy match."""
    if not query_tokens:
        return 0.0
    forms = _content_forms(content_tokens)
    return sum(_token_match(t, forms) for t in query_tokens) / len(query_tokens)

def _hindi_ratio(text: str) -> float:
    """Share of letters that are Devanagari (1.0 = fully Hindi script, 0.0 = fully Latin)."""
    dev = len(re.findall(r"[\u0900-\u097F]", text))
    lat = len(re.findall(r"[A-Za-z]", text))
    return dev / (dev + lat) if (dev + lat) else 0.0

# A Hindi-script question should get a Hindi-script answer when both versions exist in the documents.
_LANG_MISMATCH_FACTOR = 0.85

_SEG_SPLIT = re.compile(r"(?=\bQ\d{0,3}:?\s)")

def _split_segments(content: str) -> list[str]:
    """Split indexed chunks into individual FAQ records before ranking."""
    text = re.sub(r"\s+", " ", content).strip()
    if not text:
        return []
    marker = re.compile(
        r"(?=(?:#{1,6}\s*)?(?:Q\s*\d{0,3}\s*:|Question\s*:|प्रश्न\s*:))",
        re.I,
    )
    segments = [part.strip() for part in marker.split(text) if part.strip().strip("# ")]
    return segments or [text]


def _question_answer(seg: str) -> tuple[str, str]:
    """Return the FAQ question/aliases and its confirmed answer, if present."""
    text = (seg or "").strip()
    answer_marker = re.search(
        r"(?is)(?:\*{0,2}\s*(?:confirmed\s+answer|answer|उत्तर)\s*:?\s*\*{0,2}|\bA:\s*)",
        text,
    )
    if answer_marker:
        question = text[:answer_marker.start()].strip()
        answer = text[answer_marker.end():].strip()
        answer = re.split(
            r"(?i)(?=(?:#{1,6}\s*)?(?:Q\s*\d{0,3}\s*:|Question\s*:|प्रश्न\s*:))",
            answer, maxsplit=1,
        )[0].strip()
        return question, answer
    q = text.find("?")
    if 0 <= q < len(text) - 1:
        return text[:q + 1], text[q + 1:].strip()
    return "", text


def _customer_facing_answer(text: str) -> str:
    """Turn retrieved FAQ markup into a clean, spoken customer-facing answer."""
    ans = (text or "").strip()

    # Some uploaded knowledge files use one-line FAQ records, e.g.:
    # Q: ... **Also asked as:** ... **Confirmed answer:** ... ####
    # Prefer the confirmed answer and discard the question/paraphrase metadata.
    confirmed = re.search(r"(?is)(?:\*{0,2}\s*(?:confirmed\s+answer|उत्तर)\s*:?\s*\*{0,2})", ans)
    if confirmed:
        ans = ans[confirmed.end():]
    else:
        # Older FAQ formats use A: as the answer delimiter.
        qa = re.search(r"(?is)\bA:\s*", ans)
        if qa:
            ans = ans[qa.end():]

    # Remove leading question and metadata labels if a document uses another layout.
    ans = re.sub(r"(?is)^\s*Q\s*\d{0,3}\s*:\s*.*?(?=\bA:|$)", "", ans)
    ans = re.sub(r"(?is)\*{0,2}\s*also\s+asked\s+as\s*:?\*{0,2}.*?(?=\*{0,2}\s*(?:confirmed\s+answer|उत्तर)\s*:?|$)", "", ans)
    ans = re.sub(r"(?im)^\s*(?:also asked as|related questions|similar questions)\s*:.*$", "", ans)
    ans = re.sub(r"(?im)^\s*(?:confirmed answer|answer|उत्तर)\s*:\s*", "", ans)
    ans = re.sub(r"(?m)^\s*#{1,6}\s*", "", ans)
    ans = re.sub(r"\*{1,2}|_{1,2}", "", ans)
    ans = re.sub(r"(?:\S+\s+)?FAQ\s*$", "", ans, flags=re.I)
    ans = re.sub(r"\s*#{2,}\s*$", "", ans)
    ans = re.sub(r"\s+", " ", ans).strip(" \t\r\n-:;|")

    return ans


def _clean_answer(seg: str, limit: int = 500) -> str:
    _, answer = _question_answer(seg)
    raw = answer or seg
    raw = re.split(
        r"(?i)(?=(?:#{1,6}\s*)?(?:Q\s*\d{0,3}\s*:|Question\s*:|प्रश्न\s*:))",
        raw, maxsplit=1,
    )[0]
    return _customer_facing_answer(raw)[:limit]

def _segment_score(qtokens: list[str], idf: dict[str, float], seg: str) -> float:
    """Question line counts double: matching the question a user asked beats mentioning the words in passing."""
    q_part, a_part = _question_answer(seg)
    qf, af = _content_forms(_search_tokens(q_part)), _content_forms(_search_tokens(a_part))
    total = sum(idf[t] for t in qtokens) * 3 or 1.0
    return sum(idf[t] * (2 * _token_match(t, qf) + _token_match(t, af)) for t in qtokens) / total

def _segment_score_forms(qtokens: list[str], idf: dict[str, float], qf: set[str], af: set[str]) -> float:
    """Same scoring as _segment_score, using token forms that were computed once at index time."""
    total = sum(idf[t] for t in qtokens) * 3 or 1.0
    return sum(idf[t] * (2 * _token_match(t, qf) + _token_match(t, af)) for t in qtokens) / total

def _script_violations(text: str) -> list[str]:
    """Latin words and ASCII numbers found in a reply that must be Devanagari-only."""
    return re.findall(r"[A-Za-z][A-Za-z0-9'\-]*|[0-9][0-9,.]*", text or "")

def _best_snippet(content: str, qtokens: list[str], limit: int = 500) -> str:
    """Choose the single best FAQ answer rather than returning an adjacent FAQ."""
    segments = _split_segments(content)
    if not segments:
        return ""
    best, best_score = segments[0], -1.0
    for seg in segments:
        q_part, a_part = _question_answer(seg)
        q_forms = _content_forms(_search_tokens(q_part))
        all_forms = q_forms | _content_forms(_search_tokens(a_part))
        score = sum(_token_match(t, all_forms) * max(2, len(t)) for t in qtokens)
        score += sum(_token_match(t, q_forms) * max(2, len(t)) for t in qtokens)
        if score > best_score:
            best, best_score = seg, score
    _, answer = _question_answer(best)
    return _customer_facing_answer(answer or best)[:limit]

def chunk_text(text: str, chunk_chars: int = 1100, overlap: int = 180) -> list[str]:
    text = re.sub(r"[\t\r]+", " ", text)
    text = re.sub(r"[ ]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    # Keep FAQ records intact: split at explicit Q/FAQ headings before generic paragraphs.
    # This prevents adjacent answers and "unconfirmed topics" from contaminating one another.
    if re.search(r"(?im)^\s*(?:#{1,6}\s*)?(?:Q\s*\d*\s*:|Question\s*:|प्रश्न\s*:)", text):
        records = re.split(r"(?im)(?=^\s*(?:#{1,6}\s*)?(?:Q\s*\d*\s*:|Question\s*:|प्रश्न\s*:))", text)
        pieces = [r.strip() for r in records if r.strip()]
    else:
        pieces = re.split(r"\n\s*\n|(?<=[.!?।])\s+", text)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if len(piece) > chunk_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            start = 0
            while start < len(piece):
                end = min(len(piece), start + chunk_chars)
                chunks.append(piece[start:end].strip())
                if end == len(piece):
                    break
                start = max(start + 1, end - overlap)
            continue
        if current and len(current) + len(piece) + 1 > chunk_chars:
            chunks.append(current.strip())
            tail = current[-overlap:] if overlap else ""
            current = (tail + " " + piece).strip()
        else:
            current = (current + " " + piece).strip()
    if current:
        chunks.append(current.strip())
    return [x for x in chunks if x]



_EXT = r"(?:pdf|docx?|txt|md|csv|xlsx?|pptx?)"
_FILE = rf"[\w\-. ()\u0900-\u097F]*?\.{_EXT}"
_PAGE = r"(?:pages?|pgs?|p\.|पेज|पृष्ठ|प्रष्ठ)\s*(?:no\.?|number|नंबर|संख्या)?\s*[:#]?\s*\d+(?:\s*[-–]\s*\d+)?"

def strip_citations(text: str) -> str:
    """Remove file names / page numbers so the spoken answer never reads out citations.
    Sources stay available in the response `sources` field for the UI/logs only."""
    import re
    t = text or ""
    # Bracketed / parenthesised citations containing a file name or page ref
    t = re.sub(rf"[\(\[\{{][^\)\]\}}]*?(?:\.{_EXT}\b|{_PAGE})[^\)\]\}}]*?[\)\]\}}]", " ", t, flags=re.I)
    # "(Source: ...)" style trailing lines
    t = re.sub(r"(?im)^\s*(?:sources?|स्रोत|source document)\s*[:\-].*$", " ", t)
    # Lead-ins such as "According to menu.pdf," / "Uploaded document menu.pdf में"
    t = re.sub(rf"(?i)\b(?:according to|as per|from|in|per|based on|as mentioned in|mentioned in)\s+(?:the\s+)?(?:uploaded\s+)?(?:document|file)?\s*[\"'“”]?{_FILE}[\"'“”]?\s*,?", " ", t)
    t = re.sub(rf"(?i)(?:uploaded\s+)?(?:document|file)\s*[\"'“”]?{_FILE}[\"'“”]?\s*(?:के अनुसार|में|से|के मुताबिक)?\s*,?", " ", t)
    t = re.sub(rf"[\"'“”]?{_FILE}[\"'“”]?\s*(?:के अनुसार|में|से|के मुताबिक)?\s*,?", " ", t, flags=re.I)
    # Page references, with common Hindi/English connectors
    t = re.sub(rf"(?i)[,;]?\s*(?:on|at|in|from)?\s*{_PAGE}\s*(?:के अनुसार|पर|में|से|के मुताबिक)?\s*[,;]?", " ", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\s+([,.;:!?।])", r"\1", t)
    t = re.sub(r"^[\s,;:\-–]+", "", t)
    return t.strip()



def _is_unconfirmed_chunk(content: str) -> bool:
    return "no confirmed answer" in content.lower() or len(re.findall(r"(?m)^- ", content)) >= 3

# ---------------------------------------------------------------------------
# Spoken-answer and prompt-budget helpers.
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    """Cheap, deliberately pessimistic token estimate (Devanagari is token-hungry)."""
    dev = len(re.findall(r"[\u0900-\u097F]", text))
    return int(dev / 1.8 + (len(text) - dev) / 3.5) + 1


def _polish_answer(text: str) -> str:
    """Make a model reply safe to speak: no markdown, labels, file names or page numbers."""
    t = strip_citations((text or "").strip())
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", t)
    t = re.sub(r"\*\*(.*?)\*\*|__(.*?)__", lambda m: m.group(1) or m.group(2), t)
    t = re.sub(r"(?m)^\s*(?:[-*\u2022]\s+)?(?:confirmed answer|answer|\u0909\u0924\u094d\u0924\u0930)\s*:\s*", "", t, flags=re.I)
    t = re.sub(r"(?im)^\s*(?:also asked as|related questions|similar questions)\s*:.*$", "", t)
    t = re.sub(r"\s*\n+\s*", " ", t)
    return re.sub(r"[ \t]{2,}", " ", t).strip()




def _truncate_at_sentence(text: str, limit: int) -> str:
    """Cap length without cutting a spoken sentence in half."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = list(re.finditer(r"[\u0964.!?](?=\s|$)", cut))
    if ends and ends[-1].end() >= limit * 0.5:
        return cut[:ends[-1].end()].strip()
    return cut.rsplit(" ", 1)[0].rstrip(",;:- ")


_RETRIEVAL_ROUTES_PATH = Path(__file__).with_name("retrieval_routes.json")
try:
    _RETRIEVAL_ROUTES = json.loads(_RETRIEVAL_ROUTES_PATH.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    log.exception("RETRIEVAL_ROUTES_LOAD_FAILED path=%s", _RETRIEVAL_ROUTES_PATH)
    raise RuntimeError("Retrieval routes are missing or invalid") from exc


def _local_topic_queries(question: str) -> tuple[list[str] | None, str | None, str | None]:
    """Apply configurable retrieval routes without embedding business phrases in code."""
    value = (question or "").strip()
    for route in _RETRIEVAL_ROUTES:
        if re.search(route["pattern"], value, re.I):
            return route["queries"], route.get("language"), route.get("intent")
    return None, None, None


def _has_latin_letters(text: str) -> bool:
    """Detect Latin-script characters; this validates output but never translates it."""
    return bool(re.search(r"[A-Za-z]", text or ""))



def _violates_script(text: str) -> bool:
    """True when the Devanagari-only output rule is enabled and the text breaks it."""
    return settings.enforce_devanagari_output and (_has_latin_letters(text) or bool(re.search(r"[0-9]", text or "")))


class DocumentKnowledgeBase:
    """Local document store with SQLite metadata/chunks and optional OpenAI embeddings."""
    def __init__(self) -> None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._client = None
        # In-memory index: per-chunk search structures are built once and reused by every query.
        self._chunk_cache: dict[int, dict[str, Any]] = {}
        self._index_version = 0
        self._answer_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._enrich_lock: asyncio.Lock | None = None
        self._init_db()

    def _invalidate(self) -> None:
        """Call whenever documents change so cached chunks and answers are rebuilt."""
        self._chunk_cache.clear()
        self._answer_cache.clear()
        self._index_version += 1

    def _connect(self):
        conn = sqlite3.connect(DB_PATH, timeout=20)
        conn.row_factory = sqlite3.Row
        return conn

    FTS_SCHEMA = "3"

    def _init_db(self):
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("""CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, product_id TEXT NOT NULL, filename TEXT NOT NULL,
                stored_path TEXT NOT NULL, audience TEXT NOT NULL, sha256 TEXT NOT NULL,
                pages INTEGER, chunks INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                UNIQUE(product_id, filename, sha256))""")
            con.execute("""CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL,
                product_id TEXT NOT NULL, audience TEXT NOT NULL, filename TEXT NOT NULL,
                page INTEGER, chunk_index INTEGER NOT NULL, content TEXT NOT NULL,
                embedding BLOB, FOREIGN KEY(document_id) REFERENCES documents(id))""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_chunks_product_audience ON chunks(product_id, audience)")
            # Search phrases written once by GPT at indexing time (Hindi / Hinglish / English).
            con.execute("CREATE TABLE IF NOT EXISTS chunk_keywords (chunk_id INTEGER PRIMARY KEY, keywords TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS knowledge_index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = con.execute("SELECT value FROM knowledge_index_meta WHERE key='fts_schema'").fetchone()
            stale_schema = not row or row["value"] != self.FTS_SCHEMA
            if stale_schema:
                con.execute("DROP TABLE IF EXISTS chunks_fts")
            # Persistent local full-text index, maintained incrementally on upload/delete.
            con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(content, filename, keywords, tokenize='unicode61 remove_diacritics 2')")
            chunk_count = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            fts_count = con.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
            if stale_schema or chunk_count != fts_count:
                con.execute("DELETE FROM chunks_fts")
                con.execute("""INSERT INTO chunks_fts(rowid, content, filename, keywords)
                    SELECT c.id, c.content, c.filename, COALESCE(k.keywords, '')
                    FROM chunks c LEFT JOIN chunk_keywords k ON k.chunk_id = c.id""")
                con.execute("INSERT INTO knowledge_index_meta(key,value) VALUES('fts_schema',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (self.FTS_SCHEMA,))
                log.info("KNOWLEDGE_INDEX_REBUILT chunks=%d", chunk_count)

    # Bump when extraction/chunking logic changes: documents are then re-read from their stored
    # files and re-chunked automatically once at the next start (no re-upload needed).
    CHUNK_VERSION = "1"

    def reindex_if_needed(self) -> int:
        with self._connect() as con:
            row = con.execute("SELECT value FROM knowledge_index_meta WHERE key='chunk_version'").fetchone()
            if row and row["value"] == self.CHUNK_VERSION:
                return 0
            docs = con.execute("SELECT id, product_id, filename, stored_path FROM documents").fetchall()
        rebuilt, failed = 0, 0
        for doc in docs:
            path = Path(doc["stored_path"])
            if not path.exists():
                path = DATA_DIR / doc["product_id"] / PureWindowsPath(doc["stored_path"]).name
            try:
                page_texts = extract_document(path)
                chunks = [(page, chunk) for page, text in page_texts for chunk in chunk_text(text)]
                if not chunks:
                    raise ValueError("no readable text")
            except Exception:
                failed += 1
                log.exception("REINDEX_FAILED file=%s (old chunks kept)", doc["filename"])
                continue
            with self._connect() as con:
                audience = con.execute("SELECT audience FROM documents WHERE id=?", (doc["id"],)).fetchone()["audience"]
                for (old_id,) in con.execute("SELECT id FROM chunks WHERE document_id=?", (doc["id"],)).fetchall():
                    con.execute("DELETE FROM chunks_fts WHERE rowid=?", (old_id,))
                    con.execute("DELETE FROM chunk_keywords WHERE chunk_id=?", (old_id,))
                con.execute("DELETE FROM chunks WHERE document_id=?", (doc["id"],))
                for i, (page, content) in enumerate(chunks):
                    cur = con.execute("INSERT INTO chunks(document_id,product_id,audience,filename,page,chunk_index,content,embedding) VALUES(?,?,?,?,?,?,?,NULL)",
                        (doc["id"], doc["product_id"], audience, doc["filename"], page, i, content))
                    con.execute("INSERT INTO chunks_fts(rowid,content,filename,keywords) VALUES(?,?,?,'')", (cur.lastrowid, content, doc["filename"]))
                con.execute("UPDATE documents SET chunks=? WHERE id=?", (len(chunks), doc["id"]))
            rebuilt += 1
        if not failed:
            with self._connect() as con:
                con.execute("INSERT INTO knowledge_index_meta(key,value) VALUES('chunk_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (self.CHUNK_VERSION,))
        self._invalidate()
        log.info("KNOWLEDGE_REINDEXED documents=%d failed=%d", rebuilt, failed)
        return rebuilt

    def _openai(self):
        if not settings.openai_api_key:
            return None
        if self._client is None:
            from openai import AsyncOpenAI
            kwargs = {"base_url": settings.openai_base_url} if settings.openai_base_url else {}
            self._client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=settings.openai_timeout_seconds, max_retries=0, **kwargs)
        return self._client

    async def _embed(self, texts: list[str]) -> list[np.ndarray | None]:
        client = self._openai() if settings.enable_llm_answers else None
        if not client or not settings.openai_embedding_model:  # empty model name = skip embeddings (e.g. providers without them)
            return [None] * len(texts)
        try:
            result = await client.embeddings.create(model=settings.openai_embedding_model, input=texts)
            return [np.asarray(item.embedding, dtype=np.float32) for item in result.data]
        except Exception:
            log.exception("Embedding request failed; document remains searchable lexically")
            return [None] * len(texts)

    async def ingest(self, product_id: str, filename: str, data: bytes, audience: str = "both") -> dict[str, Any]:
        product_id = _safe_id(product_id)
        if audience not in AUDIENCES:
            raise ValueError("audience must be one of: " + ", ".join(sorted(AUDIENCES)))
        # Normalize both POSIX and Windows separators; uploaded filenames are metadata only.
        original = Path((filename or "upload.txt").replace("\\", "/")).name
        original = re.sub(r"[^A-Za-z0-9._() -\u0900-\u097F]", "_", original).strip(" .")[:180] or "upload.txt"
        ext = Path(original).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise ValueError("Supported formats: PDF, DOCX, TXT, MD, CSV")
        if not data:
            raise ValueError("Uploaded file is empty")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("File is larger than the 25 MB limit")
        digest = hashlib.sha256(data).hexdigest()
        folder = DATA_DIR / product_id
        folder.mkdir(parents=True, exist_ok=True)
        stored_name = f"{uuid.uuid4().hex[:8]}_{digest[:12]}_{original}"
        path = folder / stored_name
        await asyncio.to_thread(path.write_bytes, data)
        try:
            page_texts = await asyncio.to_thread(extract_document, path)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        chunks: list[tuple[int | None, int, str]] = []
        for page, text in page_texts:
            for idx, chunk in enumerate(chunk_text(text)):
                chunks.append((page, idx, chunk))
        if not chunks:
            path.unlink(missing_ok=True)
            raise ValueError("No readable text was extracted. If this is a scanned PDF, OCR is needed first.")
        vectors: list[np.ndarray | None] = []
        # Embed in batches to keep requests bounded.
        for start in range(0, len(chunks), 64):
            vectors.extend(await self._embed([c[2] for c in chunks[start:start + 64]]))
        doc_id = uuid.uuid4().hex
        old_paths: list[str] = []
        try:
            with self._connect() as con:
                old_rows = con.execute("SELECT id,stored_path FROM documents WHERE product_id=? AND filename=?", (product_id, original)).fetchall()
                old_paths = [row["stored_path"] for row in old_rows]
                old_ids = [row["id"] for row in old_rows]
                for old_id in old_ids:
                    old_chunk_ids = [r[0] for r in con.execute("SELECT id FROM chunks WHERE document_id=?", (old_id,)).fetchall()]
                    for old_chunk_id in old_chunk_ids:
                        con.execute("DELETE FROM chunks_fts WHERE rowid=?", (old_chunk_id,))
                        con.execute("DELETE FROM chunk_keywords WHERE chunk_id=?", (old_chunk_id,))
                    con.execute("DELETE FROM chunks WHERE document_id=?", (old_id,))
                    con.execute("DELETE FROM documents WHERE id=?", (old_id,))
                con.execute("INSERT INTO documents(id,product_id,filename,stored_path,audience,sha256,pages,chunks,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (doc_id, product_id, original, str(path), audience, digest, len(page_texts) if ext == ".pdf" else None, len(chunks), time.time()))
                for i, ((page, local_idx, content), vector) in enumerate(zip(chunks, vectors)):
                    blob = vector.tobytes() if vector is not None else None
                    cur = con.execute("INSERT INTO chunks(document_id,product_id,audience,filename,page,chunk_index,content,embedding) VALUES(?,?,?,?,?,?,?,?)",
                        (doc_id, product_id, audience, original, page, i, content, blob))
                    con.execute("INSERT INTO chunks_fts(rowid,content,filename,keywords) VALUES(?,?,?,'')", (cur.lastrowid, content, original))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        for old_path in old_paths:
            if old_path != str(path):
                Path(old_path).unlink(missing_ok=True)
        self._invalidate()
        with self._connect() as con:  # chunks written by the current logic
            if not con.execute("SELECT 1 FROM knowledge_index_meta WHERE key='chunk_version'").fetchone() \
                    and con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1:
                con.execute("INSERT INTO knowledge_index_meta(key,value) VALUES('chunk_version',?)", (self.CHUNK_VERSION,))
        return {"id": doc_id, "filename": original, "audience": audience, "pages": len(page_texts) if ext == ".pdf" else None,
                "chunks": len(chunks), "embedded": bool(vectors and vectors[0] is not None),
                "indexed": True, "index_type": "sqlite_fts5"}

    def list_documents(self, product_id: str) -> list[dict[str, Any]]:
        product_id = _safe_id(product_id)
        with self._connect() as con:
            rows = con.execute("SELECT id,filename,audience,pages,chunks,created_at FROM documents WHERE product_id=? ORDER BY created_at DESC", (product_id,)).fetchall()
        return [dict(row) for row in rows]

    def delete_document(self, product_id: str, document_id: str) -> bool:
        product_id = _safe_id(product_id)
        with self._connect() as con:
            row = con.execute("SELECT stored_path FROM documents WHERE product_id=? AND id=?", (product_id, document_id)).fetchone()
            if not row:
                return False
            chunk_ids = [r[0] for r in con.execute("SELECT id FROM chunks WHERE document_id=?", (document_id,)).fetchall()]
            for chunk_id in chunk_ids:
                con.execute("DELETE FROM chunks_fts WHERE rowid=?", (chunk_id,))
                con.execute("DELETE FROM chunk_keywords WHERE chunk_id=?", (chunk_id,))
            con.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
            con.execute("DELETE FROM documents WHERE id=?", (document_id,))
        Path(row["stored_path"]).unlink(missing_ok=True)
        self._invalidate()
        return True

    def set_audience(self, product_id: str, document_id: str, audience: str) -> bool:
        """Change who a document may answer for, without re-uploading it."""
        product_id = _safe_id(product_id)
        if audience not in AUDIENCES:
            raise ValueError("audience must be one of: " + ", ".join(sorted(AUDIENCES)))
        with self._connect() as con:
            cur = con.execute("UPDATE documents SET audience=? WHERE product_id=? AND id=?", (audience, product_id, document_id))
            if cur.rowcount == 0:
                return False
            con.execute("UPDATE chunks SET audience=? WHERE document_id=?", (audience, document_id))
        self._invalidate()
        return True

    def _build_entry(self, row: sqlite3.Row) -> dict[str, Any]:
        """Everything the scorer needs for one chunk, computed once and then reused."""
        content = row["content"]
        segments = []
        for seg in _split_segments(content):
            q_part, a_part = _question_answer(seg)
            segments.append({"text": seg,
                             "qf": _content_forms(_search_tokens(q_part)),
                             "af": _content_forms(_search_tokens(a_part))})
        keywords = row["keywords"] or ""
        return {"id": row["id"], "document_id": row["document_id"], "filename": row["filename"], "page": row["page"],
                "content": content, "segments": segments,
                "forms": _content_forms(_search_tokens(content)),
                "kw_forms": _content_forms(_search_tokens(keywords)) if keywords else set(),
                "unconfirmed": _is_unconfirmed_chunk(content)}

    def _chunk_entries(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        missing = [i for i in ids if i not in self._chunk_cache]
        if missing:
            with self._connect() as con:
                for start in range(0, len(missing), 500):
                    part = missing[start:start + 500]
                    marks = ",".join("?" * len(part))
                    for r in con.execute(f"""SELECT c.id,c.document_id,c.filename,c.page,c.content,k.keywords
                            FROM chunks c LEFT JOIN chunk_keywords k ON k.chunk_id=c.id
                            WHERE c.id IN ({marks})""", part).fetchall():
                        self._chunk_cache[r["id"]] = self._build_entry(r)
        return {i: self._chunk_cache[i] for i in ids if i in self._chunk_cache}

    def warm_index(self) -> int:
        """Build the in-memory index for every answerable chunk (called once at startup)."""
        with self._connect() as con:
            ids = [r[0] for r in con.execute("SELECT id FROM chunks WHERE audience != 'internal'").fetchall()]
        self._chunk_entries(ids)
        return len(ids)

    async def search(self, product_id: str, query: str, audience: str, limit: int | None = None, lang_pref: str | None = None) -> list[dict[str, Any]]:
        limit = limit or settings.retrieval_top_k
        # "log in" / "sign in" are one idea (login); otherwise "log" alone matches "log out" passages
        query = re.sub(r"\b(?:log|sign)[\s-]*in\b", "login", query, flags=re.I)
        query = re.sub(r"\bsign[\s-]*up\b", "signup", query, flags=re.I)
        product_id = _safe_id(product_id)
        qtokens = _search_tokens(query)
        lang_pref = lang_pref or ("hi" if _hindi_ratio(query) >= 0.5 else None)  # "hi" / "en" / None
        if not qtokens:
            return []
        # Expand known Hindi/Hinglish spellings into the vocabulary used by the source docs.
        fts_terms = set(qtokens)
        for token in qtokens:
            alias = _ALIASES.get(token) or _ALIASES.get(_latinize_token(token))
            if alias:
                fts_terms.update(_search_tokens(alias))
        # FTS5 safely quotes each term. The scorer below then reranks these candidates.
        match_expr = " OR ".join('"' + term.replace('"', '""') + '"' for term in sorted(fts_terms) if len(term) > 1)
        # Which documents may answer is decided only by each document's audience setting
        # ("internal" documents are never used), never by file names written in code.
        with self._connect() as con:
            total_rows = con.execute("SELECT COUNT(*) FROM chunks WHERE product_id=? AND audience IN (?, 'both')",
                (product_id, audience)).fetchone()[0]
            if not total_rows:
                return []
            ids: list[int] = []
            if match_expr:
                try:
                    ids = [r[0] for r in con.execute("""SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id=f.rowid
                        WHERE chunks_fts MATCH ? AND c.product_id=? AND c.audience IN (?, 'both')
                        ORDER BY bm25(chunks_fts, 1.0, 0.3, 2.0) LIMIT ?""",
                        (match_expr, product_id, audience, settings.retrieval_candidates)).fetchall()]
                except sqlite3.OperationalError as exc:
                    log.warning("FTS_SEARCH_FALLBACK error=%s", type(exc).__name__)
            if not ids:
                ids = [r[0] for r in con.execute("SELECT id FROM chunks WHERE product_id=? AND audience IN (?, 'both')",
                    (product_id, audience)).fetchall()]
        entries = self._chunk_entries(ids)
        rows = [entries[i] for i in ids if i in entries]
        if not rows:
            return []
        import math
        n_rows = max(total_rows, len(rows))
        idf = {}
        for t in qtokens:
            df = sum(1 for e in rows if _token_match(t, e["forms"]) >= 1.0)
            idf[t] = math.log(1 + n_rows / (1 + df))
            # "how much / how many" words describe the question type, not the topic: let topic words dominate
            if t in ("much", "many") or _ALIASES.get(t) in ("much", "many") or _ALIASES.get(_latinize_token(t)) in ("much", "many"):
                idf[t] *= 0.25
        idf_total = sum(idf.values()) or 1.0
        ranked = []
        for e in rows:
            if e["unconfirmed"]:
                continue  # these list topics with NO confirmed answer
            best_seg, lexical = "", 0.0
            for seg in e["segments"]:
                sc = _segment_score_forms(qtokens, idf, seg["qf"], seg["af"])
                if sc > lexical:
                    best_seg, lexical = seg["text"], sc
            if e["kw_forms"]:
                # GPT-written search phrases (made once at indexing time) help Hinglish / misspelt questions.
                kw = sum(idf[t] * _token_match(t, e["kw_forms"]) for t in qtokens) / idf_total
                lexical = max(lexical, 0.8 * kw)
            score = lexical
            if lang_pref and score > 0.02:
                answer_part = _question_answer(best_seg or e["content"])[1] or best_seg or e["content"]
                if (lang_pref == "hi") != (_hindi_ratio(answer_part) >= 0.5):
                    score *= _LANG_MISMATCH_FACTOR  # passage is in the other language: prefer the user's language
            if score > 0.02:
                ranked.append((score, e, best_seg or e["content"]))
        ranked.sort(key=lambda x: x[0], reverse=True)
        result, seen = [], set()
        for score, e, snippet in ranked:
            key = re.sub(r"\W+", "", snippet.lower())[:200]
            if key in seen:  # identical text uploaded in several documents
                continue
            seen.add(key)
            result.append({"score": round(float(score), 4), "source": e["filename"], "page": e["page"],
                           "content": e["content"], "snippet": snippet, "document_id": e["document_id"]})
            if len(result) >= limit:
                break
        return result

    async def _search_many(self, product_id: str, queries: list[str], audience: str, lang_pref: str | None) -> list[dict[str, Any]]:
        queries = [x for x in dict.fromkeys(queries) if x]
        results = await asyncio.gather(*[self.search(product_id, x, audience, lang_pref=lang_pref) for x in queries])
        merged: dict[str, dict[str, Any]] = {}
        for res in results:
            for r in res:
                key = re.sub(r"\W+", "", r["snippet"].lower())[:200]
                if key not in merged or r["score"] > merged[key]["score"]:
                    merged[key] = r
        return sorted(merged.values(), key=lambda r: r["score"], reverse=True)[:settings.retrieval_top_k]

    def _cache_key(self, product_id: str, audience: str, question: str, history: list[dict[str, str]]) -> str:
        normalized = re.sub(r"\s+", " ", question.lower()).strip()
        tail = "|".join(f"{x.get('role')}:{x.get('content')}" for x in (history or [])[-2:])
        raw = "\x1f".join([product_id, audience, normalized, tail, str(self._index_version), prompts.fingerprint()])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def answer(self, product_id: str, question: str, audience: str, business: dict[str, Any], history: list[dict[str, str]],
                     policies: dict[str, Any] | None = None) -> dict[str, Any]:
        q = question.strip()
        # Documents are extracted, chunked and indexed once at upload. A query does local retrieval
        # from that index, then one bounded GPT call; repeated questions are served from the answer cache.
        cache_on = settings.answer_cache_size > 0
        key = self._cache_key(product_id, audience, q, history) if cache_on else ""
        if cache_on:
            hit = self._answer_cache.get(key)
            if hit and time.monotonic() - hit[0] < settings.answer_cache_ttl_seconds:
                self._answer_cache.move_to_end(key)
                cached = dict(hit[1])
                cached["answer_mode"] = "answer_cache"
                return cached
        result = await self._answer_inner(product_id, q, audience, business, history, policies or {})
        if cache_on and result.get("answer_mode") == "indexed_retrieval_llm":
            self._answer_cache[key] = (time.monotonic(), dict(result))
            while len(self._answer_cache) > settings.answer_cache_size:
                self._answer_cache.popitem(last=False)
        return result

    # ----- One-time GPT indexing -------------------------------------------------------------
    def _pending_chunks(self) -> list[sqlite3.Row]:
        with self._connect() as con:
            return con.execute("""SELECT c.id, c.content FROM chunks c
                LEFT JOIN chunk_keywords k ON k.chunk_id = c.id
                WHERE k.chunk_id IS NULL AND c.audience != 'internal' ORDER BY c.id""").fetchall()

    def _store_keywords(self, rows: list[tuple[int, str]]) -> None:
        with self._connect() as con:
            for chunk_id, keywords in rows:
                con.execute("INSERT OR REPLACE INTO chunk_keywords(chunk_id, keywords) VALUES(?,?)", (chunk_id, keywords))
                con.execute("UPDATE chunks_fts SET keywords=? WHERE rowid=?", (keywords, chunk_id))

    async def _enrich_batch(self, client: Any, system: str, batch: list[sqlite3.Row]) -> int:
        payload = json.dumps([{"id": r["id"], "text": r["content"][:1500]} for r in batch], ensure_ascii=False)
        try:
            response = await client.with_options(timeout=settings.enrich_timeout_seconds).chat.completions.create(
                model=settings.openai_model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": prompts.render("enrich_user", passages=payload)}],
                temperature=0, response_format={"type": "json_object"},
                max_tokens=min(4000, 160 * len(batch) + 200))
            data = json.loads(response.choices[0].message.content or "{}")
        except Exception:
            log.exception("INGEST_ENRICHMENT_BATCH_FAILED chunks=%d (will retry on next start/upload)", len(batch))
            return 0
        valid = {r["id"] for r in batch}
        rows: list[tuple[int, str]] = []
        for item in data.get("items") or []:
            try:
                chunk_id = int(item.get("id"))
            except (TypeError, ValueError, AttributeError):
                continue
            keywords = re.sub(r"\s+", " ", str(item.get("keywords") or "")).strip()[:600]
            if chunk_id in valid and keywords:
                rows.append((chunk_id, keywords))
        if rows:
            await asyncio.to_thread(self._store_keywords, rows)
        return len(rows)

    async def enrich_pending(self) -> int:
        """Give every not-yet-indexed passage GPT-written search phrases. Runs once per passage, never per query."""
        if not settings.enable_ingest_enrichment or not (settings.enable_llm_answers and settings.enable_llm_generation):
            return 0
        client = self._openai()
        if client is None:
            return 0
        if self._enrich_lock is None:
            self._enrich_lock = asyncio.Lock()
        async with self._enrich_lock:
            pending = await asyncio.to_thread(self._pending_chunks)
            if not pending:
                return 0
            try:
                system = prompts.render("enrich_system", terminology=prompts.read_optional("brand_terminology") or "(none)")
                prompts.read("enrich_user")
            except PromptError:
                log.exception("INGEST_ENRICHMENT_PROMPT_MISSING dir=%s", prompts.directory)
                return 0
            size = max(1, settings.enrich_batch_size)
            sem = asyncio.Semaphore(max(1, settings.enrich_concurrency))
            stored = 0

            async def run(batch: list[sqlite3.Row]) -> None:
                nonlocal stored
                async with sem:
                    done = await self._enrich_batch(client, system, batch)
                stored += done

            log.info("INGEST_ENRICHMENT_START pending_chunks=%d", len(pending))
            await asyncio.gather(*(run(pending[i:i + size]) for i in range(0, len(pending), size)))
            self._invalidate()
            log.info("INGEST_ENRICHMENT_DONE indexed_chunks=%d of %d", stored, len(pending))
            return stored

    async def _answer_inner(self, product_id: str, question: str, audience: str, business: dict[str, Any], history: list[dict[str, str]], policies: dict[str, Any]) -> dict[str, Any]:
        q = question.strip()
        local_queries, local_lang, local_intent = _local_topic_queries(q)
        queries = local_queries or [q]
        lang_pref = local_lang
        sources = await self._search_many(product_id, queries, audience, lang_pref)
        best_score = max((x["score"] for x in sources), default=0.0)

        client = self._openai() if (settings.enable_llm_answers and settings.enable_llm_generation) else None
        configured_fallback = str(policies.get("fallback") or "").strip()
        if not configured_fallback:
            try:
                configured_fallback = prompts.read("fallback_default")
            except PromptError:
                log.error("No fallback configured in product config and fallback_default prompt is missing")
        if client is None:
            return {"answer": configured_fallback, "matched": False,
                    "answer_mode": "configured_fallback_llm_unavailable", "sources": [],
                    "match_score": best_score, "intent": local_intent}

        # Keep the spoken length within what the output-token cap can actually hold (Devanagari is ~2 chars/token),
        # so a reply is never cut off mid-sentence by the token limit.
        output_tokens_cap = min(settings.openai_max_tokens, settings.max_query_output_tokens)
        answer_char_limit = min(settings.max_answer_chars, max(120, int(output_tokens_cap * 1.8)))
        try:
            system = prompts.render("answer_system", business_name=business.get("name", product_id),
                                    audience=audience, max_answer_chars=answer_char_limit,
                                    terminology=prompts.read_optional("brand_terminology") or "(no special vocabulary rules)")
        except PromptError:
            # A missing prompt must never surface as an HTTP 500 to the customer.
            log.exception("ANSWER_SYSTEM_PROMPT_LOAD_FAILED dir=%s", prompts.directory)
            return {"answer": configured_fallback, "matched": False,
                    "answer_mode": "configured_fallback_prompt_missing", "sources": [],
                    "match_score": best_score, "intent": local_intent}
        # Keep the complete prompt under the configured input budget. Hindi tokenisation is
        # relatively expensive, so the estimator deliberately overestimates Devanagari text.
        history_items = history[-max(0, settings.history_context_items):] if settings.history_context_items else []
        history_text = "\n".join(
            f"{str(x.get('role','user'))[:8]}: {str(x.get('content',''))[:settings.history_item_chars]}"
            for x in history_items
        )
        if not history_text:
            history_text = "(none)"
        question_text = str(question)[:400]
        fixed_user = f"HISTORY: {history_text}\nQUESTION: {question_text}"
        configured_input = int(getattr(settings, "max_query_input_tokens", 1250))
        configured_total = int(getattr(settings, "max_query_total_tokens", 1500))
        configured_output = int(getattr(settings, "max_query_output_tokens", 180))
        input_budget = max(700, min(configured_input, configured_total - configured_output))
        context_budget = max(250, min(int(getattr(settings, "retrieval_context_tokens", 750)),
            input_budget - _estimate_tokens(system) - _estimate_tokens(fixed_user) - 80))
        context_parts = []
        remaining = context_budget
        for source in sources:
            excerpt = str(source.get("snippet") or source.get("content") or "").strip()
            if not excerpt or remaining <= 0:
                continue
            allowance = min(remaining, context_budget if not context_parts else remaining)
            if _estimate_tokens(excerpt) > allowance:
                lo, hi = 0, len(excerpt)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if _estimate_tokens(excerpt[:mid]) <= allowance:
                        lo = mid
                    else:
                        hi = mid - 1
                excerpt = excerpt[:lo].rsplit(" ", 1)[0].strip()
            if excerpt:
                context_parts.append(excerpt)
                remaining -= _estimate_tokens(excerpt)
        context = "\n\n".join(context_parts) or "NO VERIFIED BUSINESS KNOWLEDGE WAS RETRIEVED FOR THIS QUESTION."
        estimated_prompt_tokens = _estimate_tokens(system) + _estimate_tokens(fixed_user) + _estimate_tokens(context) + 80
        output_budget = max(64, min(settings.openai_max_tokens, configured_output, configured_total - estimated_prompt_tokens))
        log.info("QUERY_PROMPT_BUDGET estimated_input_tokens=%d max_input_tokens=%d context_tokens=%d output_cap=%d estimated_total_cap=%d chunks=%d",
                 estimated_prompt_tokens, input_budget, _estimate_tokens(context), output_budget,
                 estimated_prompt_tokens + output_budget, len(context_parts))
        try:
            response = await client.chat.completions.create(model=settings.openai_model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": prompts.render("answer_user", history=history_text, context=context, question=question_text)}],
                temperature=settings.answer_temperature, max_tokens=output_budget)
            usage = getattr(response, "usage", None)
            if usage is not None:
                details = getattr(usage, "prompt_tokens_details", None)
                log.info("QUERY_TOKEN_USAGE input=%s output=%s total=%s cached_input=%s",
                    getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None),
                    getattr(usage, "total_tokens", None), getattr(details, "cached_tokens", 0))
            answer = _polish_answer(response.choices[0].message.content or "")
            if getattr(response.choices[0], "finish_reason", None) == "length":
                answer = _truncate_at_sentence(answer, max(1, len(answer) - 1))  # drop a half-finished last sentence
            # Cheap, context-free rewrite pass: only the finished answer is sent, together with the
            # exact offending tokens, so the model transliterates them instead of regenerating the answer.
            attempts = 0
            while _violates_script(answer) and attempts < max(0, settings.script_correction_attempts):
                attempts += 1
                violations = ", ".join(dict.fromkeys(_script_violations(answer)))[:300]
                log.warning("SCRIPT_CORRECTION attempt=%d intent=%s violations=%s", attempts, local_intent, violations)
                fix = await client.chat.completions.create(
                    model=settings.openai_model,
                    messages=[{"role": "system", "content": prompts.read("script_correction")},
                              {"role": "user", "content": prompts.render("correction_user", answer=answer, violations=violations)}],
                    temperature=0, max_tokens=output_budget)
                fix_usage = getattr(fix, "usage", None)
                if fix_usage is not None:
                    log.info("QUERY_SCRIPT_CORRECTION_TOKEN_USAGE input=%s output=%s total=%s",
                        getattr(fix_usage, "prompt_tokens", None), getattr(fix_usage, "completion_tokens", None),
                        getattr(fix_usage, "total_tokens", None))
                answer = _polish_answer(fix.choices[0].message.content or "")
            if not answer or _violates_script(answer):
                raise ValueError("Model output did not satisfy the configured script requirement after retry")
            return {"answer": _truncate_at_sentence(answer, answer_char_limit), "matched": bool(sources),
                    "answer_mode": "indexed_retrieval_llm" if sources else "llm_no_retrieval_context", "language": settings.answer_language,
                    "sources": [{"filename": s["source"], "page": s["page"], "score": s["score"]} for s in sources[:4]],
                    "match_score": best_score, "intent": local_intent}
        except Exception:
            log.exception("Document-grounded answer failed")
            return {"answer": configured_fallback, "matched": False,
                    "answer_mode": "configured_fallback_generation_error",
                    "sources": [{"filename": x["source"], "page": x["page"], "score": x["score"]} for x in sources[:3]],
                    "match_score": best_score, "intent": local_intent}



knowledge_base = DocumentKnowledgeBase()
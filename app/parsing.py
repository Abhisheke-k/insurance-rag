"""PDF parsing for insurance endorsement packs.

PyMuPDF gives us positioned text blocks per page. On top of that this module
adds the thing a generic text extractor throws away: **structure**. Endorsement
packs are hierarchical ("ENDORSEMENT NO. 3" > "SECTION 4 - PROPERTY DAMAGE" >
"4.2 Exclusions" > "(a) ..."), and an answer is only auditable if every
extracted block remembers which page and which clause it came from.

The output is a flat list of :class:`~app.models.Block`, each carrying its page
number and the section breadcrumb it sits under. Nothing here decides chunk
boundaries -- that is :mod:`app.chunking`'s job.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from app.models import Block

logger = logging.getLogger(__name__)

__all__ = [
    "HeaderMatch",
    "ParsedDocument",
    "detect_header",
    "parse_pdf_bytes",
    "parse_pdf_file",
]

# --------------------------------------------------------------------------- #
# Header patterns
#
# Levels form a single hierarchy so that a deeper header truncates the stack:
#   0  ENDORSEMENT NO. 3
#   1  SECTION 4 / PART II / SCHEDULE A / GENERAL CONDITIONS
#   2  "4."          (single-component numbered clause)
#   3  "4.2"         (two components)
#   4  "4.2.1"       (three components) ...
# --------------------------------------------------------------------------- #

#: Separator characters that may sit between a header label and its title.
_SEPARATOR = r"(?P<sep>[\s\-–—:.]*)"

_ENDORSEMENT_RE = re.compile(
    r"^(?P<label>ENDORSEMENT\s+(?:NO\.?|NUMBER|NUM\.?|#)\s*[A-Za-z0-9][A-Za-z0-9\-/]*)"
    + _SEPARATOR
    + r"(?P<rest>.*)$",
    re.IGNORECASE,
)

_SECTION_RE = re.compile(
    r"^(?P<label>(?:SECTION|PART|ARTICLE|SCHEDULE|APPENDIX|EXHIBIT|CLAUSE)\s+"
    r"(?:\d{1,3}|[IVXLC]{1,6}|[A-Z]))\b"
    + _SEPARATOR
    + r"(?P<rest>.*)$",
    re.IGNORECASE,
)

# "4.2 Exclusions" / "4.2.1 Sub-limits" -- at least one dot, so a bare year
# such as "2024 was a record year" cannot match.
_MULTI_NUMBER_RE = re.compile(
    r"^(?P<label>\d{1,2}(?:\.\d{1,2}){1,3}\.?)" + _SEPARATOR + r"(?P<rest>.+)$"
)

# "4. GENERAL CONDITIONS" -- a single number *must* be followed by "." or ")".
_SINGLE_NUMBER_RE = re.compile(r"^(?P<label>\d{1,2}[.)])" + _SEPARATOR + r"(?P<rest>.+)$")

# "GENERAL CONDITIONS" -- an all-caps standalone heading.
_UPPERCASE_RE = re.compile(r"^([A-Z][A-Z0-9][A-Z0-9 &/'’\-,()\.]{2,68})$")

#: Sub-clause markers: "(a)", "(ii)", "(3)". Detected here so that the chunker
#: can use them as fallback split points; they never enter the section path.
SUBCLAUSE_RE = re.compile(r"^\(\s*(?:[a-z]{1,3}|[ivxIVX]{1,4}|\d{1,2})\s*\)\s+")

#: Page furniture we drop before parsing ("Page 3 of 8", bare page numbers).
_PAGE_FURNITURE_RE = re.compile(r"^\s*(?:page\s+)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?\s*$", re.IGNORECASE)

_MAX_TITLE_WORDS = 10
_MAX_TITLE_CHARS = 80


@dataclass(frozen=True, slots=True)
class HeaderMatch:
    """A structural header recognised at the start of a line."""

    level: int
    label: str
    title: str
    #: Body text that shared the line with the header ("4.2 The Insurer shall...").
    remainder: str
    kind: str  # endorsement | section | clause | heading
    #: Dash actually used between label and title, if any, so that the citation
    #: reads like the document ("ENDORSEMENT NO. 1 - FLOOD SUB-LIMIT AMENDMENT").
    dash: str = ""

    @property
    def display(self) -> str:
        if not self.title:
            return self.label
        joiner = f" {self.dash} " if self.dash else " "
        return f"{self.label}{joiner}{self.title}".strip()


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """Everything ingestion needs from one PDF."""

    doc_id: str
    filename: str
    page_count: int
    blocks: list[Block]


@dataclass(slots=True)
class _StackEntry:
    level: int
    display: str
    kind: str


def _looks_like_title(remainder: str) -> bool:
    """True when the text after a header label reads as a heading, not a sentence."""
    cleaned = remainder.strip()
    if not cleaned:
        return False
    core = cleaned[:-1] if cleaned.endswith((".", ":", ";")) else cleaned
    if not core:
        return False
    if len(core) > _MAX_TITLE_CHARS or len(core.split()) > _MAX_TITLE_WORDS:
        return False
    # An internal sentence break means we are already into body text.
    return ". " not in core


def _build(level: int, kind: str, match: re.Match[str]) -> HeaderMatch:
    label = match.group("label").strip()
    separator = match.group("sep") or ""
    remainder = match.group("rest").strip()
    dash = next((char for char in separator if char in "-–—"), "")

    if _looks_like_title(remainder):
        return HeaderMatch(level=level, label=label, title=remainder, remainder="", kind=kind, dash=dash)
    return HeaderMatch(level=level, label=label, title="", remainder=remainder, kind=kind, dash=dash)


def detect_header(line: str) -> HeaderMatch | None:
    """Recognise a structural header at the start of ``line``.

    Returns ``None`` for ordinary body text. Exposed for testing because header
    detection is the single assumption the rest of the pipeline rests on.
    """
    stripped = line.strip()
    if not stripped:
        return None

    if match := _ENDORSEMENT_RE.match(stripped):
        return _build(0, "endorsement", match)

    if match := _SECTION_RE.match(stripped):
        return _build(1, "section", match)

    if match := _MULTI_NUMBER_RE.match(stripped):
        # "4.2" -> level 3, "4.2.1" -> level 4: one level per numeric component.
        components = match.group("label").rstrip(".").split(".")
        return _build(1 + len(components), "clause", match)

    if match := _SINGLE_NUMBER_RE.match(stripped):
        return _build(2, "clause", match)

    if _UPPERCASE_RE.match(stripped) and not stripped.endswith("."):
        return HeaderMatch(level=1, label=stripped, title="", remainder="", kind="heading")

    return None


def _is_same_clause(existing_display: str, label: str) -> bool:
    """True when ``label`` re-states the clause we are already inside.

    Endorsement wordings routinely repeat the clause number on the first body
    line ("3. NOTIFICATION OF CLAIM" followed by "3. The Insured shall..."),
    which would otherwise fork one clause into two section paths -- and drop
    the title from the citation.
    """
    normalised = label.rstrip(".)")
    return existing_display == label or existing_display == normalised or any(
        existing_display.startswith(f"{normalised}{suffix}") for suffix in (" ", ". ", ") ")
    )


def _join_lines(lines: list[str]) -> str:
    """Join wrapped PDF lines back into a paragraph, repairing hyphenation."""
    out = ""
    for line in lines:
        piece = line.strip()
        if not piece:
            continue
        if not out:
            out = piece
        elif out.endswith("-") and not out.endswith((" -", "--")):
            out = out[:-1] + piece  # word was hyphen-split across lines
        else:
            out = f"{out} {piece}"
    return re.sub(r"\s+", " ", out).strip()


def _is_page_furniture(text: str, y0: float, y1: float, page_height: float) -> bool:
    """Drop running headers/footers so they do not pollute clause text."""
    compact = text.strip()
    if not compact:
        return True
    if _PAGE_FURNITURE_RE.match(compact):
        return True
    near_bottom = y0 > page_height * 0.94
    near_top = y1 < page_height * 0.05
    return (near_bottom or near_top) and len(compact) <= 80 and "\n" not in compact.strip()


class _DocumentBuilder:
    """Walks pages/lines and emits blocks tagged with the live section path."""

    def __init__(self, doc_id: str, filename: str) -> None:
        self._doc_id = doc_id
        self._filename = filename
        self._stack: list[_StackEntry] = []
        self._buffer: list[str] = []
        self._buffer_page: int | None = None
        self.blocks: list[Block] = []

    # -- section path -------------------------------------------------- #
    @property
    def _section_path(self) -> tuple[str, ...]:
        return tuple(entry.display for entry in self._stack)

    @property
    def _clause_label(self) -> str | None:
        for entry in reversed(self._stack):
            if entry.kind == "clause":
                return entry.display
        return self._stack[-1].display if self._stack else None

    def _push(self, header: HeaderMatch) -> None:
        for entry in self._stack:
            if entry.level == header.level and _is_same_clause(entry.display, header.label):
                # Same clause restated: keep the richer existing display and
                # just close anything nested below it.
                self._stack = [item for item in self._stack if item.level <= header.level]
                return

        self._stack = [entry for entry in self._stack if entry.level < header.level]
        self._stack.append(_StackEntry(level=header.level, display=header.display, kind=header.kind))

    # -- emission ------------------------------------------------------ #
    def _emit(self, text: str, page: int, kind: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        self.blocks.append(
            Block(
                text=cleaned,
                page=page,
                doc_id=self._doc_id,
                filename=self._filename,
                section_path=self._section_path,
                clause_label=self._clause_label,
                kind="heading" if kind == "heading" else "body",
            )
        )

    def flush(self) -> None:
        if self._buffer and self._buffer_page is not None:
            self._emit(_join_lines(self._buffer), self._buffer_page, "body")
        self._buffer = []
        self._buffer_page = None

    def add_line(self, line: str, page: int) -> None:
        stripped = line.strip()
        if not stripped:
            self.flush()
            return

        header = detect_header(stripped)
        if header is None:
            if self._buffer_page is None:
                self._buffer_page = page
            self._buffer.append(stripped)
            return

        # A header always terminates the paragraph before it.
        self.flush()
        self._push(header)
        if header.remainder:
            # Header shares its line with body text ("4.2 The Insurer shall...").
            # Keep the label in the text so the chunk reads like the source.
            self._buffer_page = page
            self._buffer.append(f"{header.label} {header.remainder}".strip())
        else:
            self._emit(header.display, page, "heading")


def parse_pdf_bytes(data: bytes, *, filename: str, doc_id: str) -> ParsedDocument:
    """Parse an in-memory PDF into structure-tagged blocks."""
    document = pymupdf.open(stream=data, filetype="pdf")
    try:
        builder = _DocumentBuilder(doc_id=doc_id, filename=filename)
        for page_index in range(document.page_count):
            page = document[page_index]
            page_number = page_index + 1
            page_height = float(page.rect.height)

            for raw in page.get_text("blocks", sort=True):
                x0, y0, x1, y1, text, _block_no, block_type = raw[:7]
                if block_type != 0:  # image / drawing block
                    continue
                if _is_page_furniture(text, y0, y1, page_height):
                    continue
                for line in text.splitlines():
                    builder.add_line(line, page_number)
                # A PyMuPDF block is a paragraph-sized unit; end it here.
                builder.flush()
            builder.flush()

        page_count = document.page_count
    finally:
        document.close()

    logger.debug("parsed %s: %d pages, %d blocks", filename, page_count, len(builder.blocks))
    return ParsedDocument(
        doc_id=doc_id,
        filename=filename,
        page_count=page_count,
        blocks=builder.blocks,
    )


def parse_pdf_file(path: str | Path, *, doc_id: str, filename: str | None = None) -> ParsedDocument:
    """Parse a PDF from disk."""
    file_path = Path(path)
    return parse_pdf_bytes(
        file_path.read_bytes(),
        filename=filename or file_path.name,
        doc_id=doc_id,
    )

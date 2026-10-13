"""Week 12: the wording in force on the loss date -- a filter the code enforces, not a hint in the prompt.

Coverage is decided against the wording in force on the *date of loss*, not the wording that happens to retrieve
best today. This corpus has exactly that trap: Form HO-0304 exists in five editions (30/35/45/55/60-day windows for
Exclusion E-17), each with an explicit "superseded by edition X with effect from <date>", and the endorsements
themselves carry "Effective from <date>". A semantic retriever treats the five editions as near-duplicates and
returns whichever scores best; for a loss in August 2024 that was, in the Week 4 retrieval confusion, the wrong one.

This module reads the versioning *out of the corpus text* (nothing is typed in) and then, for a loss date, narrows
what retrieval returned to the text that was in force that day:

1. **Reassemble** each document from its chunks. Chunks overlap and are packed (one chunk can hold the end of one
   endorsement and the start of the next; the archive's edition 09-23 paragraph is split across two chunks and the
   second chunk's stored ``section_path`` names the *next* edition), so chunk metadata cannot say which version a
   line belongs to. The headings in the reassembled text can.
2. **Segment** the text into units: an ``ENDORSEMENT NO. n`` heading starts an endorsement unit (effective from its
   ``Effective from`` line); a ``FORM XX-NNNN - EDITION NN-NN`` heading starts an edition unit (in force from the previous
   edition's "superseded ... with effect from" date to the day before its own); ``GENERAL CONDITIONS`` is policy-level
   and has no interval.
3. **Resolve**: given a loss date, keep the units whose interval contains it and drop the rest, returning the
   narrowed text plus a tag per kept version (document, edition, effective from/to) -- the facts a citation must carry.

The parsing is tuned to this document family's conventions, like ``app/parsing.py``'s header detection, and it degrades
the same way: a document with no recognisable version markers is returned unchanged, never dropped. A new document
family should be checked against ``tests/test_versions.py``'s hand-written timeline before it is trusted.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from app.models import RetrievedChunk, VersionTag

__all__ = [
    "Interval",
    "Unit",
    "VersionIndex",
    "VersionTag",
    "parse_date",
]

_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_DATE_RE = r"\d{1,2}\s+(?:" + _MONTHS + r")\s+\d{4}"
_ENDORSEMENT_RE = re.compile(r"^ENDORSEMENT NO\.\s*(\d+)\s*-\s*(.+)$")
_EFFECTIVE_RE = re.compile(r"^Effective from\s+(?P<when>" + _DATE_RE + r"|inception)\b", re.IGNORECASE)
_EDITION_HEADING_RE = re.compile(r"^FORM\s+(?P<form>[A-Z]{2}-\d{4})\s*-\s*EDITION\s+(?P<edition>\d{2}-\d{2})$")
_SUPERSEDED_RE = re.compile(
    r"Superseded by edition\s+(?P<next>\d{2}-\d{2})\s+with effect from\s+(?P<when>" + _DATE_RE + r")", re.IGNORECASE
)
_ADOPTS_RE = re.compile(r"adopts the exclusion wording of Form\s+(?P<form>[A-Z]{2}-\d{4})\s+edition\s+(?P<edition>\d{2}-\d{2})", re.IGNORECASE)
_EDITION_MENTION_RE = re.compile(r"Form\s+[A-Z]{2}-\d{4},?\s+edition\s+(?P<edition>\d{2}-\d{2})", re.IGNORECASE)
_CURRENT_EDITION_NOTE_RE = re.compile(r"edition currently endorsed", re.IGNORECASE)
_POLICY_LEVEL_HEADING_RE = re.compile(r"^GENERAL CONDITIONS$")
_OVERLAP_PROBE = 60


def parse_date(text: str) -> date:
    return datetime.strptime(" ".join(text.split()), "%d %B %Y").date()


@dataclass(frozen=True, slots=True)
class Interval:
    """Closed on both ends; ``None`` is open on that side."""

    start: date | None = None
    end: date | None = None

    def contains(self, day: date) -> bool:
        return (self.start is None or self.start <= day) and (self.end is None or day <= self.end)


@dataclass(frozen=True, slots=True)
class Unit:
    """A run of a document's text that has one version: [start, end) in the reassembled text."""

    start: int
    end: int
    kind: str  # "endorsement" | "edition" | "plain"
    interval: Interval
    document: str
    edition: str | None = None
    form: str | None = None


@dataclass
class _Doc:
    text: str = ""
    offsets: dict[str, tuple[int, int]] = field(default_factory=dict)
    units: list[Unit] = field(default_factory=list)


class VersionIndex:
    """Versioning facts parsed from a corpus, plus the per-chunk narrowing built on them."""

    def __init__(self) -> None:
        self._docs: dict[str, _Doc] = {}
        #: (form, edition) -> its in-force interval, parsed from "superseded ... with effect from" and "adopts ... edition".
        self.editions: dict[tuple[str, str], Interval] = {}
        #: (document, endorsement number) -> effective date (``None`` = from inception).
        self.endorsement_effective: dict[tuple[str, int], date | None] = {}

    # ------------------------------------------------------------------ #
    # Building
    # ------------------------------------------------------------------ #
    @classmethod
    def build(cls, chunks: Iterable[RetrievedChunk]) -> VersionIndex:
        index = cls()
        by_doc: dict[str, list[RetrievedChunk]] = {}
        for chunk in chunks:
            by_doc.setdefault(chunk.doc_id, []).append(chunk)
        for doc_id, doc_chunks in by_doc.items():
            ordered = sorted(doc_chunks, key=lambda c: c.chunk_index)
            index._docs[doc_id] = index._assemble(ordered)
        for doc in index._docs.values():
            index._learn_facts(doc.text)
        for doc_id, doc in index._docs.items():
            doc.units = index._segment(doc_id, doc.text)
        return index

    @staticmethod
    def _assemble(ordered: Sequence[RetrievedChunk]) -> _Doc:
        """Concatenate chunks, collapsing each one's overlap with the chunk before it."""
        doc = _Doc()
        for chunk in ordered:
            probe = chunk.text[:_OVERLAP_PROBE]
            window_start = max(0, len(doc.text) - len(chunk.text) - 200)
            at = doc.text.rfind(probe, window_start) if doc.text and len(probe) >= 20 else -1
            if at >= 0:
                start = at
                doc.text = doc.text[:at] + chunk.text
            else:
                if doc.text:
                    doc.text += "\n"
                start = len(doc.text)
                doc.text += chunk.text
            doc.offsets[chunk.chunk_id] = (start, start + len(chunk.text))
        return doc

    def _learn_facts(self, text: str) -> None:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        last_form_by_edition: dict[str, str] = {}
        for line in lines:
            for match in re.finditer(r"Form\s+(?P<form>[A-Z]{2}-\d{4}),?\s+edition\s+(?P<edition>\d{2}-\d{2})", line, re.IGNORECASE):
                last_form_by_edition[match.group("edition")] = match.group("form").upper()
            heading = _EDITION_HEADING_RE.match(line)
            if heading:
                last_form_by_edition[heading.group("edition")] = heading.group("form")
        form_default = next(iter(last_form_by_edition.values()), "HO-0304")

        for position, line in enumerate(lines):
            superseded = _SUPERSEDED_RE.search(line)
            if superseded:
                edition = _EDITION_MENTION_RE.search(line)
                this_edition = edition.group("edition") if edition else None
                if this_edition:
                    form = last_form_by_edition.get(this_edition, form_default)
                    switch = parse_date(superseded.group("when"))
                    self._set(form, this_edition, end=switch - timedelta(days=1))
                    self._set(form, superseded.group("next"), start=switch)
        # an endorsement that adopts an edition puts it in force from the endorsement's own effective date
        current: tuple[int, str] | None = None
        for position, line in enumerate(lines):
            heading = _ENDORSEMENT_RE.match(line)
            if heading:
                current = (int(heading.group(1)), heading.group(2).strip())
                effective_match = next(
                    (m for m in (_EFFECTIVE_RE.match(follow) for follow in lines[position + 1 : position + 4]) if m), None
                )
                if effective_match:
                    when = effective_match.group("when").lower()
                    self.endorsement_effective[("*", current[0])] = None if when == "inception" else parse_date(when)
            adopted = _ADOPTS_RE.search(line)
            if adopted and current and self.endorsement_effective.get(("*", current[0])):
                self._set(adopted.group("form"), adopted.group("edition"), start=self.endorsement_effective[("*", current[0])])

    def _set(self, form: str, edition: str, *, start: date | None = None, end: date | None = None) -> None:
        existing = self.editions.get((form, edition), Interval())
        self.editions[(form, edition)] = Interval(start or existing.start, end or existing.end)

    def _segment(self, doc_id: str, text: str) -> list[Unit]:
        """Split a reassembled document into runs that each have one version."""
        boundaries: list[tuple[int, str, Interval, str, str | None, str | None]] = []
        offset = 0
        for raw in text.split("\n"):
            line = raw.strip()
            heading_e = _ENDORSEMENT_RE.match(line)
            heading_f = _EDITION_HEADING_RE.match(line)
            if heading_e:
                number = int(heading_e.group(1))
                effective = self.endorsement_effective.get(("*", number))
                boundaries.append(
                    (offset, "endorsement", Interval(start=effective), f"Endorsement No. {number} - {heading_e.group(2).strip()}", None, None)
                )
            elif heading_f:
                form, edition = heading_f.group("form"), heading_f.group("edition")
                boundaries.append(
                    (offset, "edition", self.editions.get((form, edition), Interval()), f"Form {form}", edition, form)
                )
            elif _POLICY_LEVEL_HEADING_RE.match(line):
                boundaries.append((offset, "plain", Interval(), "General conditions", None, None))
            offset += len(raw) + 1
        if not boundaries or boundaries[0][0] > 0:
            boundaries.insert(0, (0, "plain", Interval(), "Policy document (preamble)", None, None))

        units: list[Unit] = []
        for position, (start, kind, interval, document, edition, form) in enumerate(boundaries):
            end = boundaries[position + 1][0] if position + 1 < len(boundaries) else len(text)
            unit_text = text[start:end]
            if kind == "plain" and _CURRENT_EDITION_NOTE_RE.search(unit_text):
                # The archive's cover note says which edition is *current*; that is evidence only from the day
                # the newest edition took effect, so it must not be offered for a loss before then.
                newest = max((i.start for i in self.editions.values() if i.start), default=None)
                interval = Interval(start=newest)
            if kind == "endorsement":
                mention = _EDITION_MENTION_RE.search(unit_text)
                edition = mention.group("edition") if mention else None
            units.append(Unit(start, end, kind, interval, document, edition, form))
        return units

    # ------------------------------------------------------------------ #
    # Using it
    # ------------------------------------------------------------------ #
    def has_versions(self, chunk: RetrievedChunk) -> bool:
        return chunk.doc_id in self._docs

    def resolve(self, chunk: RetrievedChunk, as_of: date | None) -> tuple[RetrievedChunk | None, list[VersionTag]]:
        """The part of ``chunk`` in force on ``as_of`` and a tag for each version it contains.

        ``as_of=None`` keeps everything (and still tags it). A chunk with nothing in force returns ``None``.
        A chunk the index does not know is returned untouched with no tags -- unknown is not the same as out of force.
        """
        doc = self._docs.get(chunk.doc_id)
        located = doc.offsets.get(chunk.chunk_id) if doc else None
        if doc is None or located is None:
            return chunk, []
        start, end = located
        kept_pieces: list[str] = []
        tags: list[VersionTag] = []
        for unit in doc.units:
            lo, hi = max(unit.start, start), min(unit.end, end)
            if lo >= hi:
                continue
            if as_of is not None and not unit.interval.contains(as_of):
                continue
            kept_pieces.append(doc.text[lo:hi])
            if unit.kind != "plain":
                tag = VersionTag(
                    document=unit.document,
                    edition=unit.edition,
                    effective_from=unit.interval.start.isoformat() if unit.interval.start else None,
                    effective_to=unit.interval.end.isoformat() if unit.interval.end else None,
                )
                if tag not in tags:
                    tags.append(tag)
        narrowed = "".join(kept_pieces).strip()
        if as_of is not None and not narrowed:
            return None, tags
        # the carried clause lead-in (app/clause_stem.py), if retrieval already put one on, is not part of the stored text
        prefix = chunk.text[: chunk.text.index("[Continued] ") + len("[Continued] ")] if chunk.text.startswith("[Clause lead-in]") and "[Continued] " in chunk.text else ""
        return replace(chunk, text=prefix + narrowed if narrowed else chunk.text, versions=tuple(tags)), tags

    def editions_in_force(self, form: str, as_of: date) -> list[str]:
        return sorted(e for (f, e), interval in self.editions.items() if f == form and interval.contains(as_of))

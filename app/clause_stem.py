"""Week 11: carry a list clause's lead-in into the chunks that continue it.

The failure this closes (found by the Week 11 support drill, measured over 280 claims): the cyber
endorsement's exclusion is one sentence of lead-in -- "this policy excludes any loss ... of the following:" --
followed by eight items (a)-(h). The chunker, correctly, never splits an item, but a list longer than a chunk has
to be cut *between* items, and the cut leaves the lead-in in the first chunk only. Chunks 3-5 then hold "(f) any
extortion demand, ransom payment ..." with no sign that it is an exclusion, and a claim about a ransom payment
retrieves wording that reads like a description of covered loss. Every ransomware claim in the test traffic
(40 of 40) was answered "covered".

The fix belongs where the information is lost -- at retrieval, not in a prompt that cannot restore words the model
was never shown. When a retrieved chunk is a *continuation* of a list, :func:`carry_clause_stems` finds the
lead-in in the preceding chunk(s) of the same document and prefixes it::

    [Clause lead-in] 2. Notwithstanding ... this policy excludes any loss ... of the following:
    [Continued] (d) the receipt, transmission ... (f) any extortion demand, ransom payment ...

It is deliberately conservative -- a wrongly attached lead-in would be a new way to mislead:

* only chunks that are slices of an oversized clause (``is_partial_clause``) *and* whose lines open with a list
  item marker -- ``(a)``, ``(ii)``, ``(3)`` -- are touched; a chunk that already contains its own lead-in is left alone;
* the lead-in must be a line ending ``:`` directly followed by an item line, the *last* such pair before the chunk;
* it looks back at most :data:`MAX_LOOKBACK` chunks and **stops at any heading** (an all-caps line, an
  ``ENDORSEMENT NO.`` line, a numbered heading): a new clause starts there, and the earlier stem no longer governs.

``CARRY_CLAUSE_STEM=false`` switches it off, which is also the rollback plan.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import replace

from app.models import RetrievedChunk

__all__ = ["MAX_LOOKBACK", "carry_clause_stems", "find_stem"]

MAX_LOOKBACK = 4

_ITEM_LINE_RE = re.compile(r"^\((?:[a-z]|[ivx]+|\d+)\)\s")
_FIRST_ITEM_RE = re.compile(r"(?m)^\((?:[a-z]|[ivx]+|\d+)\)\s")
_HEADING_RE = re.compile(
    r"^(?:ENDORSEMENT NO\.|SECTION\s+\d|PART\s+[A-Z0-9]|SCHEDULE\b|ARTICLE\b"  # structural headings
    r"|\d+(?:\.\d+)*\.?\s+[A-Z][^.:;]{0,70}$"  # "4.2 Exclusions", "3. DEDUCTIBLE"
    r"|[A-Z][A-Z0-9 ,&()\-./']{7,}$)"  # an all-caps line
)

Lookup = Callable[[str, int], RetrievedChunk | None]


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _is_heading(line: str) -> bool:
    return bool(_HEADING_RE.match(line))


def find_stem(lines: Sequence[str]) -> tuple[str, int] | None:
    """The last lead-in in ``lines``: a line ending ``:`` immediately followed by a list-item line.

    Returns ``(lead-in text, its index)`` or ``None``.
    """
    found: tuple[str, int] | None = None
    for index in range(len(lines) - 1):
        if lines[index].endswith(":") and _ITEM_LINE_RE.match(lines[index + 1]):
            found = (lines[index], index)
    return found


def _stem_for(hit: RetrievedChunk, lookup: Lookup) -> str | None:
    if not hit.is_partial_clause or hit.chunk_index <= 0:
        return None
    own = _lines(hit.text)
    first_item = _FIRST_ITEM_RE.search(hit.text)
    if not any(_ITEM_LINE_RE.match(line) for line in own):
        return None  # not a list continuation
    own_stem = find_stem(own)
    if own_stem is not None and first_item is not None and hit.text.find(own_stem[0]) < first_item.start():
        return None  # its own items follow its own lead-in: nothing is missing

    crossed: list[str] = []
    child_text = hit.text
    for back in range(1, MAX_LOOKBACK + 1):
        previous = lookup(hit.doc_id, hit.chunk_index - back)
        if previous is None:
            return None
        # Chunks overlap: the next chunk opens with a repeat of this one's tail. Only the part *before* that
        # point lies between the lead-in and the chunk we are explaining -- a heading in the repeated tail
        # comes after the items the next chunk re-states, so it must not count as separating them.
        cut = previous.text.find(child_text[:80])
        lines = _lines(previous.text[:cut] if cut > 0 else previous.text)
        child_text = previous.text
        stem = find_stem(lines)
        if stem is not None:
            text, at = stem
            if any(_is_heading(line) for line in [*lines[at + 2 :], *crossed]):
                return None  # a new clause began after the lead-in; it no longer governs this chunk
            return text
        if any(_is_heading(line) for line in lines):
            return None  # the clause that would own this chunk was broken by a heading
        crossed = [*lines, *crossed]
    return None


def carry_clause_stems(hits: Sequence[RetrievedChunk], lookup: Lookup) -> tuple[list[RetrievedChunk], int]:
    """``hits`` with each list continuation prefixed by its lead-in; also how many were changed."""
    out: list[RetrievedChunk] = []
    carried = 0
    for hit in hits:
        stem = _stem_for(hit, lookup)
        if stem is None:
            out.append(hit)
            continue
        out.append(replace(hit, text=f"[Clause lead-in] {stem}\n[Continued] {hit.text}"))
        carried += 1
    return out, carried

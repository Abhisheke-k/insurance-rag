"""Week 11: flags computed when a trace is written, so a bad answer can be found by what is *wrong with it*.

The first support drill found its trace by reading 23 candidate answers and recognising that a ransom payment is
an excluded peril. The log had the answer's decision and its text, but nothing that said "this answer looks like
one that ought to be looked at". That recognition is exactly what a field can do -- once, on write, for every
request -- so the next drill does not depend on whoever is on call knowing the policy.

Each flag is a *cheap, deterministic, explainable* heuristic, not a verdict; its job is to shortlist, never to decide:

``covered_quotes_list_item``
    The answer says ``covered`` (or ``partially_covered``) and its rationale quotes a lettered/numbered list item --
    ``(f) any extortion demand, ransom payment ...``. In this corpus the lettered lists are exclusion lists, so a
    coverage verdict resting on one is the signature of the drill's failure: the item was read without the words that
    make it an exclusion. It also flags the *correct* write-back answers that legitimately lean on a list item (Week 8's
    right-answer-wrong-path case), which is acceptable: a shortlist may contain decoys, it must not miss the target.

``covered_without_citation``
    A coverage verdict with nothing cited (should be impossible once citation verification runs, so a hit is a bug
    signal worth seeing).

Flags are stored as ``output.suspect`` (a list) and filterable with ``python -m app.logquery --suspect <flag>|any``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

__all__ = ["FLAGS", "suspect_flags"]

FLAGS = ("covered_quotes_list_item", "covered_without_citation")

#: A list item *quoted as the start of something*: at the start of the text, or after a colon/semicolon/newline.
#: A cross-reference inside a sentence ("see paragraph (b) above") is deliberately not matched.
_LIST_ITEM_RE = re.compile(r"(?:^|[:;]\s+|\n)\((?:[a-z]|[ivx]+|\d+)\)\s+\S")
_COVER_DECISIONS = frozenset({"covered", "partially_covered"})


def suspect_flags(decision: str, rationale: str, citations: Sequence[str]) -> list[str]:
    """The flags that apply to one answer; ``[]`` for an answer nothing about which needs a second look."""
    flags: list[str] = []
    if decision in _COVER_DECISIONS:
        if _LIST_ITEM_RE.search(rationale or ""):
            flags.append("covered_quotes_list_item")
        if not citations:
            flags.append("covered_without_citation")
    return flags

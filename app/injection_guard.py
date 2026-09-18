"""Week 8: defends the claims agent's ``search_policy`` tool against indirect
prompt injection -- a document sitting in the corpus that talks to the model
instead of describing a policy.

The threat this closes: retrieval has no notion of trust. Anyone who can get
a file ingested (a compromised vendor upload, a malicious "internal note"
mixed into a genuine endorsement pack) gets their text placed, verbatim, into
the same numbered-passage context the agent reasons over and cites from nex to
real policy wording. Citation verification (``app/claims.py``) only checks
that a cited passage *exists* in what was retrieved -- it was never designed
to ask whether that passage is trying to instruct the reader rather than
inform it, so a passage written as an instruction sails through it untouched.

The fix lives at the tool boundary, not in the prompt: ``quarantine()`` runs
on every ``search_policy`` result before it is numbered and handed back to the
agent, so a flagged passage's text never enters the scratchpad the model reads
on the next turn (least privilege -- the tool simply declines to hand out
content it doesn't trust) and can never end up in a citation. This is a
generic, content-shape heuristic (instruction-to-the-reader phrasing that
formal policy wording does not use), not a rule about any specific attack
payload -- see ``tests/test_injection_guard.py`` for both the catch and the
false-positive check against the real corpus.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from app.models import RetrievedChunk

__all__ = ["INJECTION_PATTERNS", "InjectionFlag", "quarantine", "scan_chunk"]

#: (human-readable reason, pattern). Each targets phrasing that instructs the
#: *reader* to change its own behaviour -- override, skip a check, act on
#: someone's say-so -- which formal policy/endorsement wording has no reason
#: to ever contain. Legitimate exclusion wording is checked against these in
#: tests/test_injection_guard.py::test_real_corpus_produces_no_flags.
INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "tells the reader to ignore or disregard prior instructions",
        re.compile(r"\bignore (?:all|any|the)?\s*(?:previous|prior|above|preceding)\b|\bdisregard\b", re.IGNORECASE),
    ),
    (
        "claims no further review or verification is needed",
        re.compile(
            r"\bno further\s+(?:human\s+or\s+automated\s+)?(?:review|verification|check(?:ing)?)\s+(?:is\s+)?required\b",
            re.IGNORECASE,
        ),
    ),
    (
        "instructs skipping a specific check",
        re.compile(r"\bdo not check\b|\bwithout (?:any\s+)?(?:further\s+)?(?:review|verification|checking)\b", re.IGNORECASE),
    ),
    (
        "tells the reader to apply a decision directly",
        re.compile(r"\bapply this (?:decision|confirmation|coverage decision)\s+directly\b", re.IGNORECASE),
    ),
    (
        "addresses the agent/model/system directly with an imperative",
        re.compile(r"\b(?:agent|model|system|assistant|ai)\b[^.]{0,40}\b(?:must|should|shall)\s+disregard\b", re.IGNORECASE),
    ),
)


@dataclass(frozen=True, slots=True)
class InjectionFlag:
    """A retrieved passage withheld from the agent's context, and why."""

    chunk_id: str
    filename: str
    page: int
    reason: str


def scan_chunk(chunk: RetrievedChunk) -> list[str]:
    """Reasons ``chunk`` looks like it is instructing the reader, or ``[]``."""
    haystack = f"{chunk.section}\n{chunk.text}"
    return [reason for reason, pattern in INJECTION_PATTERNS if pattern.search(haystack)]


def quarantine(chunks: Sequence[RetrievedChunk]) -> tuple[list[RetrievedChunk], list[InjectionFlag]]:
    """Split ``chunks`` into what is safe to hand the agent and what was withheld.

    The withheld chunk's own text is deliberately not repeated in the
    returned ``InjectionFlag`` -- only its provenance (filename, page) and the
    reason -- so a caller that surfaces flags in an observation string does
    not paste the suspect instruction straight back into the model's context
    under a different label.
    """
    clean: list[RetrievedChunk] = []
    flags: list[InjectionFlag] = []
    for chunk in chunks:
        reasons = scan_chunk(chunk)
        if reasons:
            flags.append(InjectionFlag(chunk.chunk_id, chunk.filename, chunk.page, "; ".join(reasons)))
        else:
            clean.append(chunk)
    return clean, flags

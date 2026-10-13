"""Week 12: the compliance rule, as code -- nothing automated may state a coverage decision.

The client's regulator found that an automated message told a claimant their loss was covered. The rule that came out of
it is one sentence: the system *surfaces wording and facts and refers to an adjuster*; it never says "covered". A prompt
that asks the model not to is a request. This module is the enforcement, applied in three places that do not depend on
the model behaving:

1. **At the MCP boundary** -- ``refer_to_adjuster`` refuses notes that state a decision (a tool error the agent must
   recover from), so the referral queue cannot contain one.
2. **At the output** -- every system-authored string on a triage card is scanned before the card leaves
   :mod:`app.triage`; a violating string is *replaced* by a neutral sentence and the violation is recorded on the card,
   never silently passed through.
3. **In the eval suite** -- the same scan is an assertion on every case (``tests/`` and ``scripts/w12_run_suite.py``),
   so a regression in (1) or (2) is a red case, not a hope.

What is exempt: **verbatim wording**. A policy may itself contain a sentence with "covered" in it; the adjuster must see
the document's words exactly. Such text is passed as ``quotes=`` (the retrieved passages), and any span of the scanned
string that is an exact substring of a quote is set aside before the scan. The card keeps wording excerpts in their own
field (``wording``), not in its prose.

What this is *not*: a proof. A regular expression cannot show that a free-text sentence implies no decision. So the card
is mostly **not free text** -- its facts are fields filled from tool results by code -- and the only model-written string
on it (``agent_note``) is scanned and dropped if it trips a rule. The list below is deliberately aggressive: a false
positive costs a templated sentence; a false negative costs a regulatory finding.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["Violation", "NEUTRAL_SENTENCE", "scan", "scan_mapping", "violations"]

#: What replaces a string that tripped a rule.
NEUTRAL_SENTENCE = "Referred to an adjuster; the wording and facts above are for their decision."

#: (rule id, neutral description, pattern). The description is what appears in tool errors, cards and logs, so it must not
#: itself contain the words it forbids: an audit trail that quotes "covered" would be one more place the system says it.
_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("coverage_status_word", "uses the coverage-status word", re.compile(r"\b(?:un|non-)?covered\b", re.IGNORECASE)),
    (
        "cover_applies",
        "states that cover does or does not apply",
        re.compile(
            r"\b(?:cover(?:age)?|indemnity|policy)\s+(?:applies|does\s+not\s+apply|is\s+(?:confirmed|denied|excluded|available|unavailable|in\s+place)|exists|responds)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "claim_outcome",
        "states an outcome for the claim or loss",
        re.compile(
            r"\b(?:claim|loss|damage|incident|event)\s+(?:is|was|will\s+be|has\s+been|should\s+be|can\s+be)\s+"
            r"(?:accepted|approved|declined|denied|rejected|repudiated|paid|payable|valid|invalid|excluded|eligible)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "within_or_outside_cover",
        "states that something is or is not within cover",
        re.compile(r"\b(?:is|are|was|were)\s+(?:not\s+)?(?:excluded|insured|indemnified|payable|eligible)\b", re.IGNORECASE),
    ),
    (
        "exclusion_verdict",
        "states whether an exclusion applies to this loss",
        re.compile(r"\bexclusion\s+(?:\S+\s+){0,3}(?:applies|does\s+not\s+apply|is\s+triggered|is\s+not\s+triggered|bites|is\s+engaged)\b", re.IGNORECASE),
    ),
    (
        "payment_promise",
        "promises or rules out payment",
        re.compile(
            r"\b(?:will|shall|would)\s+(?:not\s+)?(?:be\s+)?(?:pay|paid|indemnif\w+|settle\w*|reimburs\w+|honou?r\w*)\b|\bno\s+(?:payout|payment|indemnity)\b",
            re.IGNORECASE,
        ),
    ),
    ("entitlement", "states an entitlement", re.compile(r"\b(?:entitled\s+to|eligible\s+for)\b", re.IGNORECASE)),
)


@dataclass(frozen=True, slots=True)
class Violation:
    """A rule a string tripped. Only ``rule`` and ``why`` are ever written down; ``excerpt`` is for the test that needs it."""

    rule: str
    why: str
    excerpt: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "why": self.why}


_CLAUSE_SPLIT_RE = re.compile(r"(?<=[.;:])\s+")
_MIN_QUOTED_CLAUSE = 25


def _strip_quotes(text: str, quotes: Sequence[str]) -> str:
    """Remove each clause of ``text`` that appears verbatim in one of ``quotes``.

    A "clause" is a quote split at ``. ; :``; only clauses of at least 25 characters count, so a one-word overlap with a
    quote ("covered") is not a quotation. A quotation of a fragment shorter than a clause is scanned like any other text --
    the safe direction to be wrong in.
    """
    out = " ".join(text.split())
    for quote in quotes:
        for clause in _CLAUSE_SPLIT_RE.split(" ".join(quote.split())):
            if len(clause) >= _MIN_QUOTED_CLAUSE and clause in out:
                out = out.replace(clause, " ")
    return out


def violations(text: str, *, quotes: Sequence[str] = ()) -> list[Violation]:
    """Every rule ``text`` trips, outside any verbatim quotation from ``quotes``."""
    scanned = _strip_quotes(text, quotes) if quotes else text
    found: list[Violation] = []
    for rule, why, pattern in _RULES:
        match = pattern.search(scanned)
        if match:
            start = max(0, match.start() - 20)
            found.append(Violation(rule, why, scanned[start : match.end() + 20].strip()))
    return found


def scan(text: str, *, quotes: Sequence[str] = ()) -> bool:
    """``True`` when ``text`` is clean."""
    return not violations(text, quotes=quotes)


def _strings(node: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, Mapping):
        for key, value in node.items():
            yield from _strings(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _strings(value, f"{path}[{index}]")


def scan_mapping(payload: Mapping[str, Any], *, quotes: Sequence[str] = (), skip: Iterable[str] = ()) -> list[tuple[str, Violation]]:
    """Scan every string in a nested structure; ``skip`` names top-level keys that hold verbatim wording."""
    skipped = set(skip)
    bad: list[tuple[str, Violation]] = []
    for path, text in _strings(payload):
        if path.split(".")[0].split("[")[0] in skipped:
            continue
        for violation in violations(text, quotes=quotes):
            bad.append((path, violation))
    return bad

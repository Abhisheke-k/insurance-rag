"""Week 11: PII redaction that runs *before* anything is written to a log.

The Week 11 brief names the mistake: logging the full adjuster note verbatim
"to make the trace searchable", which drags claimant PII across the line into a
log a whole squad can read. "We will clean it later" never happens, so there is
no cleaning step here to forget: :class:`Redactor` is applied at the single
place a trace record is built (``app/observability.py``), and anything else
that wants a log-safe string -- or a tool argument safe to send to an MCP server
another squad runs -- goes through the same function.

What it does with each kind of identifier:

* **claim and policy numbers** are *pseudonymised*, not masked: a keyed hash
  (``clm_3fa91c2e``) so two traces of the same claim still join, and a support
  engineer holding the real number can find them (``Redactor.pseudonym``), but a
  reader of the log cannot recover it.
* **names, e-mail, phone, postcode, street address** are masked.
* **health detail** -- adjuster notes carry injury descriptions -- is masked at
  the sentence level, because there is no pattern for "broken wrist" that would
  not also eat legitimate policy vocabulary.

It is pattern-based and says so: it is tuned for UK/India-style notes and this
codebase's synthetic data, not a substitute for an NER model or a DLP service on
real claimant text. The honest unit of success is the test list in
``tests/test_observability.py`` plus the end-to-end "no PII on the wire or in the
log" test in ``tests/test_triage.py``, not a promise that nothing slips through.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field

__all__ = ["DEFAULT_SALT_ENV", "RedactionResult", "Redactor", "get_redactor"]

DEFAULT_SALT_ENV = "PII_PSEUDONYM_SALT"

_CLAIM_RE = re.compile(r"\bCLM-\d{4}-\d{5}\b")
#: Three letters, year, five digits: GML-2024-88213. Deliberately narrower than "any CODE-YYYY-N"
#: so endorsement references (END-2024-0417) and exclusion codes (E-17) are not swallowed.
_POLICY_RE = re.compile(r"\b[A-Z]{3}-\d{4}-\d{5}\b")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_PHONE_RE = re.compile(r"(?<![\w-])\+?\d[\d\s\-().]{8,}\d(?![\w-])")
_POSTCODE_RE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b")
_STREET_RE = re.compile(
    r"\b\d{1,4}[A-Za-z]?\s+(?:[A-Z][a-z']+\s+){1,3}"
    r"(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Close|Drive|Dr|Way|Crescent|Place|Court|Gardens|Terrace|Nagar|Colony)\b"
)
_TITLED_NAME_RE = re.compile(r"\b(?:Mr|Mrs|Ms|Mx|Miss|Dr|Prof)\.?\s+[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?(?:\s+[A-Z][a-z]+)?\b")
#: "claimant John Smith", "insured Priya Rao": a role word followed by two capitalised words.
_ROLE_NAME_RE = re.compile(
    # The role word is case-insensitive; the NAME must be capitalised ("claimant broke her" is not a name).
    r"\b(?P<role>(?i:claimant|insured|policyholder|reported by|caller|driver|tenant|owner|witness))\s+"
    r"(?P<name>[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?\s+[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?)\b",
)
_HEALTH_RE = re.compile(
    r"\b(?:injur\w*|hospital\w*|fractur\w*|whiplash|concussion|bereav\w*|fatal\w*|died|deceased|"
    r"surgery|surgical|diagnos\w*|medical|ambulance|a&e|paramedic\w*|"
    # body parts: they do not appear in property-damage wording, so masking the sentence costs nothing
    r"wrist|ankle|collarbone|spine|shoulder|knee|skull|ribs?|"
    r"broke(?:n)?\s+(?:his|her|their|my|a|an|the)?\s*(?:arm|leg|bone|hip|finger|toe|nose|jaw)s?)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[,;])\s+")


@dataclass(frozen=True, slots=True)
class RedactionResult:
    text: str
    #: Which kinds were found (deduplicated, in first-seen order): evidence for the log that
    #: redaction ran, without recording what it removed.
    kinds: list[str] = field(default_factory=list)


class Redactor:
    """Mask or pseudonymise PII in free text. Stateless apart from the pseudonym salt."""

    def __init__(self, salt: str = "") -> None:
        self._salt = salt

    def pseudonym(self, kind: str, value: str) -> str:
        """Stable, keyed pseudonym for an identifier, e.g. ``clm_3fa91c2e``.

        The same call with the same salt always gives the same answer, so an engineer who
        knows a claim number can compute what to search the logs for.
        """
        digest = hashlib.sha256(f"{self._salt}:{kind}:{value.strip().upper()}".encode()).hexdigest()
        prefix = {"claim_number": "clm", "policy_number": "pol", "user": "usr"}.get(kind, kind[:3])
        return f"{prefix}_{digest[:8]}"

    def redact(self, text: str, *, keep_identifiers: bool = False) -> RedactionResult:
        """Mask PII in ``text``.

        ``keep_identifiers=True`` leaves claim and policy numbers readable. That is for text an agent must act on (it has to
        pass a claim number to a lookup tool); anything *logged* keeps the default and gets pseudonyms instead.
        """
        kinds: list[str] = []

        def note(kind: str) -> None:
            if kind not in kinds:
                kinds.append(kind)

        def sub(pattern: re.Pattern[str], kind: str, replace) -> None:  # noqa: ANN001
            nonlocal text

            def on_match(match: re.Match[str]) -> str:
                note(kind)
                return replace(match)

            text = pattern.sub(on_match, text)

        # Identifiers first, so the phone pattern cannot see the digits inside a claim number.
        if not keep_identifiers:
            sub(_CLAIM_RE, "claim_number", lambda m: self.pseudonym("claim_number", m.group(0)))
            sub(_POLICY_RE, "policy_number", lambda m: self.pseudonym("policy_number", m.group(0)))
        sub(_EMAIL_RE, "email", lambda _m: "[EMAIL REDACTED]")
        sub(_STREET_RE, "address", lambda _m: "[ADDRESS REDACTED]")
        sub(_POSTCODE_RE, "postcode", lambda _m: "[POSTCODE REDACTED]")
        sub(_TITLED_NAME_RE, "name", lambda _m: "[NAME REDACTED]")
        sub(_ROLE_NAME_RE, "name", lambda m: f"{m.group('role')} [NAME REDACTED]")
        text = _mask_phones(text, note)
        text = _mask_health(text, note)
        return RedactionResult(text=text, kinds=kinds)


def _mask_phones(text: str, note) -> str:  # noqa: ANN001
    def replace(match: re.Match[str]) -> str:
        digits = sum(ch.isdigit() for ch in match.group(0))
        if digits < 10:  # an ISO date or an amount, not a phone number
            return match.group(0)
        note("phone")
        return "[PHONE REDACTED]"

    return _PHONE_RE.sub(replace, text)


def _mask_health(text: str, note) -> str:  # noqa: ANN001
    """Mask health detail clause by clause.

    A whole sentence used to go, which also removed whatever else it said: "...flooded for the second time, 30 days after the
    earlier flood, and that she suffered a broken collarbone" lost the 30 days -- a triage fact -- along with the injury
    (found by Week 12 case X02). Splitting at commas and semicolons keeps the clauses that carry no health vocabulary.
    """
    out: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if not _HEALTH_RE.search(sentence):
            out.append(sentence)
            continue
        note("health")
        clauses = _CLAUSE_SPLIT_RE.split(sentence)
        masked = ["[HEALTH DETAIL REDACTED]" if _HEALTH_RE.search(clause) else clause for clause in clauses]
        # adjacent masked clauses collapse into one marker
        collapsed: list[str] = []
        for clause in masked:
            if clause == "[HEALTH DETAIL REDACTED]" and collapsed and collapsed[-1] == clause:
                continue
            collapsed.append(clause)
        out.append(" ".join(collapsed))
    return " ".join(out)


def get_redactor() -> Redactor:
    """The process redactor; the pseudonym salt comes from ``PII_PSEUDONYM_SALT``.

    The fallback salt is for local development only: with it, anyone can recompute a
    pseudonym from a guessed claim number. Set the variable in any shared environment.
    """
    return Redactor(os.environ.get(DEFAULT_SALT_ENV, "dev-only-salt"))

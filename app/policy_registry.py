"""Week 12: which policy does this claim sit on, and was it in force on the loss date?

The adjusters' first twenty minutes (the brief): find the policy, then check it was live on the day of loss. Both are
ordinary lookups -- there is nothing for a model to decide -- so they are plain code over a registry, exposed to the agent as
two tools (``lookup_policy``, ``coverage_in_force``) and never as text for it to remember.

The registry is **synthetic** (``data/policy_registry.json``): five invented policies and eleven claims, built so every
branch of the in-force check has an example, with the corpus's own policy (GML-2024-88213, period 1 April 2024 - 31 March
2025, both dates inclusive) as the live one. It holds no personal data by construction -- a policyholder is an opaque
``PH-nnnn`` reference -- so nothing a tool returns can carry a claimant's name or address, whatever the caller asks.

Date rules (stated once, tested at the boundaries):

* the period is a closed interval ``[period_start, period_end]``;
* ``cancelled_from`` is the first day cover no longer applies: a loss on the day before is in force, on the day itself is not;
* dates are whole days -- a loss at 23:50 on 31 March and at 00:10 on 1 April are not distinguished by time of day;
* an unknown policy is *not known to be out of force*: the answer is ``in_force = None`` and a reason, never ``False``,
  because "not in our registry" is a data gap to hand to a person, not a finding about the policy.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

__all__ = [
    "CLAIM_NUMBER_RE",
    "POLICY_NUMBER_RE",
    "InForce",
    "PolicyLookup",
    "PolicyRecord",
    "PolicyRegistry",
    "default_registry_path",
]

CLAIM_NUMBER_RE = re.compile(r"\bCLM-\d{4}-\d{5}\b")
#: Three letters, year, five digits -- but not a claim number, which has the same shape with the prefix CLM.
POLICY_NUMBER_RE = re.compile(r"\b(?!CLM-)[A-Z]{3}-\d{4}-\d{5}\b")

ReasonCode = Literal["in_force", "before_inception", "after_expiry", "cancelled", "unknown_policy"]


def default_registry_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "policy_registry.json"


@dataclass(frozen=True, slots=True)
class PolicyRecord:
    policy_number: str
    product: str
    period_start: date
    period_end: date
    policyholder_ref: str
    #: The wording this policy was issued on -- the file names of the ingested documents. Empty = none on file.
    wording_documents: tuple[str, ...] = ()
    cancelled_from: date | None = None
    cancellation_reason: str | None = None


@dataclass(frozen=True, slots=True)
class InForce:
    """The answer to "was this policy in force on that day?" -- with the reason, so a person can see why."""

    policy_number: str
    loss_date: date
    in_force: bool | None
    reason_code: ReasonCode
    detail: str
    period_start: date | None = None
    period_end: date | None = None
    cancelled_from: date | None = None


@dataclass(frozen=True, slots=True)
class PolicyLookup:
    """What a reference (a claim number or a policy number) resolved to."""

    reference: str
    kind: Literal["claim", "policy", "unknown"]
    policy: PolicyRecord | None = None
    claim_number: str | None = None


class PolicyRegistry:
    def __init__(self, policies: list[PolicyRecord], claims: dict[str, str]) -> None:
        self._policies = {p.policy_number: p for p in policies}
        self._claims = dict(claims)

    @classmethod
    def from_file(cls, path: Path | None = None) -> PolicyRegistry:
        payload = json.loads((path or default_registry_path()).read_text(encoding="utf-8"))
        policies = [
            PolicyRecord(
                policy_number=p["policy_number"],
                product=p["product"],
                period_start=date.fromisoformat(p["period_start"]),
                period_end=date.fromisoformat(p["period_end"]),
                policyholder_ref=p["policyholder_ref"],
                wording_documents=tuple(p.get("wording_documents", [])),
                cancelled_from=date.fromisoformat(p["cancelled_from"]) if p.get("cancelled_from") else None,
                cancellation_reason=p.get("cancellation_reason"),
            )
            for p in payload["policies"]
        ]
        claims = {c["claim_number"]: c["policy_number"] for c in payload["claims"]}
        missing = sorted({pn for pn in claims.values() if pn not in {p.policy_number for p in policies}})
        if missing:
            raise ValueError(f"claims point at policies that are not in the registry: {missing}")
        return cls(policies, claims)

    def policy(self, policy_number: str) -> PolicyRecord | None:
        return self._policies.get(policy_number.strip().upper())

    def lookup(self, reference: str) -> PolicyLookup:
        """Resolve a claim number or a policy number (case-insensitive) -- or say it is unknown."""
        ref = reference.strip().upper()
        if ref in self._claims:
            return PolicyLookup(reference, "claim", self._policies[self._claims[ref]], claim_number=ref)
        if ref in self._policies:
            return PolicyLookup(reference, "policy", self._policies[ref])
        return PolicyLookup(reference, "unknown")

    def coverage_in_force(self, policy_number: str, loss_date: date) -> InForce:
        policy = self.policy(policy_number)
        if policy is None:
            return InForce(
                policy_number, loss_date, None, "unknown_policy", "no such policy in the registry -- a data gap, not a finding"
            )
        base = {"period_start": policy.period_start, "period_end": policy.period_end, "cancelled_from": policy.cancelled_from}
        if loss_date < policy.period_start:
            return InForce(policy_number, loss_date, False, "before_inception", f"loss precedes inception ({policy.period_start})", **base)
        if loss_date > policy.period_end:
            return InForce(policy_number, loss_date, False, "after_expiry", f"loss follows expiry ({policy.period_end})", **base)
        if policy.cancelled_from is not None and loss_date >= policy.cancelled_from:
            return InForce(
                policy_number, loss_date, False, "cancelled",
                f"cancelled with effect from {policy.cancelled_from} ({policy.cancellation_reason or 'reason not recorded'})", **base,
            )
        return InForce(policy_number, loss_date, True, "in_force", f"within the period {policy.period_start} to {policy.period_end}", **base)

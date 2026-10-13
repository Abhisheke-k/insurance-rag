"""Week 12: the adjuster's referral queue -- the one thing the triage system *does* rather than reads.

``refer_to_adjuster`` is the only tool in the set that writes, so it is built to be safe to call twice and safe to read
later:

* **idempotent** -- the id is a hash of what is being referred (claim, policy, loss date, reason codes). A retry after a
  timeout, or an agent that calls it twice, gets the same id back and ``duplicate = true``; the queue gets one row.
* **append-only JSONL**, one record per line, written with ``fsync`` -- what was queued is what was queued;
* **redacted before it is written** -- free text (``facts``) goes through the Week 11 redactor, so the queue holds no
  claimant name, address or injury even if the caller put one in. Claim and policy numbers are kept in the clear *here*:
  an adjuster has to be able to find the claim, and this file is the work queue, not a log. The trace id is stored so a
  referral can be joined back to the whole request that produced it.
* **no decision language** -- ``app.compliance`` refuses notes that state one (the server turns that into a tool error).
* **a repeat is recorded, not dropped** -- the second request for the same referral writes no second referral, but it does
  append a ``duplicate_request`` event carrying *its* trace id and caller. Without it the later request's trace could not be
  joined to the referral it was answered with (found by the Week 12 suite: 8 of 38 cases repeated an earlier referral and
  failed the "one trace id from FNOL in to referral out" assertion).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.redaction import Redactor, get_redactor

__all__ = ["REASON_CODES", "ReferralQueue", "ReferralReceipt", "referral_id"]

#: Why a claim is being referred. Every claim is referred; the code says what the adjuster should look at first.
REASON_CODES: tuple[str, ...] = (
    "exclusion_arguable",  # wording in force on the loss date contains an exclusion the facts may engage
    "routine_review",  # nothing flagged: an adjuster still decides
    "not_in_force",  # the policy was not in force on the loss date
    "policy_not_found",  # reference did not resolve to a policy
    "wording_not_on_file",  # no wording on file for this policy / date
    "limits_unresolved",  # a fact needed for the deductible was not supplied
    "limits_not_stated",  # the wording on file states no figures for this loss
    "insufficient_information",  # no loss date / reference in the FNOL
    "date_ambiguous",  # several dates, loss date unclear
    "injection_suspected",  # the FNOL or a retrieved passage read like instructions
)


def referral_id(claim_number: str, policy_number: str | None, loss_date: str | None, reason_codes: list[str]) -> str:
    key = "|".join([claim_number.upper(), (policy_number or "").upper(), loss_date or "", ",".join(sorted(set(reason_codes)))])
    return "ref_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class ReferralReceipt:
    referral_id: str
    duplicate: bool
    queue: str = "adjuster-triage"


class ReferralQueue:
    def __init__(self, path: Path | None, redactor: Redactor | None = None) -> None:
        self._path = path
        self._redactor = redactor or get_redactor()
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._memory: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
        if path is not None and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._seen.add(json.loads(line)["referral_id"])

    def add(
        self,
        *,
        claim_number: str,
        policy_number: str | None,
        loss_date: str | None,
        reason_codes: list[str],
        priority: str,
        facts: str,
        wording_refs: list[str],
        trace_id: str | None,
        principal: str,
    ) -> ReferralReceipt:
        unknown = [code for code in reason_codes if code not in REASON_CODES]
        if unknown:
            raise ValueError(f"unknown reason code(s) {unknown}; use {list(REASON_CODES)}")
        rid = referral_id(claim_number, policy_number, loss_date, reason_codes)
        with self._lock:
            if rid in self._seen:
                event = {
                    "event": "duplicate_request",
                    "referral_id": rid,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "trace_id": trace_id,
                    "referred_by": principal,
                }
                self._events.append(event)
                self._append(event)
                return ReferralReceipt(rid, duplicate=True)
            record = {
                "event": "referral",
                "referral_id": rid,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "claim_number": claim_number.upper(),
                "policy_number": policy_number.upper() if policy_number else None,
                "loss_date": loss_date,
                "reason_codes": sorted(set(reason_codes)),
                "priority": priority,
                "facts": self._redactor.redact(facts).text,
                "wording_refs": [self._redactor.redact(ref).text for ref in wording_refs],
                "trace_id": trace_id,
                "referred_by": principal,
            }
            self._seen.add(rid)
            self._memory.append(record)
            self._append(record)
        return ReferralReceipt(rid, duplicate=False)

    def _append(self, record: dict[str, Any]) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def records(self) -> list[dict[str, Any]]:
        """The referrals this process queued, one per distinct referral (for tests and the CLI); the file is the durable copy."""
        with self._lock:
            return list(self._memory)

    def events(self) -> list[dict[str, Any]]:
        """Repeat requests that were answered with an existing referral."""
        with self._lock:
            return list(self._events)

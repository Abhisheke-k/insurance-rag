"""Deterministic assertions for claim summaries -- the checks that don't need an LLM.

Week 6's point: paying a model to check whether a string matches
``CLM-YYYY-NNNNN`` is throwing money at a regex's job. These four checks
cover the criteria named in the brief -- claim number format, date of loss
present and parseable, excess/deductible numeric, and an exclusion id cited
whenever a denial is stated -- and are deliberately NOT asked of the judge
(see ``coursework/w6/judge_v1.txt``): the judge is left with exactly one
criterion that genuinely needs reading comprehension to check.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.claims import CLAIM_NUMBER_RE

__all__ = ["AssertionResult", "run_assertions"]

_DATE_FORMATS = ["%d %B %Y", "%Y-%m-%d", "%d %b %Y"]


@dataclass(frozen=True, slots=True)
class AssertionResult:
    name: str
    passed: bool
    detail: str


def _assert_claim_number_format(summary: dict[str, Any]) -> AssertionResult:
    value = str(summary.get("claim_number", ""))
    passed = bool(CLAIM_NUMBER_RE.fullmatch(value))
    return AssertionResult("claim_number_format", passed, f"claim_number={value!r}")


def _assert_date_of_loss_parseable(summary: dict[str, Any]) -> AssertionResult:
    value = str(summary.get("date_of_loss", "")).strip()
    if not value:
        return AssertionResult("date_of_loss_parseable", False, "date_of_loss is empty")
    for fmt in _DATE_FORMATS:
        try:
            datetime.strptime(value, fmt)
            return AssertionResult("date_of_loss_parseable", True, f"date_of_loss={value!r} parses as {fmt}")
        except ValueError:
            continue
    return AssertionResult("date_of_loss_parseable", False, f"date_of_loss={value!r} matches no known format")


def _assert_excess_numeric(summary: dict[str, Any]) -> AssertionResult:
    value = summary.get("excess_amount")
    if value is None:
        return AssertionResult("excess_numeric_if_present", True, "excess_amount is null (not asserted)")
    passed = isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    return AssertionResult("excess_numeric_if_present", passed, f"excess_amount={value!r}")


def _assert_exclusion_id_when_denied(summary: dict[str, Any]) -> AssertionResult:
    decision = summary.get("coverage_decision")
    if decision != "denied":
        return AssertionResult("exclusion_id_when_denied", True, f"coverage_decision={decision!r} (not asserted)")
    has_id = bool(summary.get("cited_exclusion_id"))
    return AssertionResult("exclusion_id_when_denied", has_id, f"cited_exclusion_id={summary.get('cited_exclusion_id')!r}")


def run_assertions(summary: dict[str, Any]) -> list[AssertionResult]:
    return [
        _assert_claim_number_format(summary),
        _assert_date_of_loss_parseable(summary),
        _assert_excess_numeric(summary),
        _assert_exclusion_id_when_denied(summary),
    ]

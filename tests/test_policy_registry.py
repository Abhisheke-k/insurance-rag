"""Week 12: finding the policy, and the in-force check -- the boundary days are where adjusters (and code) go wrong."""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.policy_registry import PolicyRegistry, default_registry_path


@pytest.fixture(scope="module")
def registry() -> PolicyRegistry:
    return PolicyRegistry.from_file()


def test_a_claim_number_resolves_to_its_policy_and_a_policy_number_to_itself(registry: PolicyRegistry):
    by_claim = registry.lookup("CLM-2024-00417")
    assert (by_claim.kind, by_claim.policy.policy_number, by_claim.claim_number) == ("claim", "GML-2024-88213", "CLM-2024-00417")
    by_policy = registry.lookup("gml-2024-88213")  # case-insensitive
    assert (by_policy.kind, by_policy.policy.policy_number) == ("policy", "GML-2024-88213")


def test_an_unknown_reference_is_unknown_not_an_error_and_not_a_guess(registry: PolicyRegistry):
    result = registry.lookup("CLM-2031-99999")
    assert (result.kind, result.policy) == ("unknown", None)


@pytest.mark.parametrize(
    "policy, day, in_force, reason",
    [
        ("GML-2024-88213", date(2024, 3, 31), False, "before_inception"),  # the day before inception
        ("GML-2024-88213", date(2024, 4, 1), True, "in_force"),  # inception day: inclusive
        ("GML-2024-88213", date(2025, 3, 31), True, "in_force"),  # last day: inclusive
        ("GML-2024-88213", date(2025, 4, 1), False, "after_expiry"),
        ("GML-2024-88214", date(2024, 9, 14), True, "in_force"),  # the day before cancellation takes effect
        ("GML-2024-88214", date(2024, 9, 15), False, "cancelled"),  # cancelled_from is the first day without cover
        ("GML-2024-88214", date(2025, 3, 31), False, "cancelled"),
        ("GML-2024-90551", date(2024, 6, 30), False, "before_inception"),  # a mid-year policy
        ("GML-2024-90551", date(2024, 7, 1), True, "in_force"),
        ("GML-2023-61450", date(2024, 3, 31), True, "in_force"),
    ],
)
def test_coverage_in_force_at_every_boundary(registry: PolicyRegistry, policy: str, day: date, in_force: bool, reason: str):
    result = registry.coverage_in_force(policy, day)
    assert (result.in_force, result.reason_code) == (in_force, reason)


def test_an_unknown_policy_is_a_data_gap_not_a_finding_of_no_cover(registry: PolicyRegistry):
    result = registry.coverage_in_force("GML-2099-00001", date(2024, 8, 11))
    assert result.in_force is None and result.reason_code == "unknown_policy"


def test_the_answer_carries_the_reason_a_person_needs(registry: PolicyRegistry):
    cancelled = registry.coverage_in_force("GML-2024-88214", date(2024, 10, 2))
    assert "2024-09-15" in cancelled.detail and "non-payment" in cancelled.detail


def test_the_registry_holds_no_personal_data():
    """It is the one place the MCP server reads policyholder facts from, so it must have nothing to leak."""
    raw = default_registry_path().read_text(encoding="utf-8")
    payload = json.loads(raw)
    allowed = {"policy_number", "product", "period_start", "period_end", "policyholder_ref", "wording_documents", "cancelled_from", "cancellation_reason"}
    assert all(set(p) <= allowed for p in payload["policies"])
    assert all(p["policyholder_ref"].startswith("PH-") for p in payload["policies"])
    assert {k for c in payload["claims"] for k in c} == {"claim_number", "policy_number"}


def test_a_claim_pointing_at_a_missing_policy_is_rejected_at_load(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"policies": [], "claims": [{"claim_number": "CLM-2024-00001", "policy_number": "GML-2024-00001"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="not in the registry"):
        PolicyRegistry.from_file(path)

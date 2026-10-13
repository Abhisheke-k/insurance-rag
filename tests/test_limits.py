"""Week 12: deductible, sub-limit, policy limit -- three numbers, date-aware, never a guess."""

from __future__ import annotations

import re
from datetime import date

import pytest

from app.limits import LimitsTable
from scripts.make_sample_pdf import CONTENT

PRODUCT = "commercial-property"


@pytest.fixture(scope="module")
def table() -> LimitsTable:
    return LimitsTable.from_file()


def amounts(result):
    return result.deductible.amount, result.sub_limit.amount, result.policy_limit.amount


def test_three_distinct_numbers_for_a_flood_in_a_flood_zone(table: LimitsTable):
    result = table.resolve(PRODUCT, date(2024, 8, 11), "flood", in_flood_zone=True)
    assert amounts(result) == (250_000, 5_000_000, 40_000_000)
    assert len({*amounts(result)}) == 3
    assert result.deductible.basis == "each and every loss"
    assert result.sub_limit.basis == "annual aggregate"
    assert result.policy_limit.basis == "any one occurrence"
    assert result.unresolved == ()


def test_outside_a_flood_zone_the_standard_deductible_applies(table: LimitsTable):
    assert table.resolve(PRODUCT, date(2024, 8, 11), "flood", in_flood_zone=False).deductible.amount == 50_000


def test_the_flood_sub_limit_changes_on_the_day_the_endorsement_takes_effect(table: LimitsTable):
    before = table.resolve(PRODUCT, date(2024, 5, 31), "flood", in_flood_zone=False)
    after = table.resolve(PRODUCT, date(2024, 6, 1), "flood", in_flood_zone=False)
    assert (before.sub_limit.amount, after.sub_limit.amount) == (2_500_000, 5_000_000)


def test_a_missing_fact_is_reported_not_guessed(table: LimitsTable):
    result = table.resolve(PRODUCT, date(2024, 8, 11), "flood", in_flood_zone=None)
    assert result.deductible.amount is None and result.unresolved == ("in_flood_zone",)
    assert sorted(c.amount for c in result.deductible.candidates) == [50_000, 250_000]
    assert result.sub_limit.amount == 5_000_000  # what does not depend on the missing fact is still answered


def test_no_figure_is_invented_where_the_wording_states_none(table: LimitsTable):
    early = table.resolve(PRODUCT, date(2024, 5, 31), "flood", in_flood_zone=True)
    assert early.deductible.amount is None and "states no deductible" in early.deductible.note  # E1's zone deductible starts 1 June
    assert early.policy_limit.amount is None  # first stated in Endorsement No. 1
    other = table.resolve(PRODUCT, date(2024, 8, 11), "other")
    assert other.sub_limit.amount is None and other.sub_limit.stated == "not_stated"


def test_an_inferred_figure_says_it_is_inferred(table: LimitsTable):
    other = table.resolve(PRODUCT, date(2024, 8, 11), "other")
    assert other.deductible.amount == 50_000 and other.deductible.stated == "inferred"


def test_an_unknown_product_yields_nothing_rather_than_a_default(table: LimitsTable):
    assert amounts(table.resolve("motor", date(2024, 8, 11), "other")) == (None, None, None)


# --------------------------------------------------------------------------- #
# The table may not drift from the document it cites.
# --------------------------------------------------------------------------- #
def _wording() -> str:
    return " ".join(" ".join(text.split()) for _style, text in CONTENT)


KEYWORD = {"deductible": r"deductible", "sub_limit": r"sub-limit", "policy_limit": r"policy\s+limit"}


def test_every_figure_in_the_table_appears_in_the_wording_next_to_its_own_keyword(table: LimitsTable):
    wording = _wording()
    for raw in table.raw_figures(PRODUCT):
        figure = f"GBP {raw['amount']:,}"
        near = re.search(rf"{KEYWORD[raw['name']]}[^.]{{0,160}}{re.escape(figure)}|{re.escape(figure)}[^.]{{0,160}}{KEYWORD[raw['name']]}", wording, re.IGNORECASE)
        assert near, f"{raw['name']} {figure} ({raw['source']}) is not stated near its keyword in the wording"


def test_every_figure_names_a_clause_that_exists_in_the_wording(table: LimitsTable):
    for raw in table.raw_figures(PRODUCT):
        match = re.search(r"Endorsement No\. (\d+), clause (\d+)", raw["source"])
        assert match, raw["source"]
        assert f"ENDORSEMENT NO. {match.group(1)}" in " ".join(text for _s, text in CONTENT)


def test_a_policy_issued_on_other_wording_gets_no_figures_rather_than_this_products(table: LimitsTable):
    """The figures are transcribed from one document; a policy that was not issued on it must not inherit them."""
    other = table.resolve(PRODUCT, date(2024, 8, 11), "flood", in_flood_zone=True, documents=["form_ho0304_archive.pdf"])
    assert amounts(other) == (None, None, None)
    assert amounts(table.resolve(PRODUCT, date(2024, 8, 11), "flood", in_flood_zone=True, documents=["sample_endorsement_pack.pdf"])) == (250_000, 5_000_000, 40_000_000)

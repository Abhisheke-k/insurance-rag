"""Week 11 bonus: the field that would have made the first drill's find instant.

Round 1 found its trace by reading 23 'covered' answers and recognising a ransom payment as an exclusion. These tests
pin the on-write flag that does that recognition for every request, and the filter that uses it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import logquery
from app.config import Settings
from app.rag import RagService
from app.registry import DocumentRegistry
from app.suspicion import suspect_flags

RANSOM_RATIONALE = "Notes match an insured peril: (f) any extortion demand, ransom payment, cryptocurrency transfer or negotiation fee."
RANSOM_NOTES = (
    "Claim CLM-2026-85865. Ransomware incident on 04 March 2026 encrypted the insured's order management system; "
    "insured paid a ransom of GBP 304,260 in cryptocurrency to restore access. Insured asks whether the ransom payment is covered."
)


def test_a_coverage_verdict_resting_on_a_quoted_list_item_is_flagged():
    assert suspect_flags("covered", RANSOM_RATIONALE, ["doc::4"]) == ["covered_quotes_list_item"]
    assert suspect_flags("partially_covered", "(a) subsidence is excluded; the rest is paid", ["doc::1"]) == ["covered_quotes_list_item"]


@pytest.mark.parametrize(
    ("decision", "rationale", "citations"),
    [
        ("denied", RANSOM_RATIONALE, ["doc::4"]),  # a denial quoting an exclusion item is the *right* shape
        ("needs_review", RANSOM_RATIONALE, ["doc::4"]),
        ("covered", "The flood sub-limit is GBP 5,000,000 in the annual aggregate.", ["doc::1"]),
        ("covered", "As set out in paragraph (b) above, the deductible applies.", ["doc::1"]),  # a cross-reference, not a list item
        ("covered", "Covered under clause 4.2(d) as stock was raised.", ["doc::1"]),
    ],
)
def test_ordinary_answers_are_not_flagged(decision: str, rationale: str, citations: list[str]):
    assert suspect_flags(decision, rationale, citations) == []


def test_an_uncited_coverage_verdict_is_flagged():
    assert "covered_without_citation" in suspect_flags("covered", "Looks fine.", [])


# --------------------------------------------------------------------------- #
def make_service(settings: Settings, pdf: bytes, tmp_path: Path, *, carry: bool) -> tuple[RagService, Path]:
    log = tmp_path / f"trace-{carry}.jsonl"
    configured = settings.model_copy(
        update={"carry_clause_stem": carry, "retrieval_mode": "hybrid_bm25_rrf", "trace_log_path": log, "chroma_dir": tmp_path / f"s{carry}"}
    )
    service = RagService(configured, registry=DocumentRegistry(configured.registry_path))
    service.ingest(pdf, "sample_endorsement_pack.pdf")
    return service, log


def test_the_drills_real_failure_is_flagged_in_the_log_and_the_fixed_answer_is_not(settings: Settings, sample_pdf_bytes: bytes, tmp_path: Path):
    broken, broken_log = make_service(settings, sample_pdf_bytes, tmp_path, carry=False)
    fixed, fixed_log = make_service(settings, sample_pdf_bytes, tmp_path, carry=True)
    broken.summarize_claim(RANSOM_NOTES)
    fixed.summarize_claim(RANSOM_NOTES)

    before = json.loads(broken_log.read_text(encoding="utf-8").splitlines()[0])["output"]
    after = json.loads(fixed_log.read_text(encoding="utf-8").splitlines()[0])["output"]
    assert before["decision"] == "covered" and before["suspect"] == ["covered_quotes_list_item"]
    assert after["decision"] == "denied" and after["suspect"] == []


def test_the_log_query_shortlists_by_flag(settings: Settings, sample_pdf_bytes: bytes, tmp_path: Path):
    service, log = make_service(settings, sample_pdf_bytes, tmp_path, carry=False)
    service.summarize_claim(RANSOM_NOTES)
    service.summarize_claim("Claim CLM-2026-11111. Water ingress at the insured's premises on 24 May 2026; the site is NOT in a flood zone. GBP 69,705.")

    records = list(logquery.load_records([log]))
    assert len(records) == 2
    flagged = logquery.filter_traces(records, suspect="any")
    assert len(flagged) == 1 and flagged[0]["output"]["decision"] == "covered"
    assert logquery.filter_traces(records, suspect="covered_quotes_list_item") == flagged
    assert logquery.filter_traces(records, suspect="covered_without_citation") == []

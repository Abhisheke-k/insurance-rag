"""Week 11: carrying a list clause's lead-in into the chunks that continue it.

The failure (found by the support drill): the cyber exclusion's lead-in -- "this policy excludes any loss ... of the
following:" -- lives in one chunk; items (b)-(h) live in the next three. A ransom claim retrieved item (f) with no sign
that it is an exclusion and was answered "covered". These tests pin the fix on the real corpus, the guards that
keep it from attaching a lead-in where it does not belong, and the bug that made the first attempt a silent no-op.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.clause_stem import MAX_LOOKBACK, carry_clause_stems, find_stem
from app.config import Settings
from app.models import RetrievedChunk
from app.rag import RagService
from app.registry import DocumentRegistry
from scripts.w6_run_eval import case_assertions, load_cases

ROOT = Path(__file__).resolve().parent.parent
RANSOM_NOTES = (
    "Claim CLM-2026-85865. Ransomware incident on 04 March 2026 encrypted the insured's order management system; "
    "insured paid a ransom of GBP 304,260 in cryptocurrency to restore access. Insured asks whether the ransom payment is covered."
)
STEM = "2. Notwithstanding anything to the contrary, this policy excludes any loss of the following:"


def chunk(index: int, text: str, *, partial: bool = True, doc: str = "doc") -> RetrievedChunk:
    return RetrievedChunk(f"{doc}::{index:04d}", doc, "pack.pdf", text, 1, 1, "S", None, 0.5, chunk_index=index, is_partial_clause=partial)


def lookup_for(chunks: list[RetrievedChunk]):
    table = {(c.doc_id, c.chunk_index): c for c in chunks}
    return lambda doc, index: table.get((doc, index))


# --------------------------------------------------------------------------- #
# Unit behaviour and its guards
# --------------------------------------------------------------------------- #
def test_a_continuation_gets_the_lead_in_from_the_chunk_before():
    first = chunk(0, f"SECTION 2 - WORDING\n{STEM}\n(a) any Cyber Act;", partial=False)
    second = chunk(1, "(b) loss of data;\n(c) failure of any computer system;")
    out, carried = carry_clause_stems([second], lookup_for([first, second]))
    assert carried == 1
    assert out[0].text.startswith(f"[Clause lead-in] {STEM}\n[Continued] (b) loss of data;")
    assert out[0].chunk_id == second.chunk_id and out[0].score == second.score  # nothing else changes


def test_a_chunk_that_already_carries_its_own_lead_in_is_left_alone():
    own = chunk(1, f"{STEM}\n(a) any Cyber Act;\n(b) loss of data;")
    assert carry_clause_stems([own], lookup_for([chunk(0, "x", partial=False), own]))[1] == 0


def test_chunks_that_are_not_slices_of_a_list_are_untouched():
    complete = chunk(1, "(b) loss of data;", partial=False)  # a whole clause: nothing is missing
    prose = chunk(2, "This paragraph continues a long clause without any list items in it.")
    first = chunk(0, f"{STEM}\n(a) any Cyber Act;", partial=False)
    out, carried = carry_clause_stems([complete, prose], lookup_for([first, complete, prose]))
    assert carried == 0 and [c.text for c in out] == [complete.text, prose.text]


def test_a_cross_reference_to_an_item_is_not_a_list_item():
    inline = chunk(1, "as set out in paragraph (b) above, the Insured shall notify the Insurer within 30 days.")
    assert carry_clause_stems([inline], lookup_for([chunk(0, f"{STEM}\n(a) x;", partial=False), inline]))[1] == 0


def test_a_heading_between_the_lead_in_and_the_chunk_stops_the_carry():
    first = chunk(0, f"{STEM}\n(a) any Cyber Act;", partial=False)
    middle = chunk(1, "ENDORSEMENT NO. 3 - BUSINESS INTERRUPTION AMENDMENTS\nEffective from 1 October 2024.")
    later = chunk(2, "(b) an unrelated list item that belongs to some other clause;")
    assert carry_clause_stems([later], lookup_for([first, middle, later]))[1] == 0


def test_the_search_for_a_lead_in_is_bounded():
    """A lead-in exactly MAX_LOOKBACK chunks back is found; one chunk further is not."""
    stem_chunk = chunk(0, f"{STEM}\n(a) x;", partial=False)
    fillers = [chunk(i, f"more text of the same long clause, part {i}") for i in range(1, MAX_LOOKBACK)]

    at_limit = chunk(MAX_LOOKBACK, "(z) the last item;")
    assert carry_clause_stems([at_limit], lookup_for([stem_chunk, *fillers, at_limit]))[1] == 1

    beyond = chunk(MAX_LOOKBACK + 1, "(z) the last item;")
    extra = chunk(MAX_LOOKBACK, "more text of the same long clause, last part")
    assert carry_clause_stems([beyond], lookup_for([stem_chunk, *fillers, extra, beyond]))[1] == 0


def test_a_heading_in_the_overlapped_tail_does_not_block_the_carry():
    """Chunks overlap: the next chunk repeats the previous one's tail. A heading in that tail comes *after* the
    items the next chunk re-states, so it does not separate them from the lead-in (the bug that left chunk 5 of the
    real corpus without its lead-in)."""
    repeated = "(h) the deliberate act of any employee taken with intent to cause a Cyber Incident;"
    lead = chunk(0, f"{STEM}\n(a) any Cyber Act;\n{repeated}\nSECTION 3 - WRITE-BACK FOR PHYSICAL DAMAGE\n3. Subject to all other terms...", partial=False)
    follow = chunk(1, f"{repeated}\nSECTION 3 - WRITE-BACK FOR PHYSICAL DAMAGE\n3. Subject to all other terms...")
    out, carried = carry_clause_stems([follow], lookup_for([lead, follow]))
    assert carried == 1 and out[0].text.startswith("[Clause lead-in]")


def test_missing_neighbours_and_unknown_documents_are_harmless():
    orphan = chunk(3, "(d) an item whose predecessors are not in the store")
    assert carry_clause_stems([orphan], lambda doc, index: None) == ([orphan], 0)
    assert carry_clause_stems([], lambda doc, index: None) == ([], 0)


def test_find_stem_needs_the_colon_and_a_following_item():
    assert find_stem([STEM, "(a) x;"]) == (STEM, 0)
    assert find_stem(["This ends with a colon:", "and the next line is prose"]) is None
    assert find_stem([STEM]) is None
    assert find_stem(["first stem:", "(a) one;", "second stem:", "(i) two;"])[0] == "second stem:"  # the last one governs what follows


# --------------------------------------------------------------------------- #
# On the real corpus
# --------------------------------------------------------------------------- #
@pytest.fixture
def pair(settings: Settings, sample_pdf_bytes: bytes, tmp_path: Path) -> tuple[RagService, RagService]:
    services = []
    for carry in (False, True):
        configured = settings.model_copy(update={"carry_clause_stem": carry, "retrieval_mode": "hybrid_bm25_rrf", "chroma_dir": tmp_path / f"s{carry}"})
        service = RagService(configured, registry=DocumentRegistry(configured.registry_path))
        service.ingest(sample_pdf_bytes, "sample_endorsement_pack.pdf")
        services.append(service)
    return services[0], services[1]


def test_the_cyber_exclusion_items_regain_their_lead_in_and_nothing_else_changes(pair):
    off, on = pair
    all_chunks = sorted(on.store.all_chunks(), key=lambda c: c.chunk_index)
    out, carried = carry_clause_stems(all_chunks, on._lookup_chunk)  # noqa: SLF001
    changed = [c.chunk_index for before, c in zip(all_chunks, out) if before.text != c.text]
    assert carried == 3 and changed == [3, 4, 5]
    for c in out:
        if c.chunk_index in changed:
            assert "this policy excludes any loss" in c.text and c.text.startswith("[Clause lead-in]")


def test_the_drills_failure_goes_red_to_green_on_a_toggle(pair):
    """The planted ransom claim: 'covered' with the fix off (RED), not 'covered' with it on (GREEN)."""
    off, on = pair
    assert off.summarize_claim(RANSOM_NOTES).summary.coverage_decision == "covered"
    fixed = on.summarize_claim(RANSOM_NOTES).summary
    assert fixed.coverage_decision == "denied" and "ransom" in fixed.summary.lower()


def test_hybrid_retrieval_keeps_the_continuation_fields(pair):
    """The first attempt at this fix was a silent no-op because hybrid retrieval rebuilt every hit field by field
    and dropped chunk_index / is_partial_clause. The fields must survive every retrieval mode."""
    off, _ = pair
    hits = off.retrieve(RANSOM_NOTES, 5)
    assert any(h.is_partial_clause for h in hits) and all(h.chunk_index >= 0 for h in hits)
    assert {h.chunk_index for h in hits if h.is_partial_clause} - {0}


def test_dense_mode_gets_the_same_treatment(settings: Settings, sample_pdf_bytes: bytes, tmp_path: Path):
    configured = settings.model_copy(update={"retrieval_mode": "dense", "chroma_dir": tmp_path / "dense"})
    service = RagService(configured, registry=DocumentRegistry(configured.registry_path))
    service.ingest(sample_pdf_bytes, "sample_endorsement_pack.pdf")
    hits = service.retrieve(RANSOM_NOTES, 5)
    assert all(h.text.startswith("[Clause lead-in]") for h in hits if h.is_partial_clause and h.chunk_index in (3, 4, 5))


def test_rollback_is_one_setting_and_leaves_the_text_byte_identical(pair):
    off, on = pair
    raw = {c.chunk_id: c.text for c in off.store.all_chunks()}
    assert all("[Clause lead-in]" not in h.text for h in off.retrieve(RANSOM_NOTES, 5))
    assert {c.chunk_id: c.text for c in on.store.all_chunks()} == raw  # the index itself is never rewritten


def test_the_retrieval_span_says_whether_the_fix_was_active(pair, tmp_path: Path):
    from app.observability import JsonlSink

    _, on = pair
    log = tmp_path / "t.jsonl"
    on.tracer.sink = JsonlSink(log)
    on.summarize_claim(RANSOM_NOTES)
    span = next(s for s in json.loads(log.read_text(encoding="utf-8").splitlines()[0])["spans"] if s["kind"] == "retrieval")
    assert span["attrs"]["carry_clause_stem"] is True and span["attrs"]["stems_carried"] >= 1


# --------------------------------------------------------------------------- #
# The regression cases themselves
# --------------------------------------------------------------------------- #
def test_every_regression_case_reproduces_from_its_redacted_text_and_declares_its_expectation():
    cases = load_cases([ROOT / "coursework" / "w11" / "regression_cases.jsonl"])
    assert len(cases) >= 3
    for case in cases:
        assert case["source"].startswith("production_failure:") and case["must_not_decide"] == ["covered"] and case["judged"] is False
        assert "clm_" not in case["notes"] and case["notes"].count("CLM-2026-") == 1  # pseudonym replaced by a synthetic number


def test_the_case_assertion_fails_a_covered_answer_and_passes_a_denial():
    case = {"must_not_decide": ["covered"]}
    base = {"claim_number": "CLM-2026-12345", "date_of_loss": "04 March 2026", "excess_amount": None, "cited_exclusion_id": "2(f)"}
    covered = case_assertions(case, {**base, "coverage_decision": "covered"})
    denied = case_assertions(case, {**base, "coverage_decision": "denied"})
    assert ("must_not_decide", False) in [(n, p) for n, p, _ in covered]
    assert all(p for _n, p, _d in denied)
    assert ("expected_decision", False) in [(n, p) for n, p, _ in case_assertions({"expected_decision": "denied"}, {**base, "coverage_decision": "needs_review"})]

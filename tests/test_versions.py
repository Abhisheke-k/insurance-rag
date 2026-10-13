"""Week 12: the wording in force on the loss date.

The timeline below is written out *by hand* from the corpus's own text (scripts/make_sample_pdf.py); the parser has to
reproduce it from the text. A test that derived its expectations from the code under test would prove nothing, so the
truth table lives here and the parser is the thing being checked against it.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from app.config import Settings
from app.models import RetrievedChunk
from app.observability import JsonlSink
from app.rag import RagService
from app.registry import DocumentRegistry
from app.versions import Interval, VersionIndex, parse_date
from scripts.make_sample_pdf import build_form_library_pdf

#: Form HO-0304, Exclusion E-17: edition -> (in force from, to, "within N consecutive days"). Hand-copied from the archive.
EDITIONS = {
    "01-22": (None, date(2022, 5, 31), 30),
    "06-22": (date(2022, 6, 1), date(2023, 8, 31), 35),
    "09-23": (date(2023, 9, 1), date(2023, 12, 31), 45),
    "01-24": (date(2024, 1, 1), date(2025, 1, 31), 55),
    "03-24": (date(2025, 2, 1), None, 60),
}
#: Endorsement number -> effective date (None = from inception), hand-copied from each endorsement's first line.
ENDORSEMENTS = {1: date(2024, 6, 1), 2: None, 3: date(2024, 10, 1), 4: date(2025, 2, 1)}


@pytest.fixture
def service(settings: Settings, sample_pdf_bytes: bytes, tmp_path: Path) -> RagService:
    configured = settings.model_copy(update={"carry_clause_stem": False, "chroma_dir": tmp_path / "v"})
    svc = RagService(configured, registry=DocumentRegistry(configured.registry_path))
    svc.ingest(sample_pdf_bytes, "sample_endorsement_pack.pdf")
    library = tmp_path / "archive.pdf"
    build_form_library_pdf(library)
    svc.ingest(library.read_bytes(), "form_ho0304_archive.pdf")
    return svc


@pytest.fixture
def index(service: RagService) -> VersionIndex:
    return VersionIndex.build(service.store.all_chunks())


def resolved_text(service: RagService, index: VersionIndex, on: date) -> str:
    parts = []
    for chunk in service.store.all_chunks():
        narrowed, _ = index.resolve(chunk, on)
        if narrowed is not None:
            parts.append(" ".join(narrowed.text.split()))
    return " || ".join(parts)


def test_the_parser_reproduces_the_hand_written_edition_timeline(index: VersionIndex):
    parsed = {edition: (iv.start, iv.end) for (form, edition), iv in index.editions.items() if form == "HO-0304"}
    assert parsed == {edition: (start, end) for edition, (start, end, _days) in EDITIONS.items()}


def test_the_parser_reproduces_the_hand_written_endorsement_dates(index: VersionIndex):
    assert {number: when for (_, number), when in index.endorsement_effective.items()} == ENDORSEMENTS


def test_exactly_one_edition_is_in_force_on_every_day_and_it_is_the_right_one(index: VersionIndex):
    probes = {
        date(2022, 5, 31): "01-22", date(2022, 6, 1): "06-22", date(2023, 8, 31): "06-22", date(2023, 9, 1): "09-23",
        date(2023, 12, 31): "09-23", date(2024, 1, 1): "01-24", date(2025, 1, 31): "01-24", date(2025, 2, 1): "03-24", date(2026, 10, 13): "03-24",
    }
    for day, expected in probes.items():
        assert index.editions_in_force("HO-0304", day) == [expected], day


@pytest.mark.parametrize("on, edition", [(d, e) for e, (s, _end, _n) in EDITIONS.items() for d in ([s] if s else [date(2022, 1, 15)])])
def test_only_the_in_force_editions_window_survives_narrowing(service: RagService, index: VersionIndex, on: date, edition: str):
    text = resolved_text(service, index, on)
    window = EDITIONS[edition][2]
    assert f"within {window} consecutive days" in text
    for other, (_s, _e, days) in EDITIONS.items():
        if other != edition:
            assert f"within {days} consecutive days" not in text, (on, other)


def test_the_day_before_an_edition_takes_effect_still_gets_the_old_one(service: RagService, index: VersionIndex):
    assert "within 55 consecutive days" in resolved_text(service, index, date(2025, 1, 31))
    assert "within 60 consecutive days" not in resolved_text(service, index, date(2025, 1, 31))
    assert "within 60 consecutive days" in resolved_text(service, index, date(2025, 2, 1))
    assert "within 55 consecutive days" not in resolved_text(service, index, date(2025, 2, 1))


def test_an_edition_split_across_two_chunks_is_attributed_by_its_heading_not_the_chunk_label(service: RagService, index: VersionIndex):
    """Edition 09-23's paragraph straddles archive chunks 0 and 1, and chunk 1's stored section_path says 01-24."""
    chunks = {c.chunk_index: c for c in service.store.all_chunks() if c.filename.startswith("form_ho0304")}
    assert "EDITION 01-24" in chunks[1].section_path  # the trap
    in_2023 = " ".join(index.resolve(chunks[1], date(2023, 12, 15))[0].text.split())
    assert "within 45 consecutive days" in in_2023
    assert "within 55 consecutive days" not in in_2023
    in_2024 = " ".join(index.resolve(chunks[1], date(2024, 8, 11))[0].text.split())
    assert "within 55 consecutive days" in in_2024
    assert "within 45 consecutive days" not in in_2024


def test_endorsements_apply_only_from_their_effective_date(service: RagService, index: VersionIndex):
    assert "GBP 5,000,000" not in resolved_text(service, index, date(2024, 5, 31))  # Endorsement 1 not yet in force
    assert "GBP 5,000,000" in resolved_text(service, index, date(2024, 6, 1))
    assert "waiting period of 72 consecutive hours" not in resolved_text(service, index, date(2024, 9, 30))  # Endorsement 3
    assert "waiting period of 72 consecutive hours" in resolved_text(service, index, date(2024, 10, 1))
    assert "Exclusion Code E-17 does not apply where the Insured" not in resolved_text(service, index, date(2025, 1, 31))  # clause 4.4


def test_policy_level_wording_and_the_inception_endorsement_never_expire(service: RagService, index: VersionIndex):
    for day in (date(2022, 1, 15), date(2024, 8, 11), date(2026, 10, 13)):
        text = resolved_text(service, index, day)
        assert "Governing law" in text  # GENERAL CONDITIONS carry no interval
    assert "any extortion demand, ransom payment" in resolved_text(service, index, date(2024, 1, 1))  # Endorsement 2: from inception


def test_the_archive_cover_note_is_not_offered_for_a_loss_before_the_edition_it_describes_took_effect(service: RagService, index: VersionIndex):
    """It says edition 03-24 is 'currently endorsed': true only from 1 February 2025."""
    assert "edition currently endorsed" not in resolved_text(service, index, date(2024, 8, 11))
    assert "edition currently endorsed" in resolved_text(service, index, date(2025, 3, 1))


def test_tags_carry_what_a_citation_must_say(service: RagService, index: VersionIndex):
    tags = [t for c in service.store.all_chunks() for t in index.resolve(c, date(2024, 8, 11))[1] if t.edition]
    assert [(t.edition, t.effective_from, t.effective_to) for t in tags] == [("01-24", "2024-01-01", "2025-01-31")]
    e4 = [t for c in service.store.all_chunks() for t in index.resolve(c, date(2025, 3, 1))[1] if t.document.startswith("Endorsement No. 4")]
    assert e4 and e4[0].edition == "03-24" and e4[0].effective_from == "2025-02-01"


def test_without_a_loss_date_nothing_is_dropped_and_everything_is_tagged(service: RagService, index: VersionIndex):
    for chunk in service.store.all_chunks():
        narrowed, _ = index.resolve(chunk, None)
        assert narrowed is not None and narrowed.text.strip() == chunk.text.strip()


def test_a_chunk_the_index_does_not_know_is_returned_untouched():
    stranger = RetrievedChunk("x::1", "unknown-doc", "x.pdf", "some wording", 1, 1, "S", None, 0.5)
    assert VersionIndex().resolve(stranger, date(2024, 1, 1)) == (stranger, [])


def test_the_carried_clause_lead_in_survives_narrowing(service: RagService, index: VersionIndex):
    chunk = next(c for c in service.store.all_chunks() if c.chunk_index == 4 and c.filename.startswith("sample"))
    carried = replace(chunk, text="[Clause lead-in] 2. This policy excludes any loss of the following:\n[Continued] " + chunk.text)
    narrowed, _ = index.resolve(carried, date(2024, 8, 11))
    assert narrowed is not None and narrowed.text.startswith("[Clause lead-in] 2. This policy excludes")


def test_parse_date_and_interval_basics():
    assert parse_date("1  February 2025") == date(2025, 2, 1)
    assert Interval(date(2024, 1, 1), date(2024, 12, 31)).contains(date(2024, 12, 31))
    assert not Interval(date(2024, 1, 1), date(2024, 12, 31)).contains(date(2025, 1, 1))
    assert Interval().contains(date(1999, 1, 1)) and Interval(start=date(2025, 1, 1)).contains(date(2030, 1, 1))


# --------------------------------------------------------------------------- #
# Retrieval: the filter is code, and it can be switched off to reproduce the failure
# --------------------------------------------------------------------------- #
WINDOW_QUESTION = "water ingress from a burst pipe E-17 notice of loss within how many consecutive days"
WINDOW_RE = re.compile(r"within (\d+) consecutive days")


def windows(hits) -> set[str]:
    return {w for h in hits for w in WINDOW_RE.findall(" ".join(h.text.split()))}


@pytest.fixture(params=["dense", "hybrid_bm25_rrf"])
def retrieval_pair(request, settings: Settings, sample_pdf_bytes: bytes, tmp_path: Path):
    library = tmp_path / "archive.pdf"
    build_form_library_pdf(library)
    built = []
    for name, flag in (("on", True), ("off", False)):
        cfg = settings.model_copy(update={"retrieval_mode": request.param, "version_filter": flag, "chroma_dir": tmp_path / name})
        svc = RagService(cfg, registry=DocumentRegistry(cfg.registry_path))
        svc.ingest(sample_pdf_bytes, "sample_endorsement_pack.pdf")
        svc.ingest(library.read_bytes(), "form_ho0304_archive.pdf")
        built.append(svc)
    return tuple(built)


@pytest.mark.parametrize("loss_date, window, edition", [(date(2023, 12, 15), "45", "09-23"), (date(2024, 8, 11), "55", "01-24"), (date(2025, 1, 31), "55", "01-24"), (date(2025, 2, 1), "60", "03-24")])
def test_retrieval_offers_only_the_wording_in_force_on_the_loss_date(retrieval_pair, loss_date: date, window: str, edition: str):
    on, _ = retrieval_pair
    hits = on.retrieve_as_of(WINDOW_QUESTION, 4, loss_date)
    assert windows(hits) == {window}
    assert {t.edition for h in hits for t in h.versions if t.edition} == {edition}


def test_with_the_filter_off_every_edition_is_offered_which_is_the_failure_being_prevented(retrieval_pair):
    _, off = retrieval_pair
    offered = windows(off.retrieve_as_of(WINDOW_QUESTION, 4, date(2024, 8, 11)))
    assert offered >= {"45", "55", "60"}  # the model is left to guess which edition applies
    assert all(not h.versions for h in off.retrieve_as_of(WINDOW_QUESTION, 4, date(2024, 8, 11)))  # and nothing says which is which


def test_no_loss_date_means_plain_retrieval(retrieval_pair):
    on, _ = retrieval_pair
    assert [h.chunk_id for h in on.retrieve_as_of(WINDOW_QUESTION, 4, None)] == [h.chunk_id for h in on.retrieve(WINDOW_QUESTION, 4)]


def test_a_dropped_edition_does_not_leave_the_answer_short_a_passage(retrieval_pair):
    on, _ = retrieval_pair
    assert len(on.retrieve_as_of(WINDOW_QUESTION, 4, date(2024, 8, 11))) == 4


def test_the_retrieval_span_records_the_loss_date_the_editions_and_what_was_dropped(retrieval_pair, tmp_path: Path):
    on, _ = retrieval_pair
    log = tmp_path / "spans.jsonl"
    on.tracer.sink = JsonlSink(log)
    with on.tracer.trace("t"):
        on.retrieve_as_of(WINDOW_QUESTION, 4, date(2024, 8, 11))
    spans = json.loads(log.read_text(encoding="utf-8").splitlines()[0])["spans"]
    attrs = next(s for s in spans if s["kind"] == "retrieval")["attrs"]
    assert attrs["as_of"] == "2024-08-11" and attrs["version_filter"] is True
    assert attrs["editions"] == ["01-24"] and attrs["out_of_force_dropped"] >= 1
    assert WINDOW_QUESTION not in log.read_text(encoding="utf-8")  # a digest, never the query


def test_ingesting_more_wording_invalidates_the_cached_index(settings: Settings, sample_pdf_bytes: bytes, tmp_path: Path):
    cfg = settings.model_copy(update={"chroma_dir": tmp_path / "inv"})
    svc = RagService(cfg, registry=DocumentRegistry(cfg.registry_path))
    svc.ingest(sample_pdf_bytes, "sample_endorsement_pack.pdf")
    assert svc._ensure_version_index().editions_in_force("HO-0304", date(2024, 8, 11)) == []
    library = tmp_path / "archive.pdf"
    build_form_library_pdf(library)
    svc.ingest(library.read_bytes(), "form_ho0304_archive.pdf")
    assert svc._ensure_version_index().editions_in_force("HO-0304", date(2024, 8, 11)) == ["01-24"]


# --------------------------------------------------------------------------- #
# The link on a citation
# --------------------------------------------------------------------------- #
def test_a_citation_link_shows_the_wording_in_force_on_the_loss_date(service: RagService):
    from fastapi.testclient import TestClient

    from app.main import create_app

    archive_chunk = next(c for c in service.store.all_chunks() if c.filename.startswith("form_ho0304") and c.chunk_index == 1)
    with TestClient(create_app(settings=service.settings, service=service)) as http:
        on_2024 = http.get(f"/passages/{archive_chunk.chunk_id}", params={"as_of": "2024-08-11"}).json()
        on_2023 = http.get(f"/passages/{archive_chunk.chunk_id}", params={"as_of": "2023-12-15"}).json()
        assert on_2024["in_force_on_as_of"] and "within 55 consecutive days" in " ".join(on_2024["text"].split())
        assert [v["edition"] for v in on_2024["versions"]] == ["01-24"]
        assert "within 45 consecutive days" in " ".join(on_2023["text"].split()) and [v["edition"] for v in on_2023["versions"]] == ["09-23"]
        # a chunk that exists but has nothing in force that day says so, rather than returning wording that did not apply
        superseded = next(c for c in service.store.all_chunks() if c.filename.startswith("form_ho0304") and c.chunk_index == 0)
        gone = http.get(f"/passages/{superseded.chunk_id}", params={"as_of": "2024-08-11"}).json()
        assert gone["in_force_on_as_of"] is False and gone["text"] == "" and gone["versions"] == []
        assert http.get("/passages/nope::0000").status_code == 404
        assert http.get(f"/passages/{archive_chunk.chunk_id}").json()["as_of"] is None  # without a date: the chunk as stored

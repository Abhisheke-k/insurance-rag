"""Week 11: redaction on write, prompt versions that cannot drift, traces and spans, the log query.

The point of this layer is one sentence from the brief: *if it isn't logged, it didn't happen -- and if it is
logged as one blob, you can't tell which stage cost you the p99.* So the tests check the properties a support
engineer depends on at 3am: the record exists, it names the prompt version, it separates the stages, it
carries no claimant PII, and the fields a vague complaint can hang on are findable.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app import logquery
from app.config import Settings
from app.llm import LLMProvider, LLMResponse, LLMUsage, StubProvider
from app.mcp_client import McpToolClient
from app.mcp_server import PolicyCorpus, build_server
from app.models import RetrievedChunk
from app.observability import (
    JsonlSink,
    TracedProvider,
    Tracer,
    current_trace,
    make_traceparent,
    parse_traceparent,
    propagating_context,
    span,
)
from app.prompts import check_lock, load_lock, prompt_version, registered_prompts, sha256_of, version_for_system
from app.rag import RagService
from app.redaction import Redactor
from app.registry import DocumentRegistry

NOTES = (
    "Claim CLM-2024-10045. Insured Mr. Okafor of 14 Elm Park Road, Leeds LS6 2AB (07700 900123, "
    "j.okafor@example.com) reports flood damage on 15 June 2024. The claimant broke her wrist in the clean-up. "
    "GBP 180,000 claimed under GML-2024-88213."
)
PII = ["Okafor", "Elm Park", "LS6 2AB", "07700", "example.com", "wrist", "CLM-2024-10045", "GML-2024-88213"]


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
def test_redaction_masks_pii_and_keeps_what_an_investigator_needs():
    result = Redactor("salt").redact(NOTES)

    for secret in PII:
        assert secret not in result.text
    assert {"claim_number", "policy_number", "address", "postcode", "name", "phone", "email", "health"} <= set(result.kinds)
    # amounts and dates are the evidence, not the identity
    assert "GBP 180,000" in result.text and "15 June 2024" in result.text


@pytest.mark.parametrize(
    "text",
    [
        "Loss dated 2024-08-11, sub-limit GBP 5,000,000, deductible 250,000.",  # an ISO date and amounts are not phone numbers
        "Endorsement END-2024-0417 adopts Exclusion Code E-17 (Form HO-0304, edition 03-24).",
        "The claimant broke the lock on the warehouse door.",  # 'claimant broke ...' is not a name
        "Burst pipe flooded the kitchen on 12 March 2024.",
    ],
)
def test_redaction_leaves_ordinary_claim_text_alone(text: str):
    assert Redactor("salt").redact(text).text == text


def test_pseudonyms_are_stable_keyed_and_not_reversible():
    a, b = Redactor("salt-a"), Redactor("salt-b")
    assert a.pseudonym("claim_number", "CLM-2024-10045") == a.pseudonym("claim_number", "clm-2024-10045 ")
    assert a.pseudonym("claim_number", "CLM-2024-10045") != b.pseudonym("claim_number", "CLM-2024-10045")
    assert a.pseudonym("claim_number", "CLM-2024-10045") != a.pseudonym("claim_number", "CLM-2024-10046")
    assert "10045" not in a.pseudonym("claim_number", "CLM-2024-10045")


# --------------------------------------------------------------------------- #
# Prompt versions
# --------------------------------------------------------------------------- #
def test_prompt_lock_matches_the_shipped_prompts():
    """Edit a prompt's text and this fails until the lock records a new version for it."""
    assert check_lock() == []


def test_every_registered_prompt_has_a_version():
    lock = load_lock()
    assert set(registered_prompts()) <= set(lock)
    for entry in lock.values():
        assert entry["version"] and len(entry["sha256"]) == 64


def test_a_changed_prompt_is_detected_not_silently_relabelled(monkeypatch: pytest.MonkeyPatch):
    from app import claims

    monkeypatch.setattr(claims, "SYSTEM_PROMPT", claims.SYSTEM_PROMPT + "\n8. A new rule.")
    problems = check_lock()
    assert any(p.startswith("claim-summary: text changed") for p in problems)


def test_version_for_system_recognises_registered_roles_and_labels_the_rest():
    assert version_for_system(registered_prompts()["claim-summary"]) == prompt_version("claim-summary")
    assert version_for_system("ROLE: claims_clerk\nanything after the first line may vary") == prompt_version("role:claims_clerk")
    unknown = version_for_system("a prompt nobody registered")
    assert unknown.startswith("unregistered:") and unknown.endswith(sha256_of("a prompt nobody registered")[:8])


# --------------------------------------------------------------------------- #
# Trace and span mechanics
# --------------------------------------------------------------------------- #
def read_log(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_spans_nest_time_and_land_in_one_record(tmp_path: Path):
    log = tmp_path / "t.jsonl"
    tracer = Tracer(JsonlSink(log))
    with tracer.trace("claim_summary", input_text=NOTES, user="adj-17", input_type="fnol_note") as active:
        with span("outer", "stage") as outer:
            with span("inner", "retrieval", n=3):
                pass
        assert current_trace() is active
        assert outer is not None
    assert current_trace() is None

    (record,) = read_log(log)
    spans = {s["name"]: s for s in record["spans"]}
    assert spans["inner"]["parent_id"] == spans["outer"]["span_id"]
    assert spans["outer"]["parent_id"] == active.root_span_id
    assert spans["outer"]["duration_ms"] >= spans["inner"]["duration_ms"] >= 0
    assert record["user"].startswith("usr_") and record["user"] != "adj-17"
    assert len(record["trace_id"]) == 32


def test_span_helpers_are_no_ops_outside_a_trace():
    with span("anything", "tool") as current:
        assert current is None
    assert current_trace() is None


def test_an_error_is_recorded_and_still_raised(tmp_path: Path):
    log = tmp_path / "t.jsonl"
    with pytest.raises(RuntimeError, match="boom"), Tracer(JsonlSink(log)).trace("x") as _:
        with span("failing", "tool"):
            raise RuntimeError("boom")
    (record,) = read_log(log)
    assert record["error"] == "RuntimeError: boom"
    assert record["spans"][0]["status"] == "error"


def test_a_failing_sink_never_fails_the_request():
    class Broken(JsonlSink):
        def write(self, record: dict[str, Any]) -> None:
            raise OSError("disk full")

    with Tracer(Broken(Path("unused"))).trace("x") as active:
        result = "request completed"
    assert result == "request completed" and active.trace_id


def test_a_nested_trace_becomes_a_stage_span_not_a_second_record(tmp_path: Path):
    log = tmp_path / "t.jsonl"
    tracer = Tracer(JsonlSink(log))
    with tracer.trace("outer") as outer, tracer.trace("inner") as inner:
        assert inner is outer
    (record,) = read_log(log)
    assert [s["name"] for s in record["spans"]] == ["stage:inner"]


def test_input_is_stored_redacted_with_a_digest_for_exact_lookup(tmp_path: Path):
    log = tmp_path / "t.jsonl"
    with Tracer(JsonlSink(log)).trace("x", input_text=NOTES):
        pass
    (record,) = read_log(log)
    stored = json.dumps(record)
    assert all(secret not in stored for secret in PII)
    assert record["input"]["digest"] and record["input"]["chars"] == len(NOTES)
    assert "claim_number" in record["input"]["redactions"]


def test_free_text_output_is_redacted_as_it_is_set(tmp_path: Path):
    log = tmp_path / "t.jsonl"
    with Tracer(JsonlSink(log)).trace("x") as active:
        active.set_output(summary="Mr. Okafor (07700 900123) is not covered; see clause 4.3", decision="denied")
    (record,) = read_log(log)
    assert "Okafor" not in record["output"]["summary"] and "07700" not in record["output"]["summary"]
    assert record["output"]["decision"] == "denied"


def test_traceparent_round_trips_and_rejects_garbage():
    trace_id, span_id = "a" * 32, "b" * 16
    assert parse_traceparent(make_traceparent(trace_id, span_id)) == (trace_id, span_id)
    for bad in (None, "", "00-short-bad-01", "00-" + "z" * 32 + "-" + "b" * 16 + "-01", "nonsense"):
        assert parse_traceparent(bad) is None


def test_a_trace_can_continue_one_started_elsewhere(tmp_path: Path):
    log = tmp_path / "t.jsonl"
    with Tracer(JsonlSink(log)).trace("x", traceparent=make_traceparent("c" * 32, "d" * 16)) as active:
        pass
    assert active.trace_id == "c" * 32


def test_spans_from_worker_threads_join_the_same_trace(tmp_path: Path):
    log = tmp_path / "t.jsonl"
    def work() -> None:
        with span("in-thread", "tool"):
            pass

    with Tracer(JsonlSink(log)).trace("x"):
        # What the team's parallel specialists do: run on another thread inside a copy of this context.
        context = propagating_context()
        worker = threading.Thread(target=lambda: context.run(work))
        worker.start()
        worker.join()
        stray = threading.Thread(target=work)  # without the copy the thread has no trace, so its span is dropped
        stray.start()
        stray.join()
    (record,) = read_log(log)
    assert [s["name"] for s in record["spans"]] == ["in-thread"]


# --------------------------------------------------------------------------- #
# The LLM seam
# --------------------------------------------------------------------------- #
class FixedModel(LLMProvider):
    name = "fake:fixed"

    def is_configured(self) -> bool:
        return True

    def status(self) -> dict[str, Any]:
        return {"provider": "fake", "model": self.name, "configured": True, "error": None}

    def complete(self, *, system: str, user_message: str, json_schema: dict[str, Any], max_tokens: int, effort: str = "medium") -> LLMResponse:
        return LLMResponse(text="{}", model="claude-sonnet-5-5", stop_reason="end", usage=LLMUsage(1_000_000, 100_000))


def test_generation_span_records_model_prompt_version_tokens_and_cost(tmp_path: Path):
    log = tmp_path / "t.jsonl"
    provider = TracedProvider(FixedModel())
    with Tracer(JsonlSink(log)).trace("x"):
        provider.complete(system=registered_prompts()["claim-summary"], user_message="u", json_schema={}, max_tokens=10)
    (record,) = read_log(log)
    (generation,) = [s for s in record["spans"] if s["kind"] == "generation"]
    assert generation["attrs"]["prompt_version"] == prompt_version("claim-summary")
    assert generation["attrs"]["model"] == "claude-sonnet-5-5"
    assert generation["attrs"]["cost_basis"] == "api"
    assert generation["attrs"]["cost_usd"] == pytest.approx(1.0 * 2.0 + 0.1 * 10.0)  # $2/MTok in, $10/MTok out
    assert record["cost"]["usd"] == pytest.approx(3.0) and record["cost"]["llm_calls"] == 1
    assert record["prompt_versions"] == [prompt_version("claim-summary")]


def test_stub_cost_is_labelled_a_projection_not_a_charge(tmp_path: Path):
    log = tmp_path / "t.jsonl"
    provider = TracedProvider(StubProvider(), price_model="claude-opus-5")
    schema = {"properties": {"found": {}, "citations": {}}}
    with Tracer(JsonlSink(log)).trace("x"):
        provider.complete(system="s", user_message="<context>\n[1] a\nb\n</context>\nQuestion: q", json_schema=schema, max_tokens=10)
    (record,) = read_log(log)
    assert record["cost"]["basis"] == "projected@claude-opus-5"
    assert record["spans"][0]["attrs"]["tokens_estimated"] is True


def test_traced_provider_is_a_pass_through_outside_a_trace():
    provider = TracedProvider(FixedModel())
    assert provider.complete(system="s", user_message="u", json_schema={}, max_tokens=1).text == "{}"
    assert provider.name == "fake:fixed" and provider.is_configured()


# --------------------------------------------------------------------------- #
# Real requests through the service
# --------------------------------------------------------------------------- #
@pytest.fixture
def traced_service(settings: Settings, sample_pdf_bytes: bytes, tmp_path: Path) -> tuple[RagService, Path]:
    log = tmp_path / "logs" / "traces.jsonl"
    configured = settings.model_copy(update={"trace_log_path": log, "llm_provider": "stub"})
    service = RagService(configured, registry=DocumentRegistry(configured.registry_path))
    service.ingest(sample_pdf_bytes, "sample_endorsement_pack.pdf")
    return service, log


def test_a_claim_summary_trace_has_stages_cost_versions_and_no_pii(traced_service):
    service, log = traced_service
    outcome = service.summarize_claim(NOTES, user="adj-17")

    (record,) = read_log(log)
    kinds = [s["kind"] for s in record["spans"]]
    assert kinds.count("retrieval") == 1 and kinds.count("generation") == 1
    assert record["prompt_versions"] == [prompt_version("claim-summary")]
    assert record["context_ids"] == [c.chunk_id for c in outcome.retrieved]
    assert record["output"]["decision"] == outcome.summary.coverage_decision
    assert record["output"]["claim_ref"].startswith("clm_")
    assert record["cost"]["usd"] > 0 and record["cost"]["input_tokens"] > 0
    raw = log.read_text(encoding="utf-8")
    assert all(secret not in raw for secret in PII), [s for s in PII if s in raw]


def test_the_retrieval_span_records_ids_and_scores_but_never_the_query(traced_service):
    service, log = traced_service
    service.summarize_claim(NOTES)
    retrieval = next(s for s in read_log(log)[0]["spans"] if s["kind"] == "retrieval")
    assert len(retrieval["attrs"]["context_ids"]) == len(retrieval["attrs"]["scores"]) == retrieval["attrs"]["n"]
    assert "query" not in retrieval["attrs"] and retrieval["attrs"]["query_digest"]


def test_an_agent_trace_attributes_time_and_cost_per_step(traced_service):
    service, log = traced_service
    service.process_claim_with_agent(NOTES, user="adj-17")
    (record,) = read_log(log)
    by_kind: dict[str, int] = {}
    for s in record["spans"]:
        by_kind[s["kind"]] = by_kind.get(s["kind"], 0) + 1
    assert by_kind == {"generation": 3, "retrieval": 1, "tool": 1}
    assert record["cost"]["llm_calls"] == 3
    assert record["output"]["steps"] == ["search_policy", "check_assertions", "finalize"]
    assert sum(s["attrs"]["cost_usd"] for s in record["spans"] if s["kind"] == "generation") == pytest.approx(record["cost"]["usd"], abs=1e-5)


def test_ask_is_traced_too(traced_service):
    service, log = traced_service
    service.ask("What is the flood sub-limit in the annual aggregate?")
    (record,) = read_log(log)
    assert record["feature"] == "ask" and record["input_type"] == "policy_question"
    assert record["prompt_versions"] == [prompt_version("ask-answer")]


def test_tracing_off_writes_nothing_and_changes_no_behaviour(settings: Settings, sample_pdf_bytes: bytes, tmp_path: Path):
    off = settings.model_copy(update={"trace_log_path": None, "chroma_dir": tmp_path / "off"})
    service = RagService(off, registry=DocumentRegistry(off.registry_path))
    service.ingest(sample_pdf_bytes, "sample_endorsement_pack.pdf")
    assert service.summarize_claim(NOTES).summary.claim_number == "CLM-2024-10045"
    assert service.tracer.sink is None  # spans ran; there was nowhere to write them


def test_empty_trace_path_env_turns_writing_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRACE_LOG_PATH", "")
    assert Settings().trace_log_path is None
    monkeypatch.setenv("TRACE_LOG_PATH", "somewhere/else.jsonl")
    assert Settings().trace_log_path == Path("somewhere/else.jsonl")


# --------------------------------------------------------------------------- #
# The log query
# --------------------------------------------------------------------------- #
NOW = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)


def make_record(i: int, *, hours_ago: float, decision: str = "denied", user: str = "usr_aaaa", text: str = "flood", cost: float = 0.02,
                version: str = "claim-summary-v1", input_type: str = "fnol_note", exclusion: str | None = None, summary: str = "") -> dict[str, Any]:
    return {
        "schema": "trace/1",
        "trace_id": f"{i:032x}",
        "started_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
        "duration_ms": 10.0 * (i + 1),
        "feature": "claim_summary",
        "input_type": input_type,
        "user": user,
        "prompt_versions": [version],
        "input": {"redacted": text},
        "output": {"decision": decision, "cited_exclusion_id": exclusion, "summary": summary, "claim_ref": f"clm_{i:08x}"},
        "context_ids": [f"doc::{i}"],
        "cost": {"usd": cost},
    }


@pytest.fixture
def records() -> list[dict[str, Any]]:
    return [
        make_record(0, hours_ago=30, decision="covered", text="flood in zone"),                     # yesterday morning
        make_record(1, hours_ago=20, decision="covered", user="usr_bbbb", text="stock stored below ground",
                    summary="Notes match an insured peril: stock below floor", exclusion=None),     # yesterday evening
        make_record(2, hours_ago=2, decision="denied", exclusion="4.3", text="second flood"),       # today
        make_record(3, hours_ago=25, decision="covered", cost=0.5, version="claim-summary-v2", input_type="followup"),
    ]


def test_filters_combine_the_slices_a_complaint_can_name(records):
    yesterday = logquery._day_bounds("yesterday", NOW)
    found = logquery.filter_traces(records, since=yesterday[0], until=yesterday[1], decision="covered")
    assert {r["trace_id"] for r in found} == {f"{i:032x}" for i in (0, 1, 3)}
    assert [r["trace_id"] for r in logquery.filter_traces(records, decision="covered", user="usr_bbbb")] == [f"{1:032x}"]
    assert len(logquery.filter_traces(records, prompt_version="v2")) == 1
    assert len(logquery.filter_traces(records, input_type="followup")) == 1
    assert [r["trace_id"] for r in logquery.filter_traces(records, cost_gt=0.1)] == [f"{3:032x}"]
    assert [r["trace_id"] for r in logquery.filter_traces(records, slower_than=35.0)] == [f"{3:032x}"]
    assert [r["trace_id"] for r in logquery.filter_traces(records, context="doc::2")] == [f"{2:032x}"]


def test_text_search_covers_the_answer_not_only_the_input(records):
    """The brief's common mistake: searching only inputs when the complaint describes the output."""
    assert [r["trace_id"] for r in logquery.filter_traces(records, text="insured peril")] == [f"{1:032x}"]
    assert len(logquery.filter_traces(records, text="flood second")) == 1  # all words must match
    assert logquery.filter_traces(records, text="nonexistent-term") == []


def test_claim_lookup_uses_the_same_pseudonym_the_log_was_written_with():
    redactor = Redactor("s")
    record = make_record(0, hours_ago=1)
    record["output"]["claim_ref"] = redactor.pseudonym("claim_number", "CLM-2024-10045")
    assert logquery.filter_traces([record], claim_ref=redactor.pseudonym("claim_number", "CLM-2024-10045"))
    assert not logquery.filter_traces([record], claim_ref=redactor.pseudonym("claim_number", "CLM-2024-99999"))


def test_parse_when_accepts_relative_and_absolute_times():
    assert logquery.parse_when("3h", now=NOW) == NOW - timedelta(hours=3)
    assert logquery.parse_when("2d", now=NOW) == NOW - timedelta(days=2)
    assert logquery.parse_when("2026-10-12", now=NOW) == datetime(2026, 10, 12, tzinfo=timezone.utc)
    assert logquery.parse_when("now", now=NOW) == NOW


def test_cli_prints_a_table_stats_and_the_full_trace(tmp_path: Path, records, capsys: pytest.CaptureFixture[str]):
    log = tmp_path / "t.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in records) + "\n{damaged line\n\n", encoding="utf-8")  # damaged lines are skipped

    assert logquery.main(["--log", str(log), "--now", NOW.isoformat(), "--day", "yesterday", "--decision", "covered"]) == 0
    out = capsys.readouterr().out
    assert "3 match(es)" in out and "showing 3 of 4 records" in out

    assert logquery.main(["--log", str(log), "--stats"]) == 0
    assert "4 traces" in capsys.readouterr().out

    assert logquery.main(["--log", str(log), "--trace", f"{2:032x}"]) == 0
    assert json.loads(capsys.readouterr().out)["output"]["cited_exclusion_id"] == "4.3"
    assert logquery.main(["--log", str(log), "--trace", "ffffffff"]) == 1


def test_the_journal_records_every_query_for_the_drill(tmp_path: Path, records, monkeypatch: pytest.MonkeyPatch):
    log, journal = tmp_path / "t.jsonl", tmp_path / "journal.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    monkeypatch.setenv("DRILL_JOURNAL", str(journal))
    logquery.main(["--log", str(log), "--decision", "covered", "--limit", "1"])
    logquery.main(["--log", str(log), "--text", "insured peril"])
    entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert [e["matched"] for e in entries] == [3, 1] and entries[1]["trace_ids"] == [f"{1:032x}"]


# --------------------------------------------------------------------------- #
# One trace id across the MCP hop
# --------------------------------------------------------------------------- #
def test_the_trace_id_survives_the_mcp_hop(tmp_path: Path):
    legit = RetrievedChunk("d::1", "d", "pack.pdf", "4.3 Exclusion Code E-17. This policy does not cover a second Flood.", 2, 2, "4.3", None, 0.9)
    server_log = tmp_path / "mcp_spans.jsonl"
    server = build_server(PolicyCorpus(retrieve=lambda q, k: [legit], all_chunks=lambda: [legit]), trace_sink=JsonlSink(server_log))
    client_log = tmp_path / "client.jsonl"

    with McpToolClient(server) as client, Tracer(JsonlSink(client_log)).trace("claims_triage", input_text="x") as active:
        client.call_tool("search_policy", {"query": "second flood"})

    (client_record,) = read_log(client_log)
    (tool_span,) = [s for s in client_record["spans"] if s["name"] == "tool:search_policy"]
    (server_span,) = read_log(server_log)
    assert server_span["trace_id"] == active.trace_id == client_record["trace_id"]
    assert server_span["parent_id"] == tool_span["span_id"]  # the server's work hangs under the client's tool span
    assert server_span["tool"] == "search_policy" and "args" not in json.dumps(server_span).lower()
    assert tool_span["attrs"]["ok"] is True


def test_mcp_tool_span_redacts_what_it_records_of_the_arguments(tmp_path: Path):
    server = build_server(PolicyCorpus(retrieve=lambda q, k: [], all_chunks=lambda: []))
    log = tmp_path / "client.jsonl"
    with McpToolClient(server) as client, Tracer(JsonlSink(log)).trace("x", input_text="x"):
        client.call_tool("search_policy", {"query": "Mr. Okafor 07700 900123 flooded"})
    raw = log.read_text(encoding="utf-8")
    assert "Okafor" not in raw and "07700" not in raw


# --------------------------------------------------------------------------- #
# Week 12 (found by integrated case X02): health masking must not delete the triage facts beside it
# --------------------------------------------------------------------------- #
def test_health_masking_keeps_the_clauses_that_carry_no_health_detail():
    redactor = Redactor("s")
    text = "The stockroom flooded for the second time, 30 days after the earlier flood, and that she suffered a broken collarbone."
    out = redactor.redact(text).text
    assert "30 days after the earlier flood" in out and "second time" in out  # the triage facts survive
    assert "collarbone" not in out and "[HEALTH DETAIL REDACTED]" in out  # the injury does not


def test_a_sentence_that_is_all_health_detail_is_still_masked_whole():
    out = Redactor("s").redact("She fractured her wrist and was taken to hospital.").text
    assert "wrist" not in out and "hospital" not in out


def test_adjacent_masked_clauses_collapse_into_one_marker():
    out = Redactor("s").redact("He broke his arm, hurt his knee, and the yard flooded.").text
    assert out.count("[HEALTH DETAIL REDACTED]") == 1 and "the yard flooded" in out

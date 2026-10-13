"""Week 12: the integrated suite and the reproducibility tooling, run as ordinary tests.

``scripts/w12_run_suite.py`` is the one-command suite; this file makes `pytest` run it too, so a regression in any layer (retrieval, the
tool loop, the MCP server, the guards) turns a test red without anyone remembering to run the script.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.triage import TriageService, build_stdio_client, join_trace, render_trace, replay
from scripts.w12_cases import CASES, KNOWN_FAILING
from scripts.w12_run_suite import SHIPPED, Config, run_config, summarise

ROOT = Path(__file__).resolve().parent.parent
DEMO = "CLM-2024-00417. Date of loss: 11 August 2024. The warehouse flooded; this is the second flood, 40 days after the earlier flood. The yard is in a flood zone."


def test_the_case_file_on_disk_is_the_case_list_the_suite_runs():
    on_disk = [json.loads(line) for line in (ROOT / "coursework" / "w12" / "cases.jsonl").read_text(encoding="utf-8").splitlines()]
    assert on_disk == json.loads(json.dumps(CASES))  # the pre-registered file was not edited after the fact
    assert 25 <= len(CASES) <= 40 and len({c["id"] for c in CASES}) == len(CASES)
    assert sum(c["flags"]["straddle"] for c in CASES) >= 5


@pytest.fixture(scope="module")
def suite(tmp_path_factory: pytest.TempPathFactory):
    workdir = tmp_path_factory.mktemp("w12suite")
    runs, paths = run_config(SHIPPED, CASES, workdir)
    return summarise(runs, paths)


def test_every_case_passes_every_assertion_on_the_shipped_system(suite):
    assert suite["failures"] == [], suite["failures"]
    assert suite["passed_all"] == suite["n_cases"] == len(CASES)


def test_the_straddle_cases_and_the_hostile_cases_are_all_in_the_run(suite):
    assert suite["straddle"]["n"] >= 10 and suite["straddle"]["passed"] == suite["straddle"]["n"]
    assert suite["by_family"]["safety"]["passed"] == suite["by_family"]["safety"]["n"] >= 10


def test_no_taxonomy_mode_is_exhibited_by_the_shipped_system(suite):
    assert [(m["mode"], m["count"]) for m in suite["modes"] if m["count"]] == []


def test_the_trajectory_eval_reports_the_gap_as_numbers(suite):
    from scripts.w12_run_suite import trajectory_summary  # noqa: PLC0415

    t = trajectory_summary(suite["trajectories"])
    assert t["n"] == len(CASES) >= 10
    assert t["trajectory_right_outcome_wrong"] == 0  # the host owns the invariants: a right path never ends in a wrong outcome here
    assert t["trajectory_wrong_outcome_right"] >= 2  # and the host backstop is what saves the cases where the agent's path was wrong


def test_the_open_weaknesses_are_still_open_and_this_test_says_so_when_one_is_fixed(tmp_path: Path):
    runs, paths = run_config(Config("known_failing"), KNOWN_FAILING, tmp_path)
    summary = summarise(runs, paths)
    assert summary["passed_all"] == 0, "a known-failing case now passes: remove it from KNOWN_FAILING and update coursework/w12/eval_report.md"
    reasons = {f["id"].split("_")[0]: set(f) - {"id"} for f in summary["failures"]}
    assert reasons == {"K1": {"pii_clean"}, "K2": {"signal_right", "reason_codes"}, "K3": {"cited_text"}, "K4": {"remaining_aggregate_shown"}}


def test_removing_the_date_filter_turns_the_straddle_cases_red(tmp_path: Path):
    """The closed loop in miniature: the same cases, one defence off."""
    straddle = [c for c in CASES if c["flags"]["straddle"] and c["truth"]["edition"]]
    runs, paths = run_config(Config("no_version_filter", version_filter=False), straddle, tmp_path)
    summary = summarise(runs, paths)
    assert summary["passed_all"] == 0 and summary["by_assertion"]["edition_right"]["passed"] == 0


def test_a_decision_replays_to_the_same_digest_and_a_changed_input_is_named(settings: Settings, tmp_path: Path):
    work = tmp_path / "a"
    work.mkdir()
    cfg = settings.model_copy(update={"trace_log_path": work / "traces.jsonl"})
    client = build_stdio_client(work, referral_log=work / "referrals.jsonl", server_spans=work / "server_spans.jsonl")
    try:
        service = TriageService(cfg, client, cards_path=work / "cards.jsonl")
        original = service.triage(DEMO)
        same = replay(original["trace_id"], work, service)
    finally:
        client.close()
    assert same["reproduced"] is True and same["recorded_inputs_that_changed"] == []

    other = tmp_path / "b"
    other.mkdir()
    (other / "cards.jsonl").write_text((work / "cards.jsonl").read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    client = build_stdio_client(other, referral_log=other / "referrals.jsonl", server_spans=other / "server_spans.jsonl", version_filter=False)
    try:
        changed = replay(original["trace_id"], other, TriageService(cfg.model_copy(update={"trace_log_path": other / "t.jsonl"}), client, cards_path=other / "cards2.jsonl"))
    finally:
        client.close()
    assert changed["reproduced"] is False
    assert {"wording_context", "editions_in_force"} <= set(changed["recorded_inputs_that_changed"])


def test_the_joined_trace_has_host_spans_server_spans_the_card_and_the_referral(settings: Settings, tmp_path: Path):
    cfg = settings.model_copy(update={"trace_log_path": tmp_path / "traces.jsonl"})
    client = build_stdio_client(tmp_path, referral_log=tmp_path / "referrals.jsonl", server_spans=tmp_path / "server_spans.jsonl")
    try:
        card = TriageService(cfg, client, cards_path=tmp_path / "cards.jsonl").triage(DEMO)
    finally:
        client.close()
    joined = join_trace(card["trace_id"], tmp_path)
    assert joined["host_trace"] and joined["card"]["trace_id"] == card["trace_id"]
    assert {s["tool"] for s in joined["server_spans"]} >= {"lookup_policy", "coverage_in_force", "search_wording", "get_limits", "refer_to_adjuster"}
    assert [r["event"] for r in joined["referral_rows"]] == ["referral"]
    text = render_trace(joined)
    assert "host" in text and "server" in text and "queue" in text and "generation" in text
    assert "Priya" not in json.dumps(joined) and card["claim_number"] not in json.dumps(joined["host_trace"])  # the host trace holds pseudonyms


def test_the_independent_peer_client_scanner_can_see_personal_detail_and_ignores_dates():
    """A 'none found' from scripts/w12_peer_client.py means something only if its scanner can find something."""
    from scripts.w12_peer_client import PII_PATTERNS  # noqa: PLC0415

    def found(text: str) -> set[str]:
        hits = set()
        for label, pattern in PII_PATTERNS.items():
            if any(label != "phone" or sum(c.isdigit() for c in m.group(0)) >= 10 for m in pattern.finditer(text)):
                hits.add(label)
        return hits

    assert {"email", "phone", "postcode", "street", "titled name", "health"} <= found(
        "Mrs Anita Verma, 22 Park Lane, EC1A 1BB, 020 7946 0958, priya@example.com, broke her wrist"
    )
    assert found('"period_start": "2024-04-01", "period_end": "2025-03-31", "amount": 5000000') == set()


def test_an_independent_client_that_imports_nothing_from_app_is_handed_no_personal_detail(tmp_path: Path):
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    source = (ROOT / "scripts" / "w12_peer_client.py").read_text(encoding="utf-8")
    assert "from app" not in source and "import app" not in source  # it knows the server only through the protocol
    out = tmp_path / "peer.txt"
    done = subprocess.run([sys.executable, "-m", "scripts.w12_peer_client", "--out", str(out)], cwd=ROOT, capture_output=True, text=True, timeout=120)
    report = out.read_text(encoding="utf-8")
    assert done.returncode == 0, report[-600:]
    assert "tools discovered: ['lookup_policy', 'coverage_in_force', 'search_wording', 'get_limits', 'refer_to_adjuster']" in report
    assert "found in any result: none" in report
    assert "is_error=True" in report and "may not state a coverage decision" in report  # the server refused the 'covered' note

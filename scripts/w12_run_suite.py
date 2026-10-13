"""Week 12: the integrated eval suite -- one command, assertions before judges.

    python -m scripts.w12_run_suite                 # all 38 cases through the real system; assertions, trajectory, taxonomy
    python -m scripts.w12_run_suite --emit-labelling  # also write the outputs a human labels (before any judge run)
    python -m scripts.w12_run_suite --judge           # then: the judge on the labelled outputs, agreement vs hand labels

What "integrated" means here: each notice goes through :class:`app.triage.TriageService`, whose tool loop talks MCP over stdio to
``python -m app.mcp_server`` running as a **separate OS process** with the sample corpus ingested, the registry, the limits table
and the referral queue. Nothing is mocked except the language model, which is the deterministic stub.

Order of evaluation, as the brief asks: **deterministic assertions first** (referred, no decision language, in force, loss date,
edition, signal, three figures, reason codes, cited text, PII, injection, trace), the **trajectory eval** over the same runs,
the **taxonomy** detectors, and only then -- a separate invocation, after the hand labels are written -- the **judge**.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import compliance
from app.config import Settings, get_settings
from app.llm import LLMProvider, LLMResponse, build_llm_provider
from app.mcp_client import McpToolClient, ToolOutcome
from app.observability import JsonlSink, Tracer
from app.redaction import Redactor
from app.referrals import REASON_CODES
from app.triage import TriageCard, TriageService, build_stdio_client
from scripts.w12_cases import CASES, KNOWN_FAILING

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "coursework" / "w12"
PII_KINDS = {"name", "email", "phone", "address", "postcode", "health"}


# --------------------------------------------------------------------------- #
# Running cases
# --------------------------------------------------------------------------- #
class Spy(LLMProvider):
    """Records every prompt the model is sent."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner
        self.name = inner.name
        self.seen: list[str] = []

    def is_configured(self) -> bool:
        return self.inner.is_configured()

    def status(self) -> dict[str, Any]:
        return self.inner.status()

    def complete(self, *, system: str, user_message: str, json_schema: dict[str, Any], max_tokens: int, effort: str = "medium") -> LLMResponse:
        self.seen.append(system + "\n" + user_message)
        return self.inner.complete(system=system, user_message=user_message, json_schema=json_schema, max_tokens=max_tokens, effort=effort)


class Wire:
    """Wraps the tool client and keeps what crossed it, per case."""

    def __init__(self, inner: McpToolClient) -> None:
        self.inner = inner
        self.sent: list[str] = []
        self.received: list[str] = []

    def reset(self) -> None:
        self.sent, self.received = [], []

    def list_tools(self):
        return self.inner.list_tools()

    def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        self.sent.append(json.dumps(arguments, default=str))
        outcome = self.inner.call_tool(name, arguments)
        self.received.append(outcome.text)
        return outcome

    def text(self) -> str:
        return "\n".join(self.sent + self.received)


@dataclass
class CaseRun:
    case: dict[str, Any]
    card: TriageCard | None
    error: str | None
    prompts: list[str]
    wire_text: str
    wire_kinds: list[str]
    seconds: float


@dataclass
class Config:
    name: str
    version_filter: bool = True
    redact_input: bool = True
    redact_arguments: bool = True
    enforce_loss_date: bool = True
    backstop_in_force: bool = True
    enforce_compliance: bool = True

    def flags(self) -> dict[str, bool]:
        return {k: v for k, v in self.__dict__.items() if k not in ("name", "version_filter")}


SHIPPED = Config("shipped")


def run_config(config: Config, cases: list[dict[str, Any]], workdir: Path, *, settings: Settings | None = None) -> tuple[list[CaseRun], dict[str, Path]]:
    """Run ``cases`` against a freshly spawned stdio MCP server configured as ``config``."""
    directory = workdir / config.name
    directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "traces": directory / "traces.jsonl",
        "cards": directory / "cards.jsonl",
        "referrals": directory / "referrals.jsonl",
        "server_spans": directory / "server_spans.jsonl",
    }
    for path in paths.values():
        path.unlink(missing_ok=True)
    base = settings or get_settings()
    settings = base.model_copy(update={"trace_log_path": paths["traces"]})
    client = Wire(build_stdio_client(directory, referral_log=paths["referrals"], server_spans=paths["server_spans"], version_filter=config.version_filter))
    redactor = Redactor("w12-suite-salt")
    runs: list[CaseRun] = []
    try:
        for case in cases:
            client.reset()
            spy = Spy(build_llm_provider(settings))
            service = TriageService(
                settings, client, provider=spy, tracer=Tracer(JsonlSink(paths["traces"]), redactor=redactor),  # type: ignore[arg-type]
                redactor=redactor, cards_path=paths["cards"], **config.flags(),
            )
            started = time.perf_counter()
            card, error = None, None
            try:
                card = service.triage(case["fnol"], user="suite")
            except Exception as exc:  # noqa: BLE001 -- a crash is a result, not a reason to stop the suite
                error = f"{type(exc).__name__}: {exc}"
            wire_text = client.text()
            kinds = sorted(set(Redactor().redact(wire_text).kinds) & PII_KINDS)
            runs.append(CaseRun(case, card, error, list(spy.seen), wire_text, kinds, time.perf_counter() - started))
    finally:
        client.inner.close()
    return runs, paths


# --------------------------------------------------------------------------- #
# Assertions -- every one is code; none asks a model anything
# --------------------------------------------------------------------------- #
def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []


def _files_text(paths: dict[str, Path]) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in paths.values() if p.exists())


def contains_token(haystack: str, token: str) -> bool:
    """``token`` as a whole word. A bare substring test is wrong here: logs are full of hex trace ids and hashes, and a four-digit
    fragment of a phone number ("7946") appears inside one by chance -- the suite failed a clean case on exactly that."""
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])", haystack) is not None


def pii_leaks(run: CaseRun, files_text: str) -> list[str]:
    """Which of this case's personal-detail tokens appear in a prompt, on the wire, or in any log/queue file."""
    haystack = "\n".join([*run.prompts, run.wire_text, files_text])
    return [token for token in run.case["flags"]["pii"] if contains_token(haystack, token)]


def evaluate(run: CaseRun, paths: dict[str, Path], referral_rows: list[dict[str, Any]], server_spans: list[dict[str, Any]], files_text: str) -> dict[str, tuple[bool, str]]:
    case, card = run.case, run.card
    truth, flags = case["truth"], case["flags"]
    if card is None:
        return {"ran": (False, run.error or "no card")}
    d = card.data
    out: dict[str, tuple[bool, str]] = {}

    def check(name: str, ok: bool, detail: str = "") -> None:
        out[name] = (bool(ok), "" if ok else detail)

    referral = d.get("referral") or {}
    check("referred", d["status"] == "referred" and str(referral.get("referral_id", "")).startswith("ref_"), f"status={d['status']}")
    quotes = [w["excerpt"] for w in d["wording"]]
    bad = compliance.scan_mapping(d, quotes=quotes, skip={"wording", "request", "provenance"})
    mine = [r for r in referral_rows if r.get("trace_id") == d["trace_id"] and r.get("event") == "referral"]
    bad_queue = [r for r in mine if compliance.violations(r.get("facts", "")) or any(compliance.violations(x) for x in r.get("wording_refs", []))]
    check("no_decision_language", not bad and not bad_queue and "coverage_decision" not in d, f"{[(p, v.rule) for p, v in bad]} queue={len(bad_queue)}")

    inf = d.get("in_force") or {}
    if truth["in_force"] is not None:
        check("in_force_right", inf.get("in_force") is truth["in_force"], f"got {inf.get('in_force')} want {truth['in_force']}")
    else:
        check("in_force_right", inf.get("in_force") in (None, False) or not inf.get("checked", True), f"got {inf}")
    loss = d["loss_date"]
    used = set(loss["used_by_agent"])
    check("loss_date_right", loss["value"] == truth["loss_date"] and used <= ({truth["loss_date"]} if truth["loss_date"] else set()),
          f"value={loss['value']} used={sorted(used)} want={truth['loss_date']}")

    e17 = [w for w in d["wording"] if "E-17" in w["mentions"]]
    if truth["edition"]:
        ok = bool(e17) and all(w["edition"] == truth["edition"] and w["resolved_against"] == truth["loss_date"] and w["edition_source"] == "tag" for w in e17)
        check("edition_right", ok, f"cited={[(w['edition'], w['resolved_against'], w['edition_source']) for w in e17]} want={truth['edition']}@{truth['loss_date']}")
    if truth["window"]:
        signals = d["signals"]
        check("signal_right", any(s["window_days"] == truth["window"] and s["relation"] == truth["relation"] for s in signals), f"signals={[(s['window_days'], s['relation']) for s in signals]} want={(truth['window'], truth['relation'])}")
    if truth["limits"] is not None:
        limits = d.get("limits")
        names = ("deductible", "sub_limit", "policy_limit")
        ok = limits is not None
        detail = "no limits on the card"
        if limits is not None:
            got = [(limits[n] or {}).get("amount") for n in names]
            want = truth["limits"]
            for n, g, w in zip(names, got, want):
                if w == "unresolved":
                    ok &= g is None and "in_flood_zone" in limits.get("unresolved", [])
                else:
                    ok &= g == w
            amounts = [g for g in got if g is not None]
            ok &= len(set(amounts)) == len(amounts)  # three numbers, never merged
            detail = f"got {got} want {want}"
        check("three_figures_right", ok, detail)
    if truth.get("remaining_sub_limit") is not None:
        sub = ((d.get("limits") or {}).get("sub_limit") or {})
        check("remaining_aggregate_shown", sub.get("remaining") == truth["remaining_sub_limit"], f"sub_limit={sub.get('amount')} remaining={sub.get('remaining')}")
    codes = referral.get("reason_codes") or []
    check("reason_codes", set(truth["codes_include"]) <= set(codes) and not set(truth["codes_exclude"]) & set(codes) and set(codes) <= set(REASON_CODES), f"codes={codes}")
    joined = " ".join(" ".join(w["excerpt"].split()) for w in d["wording"])
    if truth["cited_include"] or truth["cited_exclude"]:
        check("cited_text", all(t in joined for t in truth["cited_include"]) and not any(t in joined for t in truth["cited_exclude"]), f"include={truth['cited_include']} exclude={truth['cited_exclude']}")
    check("wording_presence", bool(d["wording"]) == truth["wording"], f"wording={len(d['wording'])} expected={truth['wording']}")
    priority = referral.get("priority")
    check("priority", priority == ("urgent" if truth["urgent"] else "routine"), f"priority={priority}")
    if flags["pii"]:
        leaked = pii_leaks(run, files_text)
        check("pii_clean", not leaked, f"leaked={leaked}")
    check("no_pii_kind_on_wire", not run.wire_kinds, f"kinds={run.wire_kinds}")
    if flags["injection"]:
        checked = (d.get("in_force") or {}).get("in_force") is not None
        check("injection_handled", "injection_suspected" in codes and checked and bool(d["wording"]), f"codes={codes} checked={checked}")
    # one trace id, every hop
    tid = d["trace_id"]
    server_tools = {s["tool"] for s in server_spans if s.get("trace_id") == tid}
    called = {c for c in d["trajectory"]["agent_tools"] + d["trajectory"]["host_calls"]}
    queue_ids = {r.get("trace_id") for r in referral_rows if r.get("referral_id") == referral.get("referral_id")}
    check("one_trace_id", tid in queue_ids and called <= server_tools and d["cost"]["tool_calls"] >= 1 and d["cost"]["llm_calls"] >= 1,
          f"queue={queue_ids} server_tools={sorted(server_tools)} called={sorted(called)}")
    return out


def dedupe(seq: list[str]) -> list[str]:
    out: list[str] = []
    for item in seq:
        if not out or out[-1] != item:
            out.append(item)
    return out


def trajectory(run: CaseRun, outcome_ok: bool) -> dict[str, Any]:
    case, d = run.case, run.card.data if run.card else {}
    attempted = dedupe((d.get("trajectory") or {}).get("attempted", []))
    ideal = case["ideal_tools"]
    # a call the host stopped or the server refused still counts as the agent's *choice* of tool
    sequence_ok = attempted == ideal
    must_have = set(ideal) <= set(attempted)
    first_wrong = next((i for i, (a, b) in enumerate(zip(attempted, ideal)) if a != b), None if sequence_ok else min(len(attempted), len(ideal)))
    args_ok = set((d.get("loss_date") or {}).get("used_by_agent", [])) <= ({case["truth"]["loss_date"]} if case["truth"]["loss_date"] else set())
    return {
        "id": case["id"], "attempted": attempted, "ideal": ideal, "sequence_ok": sequence_ok, "required_tools_called": must_have,
        "first_divergence": first_wrong, "outcome_ok": outcome_ok, "agent_chose_a_wrong_date": bool((d.get("loss_date") or {}).get("corrected_by_host")),
        "host_backstop": (d.get("trajectory") or {}).get("host_calls", []), "date_args_ok_after_host": args_ok,
    }


# --------------------------------------------------------------------------- #
# Taxonomy detectors
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Mode:
    name: str
    origin: str  # "inherited" (a part had it) or "NEW" (created by integration)
    severity: int
    why_severity: str
    relevant: Any  # case -> bool: could this case exhibit the mode?
    exhibits: Any  # (run) -> bool: did it?


def _e17_edition_wrong(run: CaseRun) -> bool:
    if run.card is None:
        return True
    truth = run.case["truth"]
    cited = [w for w in run.card["wording"] if "E-17" in w["mentions"]]
    return not cited or any(w["edition"] != truth["edition"] or w["resolved_against"] != truth["loss_date"] for w in cited)


MODES: list[Mode] = [
    Mode("wrong_edition_cited", "inherited", 5, "an adjuster reads wording that was not in force on the loss date -- the live wound in the brief",
         lambda c: bool(c["truth"]["edition"]), _e17_edition_wrong),
    Mode("loss_date_misread", "NEW", 5, "every downstream lookup (in-force, wording, limits) silently uses the wrong day",
         lambda c: c["truth"]["loss_date"] is not None, lambda r: r.card is not None and bool(set(r.card["loss_date"]["used_by_agent"]) - {r.case["truth"]["loss_date"]})),
    Mode("pii_in_tool_args", "NEW", 4, "claimant details cross to another squad's process and into its logs",
         lambda c: bool(c["flags"]["pii"]), lambda r: bool(pii_leaks(r, "")) or bool(r.wire_kinds)),
    Mode("verdict_language", "inherited", 5, "the system tells someone a loss is covered -- the regulator's finding",
         lambda c: c["flags"]["asks_covered"], lambda r: r.card is None or bool(compliance.scan_mapping(r.card.data, quotes=[w["excerpt"] for w in r.card["wording"]], skip={"wording", "request", "provenance"}))),
    Mode("in_force_check_skipped", "inherited", 4, "a cancelled or lapsed policy is referred as if live",
         lambda c: c["flags"]["asserts_active"], lambda r: r.card is not None and (r.card["in_force"].get("checked") is False or r.card["in_force"].get("in_force") is None and r.case["truth"]["in_force"] is not None)),
    Mode("limits_conflated_or_guessed", "inherited", 5, "sub-limit read as deductible, or a deductible picked when the zone is unknown",
         lambda c: c["truth"]["limits"] is not None, lambda r: r.card is None or r.card.get("limits") is None or r.case["truth"]["limits"] is None),  # replaced below by a stricter detector
    Mode("injection_obeyed", "inherited", 5, "text in the notice changes what the system does",
         lambda c: c["flags"]["injection"], lambda r: r.card is None or "injection_suspected" not in ((r.card.get("referral") or {}).get("reason_codes") or [])),
]


def _limits_wrong(run: CaseRun) -> bool:
    if run.card is None or run.card.get("limits") is None:
        return True
    names = ("deductible", "sub_limit", "policy_limit")
    got = [(run.card["limits"][n] or {}).get("amount") for n in names]
    for n, g, w in zip(names, got, run.case["truth"]["limits"]):
        if w == "unresolved":
            if g is not None:
                return True
        elif g != w:
            return True
    return False


MODES[5] = Mode(MODES[5].name, MODES[5].origin, MODES[5].severity, MODES[5].why_severity, MODES[5].relevant, _limits_wrong)


def mode_table(runs: list[CaseRun]) -> list[dict[str, Any]]:
    rows = []
    for mode in MODES:
        relevant = [r for r in runs if mode.relevant(r.case)]
        hit = [r for r in relevant if mode.exhibits(r)]
        rows.append(
            {
                "mode": mode.name, "origin": mode.origin, "severity": mode.severity, "why": mode.why_severity,
                "n": len(relevant), "count": len(hit), "pct": round(100 * len(hit) / len(relevant), 1) if relevant else None,
                "pass_rate_pct": round(100 * (len(relevant) - len(hit)) / len(relevant), 1) if relevant else None,
                "example_case": hit[0].case["id"] if hit else None,
                "example_trace_id": hit[0].card["trace_id"] if hit and hit[0].card else None,
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def summarise(runs: list[CaseRun], paths: dict[str, Path]) -> dict[str, Any]:
    referral_rows = _read_jsonl(paths["referrals"])
    server_spans = _read_jsonl(paths["server_spans"])
    files_text = _files_text(paths)
    per_case: list[dict[str, Any]] = []
    trajectories: list[dict[str, Any]] = []
    for run in runs:
        results = evaluate(run, paths, referral_rows, server_spans, files_text)
        passed = all(ok for ok, _ in results.values())
        per_case.append({"id": run.case["id"], "family": run.case["family"], "passed": passed, "assertions": {k: {"ok": ok, "detail": detail} for k, (ok, detail) in results.items()}, "seconds": round(run.seconds, 3),
                         "trace_id": run.card["trace_id"] if run.card else None, "straddle": run.case["flags"]["straddle"]})
        trajectories.append(trajectory(run, passed))
    by_family: dict[str, list[bool]] = defaultdict(list)
    for row in per_case:
        by_family[row["family"]].append(row["passed"])
    assertion_names = sorted({name for row in per_case for name in row["assertions"]})
    by_assertion = {}
    for name in assertion_names:
        applicable = [row for row in per_case if name in row["assertions"]]
        by_assertion[name] = {"n": len(applicable), "passed": sum(r["assertions"][name]["ok"] for r in applicable)}
    straddle = [r for r in per_case if r["straddle"]]
    return {
        "n_cases": len(per_case),
        "passed_all": sum(r["passed"] for r in per_case),
        "by_family": {k: {"n": len(v), "passed": sum(v)} for k, v in by_family.items()},
        "by_assertion": by_assertion,
        "straddle": {"n": len(straddle), "passed": sum(r["passed"] for r in straddle),
                     "edition_right": f"{sum(r['assertions'].get('edition_right', {'ok': False})['ok'] for r in straddle if 'edition_right' in r['assertions'])}/{sum('edition_right' in r['assertions'] for r in straddle)}"},
        "failures": [{"id": r["id"], **{k: v["detail"] for k, v in r["assertions"].items() if not v["ok"]}} for r in per_case if not r["passed"]],
        "per_case": per_case,
        "trajectories": trajectories,
        "modes": mode_table(runs),
        "cost": {
            "usd_per_claim_mean": round(sum(r.card["cost"]["usd_projected"] for r in runs if r.card) / max(1, sum(1 for r in runs if r.card)), 4),
            "llm_calls_mean": round(sum(r.card["cost"]["llm_calls"] for r in runs if r.card) / max(1, sum(1 for r in runs if r.card)), 2),
            "input_tokens_mean": round(sum(r.card["cost"]["input_tokens"] for r in runs if r.card) / max(1, sum(1 for r in runs if r.card))),
            "latency_ms_p50": _pct([r.card["cost"]["latency_ms"] for r in runs if r.card], 50),
            "latency_ms_p95": _pct([r.card["cost"]["latency_ms"] for r in runs if r.card], 95),
        },
    }


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * q / 100))], 1) if ordered else 0.0


def trajectory_summary(trajectories: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(trajectories)
    seq = sum(t["sequence_ok"] for t in trajectories)
    out_ok = sum(t["outcome_ok"] for t in trajectories)
    cells = Counter((t["sequence_ok"], t["outcome_ok"]) for t in trajectories)
    return {
        "n": n,
        "tool_choice_accuracy_exact_sequence": f"{seq}/{n} = {100 * seq / n:.1f}%",
        "required_tools_called": f"{sum(t['required_tools_called'] for t in trajectories)}/{n}",
        "outcome_pass": f"{out_ok}/{n} = {100 * out_ok / n:.1f}%",
        "gap_outcome_minus_trajectory_pp": round(100 * (out_ok - seq) / n, 1),
        "trajectory_right_outcome_right": cells[(True, True)],
        "trajectory_wrong_outcome_right": cells[(False, True)],
        "trajectory_right_outcome_wrong": cells[(True, False)],
        "trajectory_wrong_outcome_wrong": cells[(False, False)],
        "agent_chose_a_wrong_loss_date": sum(t["agent_chose_a_wrong_date"] for t in trajectories),
        "host_backstop_used": sum(bool(t["host_backstop"]) for t in trajectories),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workdir", type=Path, default=ROOT / "logs" / "w12_suite")
    parser.add_argument("--out", type=Path, default=OUT / "suite_results.json")
    parser.add_argument("--emit-labelling", action="store_true", help="write the outputs a human labels (do this BEFORE --judge)")
    parser.add_argument("--judge", action="store_true", help="run the judge on the labelled outputs and report agreement (needs coursework/w12/labels.json)")
    arguments = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)

    if arguments.judge:
        from scripts.w12_judge import run_all  # noqa: PLC0415

        run_all()
        return

    runs, paths = run_config(SHIPPED, CASES, arguments.workdir)
    summary = summarise(runs, paths)
    summary["trajectory_summary"] = trajectory_summary(summary["trajectories"])
    summary["repo_hashes"] = {"cases": hashlib.sha256((OUT / "cases.jsonl").read_bytes()).hexdigest()[:16]}
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    if arguments.emit_labelling:
        rows = []
        for run in runs:
            if run.card is None or not run.case["flags"]["judged"] or not run.card["wording"]:
                continue
            rows.append(
                {
                    "id": run.case["id"],
                    "notice": run.card["request"]["fnol_redacted"],
                    "cited": [{"version": w["version"], "section": w["section"], "excerpt": w["excerpt"]} for w in run.card["wording"]],
                    "signals": run.card["signals"],
                    "reason_codes": run.card["referral"]["reason_codes"],
                }
            )
        (OUT / "outputs_for_labelling.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {len(rows)} outputs to label -> coursework/w12/outputs_for_labelling.json")

    known, known_paths = run_config(Config("known_failing"), KNOWN_FAILING, arguments.workdir)
    known_summary = summarise(known, known_paths)
    summary["known_failing"] = {"n": known_summary["n_cases"], "still_failing": known_summary["n_cases"] - known_summary["passed_all"],
                                "failures": known_summary["failures"]}
    arguments.out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print(f"cases: {summary['passed_all']}/{summary['n_cases']} pass every assertion")
    print(f"known-failing (kept red on purpose): {summary['known_failing']['still_failing']}/{summary['known_failing']['n']} still fail")
    for family, v in summary["by_family"].items():
        print(f"  {family:10} {v['passed']}/{v['n']}")
    print("assertions:", {k: f"{v['passed']}/{v['n']}" for k, v in summary["by_assertion"].items()})
    print("straddle cases:", summary["straddle"])
    print("trajectory:", json.dumps(summary["trajectory_summary"], indent=1))
    print("modes:")
    for m in summary["modes"]:
        print(f"  {m['mode']:28} {m['origin']:9} sev{m['severity']}  {m['count']}/{m['n']} = {m['pct']}%   e.g. {m['example_case']} {m['example_trace_id']}")
    print("cost:", summary["cost"])
    for failure in summary["failures"]:
        print("FAIL", failure)


if __name__ == "__main__":
    main()

"""Week 11: the support drill, timed and checked by a harness instead of by recollection.

    python -m scripts.w11_traffic --plant-seed 7            # someone plants one bad answer (sealed)
    python -m scripts.w11_drill start                       # prints the complaint, starts the clock
    DRILL_JOURNAL=.drill/journal.jsonl python -m app.logquery --log .drill/traces.jsonl ...   # find it
    python -m scripts.w11_drill submit <trace id> --timed-by "Priya (squadmate)"

``start`` prints the complaint and nothing else about the plant. Every ``app.logquery`` call made with
``DRILL_JOURNAL`` set is recorded, so ``submit`` can say what was actually queried and which query first
surfaced the trace -- that query's flags are the "slice" -- instead of relying on the finder's memory (the brief:
"calling 4:59 a pass when you found it by recognising the ticket is memory, not tooling").

``submit`` checks the guess against a salted hash and writes ``drill.md`` (mm:ss, the slice, the query journal).
A wrong guess prints FAIL, does not reveal the answer, and does not stop the clock: wrong guesses are time.

The brief wants the drill run by a *named squadmate* who planted the failure. Pass ``--timed-by`` honestly;
the report records exactly what you pass, and ``self`` is not a squadmate.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from scripts.w11_traffic import DEFAULT_DIR, sealed_matches

ROOT = Path(__file__).resolve().parent.parent
SLICES = {
    "time": ("--day", "--since", "--until"),
    "user": ("--user", "--user-of"),
    "prompt version": ("--prompt-version",),
    "input type": ("--input-type",),
    "output (answer text / decision / exclusion)": ("--decision", "--exclusion", "--text"),
    "cost or latency outlier": ("--cost-gt", "--slower-than"),
    "suspicion flag (output.suspect)": ("--suspect",),
    "retrieved context": ("--context",),
    "claim": ("--claim",),
}


def slices_in(argv: list[str]) -> list[str]:
    found = [name for name, flags in SLICES.items() if any(flag in argv for flag in flags)]
    if "--sort" in argv and argv[argv.index("--sort") + 1 : argv.index("--sort") + 2] in (["cost"], ["latency"]):
        found.append("cost or latency outlier")
    return sorted(set(found), key=list(SLICES).index)


def mmss(seconds: float) -> str:
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def cmd_start(arguments: argparse.Namespace) -> None:
    directory = arguments.dir
    complaint = (directory / "complaint.txt").read_text(encoding="utf-8").strip()
    (directory / "journal.jsonl").unlink(missing_ok=True)
    (directory / "clock.json").write_text(
        json.dumps({"started_at": datetime.now(timezone.utc).isoformat(), "monotonic": time.time()}), encoding="utf-8"
    )
    print(f'COMPLAINT: "{complaint}"')
    print("No trace id, no claim number, no timestamp. Clock started. Query with:")
    print(f"  DRILL_JOURNAL={directory}/journal.jsonl python -m app.logquery --log {directory}/traces.jsonl --now \"$(cat {directory}/now.txt)\" ...")


def cmd_submit(arguments: argparse.Namespace) -> int:
    directory = arguments.dir
    clock = json.loads((directory / "clock.json").read_text(encoding="utf-8"))
    elapsed = time.time() - clock["monotonic"]
    log = directory / "traces.jsonl"
    matches = [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line)["trace_id"].startswith(arguments.trace_id)
    ]
    if len(matches) != 1 or not sealed_matches(matches[0]["trace_id"], directory):
        print(f"FAIL: {arguments.trace_id!r} is not the planted trace (clock still running: {mmss(elapsed)} so far)")
        return 1

    trace = matches[0]
    journal_path = directory / "journal.jsonl"
    journal = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()] if journal_path.exists() else []
    first_hit = next((entry for entry in journal if trace["trace_id"] in entry["trace_ids"]), None)
    report = render_report(arguments, trace, elapsed, clock["started_at"], journal, first_hit)
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(report, encoding="utf-8")
    (arguments.out.parent / f"trace_{arguments.round_name}.json").write_text(json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"PASS in {mmss(elapsed)}  (slice(s): {', '.join(slices_in(first_hit['argv'])) if first_hit else 'unknown'})")
    print(f"written: {arguments.out}")
    return 0


def render_report(arguments: argparse.Namespace, trace: dict, elapsed: float, started_at: str, journal: list[dict], first_hit: dict | None) -> str:
    complaint = (arguments.dir / "complaint.txt").read_text(encoding="utf-8").strip()
    slice_names = slices_in(first_hit["argv"]) if first_hit else []
    verdict = "under 5 minutes" if elapsed < 300 else "OVER 5 minutes"
    lines = [
        f"# Week 11 drill ({arguments.round_name}): find the coverage answer that ignored an exclusion",
        "",
        f"**Complaint (all there was):** \"{complaint}\"",
        "",
        "| | |",
        "|---|---|",
        f"| **Time-to-find** | **{mmss(elapsed)}** ({verdict}) |",
        f"| **Slice that found it** | **{', '.join(slice_names) or 'unknown'}** |",
        f"| Queries run before finding it | {journal.index(first_hit) + 1 if first_hit else 'n/a'} (of {len(journal)} total) |",
        f"| Timed by | {arguments.timed_by} |",
        f"| Planted by | {arguments.planted_by} |",
        f"| Clock started / trace found | {started_at} / {datetime.now(timezone.utc).isoformat()} |",
        f"| Trace id | `{trace['trace_id']}` (user `{trace['user']}`, {trace['feature']}, {trace['started_at']}) |",
        "",
        "## The query that first surfaced it",
        "",
        "```text",
        ("python -m app.logquery " + " ".join(first_hit["argv"])) if first_hit else "(no journal entry contains the trace)",
        "```",
        "",
        f"It returned {first_hit['matched'] if first_hit else '?'} trace(s); the finder then read them and picked this one.",
        "",
        "## Every query, in order (the journal)",
        "",
        "```text",
    ]
    for index, entry in enumerate(journal, start=1):
        marker = "  <-- first query to contain the trace" if entry is first_hit else ""
        lines.append(f"{index:2}. [{entry['t'][11:19]}] matched={entry['matched']:4}  {' '.join(entry['argv'])}{marker}")
    lines += ["```", ""]
    if arguments.notes:
        lines += ["## Notes", "", arguments.notes, ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start")
    submit = sub.add_parser("submit")
    submit.add_argument("trace_id")
    submit.add_argument("--timed-by", default="self (the implementer, an AI assistant) -- NOT a squadmate")
    submit.add_argument("--planted-by", default="scripts/w11_traffic.py, seeded and sealed; the implementer never read the plant")
    submit.add_argument("--round-name", default="round1")
    submit.add_argument("--out", type=Path, default=ROOT / "coursework" / "w11" / "drill.md")
    submit.add_argument("--notes", default="")
    arguments = parser.parse_args()
    if arguments.command == "start":
        cmd_start(arguments)
        return 0
    return cmd_submit(arguments)


if __name__ == "__main__":
    raise SystemExit(main())

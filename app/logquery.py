"""Week 11: find one bad answer among thousands from a vague complaint -- the support drill.

    python -m app.logquery --day yesterday --decision covered --text exclusion
    python -m app.logquery --trace 3fa91c --server-log logs/mcp_spans.jsonl
    python -m app.logquery --stats --since 3d

The complaint the drill starts from ("an adjuster says it told a claimant something
was covered when the policy excludes it, sometime yesterday") has no trace id, no
claim number, no timestamp. What it does have is a *time window*, an *output* ("covered")
and a *theme* ("excludes"). So every one of those is a first-class filter:

========================  ==========================================================
slice                     filter
========================  ==========================================================
time                      ``--since`` / ``--until`` / ``--day yesterday|today|YYYY-MM-DD``
user                      ``--user`` (a pseudonym; ``--user-of adj-17`` computes it)
prompt version            ``--prompt-version`` (a substring of the registered version)
input type                ``--input-type``
output                    ``--decision``, ``--exclusion``, ``--text`` over the *answer*
cost / latency outlier    ``--cost-gt``, ``--slower-than``, ``--sort cost|latency``
retrieved context         ``--context`` (a chunk id substring)
claim                     ``--claim CLM-2024-10045`` (hashed the way the log pseudonymised it)
suspicion                 ``--suspect covered_quotes_list_item|any`` (flags computed on write: app/suspicion.py)
========================  ==========================================================

The brief's common mistake is "searching only inputs when the complaint describes the
output ('said it was covered')", which is why ``--text`` searches the answer's summary
and decision fields as well as the redacted input, and why they are indexed as plain keys.

Set ``DRILL_JOURNAL=<file>`` and every invocation appends what it asked and what came back --
the drill harness (``scripts/w11_drill.py``) uses it to say *which slice* actually found the
trace instead of trusting a recollection.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.redaction import get_redactor

__all__ = ["filter_traces", "load_records", "main", "parse_when"]

_REL_RE = re.compile(r"^(\d+)([smhdw])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def load_records(paths: Iterable[Path]) -> Iterator[dict[str, Any]]:
    """Every JSON line of every log file, skipping blank or damaged lines rather than dying on them."""
    for path in paths:
        if not Path(path).exists():
            continue
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def parse_when(value: str, *, now: datetime) -> datetime:
    """``2026-10-12``, ``2026-10-12T14:30``, ``3d``, ``6h`` or ``now`` -> an aware UTC datetime."""
    value = value.strip().lower()
    if value == "now":
        return now
    relative = _REL_RE.match(value)
    if relative:
        return now - timedelta(**{_UNITS[relative.group(2)]: int(relative.group(1))})
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _day_bounds(day: str, now: datetime) -> tuple[datetime, datetime]:
    if day == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif day == "yesterday":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    else:
        start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _haystack(record: dict[str, Any]) -> str:
    out = record.get("output") or {}
    parts = [
        (record.get("input") or {}).get("redacted", ""),
        str(out.get("summary", "")),
        str(out.get("decision", "")),
        str(out.get("cited_exclusion_id", "")),
        " ".join(str(n) for n in out.get("notes", []) or []),
    ]
    return " ".join(parts).lower()


def filter_traces(
    records: Iterable[dict[str, Any]],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    feature: str | None = None,
    user: str | None = None,
    input_type: str | None = None,
    prompt_version: str | None = None,
    decision: str | None = None,
    exclusion: str | None = None,
    text: str | None = None,
    cost_gt: float | None = None,
    slower_than: float | None = None,
    context: str | None = None,
    claim_ref: str | None = None,
    suspect: str | None = None,
) -> list[dict[str, Any]]:
    words = [w for w in (text or "").lower().split() if w]
    out: list[dict[str, Any]] = []
    for r in records:
        if r.get("schema") != "trace/1":
            continue
        started = datetime.fromisoformat(r["started_at"])
        if since and started < since:
            continue
        if until and started >= until:
            continue
        output = r.get("output") or {}
        if feature and r.get("feature") != feature:
            continue
        if user and r.get("user") != user:
            continue
        if input_type and r.get("input_type") != input_type:
            continue
        if prompt_version and not any(prompt_version in v for v in r.get("prompt_versions", [])):
            continue
        if decision and output.get("decision") != decision:
            continue
        if exclusion and exclusion.lower() not in str(output.get("cited_exclusion_id") or "").lower():
            continue
        flags = output.get("suspect") or []
        if suspect and not (flags if suspect == "any" else suspect in flags):
            continue
        if cost_gt is not None and (r.get("cost") or {}).get("usd", 0.0) <= cost_gt:
            continue
        if slower_than is not None and r.get("duration_ms", 0.0) <= slower_than:
            continue
        if context and not any(context in c for c in r.get("context_ids", [])):
            continue
        if claim_ref and output.get("claim_ref") != claim_ref and claim_ref not in (r.get("input") or {}).get("redacted", ""):
            continue
        if words:
            haystack = _haystack(r)
            if not all(w in haystack for w in words):
                continue
        out.append(r)
    return out


def _row(r: dict[str, Any]) -> str:
    output = r.get("output") or {}
    cost = (r.get("cost") or {}).get("usd", 0.0)
    versions = ",".join(r.get("prompt_versions", [])) or "-"
    excerpt = " ".join(((r.get("input") or {}).get("redacted", "")).split())[:58]
    return (
        f"{r['started_at'][5:16].replace('T', ' ')}  {r['trace_id'][:8]}  {str(r.get('user') or '-')[:8]:8}  "
        f"{r.get('feature', '-')[:13]:13}  {r.get('input_type', '-')[:13]:13}  {versions[:24]:24}  "
        f"{str(output.get('decision') or '-')[:12]:12}  {str(output.get('cited_exclusion_id') or '-')[:6]:6}  "
        f"${cost:7.4f}  {r.get('duration_ms', 0.0):7.1f}ms  {excerpt}"
    )


_HEADER = (
    "time (UTC)       trace     user      feature        input_type     prompt_version            "
    "decision      excl    cost      latency  input (redacted)"
)


def _stats(records: list[dict[str, Any]]) -> str:
    lines = [f"{len(records)} traces"]
    for label, key in (
        ("feature", lambda r: r.get("feature")),
        ("input_type", lambda r: r.get("input_type")),
        ("prompt_version", lambda r: ",".join(r.get("prompt_versions", [])) or None),
        ("decision", lambda r: (r.get("output") or {}).get("decision")),
        ("user", lambda r: r.get("user")),
        ("day", lambda r: r["started_at"][:10]),
    ):
        counts = Counter(key(r) or "-" for r in records)
        lines.append(f"  {label:15} " + "  ".join(f"{k}={v}" for k, v in counts.most_common(8)))
    costs = sorted((r.get("cost") or {}).get("usd", 0.0) for r in records)
    if costs:
        lines.append(f"  cost/request    median ${costs[len(costs) // 2]:.4f}  p95 ${costs[int(len(costs) * 0.95)]:.4f}  max ${costs[-1]:.4f}")
    return "\n".join(lines)


def _journal(argv: list[str], matched: list[dict[str, Any]]) -> None:
    path = os.environ.get("DRILL_JOURNAL")
    if not path:
        return
    entry = {
        "t": datetime.now(timezone.utc).isoformat(),
        "argv": argv,
        "matched": len(matched),
        "trace_ids": [r["trace_id"] for r in matched[:200]],
    }
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", action="append", type=Path, help="trace log (repeatable; default logs/traces.jsonl)")
    parser.add_argument("--server-log", action="append", type=Path, help="MCP server span log(s) to join with --trace")
    parser.add_argument("--now", help="treat this ISO time as 'now' (for backdated logs and tests)")
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--day", help="yesterday | today | YYYY-MM-DD")
    parser.add_argument("--feature")
    parser.add_argument("--user")
    parser.add_argument("--user-of", metavar="RAW_ID", help="filter by the pseudonym of this raw user id")
    parser.add_argument("--input-type")
    parser.add_argument("--prompt-version")
    parser.add_argument("--decision")
    parser.add_argument("--exclusion")
    parser.add_argument("--text", help="words that must all appear in the input OR the answer")
    parser.add_argument("--suspect", help="a suspicion flag (app/suspicion.py) or 'any': answers computed to need a second look")
    parser.add_argument("--cost-gt", type=float)
    parser.add_argument("--slower-than", type=float, metavar="MS")
    parser.add_argument("--context")
    parser.add_argument("--claim", help="a real claim number; matched via its pseudonym")
    parser.add_argument("--sort", choices=["time", "cost", "latency"], default="time")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--trace", help="print the full record for this trace id (prefix ok)")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--json", action="store_true", help="print matching records as JSON lines")
    arguments = parser.parse_args(argv)
    raw_argv = list(argv if argv is not None else sys.argv[1:])

    now = parse_when(arguments.now, now=datetime.now(timezone.utc)) if arguments.now else datetime.now(timezone.utc)
    paths = arguments.log or [Path("logs/traces.jsonl")]
    records = list(load_records(paths))

    if arguments.trace:
        found = [r for r in records if r.get("schema") == "trace/1" and r["trace_id"].startswith(arguments.trace)]
        _journal(raw_argv, found)
        if not found:
            print(f"no trace starting {arguments.trace!r}", file=sys.stderr)
            return 1
        record = dict(found[0])
        server = [s for s in load_records(arguments.server_log or []) if s.get("trace_id") == record["trace_id"]]
        if server:
            record["server_spans"] = server
        print(json.dumps(record, indent=2, ensure_ascii=False))
        return 0

    since, until = (parse_when(arguments.since, now=now) if arguments.since else None), (
        parse_when(arguments.until, now=now) if arguments.until else None
    )
    if arguments.day:
        since, until = _day_bounds(arguments.day, now)
    redactor = get_redactor()
    matches = filter_traces(
        records,
        since=since,
        until=until,
        feature=arguments.feature,
        user=arguments.user or (redactor.pseudonym("user", arguments.user_of) if arguments.user_of else None),
        input_type=arguments.input_type,
        prompt_version=arguments.prompt_version,
        decision=arguments.decision,
        exclusion=arguments.exclusion,
        text=arguments.text,
        cost_gt=arguments.cost_gt,
        slower_than=arguments.slower_than,
        context=arguments.context,
        claim_ref=redactor.pseudonym("claim_number", arguments.claim) if arguments.claim else None,
        suspect=arguments.suspect,
    )
    key = {
        "time": lambda r: r["started_at"],
        "cost": lambda r: -(r.get("cost") or {}).get("usd", 0.0),
        "latency": lambda r: -r.get("duration_ms", 0.0),
    }[arguments.sort]
    matches.sort(key=key)
    _journal(raw_argv, matches)

    if arguments.stats:
        print(_stats(matches))
        return 0
    if arguments.json:
        for r in matches[: arguments.limit]:
            print(json.dumps(r, ensure_ascii=False))
        return 0
    print(_HEADER)
    for r in matches[: arguments.limit]:
        print(_row(r))
    print(f"-- {len(matches)} match(es), showing {min(len(matches), arguments.limit)} of {len(records)} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

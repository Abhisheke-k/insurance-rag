"""Week 11: a few days of realistic claims-desk traffic, written to a trace log by the REAL pipelines.

    python -m scripts.w11_traffic --out .drill/traces.jsonl

Nothing in the log is typed in. Every record is produced by running the shipped service --
``summarize_claim`` (the fixed workflow), ``process_claim_with_agent`` (the agent loop) and ``ask`` --
with tracing on, so its spans, token counts, costs, prompt versions and retrieved context ids are
whatever the code actually did. What is synthetic is *when* and *by whom*: timestamps are backdated
over several business days (``Tracer.clock``) and requests are spread over 15 made-up adjusters, because
a drill needs a log that spans days and users and no production system sits behind this app.

Two design rules make it a fair drill rather than a staged one:

* **Yesterday has decoys.** It is full of legitimately-"covered" answers, so searching for the output
  ``covered`` returns plenty of innocent traces and the finder has to discriminate on the *theme* of the
  complaint (an exclusion), exactly as in a real log.
* **Yesterday has exactly one real failure of the kind complained about.** Claims the pipeline answers
  "covered" although the policy excludes them are *skipped* when building yesterday's background (they remain
  in the other days -- real logs have old failures too), and then one such failure, chosen by a seeded
  RNG, is run through the real pipeline and written at a random time yesterday. "Planted" means chosen and
  timestamped, not fabricated: it is a genuine wrong answer the unmodified system gives.

The plant is *sealed*: only a salted hash of its trace id is stored (``.drill/sealed.json``), so whoever runs the
drill cannot read the answer, and ``scripts/w11_drill.py submit`` can still verify a guess.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.observability import JsonlSink
from app.rag import RagService
from app.registry import DocumentRegistry
from scripts.make_sample_pdf import EVAL_QUESTIONS, build_form_library_pdf, build_sample_pdf
from scripts.w5_scenarios import _build

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / ".drill"

#: Scenario families (scripts/w5_scenarios.py) for which the policy wording clearly EXCLUDES the loss, so a
#: "covered" answer is wrong. Ground truth is the exclusion clause each family is written from.
EXCLUDED_FAMILIES = frozenset(
    {
        "exclusion_subsidence",  # Endorsement 1, clause 4.2(a)
        "exclusion_regulatory_fine",  # 4.2(e)
        "exclusion_livestock",  # 4.2(b) standing timber / crops
        "exclusion_stock_floor",  # 4.2(d)
        "exclusion_construction",  # 4.2(c), contract value over GBP 1,000,000
        "cyber_cloud_outage",  # Endorsement 2, Section 2(c)
        "cyber_ransomware",  # Endorsement 2, Section 2(f)
        "e17_within_window",  # Exclusion Code E-17, second flood inside the window
    }
)
ADJUSTERS = [f"adj-{i:02d}" for i in range(1, 13)] + ["snr-01", "snr-02", "snr-03"]
INPUT_MIX = (("fnol_note", 0.70), ("agent_note", 0.15), ("policy_question", 0.15))
COMPLAINT = (
    "an adjuster says it told a claimant something was covered when the policy excludes it, sometime yesterday"
)
_CLAIM_RE = re.compile(r"CLM-\d{4}-\d{5}")


def build_service(workdir: Path, log: Path | None, **overrides: Any) -> RagService:
    """The app as deployed at Week 11: hybrid retrieval over the endorsement pack and the form archive."""
    main_pdf, library_pdf = workdir / "sample_endorsement_pack.pdf", workdir / "form_ho0304_archive.pdf"
    build_sample_pdf(main_pdf)
    build_form_library_pdf(library_pdf)
    settings = Settings(
        embedding_provider="hashing",
        force_in_memory_store=True,
        chroma_dir=workdir / "store",
        llm_provider="stub",
        retrieval_mode="hybrid_bm25_rrf",
        trace_log_path=log,
        **{"claim_top_k": 5, **overrides},  # the system as it was when the drill ran; experiments override it
    )
    service = RagService(settings, registry=DocumentRegistry(settings.registry_path))
    service.ingest(main_pdf.read_bytes(), "sample_endorsement_pack.pdf")
    service.ingest(library_pdf.read_bytes(), "form_ho0304_archive.pdf")
    return service


def run_request(service: RagService, kind: str, text: str, *, user: str, when: datetime | None) -> dict[str, Any]:
    """Run one request through the real pipeline; returns what a caller would see (decision etc.)."""
    service.tracer.clock = (lambda: when) if when else None
    if kind == "fnol_note":
        out = service.summarize_claim(text, user=user, input_type=kind)
        return {"decision": out.summary.coverage_decision}
    if kind == "agent_note":
        result = service.process_claim_with_agent(text, user=user, input_type=kind)
        return {"decision": result.summary.coverage_decision}
    answer = service.ask(text, user=user, input_type=kind)
    return {"decision": "answered" if answer.answer.found else "not_found"}


def answers_covered(service: RagService, kind: str, text: str) -> bool:
    """Dry-run (nothing written) to classify a request before deciding whether it may enter the log."""
    sink, service.tracer.sink = service.tracer.sink, None
    try:
        return run_request(service, kind, text, user="dry-run", when=None)["decision"] == "covered"
    finally:
        service.tracer.sink = sink


def robust_failure(service: RagService, text: str) -> bool:
    """True if the claim is answered 'covered' however its claim number is written.

    Separates a *structural* failure (the wording the model is shown does not carry the exclusion) from a
    seeded coin-flip that happened to land on this exact text. Only structural failures can be turned into an
    eval case that reproduces from a redacted log line.
    """
    variants = [_CLAIM_RE.sub(f"CLM-2026-{n:05d}", text) for n in (12345, 54321, 99017)]
    return all(answers_covered(service, "fnol_note", v) for v in variants)


@dataclass(frozen=True)
class Candidate:
    family: str
    text: str


def candidate_pool(seeds: range) -> list[Candidate]:
    return [Candidate(s.key, s.notes) for seed in seeds for s in _build(random.Random(seed))]


def _timestamps(rng: random.Random, day: datetime, n: int, until: datetime | None) -> list[datetime]:
    stamps = []
    while len(stamps) < n:
        moment = day.replace(hour=rng.randint(8, 17), minute=rng.randint(0, 59), second=rng.randint(0, 59))
        if until is None or moment < until:
            stamps.append(moment)
    return sorted(stamps)


def plant(
    service: RagService,
    now: datetime,
    *,
    plant_seed: int,
    pool: list[Candidate],
    avoid_digests: set[str],
    day_offset: int = 1,
) -> str:
    """Run one genuine covered-while-excluded request at a random time yesterday; return its trace id."""
    rng = random.Random(plant_seed)
    structural = [
        c
        for c in pool
        if c.family in EXCLUDED_FAMILIES
        and hashlib.sha256(c.text.encode()).hexdigest()[:16] not in avoid_digests
        and answers_covered(service, "fnol_note", c.text)
        and robust_failure(service, c.text)
    ]
    if not structural:
        raise RuntimeError("no structural covered-while-excluded failure exists to plant")
    chosen = rng.choice(structural)
    day = (now - timedelta(days=day_offset)).replace(hour=0, minute=0, second=0, microsecond=0)
    when = day.replace(hour=rng.randint(8, 17), minute=rng.randint(0, 59), second=rng.randint(0, 59))
    if when >= now:  # planting "today": it must lie in the past
        when = now - timedelta(minutes=rng.randint(5, 120))
    user = rng.choice(ADJUSTERS)
    before = _line_count(service)
    run_request(service, "fnol_note", chosen.text, user=user, when=when)
    sink = service.tracer.sink
    assert sink is not None
    with sink.path.open(encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    return json.loads(lines[before])["trace_id"]


def _line_count(service: RagService) -> int:
    sink = service.tracer.sink
    if sink is None or not sink.path.exists():
        return 0
    with sink.path.open(encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def generate(
    service: RagService, now: datetime, *, days: int, per_day: int, seed: int, clean_offset: int = 1
) -> dict[str, Any]:
    rng = random.Random(seed)
    pool = candidate_pool(range(6))
    rng.shuffle(pool)
    questions = [q["question"] for q in EVAL_QUESTIONS]
    summary: dict[str, Any] = {"requests": 0, "skipped_drill_day_failures": 0, "by_day": {}}
    cursor = 0
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for offset in range(days - 1, -1, -1):
        day = midnight - timedelta(days=offset)
        stamps = _timestamps(rng, day, per_day, until=now if offset == 0 else None)
        is_drill_day = offset == clean_offset
        written = 0
        for moment in stamps:
            kind = rng.choices([k for k, _ in INPUT_MIX], weights=[w for _, w in INPUT_MIX])[0]
            user = rng.choice(ADJUSTERS)
            if kind == "policy_question":
                text = rng.choice(questions)
            else:
                while True:
                    candidate = pool[cursor % len(pool)]
                    cursor += 1
                    if (
                        is_drill_day
                        and candidate.family in EXCLUDED_FAMILIES
                        and answers_covered(service, kind, candidate.text)
                    ):
                        summary["skipped_drill_day_failures"] += 1  # real failure, kept out of the drill day
                        continue
                    break
                text = candidate.text
            run_request(service, kind, text, user=user, when=moment)
            written += 1
        summary["by_day"][day.date().isoformat()] = written
        summary["requests"] += written
    return summary


def seal(trace_id: str, directory: Path) -> None:
    salt = secrets.token_hex(8)
    digest = hashlib.sha256((salt + trace_id).encode()).hexdigest()
    (directory / "sealed.json").write_text(json.dumps({"salt": salt, "sha256": digest}), encoding="utf-8")


def sealed_matches(trace_id: str, directory: Path) -> bool:
    sealed = json.loads((directory / "sealed.json").read_text(encoding="utf-8"))
    guess = hashlib.sha256((sealed["salt"] + trace_id).encode()).hexdigest()
    return secrets.compare_digest(guess, sealed["sha256"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--per-day", type=int, default=110)
    parser.add_argument("--seed", type=int, default=20261013)
    parser.add_argument("--plant-seed", type=int, default=None, help="plant one real failure (sealed); omit for traffic only")
    parser.add_argument("--now", help="treat this ISO time as now (default: the wall clock)")
    parser.add_argument("--drill-day", choices=["yesterday", "today"], default="yesterday", help="the day that holds the plant")
    parser.add_argument("--pre-fix", action="store_true", help="serve the traffic with carry_clause_stem=false (the system before the fix)")
    arguments = parser.parse_args()

    arguments.dir.mkdir(parents=True, exist_ok=True)
    log = arguments.dir / "traces.jsonl"
    log.unlink(missing_ok=True)
    now = datetime.fromisoformat(arguments.now).replace(tzinfo=timezone.utc) if arguments.now else datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        # --pre-fix replays a day served by the system as it was before the Week 11 retrieval fix
        service = build_service(Path(tmp), log, **({"carry_clause_stem": False} if arguments.pre_fix else {}))
        offset = 1 if arguments.drill_day == "yesterday" else 0
        summary = generate(service, now, days=arguments.days, per_day=arguments.per_day, seed=arguments.seed, clean_offset=offset)
        if arguments.plant_seed is not None:
            trace_id = plant(
                service, now, plant_seed=arguments.plant_seed, pool=candidate_pool(range(20)), avoid_digests=set(), day_offset=offset
            )
            seal(trace_id, arguments.dir)  # the id itself is never printed or stored
            when = "sometime yesterday" if offset else "earlier today"
            (arguments.dir / "complaint.txt").write_text(COMPLAINT.replace("sometime yesterday", when) + "\n", encoding="utf-8")
            summary["planted"] = "sealed (only a salted hash of the trace id is kept)"
    (arguments.dir / "now.txt").write_text(now.isoformat(), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"log: {log}")


if __name__ == "__main__":
    main()

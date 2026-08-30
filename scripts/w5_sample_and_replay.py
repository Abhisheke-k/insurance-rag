"""Week 5: draw the seeded random sample of 20 traces, and replay one of them.

    python -m scripts.w5_sample_and_replay

Prints the seed and the 20 selected trace_ids (paste both into the write-up),
then replays one trace from the trace file alone -- no live vector store, no
original PDFs -- and prints original vs replayed output side by side.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from app.claims import ClaimSummaryGenerator
from app.config import Settings
from app.tracing import read_traces, replay_trace

ROOT = Path(__file__).resolve().parent.parent
TRACE_LOG = ROOT / "coursework" / "w5" / "traces.log"

SAMPLE_SEED = 20260831  # today's date -- see coursework/w5/notes.md
SAMPLE_SIZE = 20
REPLAY_TRACE_ID = None  # if None, picked with the same seeded RNG


def main() -> None:
    traces = read_traces(TRACE_LOG)
    trace_ids = [t.trace_id for t in traces]

    rng = random.Random(SAMPLE_SEED)
    sample_ids = sorted(rng.sample(trace_ids, SAMPLE_SIZE))
    print(f"seed={SAMPLE_SEED}  population={len(trace_ids)}  sample_size={SAMPLE_SIZE}")
    print("sampled trace_ids:", sample_ids)

    replay_id = REPLAY_TRACE_ID or rng.choice(sample_ids)
    record = next(t for t in traces if t.trace_id == replay_id)

    generator = ClaimSummaryGenerator(Settings(llm_provider="stub"))
    replayed = replay_trace(record, generator=generator)

    print(f"\n=== replaying {replay_id} (from the trace file alone) ===")
    print("prompt_version:", record.prompt_version)
    print("model:", record.model)
    print("params:", record.params)
    print("retrieved chunk_ids:", [c["chunk_id"] for c in record.retrieved])
    print("\n--- original (stored) output ---")
    print(json.dumps(record.output, indent=2))
    print("\n--- replayed output ---")
    print(json.dumps(replayed, indent=2))

    by_field_match = {k: (record.output.get(k) == replayed.get(k)) for k in record.output}
    print("\nfield-by-field match:", by_field_match)

    index_path = ROOT / "coursework" / "w5" / "sample_20.json"
    index_path.write_text(
        json.dumps({"seed": SAMPLE_SEED, "sample_size": SAMPLE_SIZE, "trace_ids": sample_ids, "replayed": replay_id}, indent=2),
        encoding="utf-8",
    )
    print(f"\nwrote {index_path}")


if __name__ == "__main__":
    main()

"""Week 4 eval: baseline (dense) vs after (hybrid BM25+RRF) on the frozen golden set.

    python -m scripts.w4_eval

Prints the before/after hit-rate@3 table, p50 latency, and the MMR bonus
measurement, and writes ``coursework/w4/eval_raw.json`` with everything needed
to write up the R/G/Not-in-Corpus tally by hand (which requires reading the
actual retrieved passages, not just pass/fail booleans).
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

from scripts.w4_golden_set import GOLDEN_SET_PATH, ROOT, build_corpus_service

REPEATS = 5  # per-query latency measurements, for a stable p50


def load_golden_set() -> list[dict]:
    with GOLDEN_SET_PATH.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def run_mode(mode: str, golden: list[dict], *, mmr_lambda: float | None = None) -> dict[str, Any]:
    service = build_corpus_service(retrieval_mode=mode, chroma_dir=ROOT / ".eval_scratch" / f"w4_eval_{mode}_{mmr_lambda}")
    if mmr_lambda is not None:
        service.settings.mmr_lambda = mmr_lambda

    per_question = []
    latencies_ms: list[float] = []
    for row in golden:
        # First call warms any lazy state (BM25 index build); only subsequent
        # calls are timed, so index construction never pollutes retrieval latency.
        hits = service.retrieve(row["question"], top_k=3)
        samples = []
        for _ in range(REPEATS):
            t0 = time.perf_counter()
            service.retrieve(row["question"], top_k=3)
            samples.append((time.perf_counter() - t0) * 1000)
        latencies_ms.extend(samples)

        top3_ids = [h.chunk_id for h in hits]
        hit = row["chunk_id"] in top3_ids
        per_question.append(
            {
                "id": row["id"],
                "question": row["question"],
                "hard_token": row["hard_token"],
                "gold_chunk_id": row["chunk_id"],
                "top3": [
                    {"chunk_id": h.chunk_id, "score": round(h.score, 4), "section": h.section_path, "snippet": h.text[:160]}
                    for h in hits
                ],
                "hit": hit,
                "median_latency_ms": round(statistics.median(samples), 3),
            }
        )

    hits = sum(1 for q in per_question if q["hit"])
    return {
        "mode": mode,
        "mmr_lambda": mmr_lambda,
        "hit_rate_at_3": hits / len(golden),
        "hits": hits,
        "total": len(golden),
        "p50_latency_ms": round(statistics.median(latencies_ms), 3),
        "per_question": per_question,
    }


def _token_set(text: str) -> set[str]:
    import re

    return set(re.findall(r"[a-z0-9]+", text.lower()))


def diversity_at_3(top3_texts: list[str]) -> float:
    """Mean pairwise Jaccard DISTANCE between top-3 chunk texts (0=identical, 1=disjoint)."""
    sets = [_token_set(t) for t in top3_texts]
    pairs = [(i, j) for i in range(len(sets)) for j in range(i + 1, len(sets))]
    if not pairs:
        return 0.0
    distances = []
    for i, j in pairs:
        union = sets[i] | sets[j]
        inter = sets[i] & sets[j]
        distances.append(1 - (len(inter) / len(union) if union else 0.0))
    return sum(distances) / len(distances)


def main() -> None:
    golden = load_golden_set()

    dense = run_mode("dense", golden)
    hybrid = run_mode("hybrid_bm25_rrf", golden)

    print(f"BASELINE  (dense)            hit-rate@3 = {dense['hits']}/{dense['total']} = {dense['hit_rate_at_3']:.0%}"
          f"   p50={dense['p50_latency_ms']:.2f}ms")
    print(f"AFTER     (hybrid_bm25_rrf)  hit-rate@3 = {hybrid['hits']}/{hybrid['total']} = {hybrid['hit_rate_at_3']:.0%}"
          f"   p50={hybrid['p50_latency_ms']:.2f}ms")
    print()
    print(f"{'id':4} {'hard':18} {'dense':6} {'hybrid':7} question")
    for d, h in zip(dense["per_question"], hybrid["per_question"]):
        assert d["id"] == h["id"]
        print(f"{d['id']:4} {str(d['hard_token']):18} {str(d['hit']):6} {str(h['hit']):7} {d['question'][:70]}")

    # -- Bonus: MMR over the fused candidate list, one query (H1) as the case study --
    print("\n--- MMR bonus (Week 4 bonus challenge) ---")
    h1 = next(row for row in golden if row["id"] == "H1")
    mmr_results = {}
    for lam in [None, 0.7, 0.5, 0.3]:
        service = build_corpus_service(retrieval_mode="hybrid_bm25_rrf", chroma_dir=ROOT / ".eval_scratch" / f"w4_mmr_{lam}")
        service.settings.mmr_lambda = lam
        hits = service.retrieve(h1["question"], top_k=3)
        top3_ids = [h.chunk_id for h in hits]
        div = diversity_at_3([h.text for h in hits])
        hit = h1["chunk_id"] in top3_ids
        mmr_results[str(lam)] = {"top3": top3_ids, "diversity": round(div, 3), "hit": hit}
        print(f"lambda={lam!s:5}  hit={hit!s:5}  diversity={div:.3f}  top3={top3_ids}")

    out = {"dense": dense, "hybrid_bm25_rrf": hybrid, "mmr_bonus_h1": mmr_results}
    out_path = ROOT / "coursework" / "w4" / "eval_raw.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()

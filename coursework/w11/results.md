# Week 11 — Production: observability, cost, and the failure → test loop (Task Set D)

**The complaint:** *"an adjuster says it told a claimant something was covered when the policy excludes it, sometime yesterday"* —
no trace id, no claim number, no timestamp. This week the app got logs that can answer that, the drill was run on them, and the
failure it found was turned into permanent tests and fixed.

```bash
python -m scripts.w11_traffic --plant-seed 7                  # 3 days of real-pipeline traffic; one sealed, planted failure
python -m scripts.w11_drill start                             # prints the complaint, starts the clock
python -m app.logquery --day yesterday --decision covered ... # find it (DRILL_JOURNAL=... records every query)
python -m scripts.w11_drill submit <trace id>                 # verified against a salted hash -> drill.md
python -m scripts.w6_run_eval --embedding hashing             # the suite: RED before the fix, GREEN after
python -m scripts.w11_family_eval                             # the fix against ground truth, per family
python -m scripts.w11_cost_report && python -m scripts.w11_cost_improvement
```

| Piece | File |
|---|---|
| Traces: spans with per-step time, tokens, cost; redaction on write | [`app/observability.py`](../../app/observability.py), [`app/redaction.py`](../../app/redaction.py) |
| Prompt versions pinned by a hash lock | [`app/prompts.py`](../../app/prompts.py), `app/prompt_lock.json` |
| The support-drill query tool | [`app/logquery.py`](../../app/logquery.py) |
| The fix | [`app/clause_stem.py`](../../app/clause_stem.py) (+ `CARRY_CLAUSE_STEM`) |
| Bonus: the field that closes the gap | [`app/suspicion.py`](../../app/suspicion.py) |
| Tests | `tests/test_observability.py` (37), `test_clause_stem.py` (17), `test_suspicion.py` (9), `test_hybrid_gate.py` (3) |

## What the brief asked for → where it is

| Requirement | Result | File |
|---|---|---|
| 1. Find the planted answer from logs alone; time in mm:ss; slice named | **01:30**, slice **time + output**; 1 query surfaced it (23 candidates). *Self-run, not squadmate-timed; caveats below.* | `drill.md` |
| 2. The found trace: per-span latency, tokens, cost, prompt version, retrieved context ids | trace `279c1932…`: retrieval 1.17 ms, generation 2.12 ms (3,286 in / 176 out, $0.0208 projected), prompt `claim-summary-v1`, 5 context ids | `trace.json`, span table in `cost_by_stage.md` |
| 3. Failure → eval case; suite RED before, GREEN after, pass counts both times | **15/31 → 20/31**; the planted case and a sibling flip RED → GREEN; per-case diff shows 1 explained regression and 1 open failure | `regression_cases.jsonl`, `suite_red.txt`, `suite_green.txt` |
| 4. Fix at prompt/retrieval layer; prompt version bump; canary + rollback | retrieval fix + 5 prompt version bumps; two-line canary and rollback | `release_plan.md` |
| 5. Cost per query by stage; the 10× answer | generation is ~100% of cost; rate limit breaks first (by elimination) at ~43k tokens/min | `cost_by_stage.md`, `tenx.md` |
| Bonus: close the gap and beat your time | `output.suspect`: **23 → 2 candidates, 01:30 → 00:16** (same caveats, and then some) | `bonus.md`, `drill_round2.md` |
| Friday check: "measured cost per request **and improved it**" | passages per claim 5 → 4: **−15.8% input tokens, −12.5% cost**, recall 99% → 99% | `cost_improvement.md` |

## The loop, with the numbers that moved

1. **Found.** The ransomware claim answered *covered*, quoting exclusion item (f) — "any extortion demand, ransom payment…" — as "an insured
   peril" with no cited exclusion.
2. **Understood.** The cyber exclusion's lead-in ("this policy excludes any loss … of the following:") sits in one chunk; items (b)–(h) sit in
   the next three, with no negation cue anywhere in them. Before any fix, 112 of 280 claims whose losses the policy excludes (40%) were answered
   *covered*, and **every** ransomware claim was (40 of 40).
3. **Made permanent** *(before the fix)*: three eval cases built from the log's redacted text alone (ransomware, cloud outage, regulator's penalty),
   each asserting `must_not_decide: ["covered"]`; each checked to reproduce RED on the unfixed system, then the suite run: **15/31**.
4. **Fixed at retrieval**: carry the lead-in into list-continuation chunks. A first attempt changed *nothing* (0 of 31 cases moved) because hybrid retrieval
   rebuilt each hit field-by-field and silently dropped the new fields; fixed with `dataclasses.replace` and a regression test.
5. **Measured against ground truth, not the judge:** excluded losses answered covered **112/280 (40%) → 62/280 (22%)**
   (cloud outage 100% → 12%, ransomware 100% → 28%); covered losses wrongly denied **166/240 (69%) → 166/240 (69%)** — no over-blocking was introduced.
   Suite: **15/31 → 20/31.**

## Things this week turned up that were not the plan

- **`/ask` answered nothing in hybrid mode.** The cost table showed `ask` at $0 for all 51 requests; `output.gated` was true for every one. Hybrid
  retrieval reports fused RRF scores (≤ ~0.033) and the gate compared them with a cosine floor of 0.12. Latent since Week 4's "ship hybrid" decision (the
  default mode is dense, which hid it). Fixed with `MIN_RELEVANCE_HYBRID` and a test. *A trace field found what reading answers never would.*
- **The system denies covered claims 69% of the time** (in-zone flood claims 88%) — measured here, unchanged by this week's work, and the largest remaining
  weakness in the app. Cause: the passage the model prefers is often one containing the generic "does not cover" wording.
- **One regression, explained** (`gen_bi_exceeds_indemnity_period_0`): a packed chunk now carries an irrelevant lead-in that the model quotes. Its decision was
  always wrong; its rationale got worse. See `release_plan.md`.
- **One failure left open, deliberately** (`prod_eabf329a`, a regulator's penalty): different root cause (wrong passage preferred), so the fix correctly doesn't
  touch it. It stays RED in the suite rather than being tuned around — it is 21 of the 62 answers still wrong.
- **My own mistakes, caught by the checks built this week:** a redaction pattern that masked "claimant broke her…" as a name while *leaking* "wrist"
  (found by grepping the log for PII after the first smoke test); and a `claim_top_k` change that broke MCP/in-process parity (found by the parity test).
- **Week 5's open prediction** (a polarity-consistency check to fix verdict inversion, 35% → <10%) is still not implemented; the remaining 22% includes the stub's
  seeded verdict flips that only such a check would catch.

## Limits — read these before trusting any number

- **Not squadmate-timed.** The brief wants a named squadmate to plant and time the find. Both rounds were run by the implementer (an AI assistant) against a plant chosen by a
  seeded script whose output was never read. The finder was not naive (I had measured which families fail), so 01:30 is a floor. The harness
  (`scripts/w11_drill.py`) is built for the real run.
- **No real model.** Everything ran on the deterministic stub: tokens are estimated, cost is a projection at `claude-opus-5` list price, latency excludes model time. The
  *retrieval* findings (the lost lead-in, the recall numbers) are properties of the chunks and hold regardless of model; whether a real model gets the
  marker, and whether it needs the fifth passage, is untested.
- **The traffic is synthetic in *when* and *by whom*** (backdated timestamps, 15 invented adjusters); every record's content is produced by the real pipeline.
- **The 10× answer is by elimination** and rests on assumed volume (1,800 claims/month, from the Week 12 brief) and peak factor; I never saw the account's actual rate limit.
- **The canary is a plan.** The rollback switch and the metrics it watches are real and tested; per-user canary routing is not implemented.
- **Suite counts are a noisy instrument** (the judge's agreement with human labels was 56% in Week 6; it flipped both ways on this change), which is why the
  ground-truth table is the evidence and the suite count is the loop's gate.

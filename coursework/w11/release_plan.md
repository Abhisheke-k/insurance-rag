# Week 11 -- release record: the clause lead-in fix

## What shipped

A **retrieval-layer** change plus the **prompt version bumps** that accompany it. The retrieval change
(`app/clause_stem.py`, behind `CARRY_CLAUSE_STEM`) puts a list clause's lead-in -- "this policy excludes any loss ... of
the following:" -- in front of the continuation chunks that otherwise hold exclusion items with no sign they are
exclusions. The prompts that receive retrieved passages were told what the new `[Clause lead-in]` marker means, so
each is a new version (the lock in `app/prompt_lock.json` is hash-pinned; a changed prompt text without a bump fails
`tests/test_observability.py::test_prompt_lock_matches_the_shipped_prompts`).

| prompt | before | after | what changed in the text |
|---|---|---|---|
| `claim-summary` | `claim-summary-v1` | **`claim-summary-v2`** | + rule 7, the lead-in note |
| `claims-agent` | `claims-agent-v1` | **`claims-agent-v2`** | + rule 6, the lead-in note |
| `ask-answer` | `ask-answer-v1` | **`ask-answer-v2`** | + rule 9, the lead-in note |
| `role:claims_agent` | `role-claims_agent-v1` | **`role-claims_agent-v2`** | + rule 4 (MCP single agent) |
| `role:coverage_analyst` | `role-coverage_analyst-v1` | **`role-coverage_analyst-v2`** | + rule 3 (team analyst) |

Unchanged and still at their original versions: `role:claims_clerk`, `role:manager`, `judge` (v2). Every trace records the
prompt version of each generation span, so "which version produced this answer?" is a field, not an inference.

The version bump is the *smaller* part of this change: the stub model ignores prompt wording, so the measured effect below is
entirely from the retrieval change. Whether the added prompt rule helps a real model read the marker is untested.

## Canary and rollback (two lines, as the brief asks)

**Canary:** enable `CARRY_CLAUSE_STEM=true` for 10% of adjusters (by hashed user id) for 48 hours; promote to 100% only if
(a) `python -m app.logquery --suspect covered_quotes_list_item` falls by at least half against the previous 48 hours,
(b) mean input tokens per request rise by under 6% (measured on the suite: +4.1%), and (c) the Week 6 suite shows no
case that went green-to-red that is not explained in writing (see "Known side effect" below).

**Rollback:** set `CARRY_CLAUSE_STEM=false` -- instant, no redeploy and no re-ingest, because the stored index is never
rewritten (`tests/test_clause_stem.py::test_rollback_is_one_setting_and_leaves_the_text_byte_identical`); the prompt
bumps are harmless without the marker and can be reverted by reverting the lock commit if wanted. Trigger: any rise
in the `covered_quotes_list_item` rate, or the over-denial rate (covered losses answered `denied`) moving above its
current 69% baseline.

*The canary routing by user hash is a plan, not something this repo implements; the rollback switch and the metrics it
watches are real and tested.*

## Evidence the change did what it was for (all in this directory)

| | before | after | where |
|---|---|---|---|
| Suite pass count (28 Week 6 cases + 3 new) | **15/31** (RED: the 3 new cases fail `must_not_decide`) | **20/31** | `suite_red.txt`, `suite_green.txt`, `suite_before.json`, `suite_after.json` |
| Excluded losses answered `covered` (n=280, ground truth, no judge) | **112 (40%)** | **62 (22%)** | `family_eval.md` |
| Covered losses wrongly `denied` -- over-blocking check (n=240) | 166 (69%) | 166 (69%) | `family_eval.md` |
| Extra input tokens per request | | +133 (+4.1%), +$1.02 per 1,000 requests | `cost_by_stage.md` |

## Known side effect, and the one failure this does not fix

- **A regression in the suite, explained:** `gen_bi_exceeds_indemnity_period_0` went from pass to fail. Its decision is unchanged
  (`covered`, which the Week 6 human label says is wrong -- a 20-month interruption exceeds the 18-month cap), so the earlier
  pass was a judge false-pass. What changed is its rationale: the business-interruption claim retrieves chunk 5, a *packed* chunk
  holding one leftover cyber-exclusion item followed by unrelated Endorsement 3 text, the lead-in is now prefixed to it, and
  the simulated model quotes that lead-in as its reason. The ground-truth table shows no decision moved for that family;
  the rationale got more irrelevant. A real model may or may not be misled; scoping the marker to "governs only the list items at
  the start of this passage" is the first thing to try.
- **Not fixed -- `prod_eabf329a` (a regulator's penalty) is still answered `covered`, still RED.** Different root cause: the
  exclusion item *was* retrieved, but the simulated model preferred the "Environment Agency Flood Map" definition (word
  overlap). That is the wrong-passage-preferred mode, not a lost lead-in. It stays in the suite as a known-failing case rather than
  being deleted or tuned around; it is 21 of the 62 remaining wrong answers (regulatory fine: 88% wrong before and after).

## Also shipped this week (separate changes, each with its own switch and test)

| change | switch | evidence | rollback |
|---|---|---|---|
| Passages per claim 5 -> 4 | `CLAIM_TOP_K` (default 4; `/ask` keeps `TOP_K=5`) | `cost_improvement.md`: -15.8% input tokens, -12.5% projected cost; recall of the governing passage 99% -> 99%; suite unchanged at 20/31 | `CLAIM_TOP_K=5` |
| Hybrid-mode relevance floor | `MIN_RELEVANCE_HYBRID` (default 0 = gate off in hybrid mode) | `tests/test_hybrid_gate.py`; all 51 `/ask` requests in the drill log had been gated | `MIN_RELEVANCE_HYBRID=0.12` restores the old (broken) behaviour; do not |
| Per-request traces | `TRACE_LOG_PATH` (empty = off) | `tests/test_observability.py` | `TRACE_LOG_PATH=` |

`CLAIM_TOP_K=4` is the one change whose benefit for a *real* model is unproven: recall says the governing passage is still retrieved, not that the model
never uses a fifth passage. Watch the same two metrics as the lead-in canary, plus the judge pass rate by mode.

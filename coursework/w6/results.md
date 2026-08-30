# Week 6 — Evals: results

Feature under validation: `POST /summarize-claim` (`app/claims.py`). Judge:
`app/judge.py` + the deterministic stub LLM provider (no Anthropic/OpenAI key
configured). All numbers below are reproducible:

```bash
python -m scripts.w6_eval_set          # build the 28-case eval set
python -m scripts.w6_freeze_summaries  # freeze the 25 summaries labeled below
python -m scripts.w6_run_judge --prompt coursework/w6/judge_v1.txt --out coursework/w6/judge_v1_results.json
python -m scripts.w6_run_judge --prompt coursework/w6/judge_v2.txt --out coursework/w6/judge_v2_results.json
python -m scripts.w6_run_eval --judge coursework/w6/judge_v2.txt
```

## 1. Blind protocol — ordering evidence

[`labels_25.json`](labels_25.json) was written from a fresh read of each of
the 25 frozen summaries in [`frozen_summaries_25.json`](frozen_summaries_25.json)
and committed at **`ca0e7c1`** ("Week 6: deterministic assertions, judge v1,
blind labels (committed before judging)"), dated `2026-08-31T03:15:12+05:30`.
`coursework/w6/judge_v1_results.json` — the first time the judge was ever run
against this data in this exercise — did not exist until after that commit.
`git log --oneline -- coursework/w6/labels_25.json` shows the label file's
only commit predates every commit touching `judge_v1_results.json` /
`judge_v2_results.json`.

One honest caveat: this is a single-agent exercise with no second human
available, so "blind" means what it can mean here — labeled from a fresh
read of the citation and decision, before the judge had been run on this
data at all, not reverse-engineered from a judge verdict. It is not
independent-second-rater blindness. The file's commit-order guarantee is
real regardless.

## 2. Assertion / judge split

**4 deterministic assertions, 1 judged criterion** (`app/assertions.py`,
`coursework/w6/judge_v1.txt`):

| Assertion | Checks |
|---|---|
| `claim_number_format` | matches `CLM-YYYY-NNNNN` |
| `date_of_loss_parseable` | non-empty and parses as a known date format |
| `excess_numeric_if_present` | numeric, non-negative when not null |
| `exclusion_id_when_denied` | `cited_exclusion_id` present whenever `coverage_decision == "denied"` |

These are named in `judge_v1.txt` as explicitly **not** the judge's job —
the judge is left with exactly one criterion that needs reading
comprehension: *does the cited passage support the stated
`coverage_decision`?* Nothing else is asked of it, in either prompt version.

## 3. Agreement, before and after

**Agreement before (judge_v1): 8/25 = 32%.**
**Agreement after (judge_v2): 14/25 = 56%.**

(See [`judge_v1_results.json`](judge_v1_results.json) /
[`judge_v2_results.json`](judge_v2_results.json) for every row.) v1 is
over-lenient in one direction almost every time it's wrong: 16 of its 17
disagreements are the judge saying PASS when the human said FAIL — it
essentially never catches a bad citation, because its check ("does the whole
cited chunk contain a fixed list of negation phrases") both (a) misses
exclusion items quoted without their governing "excludes" sentence, which in
this corpus routinely lands in a different chunk once a clause is long
enough to be recursively split, and (b) cannot tell a primary exclusion from
a carve-out that switches an exclusion back off — both are negatively
worded, so a keyword check treats them identically.

## 4. The two disagreements used as few-shot examples

Both are in [`judge_v2.txt`](judge_v2.txt), verbatim, as Example 1 and 2.

**`gen_cyber_ransomware_0`** — Claim summary marked "covered", citing
`(f) any extortion demand, ransom payment, cryptocurrency transfer...`
verbatim as its rationale. v1: PASS (no cue word — "excludes" — appears in
that specific fragment). Human: FAIL (this is an enumerated exclusion list
item; ransom payments are explicitly excluded). **Verdict: the human was
right.** v1's failure is structural, not a one-off: the cyber exclusion
clause is 946 tokens, well over `CHUNK_SIZE=550`, so it gets recursively
split and the governing "this policy excludes..." sentence frequently ends
up in a chunk the citation doesn't point at.

**`gen_e17_defence_carveout_0`** — Claim summary marked "denied", citing
clause 4.4: *"Exclusion Code E-17 does not apply where the Insured can
demonstrate [defences were tested]..."* — and the notes state the defences
were tested within the required window. v1: PASS ("does not apply" reads as
negative, and v1 does not distinguish a carve-out from a primary exclusion).
Human: FAIL (the passage says the exclusion switches off; that supports
"covered", not "denied"). **Verdict: the human was right**, and this is the
more interesting error of the two — v1 wasn't merely blind to a clause, it
had the clause and inverted what it meant.

## 5. Prediction, scored

[`prediction.txt`](prediction.txt), written before iterating: **70%+
agreement after the fix.** Actual: **56%**. **The prediction was wrong** —
overshot by 14+ points. Two things I didn't account for going in:

- The lettered-item check and the carve-out/primary distinction fixed
  exactly the two example cases and their close relatives (ransomware ×2,
  the E-17 carve-out case, the cyber fire write-back case, the cyber cloud
  outage case) — that part of the prediction held.
- What I didn't predict: the topical-relevance check, run against a chunk
  that packs several unrelated clauses together (the same packing behaviour
  Week 5 flagged as a root cause), produces its own false positives and
  negatives depending on which clause happens to be quoted in the
  rationale vs. which happens to sit in the wider chunk. I fixed one
  instance of this mid-iteration (scoping the topical check to the
  rationale text, not the whole chunk) and it changed the agreement number
  twice while I was measuring it — a preview of exactly why "iterate once,
  remeasure" is not the same as "iterate until the number looks good": at
  some point you have to stop and report the number you actually have.
- A genuinely new blind spot surfaced during this round that neither
  example taught: `gen_stock_raised_covered_0` quotes `(d) loss or damage to
  stock stored below ground level unless raised at least 150 millimetres...`
  — the lettered-item check correctly flags this as an exclusion, but the
  clause carries its *own* internal carve-out ("unless raised...") that the
  notes satisfy. v2 has no mechanism for a condition nested inside the
  quoted exclusion itself, only for a *separate* carve-out clause like 4.4.
  This is a real, third distinct failure mode a v3 iteration would need to
  address.

## 6. One-command eval: pass rate by mode

Full terminal output: [`eval_run_output.txt`](eval_run_output.txt). 28 cases
(26 generated + 2 regression, tagged by the Week 5 taxonomy mode each is
expected to exercise):

```
OVERALL: 15/28 = 54%

mode                   pass   total  rate
control                3      4      75%
deductible_misapplied  1      1      100%
mislabel               4      4      100%
off_topic_citation     1      5      20%
verdict_inversion      6      14     43%
```

Read carefully: this is pass-rate-by-mode using judge_v2 + all 4 assertions,
a different measurement from the 25-case agreement number above (different
population, different scoring — agreement measures judge-vs-human on a fixed
set; this measures the full pipeline's actual pass rate). `mislabel` and
`deductible_misapplied` score highest here for the same reason they were
lower-severity in Week 5's taxonomy: the judge's single criterion (citation
supports decision) doesn't penalize a wrong `cited_exclusion_id` label at
all, only a wrong decision — exactly the split Week 6 asked for.
`off_topic_citation` is the worst-performing mode (20%, 1/5): this is the
mode the v2 topical check targets directly, and it is still the weakest,
which matches the honest limitation noted in §5. Two of the 28 cases
(`gen_e17_within_window_0/1`) fail on the **assertion**, not the judge —
`exclusion_id_when_denied` correctly catches a denial with no exclusion id
attached, which is exactly what that assertion is for.

## 7. Bonus (not attempted)

RAGAS faithfulness/context-precision was not run this pass — it needs either
a real LLM call (RAGAS's own metrics are themselves LLM-judged) or a
hand-rolled substitute, and the core Week 6 deliverables above already
consumed the available time. Flagged here rather than silently dropped.

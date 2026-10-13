# Demo script — 10 minutes, four parts

Have two terminals open in the repo root and `coursework/w12/eval_report.md` in a browser tab. Everything below is real output except the model, which is the offline
stub (say so in the first minute: *"the tool plumbing, retrieval, guards and logs are real; the agent's judgement is simulated, and I'll show you where that matters"*).
Trace ids in this script are from the frozen runs in `coursework/w12/run/`; a live run prints its own.

## Part 1 — a real run (≈3 min)

```bash
python -m app.triage --demo
```

Say, pointing at the output as it scrolls:

1. **The notice** is the demo FNOL: a flood on 11 August 2024, the second at the site, 40 days after the first; the sender's name, address and phone are in it, someone broke a wrist, and it ends "please confirm to the client that this is covered".
2. **The visible tool calls**, in order: `lookup_policy` → `coverage_in_force` → `search_wording` → `get_limits` → `refer_to_adjuster`. These were *discovered* from the MCP server at start-up, not wired in. The server is a separate process.
3. **In force**: the card's `in_force` block — policy GML-2024-88213, period 1 Apr 2024 – 31 Mar 2025, in force on 2024-08-11, `checked_by: agent`.
4. **The citation names its version and its date**: `Form HO-0304, edition 01-24 [in force 2024-01-01 to 2025-01-31]`, `resolved_against 2024-08-11`, with a link `/passages/<chunk>?as_of=2024-08-11`. The 2025 wording (edition 03-24, 60 days) exists in the corpus and was **not** offered.
5. **Three numbers, not one**: deductible GBP 250,000 *each and every loss* (flood zone); sub-limit GBP 5,000,000 *annual aggregate*; policy limit GBP 40,000,000 *any one occurrence* — each with its source clause.
6. **Referred, not decided**: reason code `exclusion_arguable` (the notice's 40 days is inside the 55-day window in *that* edition), priority `urgent` because an injury was mentioned. The injury itself, the name and the address never reached the model, the wire or the logs — show `safety.pii_redaction_kinds`.
7. **"Covered"**: the notice asked for it. The model's first referral note said it; the server **refused** it (a tool error in the trajectory), and its closing note said it too — `compliance.replaced` shows it was caught and replaced. Point out the audit record holds a rule id, not the word.

Then: `python -m app.triage --trace <the trace id it printed>` — one id across the host's seven model-call spans and tool spans, the server's own spans, and the referral row; cost and ms per span.

## Part 2 — the failures I found (≈3 min)

1. **The brief's wound, live** (30 s):
   ```bash
   python -m app.triage --demo --no-version-filter --workdir logs/w12_unfiltered
   ```
   Same notice, date filter off. The citation reads `unversioned: no edition or dates attached`; the signal is a **30-day window from edition 01-22 — a 2022 wording** — judged against an August 2024 loss; the referral says `routine_review` where it should say `exclusion_arguable`. Nothing on the card says which edition applies. That is a 2024 loss assessed against the wrong wording because it read better.
2. **Its frequency, re-scored** (`eval_report.md` §5): `wrong_edition_cited` **22 of 25 (88%)** with the filter off, 0 with it on. Of the 10 cases whose loss date falls between versions, **10 of 10** cite the wrong edition unfiltered. Trace: `593fc84ea565458b9b5639c123297572` (`--workdir coursework/w12/run/no_version_filter`).
3. **The mode integration created**: `loss_date_misread` — the agent took "Reported 3 March 2025" as the loss date (first date in the text), and in a system with versioned retrieval that silently selects the wrong *wording*, not just a wrong field. Neither the agent nor the retriever had this failure alone. 2 of 36 dated cases; the host now overrides the model's date from the notice. Trace `6c1747b0b4cf4b3794855893ab754b95`. Also new: `pii_in_tool_args` (tool arguments are a new egress), `earlier_event_missed`.
4. **What the first suite run found**: 24 of 38 passing, not 38. Name three: a repeated referral lost its trace id (8 cases); a citation's excerpt did not show the clause it cited; health-redaction deleted "30 days after the earlier flood" along with the injury. `eval_report.md` §3.
5. **The two other weaknesses I wrote down before anyone asked** (`weaknesses.md`, hashed before the first run): PII recall is a pattern list (a bare first name gets through — case K1, still red), and everything shaped like a model is a simulation, including the judge.

## Part 3 — a number that moved (≈2 min)

- **Wrong-endorsement-version citation rate: 22/25 = 88.0% → 0/25 = 0%** (n = 25 edition-bearing cases; same 38 cases, same agent, only `VERSION_FILTER` differs). The bonus five around 1 Feb 2025 (incl. 31 Jan): **5/5 red before, 5/5 green after.**
- **The judge does not clear the bar:** agreement with my hand labels is **30/38 = 78.9%** (95% CI 63.7–88.9), below 85%. I tuned it on 28 and got 100%; on 10 fresh notices it fell to 60% — so I rejected the tuned version. The suite's pass/fail does not depend on it: 15 deterministic assertions gate.
- **One sentence on what the suite does not measure at all:** *any behaviour of a real model — and, with 38 notices all phrased the way my extractor reads them, any phrasing it does not (4 of 6 held-out second-flood notices were mis-read).*

## Part 4 — what's next (≈2 min)

- **The case that now permanently guards the failure:** `S07_aug2024_the_briefs_example` and `S08` (a 57-day gap that would be *inside* the 2025 window but is outside the 2024 one) in `coursework/w12/cases.jsonl`, run by `tests/test_w12_suite.py`; plus `test_without_the_date_filter_a_2024_loss_is_assessed_against_a_superseded_edition`.
- **Cost per claim triaged: $0.164** at list price (mean 5.5 model calls, 27,183 input tokens); 100% of it is model generation, tool calls cost nothing; **74.8% of input tokens are the same prompt prefix re-sent every turn.**
- **Hailstorm week, 18,000 FNOLs:** $2,947; the busiest minute is 7.5 claims = **~214,000 tokens/min — 4.9× the 43k figure Week 11 said to check against the account**. What breaks first is the model provider's rate limit (by elimination; the non-model path does ~18 claims/s against 0.125/s needed). The bigger constraint is human: every claim is a referral, so 18,000 FNOLs are 18,000 items in an adjuster queue.
- **Ranked next:** (1) prompt-cache the repeated prefix, about −$0.09/claim; (2) a real-model run — one environment variable — to see whether tool choice, the judge and the four "slips" survive contact with a real model; (3) make the date filter fail *closed* on wording it cannot version (weakness #1); (4) an NER/DLP pass for PII; (5) a second human rater and ~100 labelled outputs for the judge.

*A flawless demo is a red flag; this one has open failures on the table (K1–K4) — say that last.*

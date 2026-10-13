# Predictions — written before the integrated suite was run on its final cases

**Written:** 2026-10-13 17:48 +05:30, before the 38-case file (`cases.jsonl`, hand-written truth) was authored and before any
suite runner for it existed. (Only the three-notice probe cited under P2 and the single demo run under P7 had been seen.) Each line
below is checked against the run in `eval_report.md` — hits and misses both.

| # | Prediction | Basis |
|---|---|---|
| P1 | With the defences on, **34–37 of 38 cases pass every assertion**. Failures, if any, are in the cited-text cases (S11/S12, business interruption) and a case where the agent anchors on the wrong clause. | The persona prefers any passage containing a "within N consecutive days" window; BI notices are the only ones where that anchor is wrong. |
| P2 | On the **12 loss-date-straddle cases**: **12/12** cite the right edition/limit with the filter on; with the filter **off**, **at most 4/12** do (the unfiltered system cites a superseded edition for most 2024 losses). | Probe on 3 notices already showed 01-22 cited for a 2024-08-11 loss with the filter off. |
| P3 | **Wrong-endorsement-version citation rate** (E-17 cases, n = 9): **~70% before → 0% after**. | Same probe. |
| P4 | **Judge agreement with hand labels: 80–90%; I put 55% on it landing below the 85% bar.** Disagreements will be "right peril, wrong clause" cards, which the topic-level rules cannot see. | Week 6's judge needed two prompt versions to get from 56% to a defensible number. |
| P5 | **Trajectory:** tool-choice accuracy ≥ 90% over ≥ 12 cases. **Outcome-vs-trajectory gap:** ≥ 2 cases where the *outcome* is right and the *trajectory* is wrong (the in-force check skipped, host backstop saves it); **0** cases where the trajectory is right and the outcome wrong. | The two "policy is active" notices are built to do this; the host owns the invariants, so a wrong outcome with a right path would need a data bug. |
| P6 | **Date filter cost on hit-rate@3 for non-straddling questions: ≈ 0 points** (not the "something" the brief expects) because this corpus is 9 chunks and the 3×k candidate pool covers all of it. The cost is real but only shows when the pool factor is forced below the corpus size; I will show it there and say so. | Arithmetic on pool size. |
| P7 | **Cost per claim triaged ≈ $0.20–0.25 projected** (7 model calls, ~38k input tokens), **latency ≈ 100–200 ms excluding model time**. | One demo run: 38,403 in / 1,592 out, $0.232. |
| P8 | **Taxonomy:** the biggest *pre-fix* mode is `wrong_edition_cited`; the integration-created mode is `loss_date_misread` (FNOL parsing × versioned retrieval — neither part had it alone); `pii_in_tool_args` and `verdict_language` are the other two that need a defence the parts did not have. | Built the defences for them. |
| P9 | **Hailstorm week (18,000 FNOLs):** the first thing to break is the **model-provider rate limit** (7 calls/claim × bursty arrival), not the MCP server, the registry or the vector store, none of which comes within 100× of their capacity here. | Week 11's by-elimination answer, now ×7 calls. |

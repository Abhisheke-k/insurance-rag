# Week 12 — Capstone, Variant D: Claims Triage & Coverage Assistant

**Start here.** One command runs the integrated system; one command runs the eval.

```bash
python -m app.triage --demo                  # an FNOL in; a card, citations, a referral and a cost out (spawns the MCP server)
python -m scripts.w12_run_suite              # the 38-case integrated suite (+4 known-failing), assertions first
python -m pytest                             # 341 passed, 2 skipped (live-model tests)
```

The model is an offline stub (no API key needed). Read [`eval_report.md`](eval_report.md) §10 for what that does and does not mean.

## The brief's requirements → where they are

| # | Requirement | Where | State |
|---|---|---|---|
| 1 | RAG behind one entry point; citations name the **endorsement version** and the **loss date** they were resolved against | `app/triage.py`, `app/versions.py`, `app/rag.py::retrieve_as_of`, `GET /passages/{id}?as_of=` | done — `wording[].version`, `resolved_against`, `link` |
| 2 | Agent loop, ≥3 tools (policy lookup, in-force vs loss date, refer-to-adjuster), MCP tools **discovered at runtime** | `app/mcp_agent.py::ToolLoopAgent`, role `claims_triage` | done — 5 tools, all discovered; vetted; the writer allowed by name |
| 3 | Own MCP server, called live by a client that isn't mine | `app/mcp_server.py` (stdio + scoped bearer HTTP) | server done and called over the protocol in tests and across a process boundary; **the outside client was not run** — [`fresh_eyes.md`](fresh_eyes.md) |
| 4 | Eval suite, one command, 25–40 cases, assertions before judges, judge agreement ≥ 85% | `scripts/w12_run_suite.py`, `cases.jsonl` (38) | done; **judge agreement 78.9% — does not clear 85%** (§7) |
| 5 | Error taxonomy re-scored on the integrated system, 4–7 modes, ≥1 NEW, one trace id each | [`eval_report.md`](eval_report.md) §5 | done — 7 modes, 3 created by integration |
| 6 | Trajectory eval, 10+ cases, tool-choice accuracy, outcome-vs-trajectory gap | §8 | done — n = 38; 92.1%; gap −28.9 pp (first run) → +7.9 pp |
| 7 | One trace id across agent + MCP hops; cost/latency per span; PII held | `app/observability.py`, `python -m app.triage --trace`, `run/` | done — `one_trace_id` 38/38; PII 2/2 in the suite, **recall unmeasured on real notes** |
| 8 | One closed failure→test loop; cost per claim; 10× plan | §3–4, §11, `scripts/w12_closed_loop.py` | done — 88% → 0%; $0.164/claim; 214k tokens/min |

## Deliverables

| Deliverable | File |
|---|---|
| Runnable repo, one command, stranger's README | this page; [`../../README.md`](../../README.md); [`../../docs/COMMANDS.md`](../../docs/COMMANDS.md) |
| Integrated suite | `scripts/w12_run_suite.py`, `scripts/w12_cases.py`, `tests/test_w12_suite.py` |
| Tuesday's seam list (reconstructed — read its first paragraph) | [`seams.md`](seams.md) |
| One-page architecture sketch | [`architecture.md`](architecture.md) |
| Eval report with stated non-coverage | [`eval_report.md`](eval_report.md) §10 |
| Top-3 weaknesses, written before the final suite (dated 2026-10-13, not "Wednesday") | [`weaknesses.md`](weaknesses.md) |
| Predictions, written before, scored after | [`predictions.md`](predictions.md), `eval_report.md` §12 |
| 10-minute, four-part demo script | [`demo_script.md`](demo_script.md) |
| Fresh-eyes note | [`fresh_eyes.md`](fresh_eyes.md) — **NOT DONE** |
| One end-to-end trace | `python -m app.triage --trace 47e1d68e4b644e988362b25749aff9fe --workdir coursework/w12/run/shipped` |

## Evidence files

`cases.jsonl` (38, hand truth) · `suite_run1_results.json` (the first run: 24/38) · `suite_results.json` (now: 38/38) · `closed_loop.json` (one-defence-off ablations, bonus, cost) ·
`labels.json` / `labels_heldout.json` + `labels_provenance.txt` / `labels_heldout_provenance.txt` (hand labels, hashed before any judge ran) · `outputs_for_labelling.json` / `heldout_outputs.json` ·
`judge_integrated.txt` (shipped), `judge_integrated_v2_rejected.txt` · `judge_results.json`, `judge_results_v1.json` · `preregistration.sha256` · `run/` (frozen traces, cards, referrals, server spans).

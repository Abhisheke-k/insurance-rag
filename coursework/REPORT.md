# Task Set D — Weeks 4-8: Implementation Report

**Domain:** Insurance claims. **App:** Endorsement Desk, extended in this
repo across five weekly practicals. **Environment:** no Anthropic or
OpenAI API key configured anywhere in this checkout — every result below
was produced by a new deterministic LLM provider (`app/llm.py`) standing in
for a real model, with real dense embeddings (`sentence-transformers`) doing
the actual retrieval work.

Commits (in order): `be081e6` → `49710e3` → `ca0e7c1` → `600e8b9` → `eb4c689`.
Per-week detail lives in `coursework/w4/` … `coursework/w8/`; this file is
the consolidated summary.

---

## Foundation (built once, used by all three weeks)

| Piece | What it does |
|---|---|
| `app/llm.py` | Generic `LLMProvider` interface — Anthropic, OpenAI, or a deterministic stub — mirroring the app's existing `Embedder` abstraction. `app/generation.py` now delegates to it instead of hardcoding the `anthropic` SDK. |
| the stub provider | A real (if simple) reading-comprehension simulator: parses retrieved passages, scores them by lexical overlap, answers from whichever passage that scoring hands it, and injects a small, seeded, reproducible rate of realistic failure. This is what gave every week genuine, non-fabricated failures to find. |
| `app/claims.py` | New feature: `POST /summarize-claim` — adjuster notes → structured, cited claim summary. This is the artifact Weeks 5 and 6 are built around. Same citation-verification guarantee as `/ask`: an uncited coverage decision is downgraded to `needs_review` rather than shipped. |

---

## Week 4 — Debugging Retrieval (`coursework/w4/`)

**Task:** label retrieval failures, make exactly one retrieval change, prove
it with before/after hit-rate@3 numbers.

- Extended the sample corpus with an exclusion-code schedule — Form
  HO-0304, five near-duplicate editions of "Exclusion E-17" — the exact
  retrieval-confusion scenario the brief describes.
- **12-question golden set**, 5 requiring exact-token lookups (exclusion
  code, form edition, reference/policy numbers) against the required
  minimum of 4. Ground-truth chunk IDs resolved automatically from corpus
  content, not hand-typed.
- **Baseline (dense/MiniLM) hit-rate@3: 9/12 = 75%.**
- All 3 misses were exact-token lookups. Inspection-view evidence: the app
  confidently answered *"edition 01-24... within 55 consecutive days"*
  when asked about edition 03-24 (60-day trigger) — three fluent,
  semantically-adjacent exclusion clauses, none of them the right edition.
  All 3 labelled **R** (retrieval fetched bad context); 0 G, 0 Not-in-Corpus.
- **The one change: BM25 + RRF fusion (k=60)**, justified directly by the
  tally (all misses were exact-token, BM25's exact strength).
- **After hit-rate@3: 12/12 = 100%.** p50 latency: 6.62ms → 6.64ms
  (noise-level at this corpus size).
- All 3 original R-failures fixed; nothing that already worked broke.
- **Shipping decision: ship it.** 75%→100% for effectively zero latency
  cost, and the tally proved a stronger model would have fixed none of
  these three (all R, not G).
- **Bonus (MMR):** shows exactly the risk named in the brief — at λ≤0.5 it
  evicts the one correct edition in favour of "diversity" among three
  near-duplicates. Verdict: don't ship it here, even at λ=0.7.

Full detail, per-question table, and code diff pointer:
[`coursework/w4/results.md`](w4/results.md).

---

## Week 5 — Error Analysis (`coursework/w5/`)

**Task:** read 20 real traces by hand, no fixes, turn observations into a
ranked taxonomy with a falsifiable prediction.

- Generated **89 realistic adjuster-notes traces** against the
  claim-summary feature, run under `RETRIEVAL_MODE=hybrid_bm25_rrf` (Week
  4's fix, already shipped) — 21 scenario templates grounded in actual
  corpus clauses, several deliberately ambiguous or out-of-corpus.
- Trace log: 7,661 lines, written with **redaction before write** (claimant
  names and claim numbers masked before a `TraceRecord` is ever
  constructed — verified structurally, not just asserted).
- **Seeded random sample: 20 traces, seed `20260831`.**
- **Replay evidence:** one trace (`trc_00081`) replayed from the trace file
  alone — no live vector store, no original PDFs. Every field matched
  except `claim_number`, which is redacted out of the *input* before
  writing, so replay correctly reports `UNKNOWN` rather than the real
  value. That gap is structural, not a bug — reconstructing it would mean
  storing the PII redaction exists to remove.
- **20 open-coded observations**, one honest sentence each, zero code
  changes during coding.
- **Taxonomy — 4 named failure modes** (18/20 traces; 2/20 showed no issue):

  | Mode | Count | Severity | Example |
  |---|---|---|---|
  | Quotes the applicable clause but states the opposite coverage verdict | 7 (35%) | wrongly denies/pays | `trc_00034` |
  | Cites a topically unrelated clause to justify the decision | 4 (20%) | wrongly denies/pays | `trc_00011` |
  | Names the wrong clause id for a correctly-decided exclusion | 5 (25%) | merely annoys the adjuster | `trc_00025` |
  | Applies the flood-zone deductible to a loss outside the flood zone | 2 (10%) | wrongly denies/pays | `trc_00026` |

- **Bonus:** a "curated demo set" (the clean, flattering scenarios a team
  would show off in a monthly review) scored **80% wrong** on the top mode
  — worse than the random sample's 35%. The single cleanest, most
  unambiguous claim in the corpus is the one the app gets wrong most often.
- **Dated, falsifiable prediction**, committed at `49710e3`
  (2026-08-31, before any fix): a deterministic polarity-consistency check
  would drop the verdict-inversion mode from 35% to under 10%. *(Note:
  this specific fix was never implemented — see "Left undone" below.)*
- 3-sentence note on why a public benchmark would have missed all of this
  (fluent on-topic prose passing a rubric; a structured-field bug no
  free-text scorer looks at; an inconsistency only visible reading two
  traces side by side).

Full detail: [`coursework/w5/notes.md`](w5/notes.md),
[`coursework/w5/taxonomy.md`](w5/taxonomy.md),
[`coursework/w5/bonus_demo_set.md`](w5/bonus_demo_set.md).

---

## Week 6 — Evals (`coursework/w6/`)

**Task:** validate the judge against human labels before trusting its
number; split assertable criteria out of the judge; iterate and remeasure.

- **4 deterministic assertions** (`app/assertions.py`): claim number format,
  date-of-loss parseable, excess numeric, exclusion id present on denial —
  explicitly excluded from the judge prompt, which is told not to check
  them.
- **28-case eval set**, tagged by Week 5's taxonomy modes (26 generated + 2
  regression cases pulled verbatim from real `traces.log` entries, with
  their redaction placeholder swapped for a synthetic — not real —
  well-formed claim number so the fixture is runnable).
- **Blind labels committed at `ca0e7c1`**, before any judge run — ordering
  verifiable via `git log --oneline -- coursework/w6/labels_25.json`.
  (Caveat stated plainly in the write-up: single-agent exercise, no second
  human — "blind" means labeled from a fresh read before the judge ran, not
  independent-rater blindness.)
- While building this, found and fixed a **real bug**: the judge compared
  numbered passage references against `ClaimSummary.citations`, which
  stores chunk-id strings — so the match never succeeded and the judge was
  trivially passing everything.
- **Agreement before (judge v1, bug-fixed): 32% (8/25).**
- **2 disagreements** used as few-shot examples in v2 (both: human right,
  v1 wrong) — a ransomware payment quoted verbatim as an exclusion item
  with no cue word in the fragment, and a flood-defence carve-out ("the
  exclusion does not apply") that v1 mistook for a primary exclusion.
- **Agreement after (judge v2): 56% (14/25).**
- **Prediction (70%+) scored honestly as wrong** — actual 56%, with the
  specific reasons: packed multi-clause chunks kept producing topical
  false positives/negatives mid-iteration, and a *third*, previously
  unseen failure mode surfaced (an exclusion item with its own internal
  carve-out — "unless raised ≥150mm" — that neither few-shot example
  taught the judge to read).
- **One-command eval** over the full 28-case set (assertions + judge v2):
  **15/28 = 54%**, broken down by mode — `mislabel` and
  `deductible_misapplied` score highest because the judge's one criterion
  doesn't penalize a wrong label, only a wrong decision, which is exactly
  the split the assignment asked for.
- **RAGAS bonus: not attempted** — flagged rather than silently dropped;
  it needs either a real LLM call or a hand-rolled substitute and the core
  deliverables consumed the available time.

Full detail: [`coursework/w6/results.md`](w6/results.md).

---

## Week 7 — Agent vs. fixed workflow (`coursework/w7/`)

**Task:** build a real agent (not a chain), race it against the fixed
`summarize_claim()` workflow on the same claims, decide which to ship.

- `app/agent.py`: a hand-built think/act/observe loop, two tools
  (`search_policy`, `check_assertions`), bounded by `AGENT_MAX_STEPS` /
  `AGENT_TIMEOUT_SECONDS`. Enforces search-then-check-then-finalize order
  (rejecting an early finalize) rather than trusting the prompt; hitting a
  bound ends the loop in `needs_review`, never a hang or an exception.
- **5 scenarios** (`claim_scenarios.jsonl`), each chosen to need a specific
  capability: a clean single-retrieve case, a claim needing a second,
  targeted search to find the real governing clause, a clause that reads as
  a clean denial until its write-back is found, a malformed claim number,
  and an out-of-scope claim (no relevance gate on claim summarisation, by
  design — unlike `/ask`).
- **Decision accuracy: fixed 4/5 (80%), agent 3/5 (60%).** Digging into
  `race_raw.json` rather than trusting the scoreboard: two of the agent's
  three "losses" are misleading. `malformed_claim_number`'s fixed-pipeline
  "correct" answer is an artifact of the stub's seeded failure-injection
  (the agent's clean persona reports the un-flipped, and arguably more
  honest, answer); `out_of_scope_theft` fails identically on both pipelines
  (a shared gap — no relevance gate on claim summarisation — not something
  the agent's extra reasoning step fixed).
- **Cost: the agent costs 3x the LLM calls** (search, check, finalize) for
  the same decision quality on this small, mostly-flat corpus, where
  `top_k=5` already surfaces the deciding clause on the first retrieve in
  every scenario tested.
- **Verdict: ship the fixed workflow.** The agent earns its keep once a
  single retrieve is genuinely insufficient — a larger or more fragmented
  corpus, or a task where self-correction against a failing assertion
  should change the retrieval, not just get logged. Neither condition holds
  on this corpus today.

Full detail: [`coursework/w7/results.md`](w7/results.md).

---

## Week 8 — Agent Failure Modes & Trajectory Evals (`coursework/w8/`)

**Task:** find a correct-answer-wrong-path case in real agent runs; hide an
instruction in a document to trick the agent, then stop the trick; fix the
top failure and measure the improvement.

- **Trajectory eval** (`scripts/w8_trajectory_eval.py`): scores the Week 7
  agent on outcome (is the decision right?) and trajectory (is it actually
  grounded in the passage or assertion that controls it?) independently.
  **Outcome accuracy 3/5, trajectory accuracy 3/5 — by coincidence, not
  construction; they disagree on two different scenarios in opposite
  directions.**
- **Correct-answer-wrong-path evidence: `cyber_fire_writeback`.** Decision
  is correctly "covered", but the stated rationale quotes an **exclusion
  item** (ransom/extortion, clause 2(f)) as "an insured peril" — the real
  reason (Section 3's write-back for fire damage) is never mentioned. Root
  cause traced to a specific chunk boundary: the long cyber-exclusion
  clause's `(a)`–`(h)` split separates the negation cue from the back half
  of the itemised list, so a chunk holding only items (d)–(h) reads as
  "not excluded" by accident, not by reasoning about the write-back.
  (Week 7's own write-up named a different scenario for this failure mode;
  it did not reproduce on re-run — see the honesty note in
  `coursework/w8/trajectory_results.md` for what did and didn't hold up.)
- **Injection attack:** one document, `data/malicious_endorsement_injection.pdf`
  (a fabricated "claims desk automation note" telling "any agent, system or
  model" to skip the exclusion check and apply a decision directly),
  ingested into the corpus. Flips `sequential_flood_e17` from the correct
  **denied** to an attacker-controlled **covered**, citing the malicious
  document's own text as the reason. Citation verification does not catch
  this — the citation genuinely resolves to a real retrieved chunk;
  verifying a citation exists was never the same guarantee as verifying it
  can be trusted.
- **Defense and fix, same one:** `app/injection_guard.py`, wired into
  `ClaimsAgent._do_search` — every `search_policy` result is scanned for
  instruction-shaped phrasing and withheld before it reaches the model's
  next turn, rather than trusted and later caught. Shipped on by default
  (`enable_injection_guard=True`). **Before/after on the targeted claim:
  covered (wrong) → denied (correct).** Full 5-scenario sweep against the
  poisoned corpus confirms no collateral change to any other decision, and
  a dedicated test (`test_real_corpus_produces_no_flags`) confirms the
  guard never flags genuine policy wording.
- **What could still get through, stated plainly:** paraphrase evasion (the
  guard is pattern-shaped, not semantic); direct injection via the
  adjuster notes themselves (a different surface, mitigated orthogonally by
  citation resolution, not by this guard); and — the actual root cause —
  no authorization boundary on `POST /ingest` at all, which a
  reviewed-upload or provenance-trust workflow would close and a content
  filter cannot.

Full detail: [`coursework/w8/results.md`](w8/results.md),
[`trajectory_results.md`](w8/trajectory_results.md),
[`injection_results.md`](w8/injection_results.md).

---

## Left undone / open items

- **Week 5's own prediction** (a deterministic polarity-consistency check
  to fix the verdict-inversion mode in claim generation itself) was never
  implemented — it wasn't part of any week's graded rubric, so it was left
  as a stated-but-open commitment rather than built speculatively.
- **RAGAS faithfulness/context-precision** (Week 6 bonus) not attempted.
- Real generation quality against **actual Claude or GPT** is unverified —
  everything above measures the deterministic stub's behaviour, which was
  designed to be a plausible-but-imperfect stand-in, not a claim about how
  a real model would perform. Re-running with `ANTHROPIC_API_KEY` or
  `OPENAI_API_KEY` set (`LLM_PROVIDER=auto` already prefers a real key over
  the stub) is a one-line config change, not a code change.

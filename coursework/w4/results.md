# Week 4 — Debugging Retrieval: results

Domain: insurance claims. App: Endorsement Desk (`app/rag.py`). Corpus: the
sample endorsement pack (`scripts/make_sample_pdf.py`) plus a new Form
HO-0304 archive document (five editions of exclusion code E-17), ingested at
`CHUNK_SIZE=200 / CHUNK_OVERLAP=40` — see "A note on chunk size" below for why
this eval uses a different setting than the app's shipped default of 550.
Embedder: `sentence-transformers/all-MiniLM-L6-v2` (real dense/semantic
embeddings, fully local — no API key). LLM: the deterministic stub provider
(`app/llm.py`), since no Anthropic/OpenAI key is configured in this
environment; retrieval numbers below do not depend on it.

Reproduce: `python -m scripts.w4_golden_set && python -m scripts.w4_eval`

---

## 1. Golden set (12 questions, known-correct chunk_id)

Full machine-readable version: [`golden_set.jsonl`](golden_set.jsonl). 5 of 12
hinge on an exact token (exclusion code, form edition, or a reference/policy
number) that a dense embedder has no structural way to represent precisely —
above the "at least 4" required.

| id | question | hard token | known-correct chunk_id |
|---|---|---|---|
| H1 | Does exclusion E-17 apply under form HO-0304 ed. 03-24? Second flood at the same site, six weeks after the first. | exclusion_code | `399904d5d2107b14::0022` |
| H2 | Under form HO-0304 edition 01-22, what's the day-count trigger for exclusion E-17? | form_edition | `bccb1c5b9d0662a7::0001` |
| H3 | What's the endorsement reference number for the E-17 sequential water intrusion exclusion? | reference_number | `399904d5d2107b14::0021` |
| H4 | What is the policy number shown on this endorsement pack? | policy_number | `399904d5d2107b14::0000` |
| H5 | Under form HO-0304 edition 09-23, what's the day-count trigger for exclusion E-17? | form_edition | `bccb1c5b9d0662a7::0003` |
| S1 | What did the flood sub-limit get bumped up to in the annual aggregate? | — | `399904d5d2107b14::0002` |
| S2 | What deductible applies if a flood loss happens inside a flood zone? | — | `399904d5d2107b14::0003` |
| S3 | How long is the waiting period on the business interruption section? | — | `399904d5d2107b14::0018` |
| S4 | How much written notice do we need to give to cancel the policy? | — | `399904d5d2107b14::0020` |
| S5 | How often does the insured need to test their flood barriers and sump pumps? | — | `399904d5d2107b14::0004` |
| S6 | Does the cyber exclusion write back cover for fire damage? | — | `399904d5d2107b14::0017` |
| S7 | How many days do we have to notify a business interruption claim? | — | `399904d5d2107b14::0019` |

These are not questions invented to make the retriever look good — H1-H5 come
directly from the failure the team lead reported (an exclusion-code lookup
almost got relied on wrong), and S1-S7 are the kind of routine coverage/limit
questions that make up most real adjuster traffic and that the app already
handles. A golden set that only contained the 5 hard ones would prove nothing
about whether the fix breaks what already works.

## 2. Baseline (before any change)

**Retriever:** dense-only — cosine search over MiniLM embeddings (`RETRIEVAL_MODE=dense`, the app's existing behaviour).

**Baseline hit-rate@3: 9/12 = 75%**

Written down before anything changed. Three misses, all on the hard-token questions: **H1, H2, H3**. H4 and H5 — also hard-token — happen to hit, which matters below.

## 3. Inspection view: the 3 misses, labelled

For each miss, the actual top-3 chunks a dense-only retriever returned, and what the app answered from them.

### H1 — MISS (R)
> "Does exclusion E-17 apply under form HO-0304 ed. 03-24?"

Top-3 retrieved: `FORM HO-0304 - EDITION 01-24` (0.757), `EDITION 09-23` (0.751), `EDITION 06-22` (0.749). The correct chunk — edition **03-24**, the one actually endorsed to this policy — is not in the top 3 at all.

**Evidence:** the app answered *"FORM HO-0304 - EDITION 01-24 ... within 55 consecutive days"* — a fluent, confidently-cited answer built entirely from the wrong edition (55-day trigger instead of the correct 60-day trigger), because edition 03-24 was never in the context the generator saw. This is the exact scenario the team lead hit: three semantically-adjacent water-damage clauses, none of them the one that applies.
**Label: R** — retrieval fetched bad context. The generator did nothing wrong with what it was given; it was given the wrong thing.

### H2 — MISS (R)
> "Under form HO-0304 edition 01-22, what's the day-count trigger for exclusion E-17?"

Top-3 retrieved: `EDITION 06-22` (0.550), `EDITION 01-24` (0.538), `EDITION 09-23` (0.523). The correct chunk (edition **01-22**, 30-day trigger) is outside the top 3 — it lands at rank 4 of 5.

**Evidence:** at the app's default `top_k=5` (not the top-3 this metric measures), edition 01-22 is barely inside the window the generator sees, and the answer does happen to come out correct in that specific case. That is not the retriever succeeding — it is generation getting lucky on a wider window than the one this metric is honestly measuring against. At `top_k=3`, or with one more distractor edition, it fails exactly like H1.
**Label: R** — retrieval ranked the correct chunk below three wrong ones; only accidentally recoverable by a wider context window.

### H3 — MISS (R)
> "What's the endorsement reference number for the E-17 sequential water intrusion exclusion?"

Top-3 retrieved: `4.3 Exclusion Code E-17` (0.705, main pack), `EDITION 06-22` (0.696, archive), `4.4 Interaction...` (0.685, main pack). The correct chunk — the endorsement's own intro clause carrying `END-2024-0417` — is rank 4.

**Evidence:** the app answered from `4.3 Exclusion Code E-17` and never surfaced `END-2024-0417`; asked for a reference number, it had no chunk containing one anywhere in view. The retriever correctly identified the *topic* (E-17) but not the specific clause the fact lives in, because "reference number" carries almost no embedding weight next to the surrounding legal prose.
**Label: R** — correct topic, wrong clause; the fact itself was never in context.

### Tally

| Label | Count | Questions |
|---|---|---|
| **R** (retrieval fetched bad context) | 3 | H1, H2, H3 |
| **G** (model misused good context) | 0 | — |
| **Not-in-Corpus** | 0 | — |

All three misses are R. No G-failures showed up in this golden set, and that
is itself informative, not an omission: these 5 hard-token questions were
built specifically to probe retrieval's handling of exact identifiers, and
every miss traces to the retriever never putting the right clause in front of
the model — not to the model mishandling a clause it did receive. (G-failures
do exist elsewhere in this app — see the claim-summary error analysis in
`coursework/w5/`, a different feature with a different failure shape.)

## 4. The one change

**Chosen: BM25 + RRF fusion (k=60)**, not a cross-encoder rerank.

Justified directly by the tally: all 3 misses are R-type failures on
*exact-token* lookups — an exclusion code, a form edition string, a reference
number — which is precisely BM25's strength and precisely what a dense
embedding cannot represent (a real number cannot preserve an arbitrary
discrete identifier; "01-22" and "09-23" and "03-24" collapse into
approximately the same semantic neighbourhood). A cross-encoder rerank would
still be scoring based on learned semantic relevance over the same top-25
dense candidates — it does not add a channel that rewards exact string
overlap the way BM25 does, so it is not the fix the tally points at. Per the
"exactly one change" rule, BM25+RRF is implemented as a new `RETRIEVAL_MODE`
(`app/hybrid_retrieval.py`, wired into `app/rag.py`); nothing else about
chunking, embeddings, or the generator changed between the two runs.

## 5. After

**Retriever:** `RETRIEVAL_MODE=hybrid_bm25_rrf` — BM25 over the same chunk
text, fused with the same dense ranking via Reciprocal Rank Fusion, k=60.

**After hit-rate@3: 12/12 = 100%**

| | Before (dense) | After (hybrid BM25+RRF) |
|---|---|---|
| hit-rate@3 | **9/12 = 75%** | **12/12 = 100%** |
| p50 latency / query (5 reps × 12 questions, in-process) | **6.62 ms** | **6.64 ms** |

Latency cost is noise-level (+0.02ms) at this corpus size (32 chunks): BM25
over a few dozen documents is sub-millisecond, and the dense embedding call —
identical in both modes — dominates total latency either way. This will not
generalise to a much larger corpus without re-measuring, but on this corpus
the fix is free.

## 6. Per-question: fixed / unfixed / still-broken

| id | dense (before) | hybrid (after) | outcome |
|---|---|---|---|
| H1 | MISS | HIT | **fixed** |
| H2 | MISS | HIT | **fixed** |
| H3 | MISS | HIT | **fixed** |
| H4 | HIT | HIT | untouched (already fine) |
| H5 | HIT | HIT | untouched (already fine) |
| S1 | HIT | HIT | untouched (already fine) |
| S2 | HIT | HIT | untouched (already fine) |
| S3 | HIT | HIT | untouched (already fine) |
| S4 | HIT | HIT | untouched (already fine) |
| S5 | HIT | HIT | untouched (already fine) |
| S6 | HIT | HIT | untouched (already fine) |
| S7 | HIT | HIT | untouched (already fine) |

**All three original R-failures (H1, H2, H3) are fixed.** Nothing that was
already working broke — the 7 semantic questions and the 2 hard-token
questions that already hit stay hits, so the fix is a strict improvement on
this golden set, not a trade. (H4 and H5 were never broken; they are useful
as a check that the change doesn't regress what dense already got right.)

## 7. Shipping decision

**Ship BM25+RRF fusion.** 75%→100% hit-rate@3 on a golden set where the
misses were exactly the ones a claims manager would find unacceptable
(confidently answering from the wrong exclusion edition), for a measured
latency cost indistinguishable from zero at this corpus size. This is not a
"swap the model" fix — the tally showed 3/3 misses were R, meaning a stronger
generator would have had nothing correct to work from either; a new model
changes none of these three. If a larger corpus later shows BM25 adding real
latency, the number to re-check is p50 at that scale, not this one.

---

## 8. Bonus: MMR over the fused candidate list

Case study: H1 ("does E-17 apply under ed. 03-24"), whose fused top-3 without
MMR is exactly the scenario in the problem statement — three near-duplicate
exclusion-edition chunks, only one of them correct:

| λ | hit@3 (H1) | top-3 diversity (mean pairwise Jaccard distance) | top-3 |
|---|---|---|---|
| off (RRF only) | **HIT** | 0.400 | ed.01-24, ed.09-23, **ed.03-24 (correct)** |
| 0.7 | **HIT** | 0.642 | ed.01-24, ed.03-24 (via 4.4), **ed.03-24 (correct)** |
| 0.5 | **MISS** | 0.862 | ed.01-24, unrelated (4.2 waiting-period), unrelated (4.4) |
| 0.3 | **MISS** | 0.914 | ed.01-24, two more unrelated chunks |

Diversity goes up monotonically as λ drops, exactly as designed — and at
λ≤0.5 it buys that diversity by evicting the one correct chunk in favour of
chunks that are merely *different*, not more relevant. This is precisely the
risk named in the brief: MMR does not know which candidate is correct, only
which ones look alike, and three near-duplicate exclusion editions are
different by design (that is what an edition history is), so MMR happily
trades the right one away for variety.

**Verdict: do not ship MMR here, even at λ=0.7.** λ=0.7 keeps the hit but buys
very little (three of five distinct token sets were already achieved by RRF
alone once BM25 is in the fusion — the real diversity problem, three
same-topic chunks crowding out everything else, is what BM25+RRF already
fixes for the *questions that need a specific edition*). Below λ=0.7 the
correct edition gets evicted outright. MMR is the right tool when several
genuinely different good answers are competing for the same slots; it is the
wrong tool when there is exactly one correct near-duplicate among several
wrong ones, which is this corpus's whole point.

---

## 9. Code diff

The one retrieval change: `app/hybrid_retrieval.py` (new — BM25 index, RRF
fusion, MMR), wired into `RagService.retrieve()` in `app/rag.py` behind
`RETRIEVAL_MODE`, plus `VectorStore.all_chunks()` (`app/store.py`) so BM25 has
something to index. Full diff: `git show <commit>` for the commit tagged
`w4-retrieval-change` (see the top-level report for the hash) — or:

```bash
git diff <before-commit> -- app/hybrid_retrieval.py app/rag.py app/store.py app/config.py
```

Config additions (`app/config.py`): `RETRIEVAL_MODE` (`dense` |
`hybrid_bm25_rrf`), `RRF_K` (default 60), `BM25_CANDIDATES` (default 25),
`MMR_LAMBDA` (default unset).

## 10. A note on chunk size

This golden set is built at `CHUNK_SIZE=200/CHUNK_OVERLAP=40`, not the app's
shipped default of 550. At 550, the exclusion-code archive's five editions —
each ~90-160 tokens — pack into a single ~500-token chunk together with
unrelated general-conditions text, which makes both the retrieval problem and
its fix invisible (there is nothing to individually rank). A reference
schedule that is looked up one code at a time benefits from being
individually addressable the way narrative endorsement clauses do not; this
is a deliberate, documented choice for *this* corpus, not a proposed change
to `CHUNK_SIZE=550` for the shipped endorsement pack.

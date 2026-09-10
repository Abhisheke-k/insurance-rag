# Endorsement Desk — RAG over insurance endorsement packs

Ask questions about a pile of endorsement PDFs and get an answer you can hand to
an auditor: every sentence traceable to a filename, a page and a clause.

The design bias throughout is **citation correctness over cleverness**. Where a
choice was available between a smarter answer and a checkable one, this codebase
takes the checkable one — and when it cannot produce a checkable answer, it says
so instead of guessing.

```
POST /ingest  →  parse (page + clause metadata)  →  structure-aware chunking
              →  embed  →  ChromaDB

POST /ask     →  embed question  →  top-5 chunks  →  relevance gate
              →  labelled prompt  →  Claude  →  citation resolution  →  JSON
```

> **Command reference:** [`docs/COMMANDS.md`](docs/COMMANDS.md) — every command
> for running, testing, configuring, resetting and troubleshooting.
>
> **Design document:** [`docs/DESIGN.md`](docs/DESIGN.md) — architecture, data
> model, flow diagrams, endpoint reference, chunking strategy, test scenarios
> and an end-to-end walkthrough.

---

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt

cp .env.example .env              # optional; every value has a default
# set ANTHROPIC_API_KEY in .env to enable answer generation

python -m scripts.make_sample_pdf # writes data/sample_endorsement_pack.pdf
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/ui/> — drop the sample PDF into **Library**, then
ask *"What is the flood sub-limit in the annual aggregate?"*

Interactive API docs are at `/docs`.

> **Without an Anthropic key** everything except answer generation works:
> ingestion, retrieval, `/documents`, `/health`, and the out-of-scope not-found
> path (which never calls the model). `/ask` on an in-scope question returns
> `503` with an actionable message rather than a stack trace.

---

## Architecture

| Module | Responsibility |
|---|---|
| `app/parsing.py` | PyMuPDF extraction → blocks tagged with page number and section path |
| `app/chunking.py` | Structure-aware recursive splitter (the piece that matters most) |
| `app/tokenizer.py` | Offline token estimation used to size chunks |
| `app/embeddings.py` | Voyage → sentence-transformers → hashing, resolved at startup |
| `app/store.py` | ChromaDB (persistent) or in-memory, behind one interface |
| `app/registry.py` | JSON manifest of ingested documents, backs `GET /documents` |
| `app/generation.py` | Prompt assembly, the Claude call, and citation verification |
| `app/rag.py` | Wires it together; owns the relevance gate |
| `app/main.py` | FastAPI surface |
| `frontend/index.html` | Self-contained UI, no build step |

Nothing above `rag.py` knows which embedder or store is in use, so swapping
Voyage for MiniLM — or Chroma for something else — touches one file.

### Structure is extracted, not discarded

A generic text extractor turns an endorsement pack into a wall of prose. This
one keeps the hierarchy, because a citation is only useful if it names the
clause:

```
ENDORSEMENT NO. 1 - FLOOD SUB-LIMIT AMENDMENT
  └ SECTION 4 - CONDITIONS APPLYING TO THIS ENDORSEMENT
      └ 4.2 Exclusions
          └ 4.2.1 Erosion of the sub-limit
```

Header detection (`app/parsing.py`) recognises `ENDORSEMENT NO. n`, `SECTION n` /
`PART` / `ARTICLE` / `SCHEDULE`, multi-level numbering (`4.2`, `4.2.1`),
single-level numbering (`3.`), and all-caps headings, assigning each a depth so
that a deeper header nests and a sibling replaces. Three details that matter on
real documents:

- **Labels that share a line with body text.** `4.2 The Insurer shall not be
  liable for…` starts a clause *and* is body text. The parser distinguishes a
  title (short, no internal sentence break) from body (everything else).
- **Restated clause numbers.** Wordings routinely print `3. NOTIFICATION OF
  CLAIM` as a heading and then open the paragraph with `3.` again. Treated
  naively this forks one clause into two section paths and drops the title from
  the citation; the parser recognises the repeat and keeps the richer heading.
- **Running headers and footers.** `Page 3 of 8` is dropped by position and
  pattern, so it never lands inside a clause.

Sub-clause markers (`(a)`, `(ii)`) are deliberately *not* pushed onto the
section path — a chunk spanning `(a)`–`(e)` should cite `4.2 Exclusions`, not
`4.2(a)`. They are still detected, and used as split points (below).

---

## Chunking rationale

**The unit of meaning in an endorsement pack is the clause, so the chunker is
built around clause boundaries rather than character counts.**

A fixed sliding window that cuts `4.2 Exclusions` in half produces two chunks
that are each individually plausible and each wrong: one carries the exclusion
without its trigger, the other the trigger without its exclusion. Retrieval will
happily return either. That is the failure mode this splitter exists to prevent.

The algorithm descends only as far as it has to:

1. **Clause boundaries first.** Consecutive blocks sharing a section path are
   glued into one clause unit. A clause that fits within the target is *never*
   split; several small clauses are packed together instead, up to the target.
2. **Recursive fallback, applied only to oversized clauses.** paragraph (PDF
   block) → sub-clause marker `(a)`/`(ii)` → sentence → hard word window. Each
   level is tried in turn, and only on the segments still too large.
3. **Overlap by whole pieces.** When a chunk is flushed, trailing pieces worth
   up to `CHUNK_OVERLAP` tokens seed the next one. If the final piece alone
   exceeds the budget — common, since clauses run to several hundred tokens — a
   sentence-aligned tail of it is used instead, so overlap is never zero and
   never starts mid-sentence.

Measured on the sample pack at 550/120, realised overlap is 81–119 tokens
against a 120-token budget, always landing on a clause or sentence boundary.

### Why 500–600 tokens

- A typical numbered clause plus its sub-items is 100–250 tokens, so 500–600
  fits **two or three whole clauses** — enough context to interpret a clause
  against its neighbours without the citation ballooning to half a page.
- The measured comparison below shows recall@5 reaching 100% at 600 while
  storage overhead stays at 1.20×, versus 1.61× at 300.
- Below ~400 the splitter starts fragmenting the long exclusion wordings that
  matter most; above ~800 a citation stops being a useful pointer for a human
  checking the source.

### Metadata on every chunk

`{doc_id, filename, page, section_path, chunk_id}` — plus `page_end`,
`clause_label`, `tokens`, `chunk_index`, and `is_partial_clause` (true when the
chunk holds a slice of a clause too large to keep whole). Everything is flat and
scalar, because ChromaDB rejects nested metadata.

One deliberate asymmetry: the **section path is prepended to the text that gets
embedded, but not to the text that gets stored or cited**. A question phrased in
the language of a heading ("what does the flood endorsement exclude?") can then
match a clause whose body never repeats the heading, while the citation still
shows exactly the source text.

### Configuring it

`CHUNK_SIZE` and `CHUNK_OVERLAP` are environment variables (see `.env.example`),
so re-running ingestion at another setting is a config change:

```bash
CHUNK_SIZE=1000 CHUNK_OVERLAP=150 uvicorn app.main:app
```

Ingestion is content-addressed — `doc_id` is a hash of the file bytes — so
re-uploading the same PDF *replaces* its chunks rather than duplicating them.
Re-ingesting at a new chunk size is therefore safe and idempotent.

---

## Embeddings

Resolved at startup, in order, when `EMBEDDING_PROVIDER=auto`:

| Provider | When | Notes |
|---|---|---|
| **Voyage AI** | `VOYAGE_API_KEY` set | Asymmetric: documents and queries use different `input_type`, which measurably helps retrieval. `voyage-law-2` is worth trying on policy wordings. |
| **sentence-transformers** | package installed | `all-MiniLM-L6-v2`, fully local. First run downloads ~90 MB. |
| **Hashing** | fallback | Signed feature hashing over word uni/bi-grams and character 4-grams, stop-words removed, sub-linear TF, BLAKE2b so vectors are stable across processes. |

The hashing backend exists so that ingestion, retrieval and the entire test
suite run with no API key, no model download and no PyTorch. It is a real
lexical retriever, not a stub — but it has no semantic generalisation, so use
Voyage or MiniLM for anything real. The active provider is reported by
`GET /health` and recorded against every ingested document, so you always know
which vectors are in the store.

---

## Retrieval, and what the relevance gate is for

`/ask` embeds the question, takes the top 5 chunks by cosine similarity, and
then applies one check: if the *best* chunk scores below `MIN_RELEVANCE`, it
returns the not-found answer **without calling Claude**.

**The gate is a cost optimisation, not the correctness mechanism.** The thing
that actually prevents fabricated answers is the model contract plus citation
verification (next section). So the gate is deliberately biased *low* — letting
a doubtful question through to the model is cheap and safe; suppressing a good
question is not.

This matters because the threshold is not portable. Measured on the sample pack
with the hashing embedder, in-scope questions and seven out-of-scope questions
separate like this:

| `CHUNK_SIZE` | in-scope min top-1 | out-of-scope max top-1 | gap |
|---|---|---|---|
| 300 | 0.132 | 0.169 | **−0.037** |
| 550 | 0.130 | 0.104 | +0.026 |
| 600 | 0.176 | 0.123 | +0.053 |
| 1000 | 0.099 | 0.124 | **−0.025** |

At 300 and 1000 tokens *no* threshold separates the two populations with this
embedder — larger chunks contain more distinct vocabulary and so match anything
slightly better. The shipped default (`MIN_RELEVANCE=0.12`) is measured against
the shipped `CHUNK_SIZE=550`. **Re-tune it whenever you change the embedding
provider or the chunk size**, and treat a negative gap as "the gate is off,
the model is doing the work" rather than as a bug. Semantic embedders separate
far more widely; a lexical one is the hard case shown here.

---

## Generation and the citation guarantee

The prompt gives Claude numbered passages, each labelled with its filename,
page, section path and chunk id, and a system prompt that: restricts it to those
passages, forbids prior knowledge, requires a citation for every factual
statement, and specifies the exact not-found sentence.

Claude replies under a **structured-output schema** (`{found, answer,
citations[]}`), so the response always parses. Then, in `app/generation.py`:

1. Each claimed passage number is resolved back to a real retrieved chunk.
   Numbers that do not resolve are **discarded** and recorded in `notes` — a
   citation the model invented cannot survive this step.
2. If `found` is false, the answer is normalised to the exact not-found string
   and citations are cleared.
3. **If `found` is true but nothing resolves, the answer is withheld** and
   downgraded to not-found. An uncited answer is not shippable, however
   plausible it reads.

Model settings: `claude-opus-5` with adaptive thinking (the default on this
model) and `output_config.effort=medium`, which is a good balance for grounded
extraction; raise it via `ANSWER_EFFORT` if you see reasoning misses.
`ANSWER_MAX_TOKENS` caps thinking *and* answer together, hence the generous
16000 default. Refusals (`stop_reason: "refusal"`) and truncation are handled
explicitly rather than surfacing as a parse error.

---

## API reference

Base URL `http://127.0.0.1:8000`.

### `POST /ingest`

`multipart/form-data`, field `files`, one or more PDFs.

```bash
curl -F "files=@data/sample_endorsement_pack.pdf" http://127.0.0.1:8000/ingest
```

```json
{
  "documents": [{
    "doc_id": "77e34963221c7200", "filename": "sample_endorsement_pack.pdf",
    "pages": 4, "blocks": 48, "chunks": 7, "tokens": 3112, "replaced": false,
    "chunk_size": 550, "chunk_overlap": 120, "embedding_model": "hashing:512"
  }],
  "total_chunks": 7,
  "errors": []
}
```

A file that fails validation is reported in `errors` while the rest still
ingest; `400` only if nothing succeeded. Non-PDFs are rejected on the `%PDF`
header, not the file extension.

### `POST /ask`

```bash
curl -X POST http://127.0.0.1:8000/ask -H "Content-Type: application/json" \
     -d '{"question": "What is the flood sub-limit in the annual aggregate?"}'
```

| Field | Type | |
|---|---|---|
| `question` | string | required, 1–4000 chars |
| `top_k` | int | optional, 1–50, defaults to `TOP_K` |

```json
{
  "answer": "The flood sub-limit is amended to GBP 5,000,000 in the annual aggregate…",
  "citations": [
    { "filename": "sample_endorsement_pack.pdf", "page": 1,
      "section": "ENDORSEMENT NO. 1 - FLOOD SUB-LIMIT AMENDMENT > 2. AMENDMENT TO LIMIT",
      "chunk_id": "77e3…::0000", "score": 0.3607, "snippet": "…" }
  ],
  "chunks_used": 5,
  "found": true,
  "model": "claude-opus-5",
  "retrieval": [{ "rank": 1, "chunk_id": "…", "filename": "…", "page": 1,
                  "section": "…", "score": 0.3607, "cited": true }],
  "notes": []
}
```

`answer`, `citations` and `chunks_used` are the contract. The rest is additive:
`found` drives the amber banner in the UI, `retrieval` shows everything that was
considered and whether it was cited (useful when an answer looks wrong), and
`notes` records anything the verifier discarded.

`503` if Claude is unreachable or unconfigured; `422` on an empty question.

### `GET /documents`

```json
{
  "documents": [{ "doc_id": "…", "filename": "…", "pages": 4, "blocks": 48,
                  "chunks": 7, "tokens": 3112, "sha256": "…",
                  "ingested_at": "2026-08-14T…", "chunk_size": 550,
                  "chunk_overlap": 120, "embedding_model": "hashing:512" }],
  "count": 1, "total_chunks": 7
}
```

Each record keeps the settings it was ingested under, which is how you spot a
store holding vectors from two different configurations.

### `DELETE /documents/{doc_id}`

`204` on success, `404` if unknown. Removes the manifest entry and every chunk.

### `GET /health`

Resolved configuration and store status — chunk settings, active embedding model
and dimension, vector-store backend and path, `top_k` / `min_relevance`, and
whether generation is configured (with the reason if not).

---

## Frontend

`frontend/index.html` — one file, vanilla JS, no build step. Served at `/ui/`
(and `/` redirects there); it also works opened straight off disk, in which case
it targets `http://127.0.0.1:8000` and relies on the permissive dev CORS default.

- **Ask** — question box, passage count, example questions.
- **Answer** — presented as an issued document: a stamped verdict line, the
  prose, and any verifier notes. The **not-found case gets an amber slip** with
  its own border and stamp, so "we don't know" never looks like an answer.
- **Sources** — one card per retrieved passage in rank order, showing filename,
  page and clause path, with cited passages filled and uncited ones marked
  `NOT CITED`. Showing what was considered but *not* used is as informative as
  showing what was.
- **Library** — drag-and-drop upload plus the ingested documents and the chunk
  settings each was built under.

---

## Chunk-size comparison

Reproduce with:

```bash
python -m scripts.chunk_size_comparison --sizes 300 600 1000 --overlap 120
```

Sample endorsement pack (4 pages, 48 blocks), 8 eval questions, hashing
embedder for determinism. Also written to `docs/chunk-size-comparison.md`.

| Metric | **300 tokens** | **600 tokens** | **1000 tokens** |
|---|---|---|---|
| Chunks produced | 17 | 6 | 4 |
| Mean tokens/chunk | 238 | 503 | 682 |
| Median tokens/chunk | 236 | 569 | 789 |
| Largest chunk | 300 | 589 | 968 |
| Tokens stored | 4,052 | 3,018 | 2,729 |
| Storage vs document | 1.61x | 1.20x | 1.09x |
| Clauses kept whole | 26/26 (100%) | 26/26 (100%) | 27/27 (100%) |
| Chunks holding a split clause | 8 | 3 | 0 |
| Recall@5 | 88% | 100% | 100% |
| MRR | 0.81 | 0.94 | 0.94 |
| Mean rank of answer | 1.14 | 1.12 | 1.12 |
| Mean top-1 similarity | 0.319 | 0.246 | 0.228 |
| Out-of-scope top-1 | 0.093 | 0.123 | 0.124 |

### Reading it

- **Clause integrity holds at every size (100%).** That is the splitter doing
  its job: clauses that *can* be kept whole always are, and only genuinely
  oversized ones fragment. The row that actually varies is "chunks holding a
  split clause" — 8 at 300, 3 at 600, 0 at 1000.
- **300 tokens loses recall.** One of eight questions ("how often must flood
  barriers be tested?") drops out of the top 5 entirely: the maintenance clause
  gets separated from the heading that names it. Small chunks also cost the
  most storage — 1.61× the document, because a fixed 120-token overlap is 40% of
  a 300-token chunk.
- **600 tokens is the sweet spot** for this corpus: full recall, MRR 0.94, only
  1.20× storage, and the widest gate separation (+0.053). The shipped default of
  550 sits just inside this band.
- **1000 tokens matches 600 on retrieval but degrades the citation.** A chunk
  averaging 682 tokens is roughly a page — pointing an auditor at it is barely
  better than pointing at the document. It also collapses the gate separation
  (−0.025). Same accuracy, worse product.
- **Higher top-1 similarity at 300 is not better retrieval.** Short chunks are
  lexically concentrated, so scores rise for in-scope *and* out-of-scope
  questions alike — which is exactly why 300 has the worst gate gap.

**Conclusion: ship 500–600.** It is the only band in this test that gets full
recall, keeps clauses whole, keeps citations precise enough to check by hand,
and leaves the relevance gate usable.

Caveat: eight questions on one synthetic four-page document. The direction of
each trade-off is what generalises; the absolute numbers are not a benchmark.

---

## Tests

```bash
python -m pytest            # 64 passed, 2 skipped (live-model tests)
```

Hermetic by construction: the hashing embedder and the in-memory store mean no
API key, no model download, no writable Chroma directory. The three required
behaviours:

| Requirement | Where |
|---|---|
| Chunking never splits a numbered clause | `tests/test_chunking.py` — every clause that fits the target is asserted to appear intact in some chunk, on the real parsed sample document; plus spot-checks that `(a)`–`(e)` stay with clause 4.2 |
| A known question retrieves the right chunk in the top 5 | `tests/test_retrieval.py` — parametrised over all 8 eval questions, asserting on the *fact* the answer depends on rather than a chunk id, so it stays meaningful when boundaries move |
| An out-of-scope question returns not-found, not a fabrication | `tests/test_answering.py` — both defences: the gate (asserting the model is *not* called), and citation verification (a fabricated passage number is discarded, and an uncited answer is withheld) |

Plus `tests/test_api.py` for the HTTP contract. Two live-model tests
(`-m integration`) run only with `ANTHROPIC_API_KEY` set: they disable the gate
so the out-of-scope question really reaches Claude, and check that the in-scope
answer cites only chunks that were actually retrieved.

---

## Known limitations

- **Scanned PDFs are rejected.** Extraction is text-layer only; ingestion fails
  with a clear message rather than silently indexing nothing. OCR is out of scope.
- **Tables are flattened.** PyMuPDF block extraction turns a schedule of limits
  into prose. A real deployment needs table-aware extraction for schedules.
- **Header detection is regex-based**, tuned for UK/London-market wordings. An
  all-caps sentence in body text can be misread as a heading. It degrades
  gracefully — a wrong section path costs citation precision, not correctness —
  but a new document family should be checked against `detect_header`.
- **Token counts are estimates, and uncalibrated.** Chunk sizing uses an offline
  ~4-chars-per-token heuristic rather than `messages.count_tokens`, so ingestion
  stays deterministic and network-free; it has not been checked against Claude's
  own counter. That is acceptable for a chunk *target*, which only needs to be
  consistent, but do not read "550 tokens" as an exact figure. `tiktoken` is
  deliberately not used — it is OpenAI's tokenizer and undercounts Claude tokens.
  `StructureAwareChunker` takes a `token_counter` argument if you want to swap in
  the real one.
- **The relevance gate is not portable** across embedders or chunk sizes, as
  measured above.
- **No re-ranking and no hybrid search.** Top-5 dense retrieval only. A
  cross-encoder re-rank over the top 20, and BM25 blended with dense scores,
  are the two changes most likely to help next — the eval harness in
  `scripts/chunk_size_comparison.py` is already the place to measure them.
- **Single-process state.** The document manifest is a JSON file with atomic
  replacement, which is fine for one process and not for a horizontally scaled
  deployment.

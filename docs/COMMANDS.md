# Command reference

Every command you need, copy-pasteable. Run all of them from the project root
(`insurance-rag/`).

**Two shells, two prefixes.** Pick one and stay with it:

| | Interpreter prefix |
|---|---|
| Windows PowerShell | `.\.venv\Scripts\python.exe` |
| Windows Git Bash | `.venv/Scripts/python.exe` |
| macOS / Linux | `.venv/bin/python` |

Calling the interpreter directly always works. If you'd rather activate the
environment and type plain `python` / `pytest` / `uvicorn`:

```powershell
.\.venv\Scripts\Activate.ps1      # PowerShell — needs a permissive execution policy
source .venv/bin/activate         # macOS / Linux
```

Examples below use the PowerShell form.

---

## 1. First-time setup

Only needed on a fresh clone. Your working copy already has all of this.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

Copy-Item .env.example .env                          # optional, all values have defaults
.\.venv\Scripts\python.exe -m scripts.make_sample_pdf # writes data/sample_endorsement_pack.pdf
```

Optional embedding backends — **only if you want semantic retrieval**. This
pulls in PyTorch, roughly a 2 GB install:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-embeddings.txt
```

---

## 2. Run the server

```powershell
$env:EMBEDDING_PROVIDER='hashing'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

| URL | What |
|---|---|
| <http://127.0.0.1:8000/ui/> | The frontend |
| <http://127.0.0.1:8000/docs> | Interactive API docs |
| <http://127.0.0.1:8000/health> | Resolved configuration |

Useful variants:

```powershell
# different port
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8010

# reachable from another machine on the LAN
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# quieter logs
.\.venv\Scripts\python.exe -m uvicorn app.main:app --log-level warning
```

> **Why `EMBEDDING_PROVIDER=hashing`?** With the default `auto`, the resolver
> picks `sentence-transformers` if it is installed, which downloads ~90 MB on
> first start and produces 384-dimension vectors. The store shipped in
> `chroma_db/` holds 512-dimension hashing vectors, and ChromaDB pins
> dimensionality per collection — mixing them fails. See §7.

---

## 3. Use the API

PowerShell note: use **`curl.exe`**, not `curl`. Bare `curl` is an alias for
`Invoke-WebRequest` and takes completely different arguments.

```powershell
# health
curl.exe -s http://127.0.0.1:8000/health

# ingest one file (idempotent — same bytes replace, never duplicate)
curl.exe -F "files=@data/sample_endorsement_pack.pdf" http://127.0.0.1:8000/ingest

# ingest several at once
curl.exe -F "files=@a.pdf" -F "files=@b.pdf" http://127.0.0.1:8000/ingest

# list what is indexed, and under which settings
curl.exe -s http://127.0.0.1:8000/documents

# ask (needs ANTHROPIC_API_KEY; returns 503 without one)
curl.exe -s -X POST http://127.0.0.1:8000/ask -H "Content-Type: application/json" `
  -d '{\"question\": \"What is the flood sub-limit in the annual aggregate?\"}'

# ask with a different number of passages
curl.exe -s -X POST http://127.0.0.1:8000/ask -H "Content-Type: application/json" `
  -d '{\"question\": \"What deductible applies to flood losses?\", \"top_k\": 3}'

# out-of-scope — works with no API key, the relevance gate answers first
curl.exe -s -X POST http://127.0.0.1:8000/ask -H "Content-Type: application/json" `
  -d '{\"question\": \"What is the average annual rainfall in the Amazon basin?\"}'

# delete a document (doc_id comes from /documents or /ingest)
curl.exe -s -X DELETE http://127.0.0.1:8000/documents/77e34963221c7200
```

In Git Bash the quoting is simpler:

```bash
curl -s -X POST http://127.0.0.1:8000/ask -H "Content-Type: application/json" \
  -d '{"question": "What is the flood sub-limit in the annual aggregate?"}'
```

Pretty-print a response:

```powershell
curl.exe -s http://127.0.0.1:8000/health | .\.venv\Scripts\python.exe -m json.tool
```

To enable answers, put your key in `.env` and restart:

```
ANTHROPIC_API_KEY=sk-ant-...
```

---

## 4. Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Expected: **55 passed, 2 skipped** in about 5 seconds. The 2 skips are the
live-Claude tests, which self-skip without `ANTHROPIC_API_KEY`.

The suite is hermetic — hashing embedder plus in-memory store — so it needs no
API key, downloads nothing, and never touches `chroma_db/`. It runs whether or
not the server is up.

```powershell
# one file
.\.venv\Scripts\python.exe -m pytest tests\test_chunking.py -v

# one test
.\.venv\Scripts\python.exe -m pytest tests\test_chunking.py::test_no_clause_that_fits_is_ever_split -v

# by name pattern
.\.venv\Scripts\python.exe -m pytest -k "citation or clause" -v

# stop at the first failure, short traceback
.\.venv\Scripts\python.exe -m pytest -x --tb=short

# show print output and log statements
.\.venv\Scripts\python.exe -m pytest -s --log-cli-level=INFO

# the live-model tests (needs a key)
.\.venv\Scripts\python.exe -m pytest -m integration -v

# everything except the live-model tests
.\.venv\Scripts\python.exe -m pytest -m "not integration"

# list the test inventory without running anything
.\.venv\Scripts\python.exe -m pytest --collect-only -q
```

What each file covers:

| File | Guarantees |
|---|---|
| `tests/test_chunking.py` | Numbered clauses are never split; size, overlap, metadata |
| `tests/test_retrieval.py` | Known questions retrieve the answering chunk in top-5; ingestion is idempotent |
| `tests/test_answering.py` | Out-of-scope returns not-found; fabricated citations are discarded |
| `tests/test_api.py` | The HTTP contract for all endpoints |

---

## 5. Scripts

```powershell
# regenerate the sample endorsement pack
.\.venv\Scripts\python.exe -m scripts.make_sample_pdf

# the chunk-size comparison behind the README table
.\.venv\Scripts\python.exe -m scripts.chunk_size_comparison

# custom sizes, and write the markdown table to a file
.\.venv\Scripts\python.exe -m scripts.chunk_size_comparison `
  --sizes 300 600 1000 --overlap 120 --out docs\chunk-size-comparison.md

# compare under semantic embeddings instead of the lexical fallback
.\.venv\Scripts\python.exe -m scripts.chunk_size_comparison --provider sentence-transformers

# run against your own document
.\.venv\Scripts\python.exe -m scripts.chunk_size_comparison --pdf path\to\your.pdf

# see all options
.\.venv\Scripts\python.exe -m scripts.chunk_size_comparison --help
```

The comparison uses throwaway in-memory stores, so it never disturbs
`chroma_db/`.

---

## 6. Configuration

Every setting is an environment variable. Set it in `.env` for persistence, or
inline for one run.

```powershell
# for the current PowerShell session
$env:CHUNK_SIZE='1000'
$env:CHUNK_OVERLAP='150'
$env:EMBEDDING_PROVIDER='hashing'
$env:TOP_K='8'
$env:MIN_RELEVANCE='0.15'
$env:ANSWER_EFFORT='high'

# clear one again
Remove-Item Env:\CHUNK_SIZE
```

```bash
# Git Bash / macOS / Linux — one command only
CHUNK_SIZE=1000 CHUNK_OVERLAP=150 .venv/bin/python -m uvicorn app.main:app
```

Confirm what actually took effect — this is the fastest way to debug odd
retrieval behaviour, because it shows which embedder really loaded:

```powershell
curl.exe -s http://127.0.0.1:8000/health | .\.venv\Scripts\python.exe -m json.tool
```

Re-ingesting at a new chunk size is safe: `doc_id` is a hash of the file bytes,
so the same PDF replaces its own chunks rather than duplicating them.

```powershell
$env:CHUNK_SIZE='1000'
# restart the server, then
curl.exe -F "files=@data/sample_endorsement_pack.pdf" http://127.0.0.1:8000/ingest
```

---

## 7. Reset and cleanup

```powershell
# wipe the vector store and the document manifest
Remove-Item -Recurse -Force .\chroma_db

# remove caches
Remove-Item -Recurse -Force .\.pytest_cache
Get-ChildItem -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
```

**You must wipe `chroma_db/` whenever you change `EMBEDDING_PROVIDER`.** Vector
width is fixed per collection — hashing is 512 dimensions, MiniLM is 384, Voyage
differs again — so a switch without a wipe fails on the next ingest or query.
Changing `CHUNK_SIZE` does *not* require a wipe.

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `/ask` returns 503 | No Anthropic credentials | Put `ANTHROPIC_API_KEY` in `.env`, restart. `/health` shows the reason under `generation.error` |
| Dimension-mismatch error | Store built by a different embedder | `Remove-Item -Recurse -Force .\chroma_db`, then re-ingest |
| First start hangs ~30s | `auto` is downloading MiniLM | `$env:EMBEDDING_PROVIDER='hashing'`, or wait once |
| `curl: A parameter cannot be found that matches parameter name 'F'` | PowerShell aliases `curl` to `Invoke-WebRequest` | Use `curl.exe` |
| `Invoke-RestMethod ... 'Form'` not found | Windows PowerShell 5.1 has no `-Form` | Use `curl.exe -F` |
| Port 8000 busy | Stale process | `--port 8010`, or kill it (below) |
| `ModuleNotFoundError: app` | Running from the wrong directory | `cd` to the project root; pytest gets its path from `pyproject.toml` |
| Ingest rejects a PDF | Scanned, no text layer | Out of scope — OCR is not implemented; the error message says so |
| Activate.ps1 blocked | Execution policy | Call `.\.venv\Scripts\python.exe` directly instead |

Free port 8000:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

Check whether the server is up:

```powershell
try { Invoke-RestMethod http://127.0.0.1:8000/health -TimeoutSec 3 | ConvertTo-Json -Depth 4 }
catch { "not running" }
```

---

## 9. Typical sessions

**Demo it in two minutes**

```powershell
$env:EMBEDDING_PROVIDER='hashing'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
# open http://127.0.0.1:8000/ui/ and ask:
#   "What is the flood sub-limit in the annual aggregate?"
```

**Verify the whole thing after a change**

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m scripts.chunk_size_comparison
```

**Start completely clean**

```powershell
Remove-Item -Recurse -Force .\chroma_db
.\.venv\Scripts\python.exe -m scripts.make_sample_pdf
$env:EMBEDDING_PROVIDER='hashing'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
# then, in another terminal:
curl.exe -F "files=@data/sample_endorsement_pack.pdf" http://127.0.0.1:8000/ingest
```

**Switch to real semantic embeddings**

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-embeddings.txt
Remove-Item -Recurse -Force .\chroma_db
$env:EMBEDDING_PROVIDER='sentence-transformers'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
# re-ingest, then re-tune MIN_RELEVANCE — see README §Retrieval
```

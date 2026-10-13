# Architecture — one page

```
                    FNOL (free text: names, addresses, injuries, dates)
                                     │
        ┌────────────────────────────▼────────────────────────────────────────────────────┐
        │ HOST  python -m app.triage  (TriageService)                       one trace id ──┼─► logs/traces.jsonl
        │                                                                                  │
        │  1 read what code can verify  ── claim/policy no., labelled LOSS DATE, "N days   │
        │                                  after the earlier flood", injection patterns    │
        │  2 REDACT  names/addresses/phones/e-mail/injury clauses removed (claim/policy    │
        │            numbers kept — the tools need them); "injury mentioned" kept as a flag │
        │  3 TOOL LOOP (Week 7, hand-built)  ◄──── ★ MODEL CALLS ARE ONLY HERE ★           │
        │      discovers tools over MCP at start → vets them → offers them to the model    │
        │      model picks a tool + arguments, one per turn, 3–7 turns                     │
        │      ┌─ argument filter, before every call leaves the host ───────────────────┐  │
        │      │ loss date ≠ the one in the notice → overwritten (ambiguous → call stopped)│  │
        │      │ free text (query, facts) → redacted                                       │  │
        │      │ referral: in-force check run by the host if the agent skipped it;         │  │
        │      │           mandatory reason codes added from tool results; urgent if injury │  │
        │      └─────────────────────────────────────────────────────────────────────────┘  │
        │  4 build the CARD from the tools' STRUCTURED results (not from model text)       │
        │  5 COMPLIANCE scan of every system-written string → replace if it states a       │
        │    decision;  write the decision record (cards.jsonl)                            │
        └───────────────┬──────────────────────────────────────────────────────▲──────────┘
                tools/list, tools/call (MCP over stdio; traceparent in _meta)    │ results
        ┌───────────────▼──────────────────────────────────────────────────────┴──────────┐
        │ MCP SERVER  python -m app.mcp_server   (a separate OS process; NO model in it)    │
        │   lookup_policy      claim/policy no. → policy, period, wording on file          │──► data/policy_registry.json (synthetic, no PII)
        │   coverage_in_force  policy + loss date → in force? why (boundary rules)         │
        │   search_wording     query + policy + loss date → ONLY wording in force that day │──► RAG: parse → chunk → embed → store (Weeks 3–4)
        │                      (VersionIndex narrows; each passage tagged edition + dates) │      + app/versions.py (editions, endorsements, dates)
        │   get_limits         deductible | sub-limit | policy limit — three fields       │──► data/limits_table.json (checked against the wording by a test)
        │   refer_to_adjuster  the only writer; idempotent; refuses decision language;    │──► logs/referrals.jsonl  (redacted; duplicate = event row)
        │                      records caller + trace id                                   │
        │   scoped bearer tokens (HTTP) · output guard for injection · span per call ──────┼─► logs/server_spans.jsonl  (same trace id)
        └──────────────────────────────────────────────────────────────────────────────────┘
```

**One entry point:** `python -m app.triage --demo` (or `TriageService.triage(text)`); the eval is `python -m scripts.w12_run_suite`. The FastAPI app adds `GET /passages/{chunk_id}?as_of=YYYY-MM-DD`, the link on every citation.

## Where the model is called — and where it is not

| step | model? | why |
|---|---|---|
| redaction, date parsing, injection scan, limits, registry, in-force, wording narrowing, card, compliance scan | **no** | code owns the facts and the invariants |
| the tool loop: choose a tool, write its arguments, write a short note and reason codes | **yes — 3–7 calls per claim (mean 5.5)** | the only judgement in the system |
| the MCP server | **no** | tested with every LLM backend patched to raise |
| the eval judge | stub here | informational only; fails the 85% bar (eval_report §7) |

## Where the money goes (list price, `claude-opus-5`, tokens estimated; from `coursework/w12/run/shipped`)

| | per claim | note |
|---|---|---|
| model generation | **$0.164 mean** ($0.07 – $0.236) | **100% of the cost**; ~$0.030 per call |
| tool calls (6) | $0 | 57 ms p50 / 83 ms p95 total, `refer_to_adjuster` slowest (≈26 ms: it `fsync`s) |
| input tokens | 27,183 mean | **74.8% is the same prompt prefix re-sent every turn** (system prompt + tool catalogue + notice) |
| cheapest lever | prompt caching: up to ~$0.09 of the $0.164 | projection, not measured |

## What carries the trace id

host trace (`traces.jsonl`: root + a span per model call and per tool call, with tokens, cost, ms) → `traceparent` in each MCP request's `_meta` →
server span per call (`server_spans.jsonl`) → referral row / duplicate event (`referrals.jsonl`) → the card (`cards.jsonl`). `python -m app.triage --trace <id> --workdir <dir>` prints all four.
Claim and policy numbers are pseudonymised in the host trace (`clm_9864ad68`); they are in clear in the card and the referral queue, which are work records an adjuster must be able to act on, not logs.

## Trust boundaries

| boundary | what crosses | what is deliberately kept off it |
|---|---|---|
| FNOL → model | redacted notice | names, addresses, phones, e-mails, injury clauses |
| host → MCP server | identifiers (claim/policy number, loss date), a redacted query, a redacted note | the notice, anything personal |
| MCP server → host | registry fields (no PII by construction), wording from the operator's corpus, three figures, a referral id | an echo of any argument; ingestion (not a tool) |
| system → adjuster / claimant | wording + facts + reason codes + a referral | the words "covered", accepted, declined, payable… (`app/compliance.py`) |

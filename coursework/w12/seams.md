# Seam list — integration, in the order the seams were hit

**Provenance, stated plainly.** The brief asks for "Tuesday's unedited seam list". I did not keep a live log while integrating: this is
**reconstructed from the working session, in discovery order, on 2026-10-13 (a Tuesday)**, after the fact. Statuses are as of today. It is
complete as far as I can recall and the session record shows; it is not contemporaneous and not literally unedited. Where a seam has a test, the test is named.

Status: **fixed** (with a test) · **accepted** (a known limit I am not removing, and why) · **ran out of time** (should be fixed).

## A. Where the parts met

| # | seam | how it showed up | status |
|---|---|---|---|
| 1 | **A chunk's stored label is not where its clause is.** Archive chunk 1 is labelled *EDITION 01-24* and holds edition 09-23's text; a 2025 chunk is labelled *Endorsement 3 › General Conditions* and holds Endorsement 4's E-17 clause. | probing the versioning on the real corpus; again when the suite's citations showed the wrong section | **fixed** — editions are found from headings in the reassembled text, not from chunk labels (`tests/test_versions.py::test_an_edition_split_across_two_chunks…`); the card's section names the document holding the clause (`test_the_cited_section_names_the_document…`) |
| 2 | **Chunks overlap**, so a naive per-chunk read double-counts text and splits an edition across two chunks. | versioning probe | **fixed** — documents are reassembled from overlapping chunks before segmenting (`app/versions.py`) |
| 3 | **The Week 11 clause lead-in and the Week 12 narrowing collide**: narrowing must not discard the `[Clause lead-in]` the retrieval layer prepended. | writing the narrowing | **fixed** (`test_the_carried_clause_lead_in_survives_narrowing`) |
| 4 | **Import cycle**: the retrieval model carries version tags, the version module imports the model. | first import | **fixed** — `VersionTag` lives in `app/models.py` |
| 5 | **The host dropped what the server added**: the Week 9 `_passages()` rebuilt each passage without its `versions`, so the model never saw an edition. | first end-to-end run | **fixed** (`test_search_wording_returns_only_the_edition_in_force_and_names_it`, `test_the_straddling_loss_is_resolved…`) |
| 6 | **Pretty-printed tool JSON** cost input tokens on every later turn; compacting it broke a Week 9 tool that returns a bare string (the SDK wraps it as `{"result": …}`). | cost review; then `tests/test_mcp.py` | **fixed** — compact re-render for real structured results only |
| 7 | **The SDK hides a plain exception's message from the model** (`Error executing tool …`), so a refused referral could not tell the agent *why*. | `test_a_referral_that_states_a_decision_is_refused…` | **fixed** — `ToolError` for anticipated failures |
| 8 | **A tool that writes** could not get past a vetting policy whose rule is "read-only only". | designing `refer_to_adjuster` | **fixed** — `VetPolicy.write_allowed`, an operator-named exemption; a server cannot add itself (`test_a_write_tool_is_vetted_in_only_when_the_operator_names_it`) |
| 9 | **The server handler needs the caller's trace id and principal** but takes no context argument, and runs in a worker thread. | wanting the referral to carry the trace id | **fixed** — context variables set by the scope middleware, copied into the worker thread (`test_the_referral_carries_the_trace_id…`, `…partner_squad…`) |
| 10 | **A claim number looks like a policy number** (`CLM-2024-00417` matches `AAA-YYYY-NNNNN`). | parsing the first demo notice | **fixed** in the Python regex; **accepted** in the MCP schema pattern (pydantic's regex engine has no look-ahead), where the registry answers "unknown" |
| 11 | **Redaction vs identifiers**: pseudonymising claim/policy numbers (right for logs) leaves the agent unable to call a lookup. | first run with redaction on | **fixed** — `keep_identifiers` for text the agent must act on; identifiers still cross to the server in clear (**accepted**: it cannot look up what it cannot read) |
| 12 | **The limits table is per product, but policies are issued on different wording.** A 2023 policy issued only on the archive would have inherited Endorsement 1's figures. | adding the 2023 policy to test the old editions | **fixed** — each figure names the document that states it; a policy without that document gets `not_stated` (`test_a_policy_issued_on_other_wording_gets_no_figures…`) |
| 13 | **Idempotent referrals vs traceability**: the correct "second request writes no second row" dropped the later request's trace id. 8 of 38 cases failed `one_trace_id`. | suite run 1 | **fixed** — a `duplicate_request` event row (`test_a_repeated_referral_is_recorded…`) |
| 14 | **A citation's excerpt did not show the cited clause** (first 600 chars of a chunk). | suite run 1 (`edition_right`, `cited_text`) | **fixed** — excerpt anchored on the clause; `mentions` (`test_the_excerpt_starts_at_the_clause…`) |
| 15 | **The agent skipped the in-force check and wording/figures were still shown for a cancelled policy.** | suite run 1 (I10) | **fixed** — host runs the check before referral; wording/limits/signals withheld when "not in force" (`test_nothing_is_shown_as_applicable…`) |
| 16 | **Sentence-level health redaction deleted a triage fact** ("30 days after the earlier flood") sharing a sentence with an injury. | suite run 1 (X02) | **fixed** — clause-level masking (`test_health_masking_keeps_the_clauses…`) |
| 17 | **The injection heuristic missed "SYSTEM: you are now permitted…".** Nothing leaked or was decided (structural defences held) but `injection_suspected` was not raised. | suite run 1 (X04) | **fixed** for that phrasing (`test_a_notice_that_claims_to_be_the_system…`); the heuristic's recall on unseen attacks is **accepted** as unknown |

## B. Where the guards themselves were wrong

| # | seam | how it showed up | status |
|---|---|---|---|
| 18 | **The compliance audit trail re-emitted the forbidden word**: it recorded the rule as "uses the word 'covered'" and the offending excerpt, so the card said "covered" while suppressing it. | writing the first compliance test | **fixed** — rule ids and neutral descriptions only; a substring test (`test_the_card_never_carries_a_decision_field_or_a_decision_word…`) |
| 19 | **A flaky PII assertion**: the token "7946" (from a phone number) matched inside random hex trace ids. A clean case failed on one run and passed on others. | the last full run | **fixed** — whole-word matching (`test_a_pii_fragment_inside_a_hex_id_is_not_a_leak…`). Lesson: a false-positive leak check teaches you to ignore the real one. |
| 20 | **The judge's own prompt marker was split by a line break**, so the iterated rule never fired and "iteration" changed nothing. | the first v2 measurement (78.6% → 78.6%) | **fixed** (whitespace-normalised match) — a bug in my stub, found because the number did not move |

## C. Known, open

| # | seam | status |
|---|---|---|
| 21 | **The date filter fails open** if wording is versioned in a form the parser does not read: no interval → kept → every edition offered, with the card saying `wording_resolution: unfiltered` but the referral still going out. | **ran out of time** — the fix is to refuse (refer `wording_unverified`) when a versioned-looking document yields no interval; `weaknesses.md` #1 |
| 22 | **PII recall is a pattern list**; bare names, building names, non-UK addresses pass (case K1). | **ran out of time** — an NER pass or a DLP service; `weaknesses.md` #2 |
| 23 | **Wrong clause cited**: the agent prefers any passage with a "consecutive days" window, so fire, business-interruption and subsidence notices get the flood exclusion (K3; 12 of 38 labelled outputs unfit). | **ran out of time** — selection belongs in ranking, not in a simulated agent's habit; needs a real-model run first |
| 24 | **Earlier-flood phrasing**: only "N days after the earlier flood" or "earlier flood on <date>" is read (K2; 4 of 6 held-out second-flood notices missed). | **ran out of time** |
| 25 | **Sub-limit erosion**: paid-to-date is not held anywhere (K4). | **ran out of time** — needs a payments feed |
| 26 | **Retrieval pool starvation**: the date filter narrows a 3×k candidate pool; at 1×k answers come back 0.38 passages short. Invisible on 9 chunks. | **accepted** at pool 3; filter pushed into the store query is the real fix |
| 27 | **Day granularity**: a loss at 23:50 on a boundary day is not distinguished. | **accepted** |
| 28 | **Registry, limits table and wording are three sources of truth.** A test keeps the table honest against the wording; nothing keeps the registry honest. | **accepted** for a synthetic registry; unacceptable for production |
| 29 | **Seven model calls for three mechanical lookups.** | **ran out of time** (cost lever #2 in `eval_report.md` §11) |
| 30 | **The judge does not clear 85%** (78.9%), and tuning it made it worse on fresh notices. | **accepted** — informational only; assertions gate |
| 31 | **No fresh-eyes run.** No one from another squad ran the system or called the server. | **ran out of time / not possible alone** — `fresh_eyes.md` |
| 32 | **No real-model run.** | **ran out of time** — one environment variable; every model-shaped number here is a simulation |

# Fresh-eyes note — NOT DONE

**Status: not done.** The brief asks for "who from which squad ran it, where they stumbled, what you changed". No one else has run this system.
I (an AI assistant working in this repo) cannot supply a person from another squad, and I will not write a
note that pretends to be theirs. What follows is the form the note should take, the exact steps to hand a reviewer, and — separately, labelled as a substitute —
the closest evidence I could produce without one.

## What a reviewer should do (20 minutes, no help from me)

1. Clone the repo, `python -m venv .venv`, install `requirements.txt`, and run **one command**: `python -m app.triage --demo`. Note every place you had to guess.
2. Run `python -m pytest` (expect 341 passed, 2 skipped) and `python -m scripts.w12_run_suite` (expect 38/38 and 4/4 known-failing).
3. **Be the client that isn't mine.** Start the server yourself —
   `python -m app.mcp_server --tools lookup_policy,coverage_in_force,search_wording,get_limits,refer_to_adjuster --ingest data/sample_endorsement_pack.pdf --ingest data/form_ho0304_archive.pdf`
   (regenerate the PDFs first with `python -m scripts.make_sample_pdf` if `data/` is empty) — and call it from **your own MCP client**, not `app/mcp_client.py`. List the tools; call `search_wording` for a loss on 11 August 2024 and for 1 February 2025.
4. Try to break it: a notice with a name and address; a notice asking it to say "covered"; a loss date either side of 1 February 2025; a policy number that does not exist.
5. Fill in the table below.

## To be completed by the reviewer

| | |
|---|---|
| Reviewer / squad | *(not filled)* |
| Date, time spent | |
| Client used to call the MCP server | |
| First place you stumbled | |
| Anything in the README that was wrong or missing | |
| Did `search_wording` return the wording you expected for 11 Aug 2024? for 1 Feb 2025? | |
| What did the server hand you that you didn't expect? (check for names, addresses, injuries) | |
| What I changed because of it | |

## Substitute evidence (not a replacement)

- `tests/test_triage_tools.py` calls every triage tool **over the MCP protocol** through the SDK's own client, including over HTTP with bearer tokens: a token scoped to `search_wording` + `coverage_in_force` cannot see or call `refer_to_adjuster`, and a refused call is byte-for-byte what an unknown tool returns.
- `scripts/w12_peer_client.py` imports **nothing from `app/`** (only the MCP SDK; a test asserts that) and calls the five tools the way another squad's agent would. Its recorded output is `peer_client_output.txt`: it was handed registry fields (no personal data), wording tagged with edition and dates, three money figures, and a refusal of a referral note that said "covered"; a query stuffed with a name, address, phone number and injury was **not echoed back**; its scanner found no personal-detail pattern in any result (and a test shows the scanner can find one). It is my script, written by the person who wrote the server — which is exactly why it is not a fresh-eyes run.
- `tests/test_w12_suite.py::test_one_trace_id_crosses_the_process_boundary…` runs the server as a separate process.
None of this finds what a stranger finds — the README step that assumes a file exists, the error message that assumes context. That is exactly what this note is for, and it is empty.

# Top-3 known weaknesses — written before the integrated suite was run on its final cases

**Written:** 2026-10-13 17:46 +05:30 (a Tuesday; the brief says "Wednesday" — I am dating this when it was actually written,
not when the brief wanted it). **Unprompted:** no reviewer has seen the system. **Not edited after the suite ran**; anything
learned afterwards is in `eval_report.md` and `seams.md`, not here.

These are the three I would bet on being asked about, in the order I think they matter. I wrote them knowing the code; that makes
them informed guesses, not blind ones.

## 1. The date filter fails open when wording is not versioned in a form the parser understands

`app/versions.py` finds editions and effective dates with regular expressions fitted to *this corpus's* phrasing
(`FORM HO-0304 - EDITION 01-24`, `Effective from 1 June 2024`, `Superseded by edition … with effect from …`). A wording set that
says "Edn. 2025/1, operative 1.2.25" parses to no interval at all, and a chunk with no interval is treated as *always in force*.
Nothing downstream refuses: the card says `wording_resolution: unfiltered`, the referral still goes out, and the adjuster is shown
every edition with no dates — which is precisely the live wound in the brief (a 2024 flood assessed against 2025 wording because it read better).
I made "unknown" mean "keep" on purpose, so an unparseable document is not silently discarded; the price is that the safeguard
is only as general as the regexes. Not measured beyond one corpus.

## 2. Personal-detail redaction is a pattern list tuned to my own synthetic notices

`app/redaction.py` catches titled names, `claimant/insured/… Firstname Surname`, UK-style streets and postcodes, e-mail, long phone
numbers and sentences containing injury vocabulary. It will miss a bare name ("Priya went to hospital" is caught by the injury word; "Priya Rao arrived" is not),
a flat or building name with no street suffix, a non-UK address, a name in an unusual script. The "no PII on the wire or in the logs" result
in this report is therefore **recall against fixtures I wrote to match the patterns** (n = 2 PII notices + the unit tests), which is the weakest possible
evidence for the strongest possible claim. The mitigating design choices are real (the agent never sees the raw notice; the registry holds none; the
server returns no echo of arguments), but the measured recall on real adjuster notes is unknown.

## 3. Everything shaped like a model is a simulation — including the judge

The agent's reasoning, the four "mistakes" it makes (first-date-in-text, skipping the in-force check when told the policy is active, anchoring on
the best-matching edition, writing "covered" when asked to confirm), and the LLM judge are deterministic code I wrote. So (a) any rate of
*model* misbehaviour in this report is a function of how many notices I wrote that trigger each slip, not an estimate for a real model; (b) the
judge's "agreement with hand labels" measures how well one set of rules I wrote matches one set of labels I wrote; (c) tokens are estimated and cost is a
projection at list price; (d) the cases that feel most like "it works" — correct edition, correct figures — are properties of code and data, and would hold for
a real model only because code, not the model, owns them. What I cannot say is how often a real model picks a sensible tool order or writes a clean referral note.

---

Also known, not in the top three (so they are not hidden): the sub-limit shown is the annual aggregate, but *paid-to-date is not held anywhere*, so an adjuster could read
"GBP 5,000,000" as available when earlier payments have eroded it; dates are whole days (a loss at 23:50 on a boundary day is not distinguished); the registry and limits table are
synthetic and tiny; the policy registry and limits table are separate sources of truth from the wording (a test keeps the table honest, nothing keeps the registry honest);
cost is dominated by seven model calls for three mechanical lookups.

# Week 11 bonus -- wire the drill shut

**The brief:** add the missing log field or index that would have made the find instant, have a second bad answer planted,
beat your own time, and report both times and the one field that closed the gap.

## The one field: `output.suspect` (flag `covered_quotes_list_item`)

Round 1 found the planted trace by reading 23 "covered" answers from yesterday and *recognising* that a ransom payment is an
excluded peril. The log held the decision and the answer text, but nothing that said "this answer ought to be looked at". That
recognition is what a field can do once, on write, for every request (`app/suspicion.py`): a coverage verdict whose rationale
**quotes a lettered list item** -- `covered ... (f) any extortion demand, ransom payment ...` -- is the signature of the failure,
because in this corpus the lettered lists are exclusion lists. It is filterable: `python -m app.logquery --suspect any`.

## Both rounds

| | round 1 (before the field) | round 2 (with the field) |
|---|---|---|
| System under test | pre-fix (`carry_clause_stem` off) | same (a replay of the pre-fix day) |
| The plant | ransomware claim (sealed, seed 7) | cloud-hosting outage (sealed, seed 11) |
| Query that found it | `--day yesterday --decision covered` | `--day yesterday --suspect any` |
| **Candidates to read** | **23** | **2** |
| **Time to find (harness stopwatch)** | **01:30** | **00:16** |
| Queries before the trace appeared | 1 | 1 |
| Slice(s) | time + output | time + suspicion flag |

Reports: `drill.md` (round 1), `drill_round2.md` (round 2), `trace.json` / `trace_round2.json` (the found traces).

## How much weight these numbers bear -- not much, and here is why

- **Both runs are by the same assistant, not a squadmate**, and round 2 is *practised*: I knew the procedure, the tool, and the
  family of failure. Some of 01:30 -> 00:16 is learning, not the field.
- **The stopwatch is dominated by this assistant's own turn latency between commands.** 16 seconds and 90 seconds are both mostly
  waiting for the next tool call; the queries themselves take well under a second. The *candidate count* (23 -> 2) is the
  measure that does not depend on who is typing, and it is the one to trust.
- **The shortlist contained a decoy** (a legitimately covered claim about stock raised off the floor, which also leans on a list item),
  as the design intends: a shortlist may contain false positives, it must not miss the target.
- **The flag would not have found every failure of this kind.** `prod_eabf329a` (the regulator's penalty, still open) quotes a
  *definition* as its rationale, not a list item, so `covered_quotes_list_item` does not fire on it. The field closes the gap for the
  failure class that was drilled; it is not a general "this answer is wrong" detector, and a drill planting a regulatory-fine failure
  would be slower.
- The flag is a heuristic over the answer text and is computed from the rationale the model *wrote*; a model that paraphrases the list item
  instead of quoting it would slip past it.

## What else the drills changed in the log

Round 1 asked for "the answer's text and decision as plain keys", and that was already there. Writing the second drill's query made
one more gap visible, which I have *not* closed: there is no field for *which clause was cited* (a section path), so a query like
"covered AND cited a definitions clause" is impossible; it is the natural next index.

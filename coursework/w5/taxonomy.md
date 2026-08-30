# Week 5 — Claim-summary failure taxonomy

Feature: `POST /summarize-claim`. Sample: 20 traces, seed `20260831` (see
[`notes.md`](notes.md)). 18/20 traces showed one of the 4 modes below; 2/20
(trc_00072, trc_00087) showed no issue.

| Mode | Count | % of 20 | Severity | Example |
|---|---|---|---|---|
| **Quotes the applicable clause but states the opposite coverage verdict** | 7 | 35% | wrongly denies or wrongly pays a claim | `trc_00034` |
| **Cites a topically unrelated clause to justify the decision** | 4 | 20% | wrongly denies or wrongly pays a claim | `trc_00011` |
| **Names the wrong clause id for a correctly-decided exclusion** | 5 | 25% | merely annoys the adjuster | `trc_00025` |
| **Applies the flood-zone deductible to a loss outside the flood zone** | 2 | 10% | wrongly denies or wrongly pays a claim | `trc_00026` |
| *(no issue observed)* | 2 | 10% | — | `trc_00072` |

### 1. Quotes the applicable clause but states the opposite coverage verdict (7/20, 35%)

The app retrieves and cites the one passage that actually governs the claim
— an exclusion, a carve-out, or a conditions clause — and then states a
coverage decision that contradicts what that passage says. Both directions
occur: `trc_00034` quotes the stock-below-floor exclusion verbatim and still
says "covered"; `trc_00054` quotes clause 4.4 saying E-17 "does not apply"
and still says "denied"; `trc_00041` cites the 72-hour waiting-period clause
itself as grounds for denial regardless of whether the actual interruption
(201 hours) exceeded it. **Severity: wrongly denies or wrongly pays a
claim** — this is the mode an adjuster relying on the summary would act on
incorrectly, in either direction, and it is the most frequent mode in the
sample.

### 2. Cites a topically unrelated clause to justify the decision (4/20, 20%)

All four questions about the flood aggregate sub-limit (`trc_00011`,
`trc_00066`, `trc_00088`) were denied citing the *cyber* exclusion's Cyber
Act sub-clause — a clause about hacking, not about cumulative flood
payments — and one business-interruption claim (`trc_00030`) was decided
from the policy's front-matter aggregate-limit paragraph rather than any
business-interruption clause. **Severity: wrongly denies or wrongly pays a
claim** — every instance in this sample was a denial with no real basis in
the cited text.

### 3. Names the wrong clause id for a correctly-decided exclusion (5/20, 25%)

The coverage decision itself is right and the summary text quotes the
correct sub-clause — but `cited_exclusion_id` names the enclosing heading
(`4.1`, `4.2`) or, twice in the wider trace set, a stray letter (`h`)
borrowed from an unrelated clause's lettering, instead of the `(a)`-`(e)`
label actually quoted. **Severity: merely annoys the adjuster** — the
verdict is correct and actionable, but the audit trail pointing at *which*
exclusion applied is wrong, which matters the moment someone tries to check
the decision against the policy wording by hand.

### 4. Applies the flood-zone deductible to a loss outside the flood zone (2/20, 10%)

Two burst-water-main claims that explicitly state the site is *not* in the
mapped flood zone were still charged the GBP 250,000 flood-zone deductible,
when the same retrieved sentence names GBP 50,000 as the standard deductible
for locations outside a flood zone. **Severity: wrongly denies or wrongly
pays a claim** — the coverage call ("covered") happens to be right, but the
net payout is wrong by GBP 200,000, which is as consequential as a wrong
verdict.

---

Root cause note (not graded, but relevant to the prediction below): most of
modes 1 and 2 trace back to the same mechanical cause — at `CHUNK_SIZE=550`,
a single chunk routinely packs 2-4 unrelated clauses together (e.g. the
DEDUCTIBLE clause followed immediately by the start of `4.2 Exclusions`), so
the stub's "does this passage carry a negation cue" check runs against the
*whole packed chunk* while the "which sentence do I quote" step picks a
different sentence within it. The two steps are reading different parts of
the same chunk without knowing it.

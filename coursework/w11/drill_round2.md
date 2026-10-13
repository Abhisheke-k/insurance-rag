# Week 11 drill (round2): find the coverage answer that ignored an exclusion

**Complaint (all there was):** "an adjuster says it told a claimant something was covered when the policy excludes it, sometime yesterday"

| | |
|---|---|
| **Time-to-find** | **00:16** (under 5 minutes) |
| **Slice that found it** | **time + suspicion flag (`output.suspect`)** -- the harness printed only `time` because it did not yet know the `--suspect` flag; corrected by hand, harness mapping fixed |
| Queries run before finding it | 1 (of 2 total) |
| Timed by | self (the implementer, an AI assistant) -- NOT a squadmate |
| Planted by | scripts/w11_traffic.py (second sealed plant, seed 11, a replay of the pre-fix day with the new suspect field) |
| Clock started / trace found | 2026-10-13T11:25:23.563369+00:00 / 2026-10-13T11:25:40.117791+00:00 |
| Trace id | `e9e868eeb62240538c96570ba72df3b4` (user `usr_19a9cf7f`, claim_summary, 2026-10-12T16:54:59+00:00) |

## The query that first surfaced it

```text
python -m app.logquery --log .drill/round2/traces.jsonl --now 2026-10-13T11:25:17.761226+00:00 --day yesterday --suspect any --limit 80
```

It returned 2 trace(s); the finder then read them and picked this one.

## Every query, in order (the journal)

```text
 1. [11:25:30] matched=   2  --log .drill/round2/traces.jsonl --now 2026-10-13T11:25:17.761226+00:00 --day yesterday --suspect any --limit 80  <-- first query to contain the trace
 2. [11:25:39] matched=   1  --log .drill/round2/traces.jsonl --now 2026-10-13T11:25:17.761226+00:00 --trace e9e868ee
```

## Notes

Round 2 of the bonus. Same system state as round 1 (fix off), a different sealed plant, the new output.suspect field available. See bonus.md for the comparison and its caveats.

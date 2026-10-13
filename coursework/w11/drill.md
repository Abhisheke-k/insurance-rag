# Week 11 drill (round1): find the coverage answer that ignored an exclusion

**Complaint (all there was):** "an adjuster says it told a claimant something was covered when the policy excludes it, sometime yesterday"

| | |
|---|---|
| **Time-to-find** | **01:30** (under 5 minutes) |
| **Slice that found it** | **time, output (answer text / decision / exclusion)** |
| Queries run before finding it | 1 (of 2 total) |
| Timed by | self (the implementer, an AI assistant) -- NOT a squadmate |
| Planted by | scripts/w11_traffic.py, seeded and sealed; the implementer never read the plant |
| Clock started / trace found | 2026-10-13T11:07:55.485171+00:00 / 2026-10-13T11:09:26.245865+00:00 |
| Trace id | `279c193285c04c7b9f93373112f10c55` (user `usr_abcd06bf`, claim_summary, 2026-10-12T10:25:41+00:00) |

## The query that first surfaced it

```text
python -m app.logquery --log .drill/traces.jsonl --now 2026-10-13T11:07:12.942004+00:00 --day yesterday --decision covered --limit 80
```

It returned 23 trace(s); the finder then read them and picked this one.

## Every query, in order (the journal)

```text
 1. [11:08:01] matched=  23  --log .drill/traces.jsonl --now 2026-10-13T11:07:12.942004+00:00 --day yesterday --decision covered --limit 80  <-- first query to contain the trace
 2. [11:08:13] matched=   1  --log .drill/traces.jsonl --now 2026-10-13T11:07:12.942004+00:00 --trace 279c1932
```

## Notes and caveats (read these before trusting the 01:30)

- **Not a squadmate-timed run.** The brief wants a named squadmate to plant the failure and time the find. This was
  run by the implementer (an AI assistant) against a plant chosen by a seeded script whose output was never read.
  `scripts/w11_drill.py` is the harness for the real run; the number below should be replaced by theirs.
- **The finder was not naive.** Before the drill I had measured which claim families the pipeline answers "covered"
  wrongly (ransomware: 40 of 40), so I recognised the theme of the ransomware trace at once. A squadmate who had
  not built the system would plausibly take longer scanning the 23 candidates. Treat 01:30 as a floor, not an estimate.
- **Wall-clock includes this assistant's turn latency** between commands, which *overstates* tool time; the queries
  themselves ran in well under a second each.
- **What made it fast, and is therefore a log field I would not remove:** the answer's `decision` and the time window
  were both top-level, indexed keys, so one query ("yesterday AND covered") cut 331 records to 23. Searching only
  the *input* would have returned yesterday's ~110 requests with nothing to separate them; the complaint
  describes the output ("said it was covered").
- **What the log could not do, which cost a human step:** it has no field for "the cited text is an exclusion". The
  finder had to recognise, by reading the 23 input excerpts, that a ransom payment is the excluded peril. That
  recognition is exactly what the fix (and the eval case) will encode; see `bonus.md` for the field added to close it.

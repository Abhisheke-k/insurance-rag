# Week 10 — Multi-agent & A2A: with evidence, not fashion

**Question:** does a manager + two specialists beat the single agent on the
same claims? **Answer, on what could be measured offline: no. Keep the single
agent.** The team ties it on quality *by construction*, costs 6–30% more per
claim, and puts one more model call on the critical path. The one place the
numbers show real money to be saved is routing the specialists to a cheaper
model — and that is a quality question this run cannot answer.

```bash
python -m scripts.w10_team_vs_single                 # offline stub, ~7 s  -> race_tables.md, race_raw.json
python -m scripts.w10_team_vs_single --llm-provider anthropic --yes --repeats 3   # LIVE: spends money
```

Full tables: [`race_tables.md`](race_tables.md). Predictions, written before
the run: [`prediction.txt`](prediction.txt). Code: [`app/team.py`](../../app/team.py),
[`app/metering.py`](../../app/metering.py), [`tests/test_team.py`](../../tests/test_team.py) (18 tests).

## The team, and why it is a fair fight

| Role | Job | Tools it can see |
|---|---|---|
| **manager** | splits the work, merges the reports, owns the final summary | none — it delegates |
| **analyst** | policy research and the coverage decision | `search_policy`, `lookup_exclusion_code` |
| **clerk** | claim number & date of loss off the notes, run the deterministic checks | `check_assertions` only |

I built it in its best light, so a loss means something: the two specialist
jobs are genuinely independent (neither needs the other's output), need
different instructions, and get least-privilege tool sets; the manager
delegates to both in **one** turn and they run **in parallel** over one shared
MCP connection (`test_specialists_run_concurrently_when_parallel` proves the
overlap with a barrier). Both sides use the same MCP server, the same corpus,
the same tools, the same metering.

Two hand-off styles, because "every hand-off re-sends the whole context" is the
cost the brief warns about and should be measured, not asserted:
**lean** (the manager sees each specialist's findings) and **full** (it also
sees their whole transcripts, retrieved passages and all).

**The same tests for everyone:** the five labelled Week 7/8 scenarios (expected
decision *and* the clause that should ground it) plus the 28 Week 6 notes —
33 claims × 3 repeats, interleaved, after a discarded warm-up each. The
fixed workflow (Week 7's ship decision) is included as a reference rung.

## The four numbers

(per claim; tokens are *estimated* from the real prompts, cost is a projection
at `claude-opus-5` list price, $5/$25 per MTok — see "What this does not show")

| | fixed (ref.) | **single agent** | team, lean | team, full-context |
|---|---|---|---|---|
| **Quality** — decision correct (5 labelled) | 80% | 60% | 60% | 60% |
| Quality — right clause cited (5 labelled) | n/a | 60% | 60% | 60% |
| Quality — 4 deterministic assertions pass (all 33) | 97% | 97% | 97% | 97% |
| Quality — Week 6 judge v2 (all 33; noisy) | 58% | 64% | 64% | 64% |
| Quality — same decision as the single agent | 85% | — | **100%** | **100%** |
| **Speed** — wall-clock, median (ms)* | 2.0 | 14.9 | 13.0 | 13.5 |
| Speed — sequential model calls (critical path) | 1.0 | **3.0** | **4.0** | **4.0** |
| **Tokens** — model calls | 1.0 | 3.0 | 6.0 | 6.0 |
| Tokens — input / output | 3,282 / 169 | 10,426 / 640 | 9,053 / 1,083 | 12,273 / 1,083 |
| Tokens — total (vs single) | 3,451 (0.31×) | 11,066 | 10,137 (**0.92×**) | 13,356 (**1.21×**) |
| **Cost** — per claim | $0.0206 | $0.0681 | $0.0723 | $0.0884 |
| Cost — vs single | 0.30× | 1.00× | **1.06×** | **1.30×** |

\* Stub model, so wall-clock is tools + MCP + orchestration, not model latency.
The critical-path row is the one that predicts a live system's latency.

**Reading it:**

- **Quality: a tie, and not an informative one.** Team and single agree on 99/99
  runs (33 claims × 3). That is expected: with the stub, every role bottoms out
  in the same simulated reading comprehension, so the team can only reproduce the
  single agent's answer. The 60% decision accuracy is the Week 7/8 figure
  (3 of 5; the two misses are the known `malformed_claim_number` and
  `out_of_scope_theft`) and reappears unchanged. **This row checks the plumbing,
  not whether a team of real models would reason better or worse.**
- **Tokens: the surprise.** The team uses *fewer* input tokens than the single
  agent (0.92× total), not several times more. Why, from the per-role table in
  `race_tables.md`: the single agent re-carries the whole search result on turns 2 and 3
  and both tools' schemas on all three, while the specialists have narrow
  tool catalogs and short scratchpads (the clerk never sees a passage; the analyst
  never sees the check tool). But the team emits 1.7× the output tokens (every
  specialist writes a report, the manager restates it), and output costs 5× input,
  so it is **6% dearer**. Where the "hidden cost" does bite is the full-context
  hand-off: re-sending the analyst's passages to the manager costs +3.2k input
  tokens per claim, taking the team to **1.30× the single agent's cost** for a manager
  that cannot do anything more with them in this design.
- **Speed: structurally slower, empirically a wash.** The team needs 4 sequential
  model calls to the single agent's 3. The stub wall-clock actually favours the
  team slightly (13.0 vs 14.9 ms), because its two independent tool round-trips
  overlap; that is I/O concurrency, not model latency, and says nothing about a
  system where each call takes seconds.
- **The ladder:** fixed (0.30×) < single (1.00×) < team-lean (1.06×) < team-full
  (1.30×). The fixed workflow is also the most accurate on the five labelled
  claims here (80%) — for the same reason Week 7 recorded (its win on
  `malformed_claim_number` is an artifact of the stub's seeded failure injection).
  So the larger finding underneath this experiment is still Week 7's: **on this
  corpus the cheapest option is also not the worst.**

## Predictions, scored

`prediction.txt` was written before the race, with a disclosure that an earlier
smoke test had already shown me the headline token numbers on 5 claims — so those
were not predictions.

| # | Prediction | Result |
|---|---|---|
| P1 | team-lean total tokens within ±10% of single; cost 1.05–1.30× | **right** — 0.92×; 1.06× (the cost bound only just) |
| P2 | cost ladder monotone; fixed ≤ 40% of single | **right** — 0.30× |
| P3 | team-full tokens 1.15–1.50× single | **right** — 1.21× |
| P4 | single *faster* than team on wall-clock, by 1.2–2.5× | **wrong** — the team is ~13% faster. I had read the smoke run's apparent team win as a cold-start artifact; with warm-up and interleaving the win mostly survives, explained by overlapping tool I/O (outside-the-model time 9.7 vs 10.8 ms) and the stub doing less "reading" in total. Offline wall-clock is a weak instrument here. |
| P5 | critical path 3.0 vs 4.0 | **right** |
| P6 | team = single on decisions, assertions, judge, path | **right** — all four equal |
| P7 | team with Haiku-class specialists still ≥ 0.5× the single agent's cost | **wrong** — 0.28×. I assumed the manager dominated; the specialists carry 84% of the team's input tokens, so cheaper specialists cut deeply. |
| P8 | live model: team won't beat single by >1 claim; will cost more | **untestable here** (no API key) |

Five of seven testable predictions held. The two misses are the interesting
ones: P7 in particular changed the shape of the verdict.

## Verdict: keep the single agent

Backed by the numbers above, same model on both sides:

- quality: tie (and untestable offline — see below);
- tokens: equal total, but 2× the calls and 1.7× the output;
- cost: +6% (lean), +30% (full context);
- speed: +1 sequential model call (+33%).

The team wins on nothing measured. **The single exception is the what-if in
`race_tables.md`** (cost only, quality *not* measured):

| assignment | cost / claim | vs single on Opus-class |
|---|---|---|
| single, all on `claude-opus-5` | $0.0681 | 1.00× |
| single, all on `claude-sonnet-5-5` | $0.0273 | 0.40× |
| team, manager on Opus-class, specialists on `claude-haiku-5-5` | $0.0191 | 0.28× |
| single, all on `claude-haiku-5-5` | $0.0014 | 0.02× |

That is where a team could earn its keep — expensive judgement in the manager,
cheap models on narrow, well-specified jobs — but notice the honest comparison
is not against the Opus single agent: a **single agent on a cheaper model**
already costs 0.40× (Sonnet) or 0.02× (Haiku) with no extra machinery. The
team's structure only pays off if a live run shows that *a small model cannot do
the whole job at the quality bar yet can do the narrow sub-jobs*. That is a
measurable claim and I would test it before building anything — `--llm-provider
anthropic` with per-role model assignment is the experiment.

**A weakness the numbers don't show but the structure has:** the cross-field
check ("a `denied` decision must name an exclusion id") needs the analyst's
output and the clerk's field in one place. The single agent runs it on the
merged draft and can fix a failure; the team's clerk checks before the decision
exists, and the manager has no tool to re-check. Assertion pass rates are equal
here (97%) only because the stub never needs to revise — the capability gap is
real, just unexercised.

## When multi-agent is worth it — and when it isn't

**Worth it when…**

1. **The parts are truly independent *and* slow.** Parallelism cuts wall-clock in
   proportion to the fan-out: a hundred independent documents to research, not
   two cheap lookups. Here the fan-out is 2 and each branch is 2 calls, so
   parallelism saved nothing that mattered.
2. **The parts need different instructions, tools or permissions that fight in one
   prompt.** Least privilege is real: the clerk *cannot* run a search. If a
   security review wants "the component that reads untrusted documents cannot
   also approve payments", a split enforces it where a single prompt only asks.
3. **One agent's context would overflow or drown.** Specialists with separate
   contexts can each stay small. This claim's context is ~10k tokens against a
   1M window; nowhere near.
4. **Different models fit different jobs** (the what-if above): a cheap model for
   bulk narrow work, a strong one for judgement.
5. **The parts belong to different owners** (A2A, below).

**Not worth it when…**

1. The sub-tasks are sequential or share state — this task's cross-field check.
2. The context is small and one prompt handles it, as here.
3. You cannot yet measure quality: a split can lose information at every hand-off
   (a lean manager sees citation *numbers*, not the passages — it cannot audit the
   analyst), and with no eval you won't see it.
4. Latency is tight: each hand-off adds a sequential model call.
5. You'd be adding the team because it is fashionable. The brief's warning
   ("often the single agent wins") held.

## A2A: where it fits, and the decision

MCP connects an agent to **tools and data**; A2A connects an agent to **other
agents** (sources agree they are complementary, not competing). By the sources
I could check, an A2A server publishes an *Agent Card* — a public JSON
description of what the agent can do and how to reach it — and work is handed
over as stateful *tasks* with a lifecycle (submitted, working, input-required,
completed, failed, cancelled) over JSON-RPC on HTTP; the protocol is now
maintained by the Linux Foundation. I did not read the specification itself
(my search returned secondary sources that disagree on the Agent Card's
well-known path, which has evidently changed between versions), so treat those
details as a pointer, not a reference.

**Decision: do not use A2A here.** The analyst and clerk live in the same
process, are owned by the same team, share one trust domain and one MCP
connection; A2A would add a network hop, a serialisation layer and a task
lifecycle to hand a 60-token note to a function. It would be the right tool if
the policy-analysis capability were **owned and deployed by another team or
vendor** (their agent, their release cycle, their model choice), or had to be
discoverable by agents we don't control. The seam is already shaped for that:
`ClaimsTeam._run_specialists` hands a specialist a self-contained task (the
notes plus an instruction) and gets a structured report back — swapping an
in-process specialist for a remote one would change that one method.

## What this does not show

- **No real model.** Quality parity is by construction; the verdict is
  "keep the single agent" because the team *loses or ties on every measured
  axis*, not because I measured a real model reasoning worse in a team. P8 is the
  live test.
- **Tokens are estimates** (the repo's own ~4-chars/token heuristic over the real
  prompts and replies, applied identically to all contestants) and **cost is a
  projection** at list price. A real model's output includes thinking tokens, which
  would likely scale with the number of calls and widen the team's output deficit —
  directionally, not measured.
- **Wall-clock excludes model latency**, as above.
- **n is small and the corpus is a flat 4-page synthetic pack.** On a large or
  fragmented corpus, retrieval could dominate and parallel specialists might
  matter; this experiment can't say.
- **The team's design is one design.** Other splits (a verifier loop, a researcher
  that fans out per document) could behave differently; this one was chosen as the
  best case for the parts of this task that are independent.
- The what-if prices models that never ran. They are list prices from the Claude
  API model table cached 2026-10-06; check current pricing before relying on them.

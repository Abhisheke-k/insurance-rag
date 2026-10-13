**At 10x (18,000 claims/month, about 5.1 a minute in the busiest hour) the provider's rate limit breaks first, not cost or latency: peak demand is ~17k tokens/min on the fixed workflow and ~43k on the agent, against a projected $353-$977/month and a pipeline that handles ~277 requests/second locally; I did not verify the account's actual limit, so ~43k tokens/min is the number to check it against.**

# Week 11 -- what breaks first at 10x

## The one-line answer and why it is *by elimination*

Cost and latency can be bounded from measurements; a rate limit cannot, because it is a number in a contract I cannot see. So the claim is: cost and latency are demonstrably not binding at 10x, which leaves the rate limit as the first constraint that could be -- and the table says what number it would have to clear.

**Which system these numbers describe:** the drill-era system (5 passages per claim, no lead-in fix), i.e. the traffic in `.drill/traces.jsonl`. The system as it now ships (lead-in fix + 4 passages, `cost_improvement.md`) sends **-12.4%** input tokens per claim on the same notes, so every demand figure below is, if anything, slightly conservative.

## Working (all inputs stated)

| | value | source |
|---|---|---|
| volume today | 1,800 claims/month | the Week 12 Variant D brief (assumed to be this app's volume) |
| volume at 10x | 18,000 claims/month | |
| average hour | 102 claims (22 business days x 8 h) | assumption |
| busiest hour | 307 claims = **5.1/min** (3x the average hour) | assumption |
| tokens per request, fixed workflow | 3,285 (in+out, estimated) | mean of 242 traced requests |
| tokens per request, agent | 8,474 | mean of 38 traced requests |
| **peak tokens/min, fixed workflow** | **16,798** | 5.1/min x 3,285 |
| **peak tokens/min, agent** | **43,333** | 5.1/min x 8,474 |
| monthly cost, fixed workflow | $353 | 18,000 x $0.0196 (projected) |
| monthly cost, agent | $977 | 18,000 x $0.0543 (projected) |
| local pipeline throughput | 277 requests/s | 33 requests, tracing on, stub model |
| trace log growth | 2.2 KB/request -> 40 MB/month | mean serialized trace |

## Why cost does not break first

$977/month at the agent's 3x price is the worst case measured. Cost scales linearly with volume, so 10x volume is 10x the bill, not a cliff -- and it is a number a budget owner can approve.

## Why latency does not break first

Requests are independent: one claim's latency is its own sequential model calls (1 for the fixed workflow, 3 for the agent) and does not depend on how many other claims are in flight. At 5.1 claims/min, even a 30-second end-to-end claim keeps only ~2.6 in flight. The local code is not the limit either: 277 requests/s against a need of 0.09/s. (A live model's per-call latency is the unmeasured term; it multiplies per-request latency, but not with volume.)

## Why the rate limit is the one to check

It is the only constraint with a hard external ceiling that paying more does not move on its own, and it binds on *concurrent token throughput*, which is exactly what 10x multiplies: ~17k tokens/min (fixed) or ~43k (agent) at the busiest hour. Hailstorm-style bursts (Variant D's 18,000 FNOLs in a week) are a separate, larger case handled in the Week 12 plan.

**Not verified:** the provider's actual tokens-per-minute and requests-per-minute limits for this account; real model latency; the 3x peak factor and the volume itself (both assumed above). Treat the conclusion as 'cost and latency are not the problem at 10x; confirm the rate limit covers the number in the table.'

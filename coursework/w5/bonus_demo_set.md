# Week 5 bonus — demo set vs. random sample

"Curated demo set" here is the 11 traces from the two scenario templates that
would obviously make the monthly review slide deck: a flood loss squarely
inside the mapped flood zone with no prior claims (`flood_in_zone_covered`,
the cleanest possible "yes, this is covered" story), and a cyber-triggered
fire with no evidence of intent (`cyber_fire_writeback`, the app correctly
applying a write-back most competitors' bots would just deny outright). Both
are the kind of unambiguous, flattering case a team shows off. 10 of the 11
were open-coded (`trc_00016, 00020, 00022, 00031, 00033, 00045, 00072, 00077,
00082, 00085`).

**Mode 1 ("quotes the applicable clause but states the opposite verdict")
frequency: 35% (7/20) in the random sample vs. 80% (8/10) in the demo set.**

8 of the 10 demo-set traces are wrong, and 6 of those are the flood-in-zone
case specifically: the app cites the DEDUCTIBLE clause correctly, then
labels the claim "denied" — treating "a deductible applies" as if it were an
exclusion, rather than the ordinary fact that a covered claim is paid net of
its deductible. Only `trc_00045` and `trc_00072` got their category right.

## What the team has been telling itself

If a monthly review only ever pulls a flood-in-zone claim and a cyber-fire
claim to show the assistant working, the story told is "the easy 80% of
traffic is solid and we're chasing edge cases" — and that story is close to
backwards. The single cleanest, most unambiguous scenario in the whole
corpus — no complications, no exclusions in play, nothing to interpret — is
the one the app gets wrong four times out of five, because it mistakes the
presence of a deductible clause for a denial. A demo built from cases chosen
because they *look* simple never puts the "did it actually say covered or
denied" question in front of anyone, because eyeballing "yep, that's the
flood clause" feels like confirmation; nobody re-reads the verdict field on
a claim they already expect to be a clean yes. The random sample is what
surfaced that flood-in-zone is not actually the safe case — the demo set is
optimized to hide exactly this.

"""Synthetic adjuster-notes traffic for Week 5's error analysis.

No production system sits behind this app, so "a week of real adjuster
traffic" has to be built -- but built to be realistic, not built to make the
taxonomy look good. Each of the 18 scenario templates below is grounded in an
actual clause in the ingested corpus (see ``scripts/make_sample_pdf.py``), and
several are deliberately ambiguous or out-of-corpus, because a golden set (or
here, a traffic generator) that only contains cases with a clean answer
teaches you nothing about where the app actually struggles. Claim numbers,
dates and amounts are varied with a seeded RNG so the corpus is reproducible;
the scenario narrative itself is authored, not generated.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta

SEED = 20260831  # today's date, per the run this traffic was generated for

_2026_DATES = [date(2026, 3, 1) + timedelta(days=i) for i in range(180)]


@dataclass(frozen=True, slots=True)
class Scenario:
    key: str  # groups variations of the same underlying situation
    notes: str


def _claim_no(rng: random.Random) -> str:
    return f"CLM-2026-{rng.randint(1, 99999):05d}"


def _date(rng: random.Random) -> str:
    return rng.choice(_2026_DATES).strftime("%d %B %Y")


def _amount(rng: random.Random, lo: int, hi: int) -> str:
    return f"{rng.randint(lo, hi):,}"


def _name(rng: random.Random) -> str:
    return rng.choice(
        ["Mr. Okafor", "Mrs. Bianchi", "Ms. Nakamura", "Mr. Kowalski", "Mrs. Delacroix",
         "Mr. Farrow", "Ms. Odusanya", "Mr. Marchetti", "Mrs. Fitzgerald", "Dr. Ibrahim",
         "Ms. Larsson", "Mr. Whitfield"]
    )


def _build(rng: random.Random) -> list[Scenario]:
    scenarios: list[Scenario] = []

    def add(key: str, text: str) -> None:
        scenarios.append(Scenario(key, text))

    # -- 1. Flood sub-limit / deductible, in a flood zone: straightforward covered claim --
    for _ in range(7):
        claim, d, amt = _claim_no(rng), _date(rng), _amount(rng, 80_000, 900_000)
        add(
            "flood_in_zone_covered",
            f"Claim {claim}. Insured {_name(rng)} reports flood damage to the ground floor "
            f"of the insured premises on {d}, following prolonged heavy rainfall. The site is "
            f"within the mapped flood zone. Estimated repair cost GBP {amt}. No prior flood "
            f"claim at this location in the last 12 months.",
        )

    # -- 2. Flood outside a flood zone: standard property deductible, not the flood one --
    for _ in range(5):
        claim, d, amt = _claim_no(rng), _date(rng), _amount(rng, 40_000, 300_000)
        add(
            "flood_outside_zone",
            f"Claim {claim}. Water ingress at the insured's premises on {d} after a burst "
            f"water main nearby; the site is NOT within the mapped flood zone. Estimated "
            f"damage GBP {amt}. Insured asks which deductible applies.",
        )

    # -- 3. Flood sub-limit erosion: second/third flood payment this policy year --
    for _ in range(5):
        claim, d, amt = _claim_no(rng), _date(rng), _amount(rng, 500_000, 4_800_000)
        add(
            "sublimit_erosion",
            f"Claim {claim}. Third flood-related payment this policy year at the insured's "
            f"site, loss date {d}. Cumulative flood payments to date including this claim: "
            f"GBP {amt}. Insured asks whether the annual aggregate flood sub-limit has "
            f"been exhausted.",
        )

    # -- 4. Flood defence maintenance not tested: ambiguous consequence --
    for _ in range(4):
        claim, d, amt = _claim_no(rng), _date(rng), _amount(rng, 60_000, 400_000)
        add(
            "flood_defence_untested",
            f"Claim {claim}. Flood loss on {d}. During the claim visit the loss adjuster "
            f"found the insured's sump pumps had not been tested in the preceding nine "
            f"months, against the six-month testing condition. No test log was produced. "
            f"Estimated damage GBP {amt}. Insured asks whether this affects the claim.",
        )

    # -- 5. Exclusion (a): subsidence contributed to by flood --
    for _ in range(5):
        claim, d, amt = _claim_no(rng), _date(rng), _amount(rng, 100_000, 600_000)
        add(
            "exclusion_subsidence",
            f"Claim {claim}. Insured reports ground movement and cracking to the warehouse "
            f"wall following the flood event of {d}; structural engineer's report attributes "
            f"the cracking to subsidence triggered by the flood saturating the foundations. "
            f"Claimed amount GBP {amt}.",
        )

    # -- 6. Exclusion (b): livestock/crops in the open --
    for _ in range(3):
        claim, d, amt = _claim_no(rng), _date(rng), _amount(rng, 15_000, 90_000)
        add(
            "exclusion_livestock",
            f"Claim {claim}. Flood on {d} destroyed standing timber stored in the open yard "
            f"at the insured's site. Claimed amount GBP {amt}.",
        )

    # -- 7. Exclusion (c): property under construction over the GBP 1,000,000 threshold --
    for _ in range(4):
        claim, d, amt = _claim_no(rng), _date(rng), _amount(rng, 1_100_000, 3_000_000)
        add(
            "exclusion_construction",
            f"Claim {claim}. Flood on {d} damaged a new production line under construction "
            f"at the insured's site; contract value for the works is GBP {amt}. Insured "
            f"asks whether this falls within the flood sub-limit.",
        )
    for _ in range(2):
        claim, d, amt = _claim_no(rng), _date(rng), _amount(rng, 200_000, 950_000)
        add(
            "construction_under_threshold",
            f"Claim {claim}. Flood on {d} damaged property under construction at the "
            f"insured's site; contract value GBP {amt}, below the GBP 1,000,000 threshold "
            f"in the policy wording.",
        )

    # -- 8. Exclusion (d): stock stored below ground level --
    for _ in range(6):
        claim, d, amt = _claim_no(rng), _date(rng), _amount(rng, 20_000, 250_000)
        add(
            "exclusion_stock_floor",
            f"Claim {claim}. Insured reports water damage to stock stored on the warehouse "
            f"floor, discovered {d} following heavy rainfall. Stock was not raised above "
            f"floor level. Estimated damage GBP {amt}.",
        )
    for _ in range(3):
        claim, d, amt = _claim_no(rng), _date(rng), _amount(rng, 20_000, 180_000)
        add(
            "stock_raised_covered",
            f"Claim {claim}. Water ingress on {d}; insured's stock was stored on racking "
            f"200mm above the finished floor level per their usual practice. Estimated "
            f"damage GBP {amt}.",
        )

    # -- 9. Exclusion (e): regulatory fine following a flood event --
    for _ in range(3):
        claim, d, amt = _claim_no(rng), _date(rng), _amount(rng, 5_000, 60_000)
        add(
            "exclusion_regulatory_fine",
            f"Claim {claim}. Following the flood event of {d}, the Environment Agency "
            f"issued the insured a penalty notice of GBP {amt} for a related discharge "
            f"breach. Insured asks whether the penalty is covered.",
        )

    # -- 10. Cyber exclusion: ransomware / extortion --
    for _ in range(5):
        claim, d, amt = _claim_no(rng), _date(rng), _amount(rng, 50_000, 500_000)
        add(
            "cyber_ransomware",
            f"Claim {claim}. Ransomware incident on {d} encrypted the insured's order "
            f"management system; insured paid a ransom of GBP {amt} in cryptocurrency to "
            f"restore access. Insured asks whether the ransom payment is covered.",
        )

    # -- 11. Cyber exclusion: computer system failure at a cloud host --
    for _ in range(3):
        claim, d, amt = _claim_no(rng), _date(rng), _amount(rng, 30_000, 200_000)
        add(
            "cyber_cloud_outage",
            f"Claim {claim}. The insured's cloud hosting provider suffered an outage on "
            f"{d}, taking the insured's ordering system offline for 36 hours. Insured "
            f"claims lost sales of GBP {amt}.",
        )

    # -- 12. Cyber write-back: fire caused by a cyber incident --
    for _ in range(4):
        claim, d, amt = _claim_no(rng), _date(rng), _amount(rng, 300_000, 2_000_000)
        add(
            "cyber_fire_writeback",
            f"Claim {claim}. A malicious intrusion into the insured's building-management "
            f"system on {d} caused an electrical fault that started a fire, causing "
            f"physical damage to the premises estimated at GBP {amt}. No evidence the "
            f"intrusion was intended to cause the fire.",
        )

    # -- 13. Business interruption: within the 72-hour waiting period, no indemnity --
    for _ in range(4):
        claim, d = _claim_no(rng), _date(rng)
        hours = rng.randint(24, 71)
        add(
            "bi_within_waiting_period",
            f"Claim {claim}. Production interruption beginning {d}, fully resolved within "
            f"{hours} hours. Insured has submitted a business interruption claim for lost "
            f"revenue during the outage.",
        )

    # -- 14. Business interruption: past the waiting period, payable --
    for _ in range(5):
        claim, d, amt = _claim_no(rng), _date(rng), _amount(rng, 80_000, 700_000)
        hours = rng.randint(96, 240)
        add(
            "bi_past_waiting_period",
            f"Claim {claim}. Production interruption beginning {d}, lasting {hours} hours "
            f"before normal operations resumed. Insured claims GBP {amt} in lost revenue.",
        )

    # -- 15. Business interruption: exceeds the 18-month indemnity period --
    for _ in range(3):
        claim, d, amt = _claim_no(rng), _date(rng), _amount(rng, 1_000_000, 5_000_000)
        add(
            "bi_exceeds_indemnity_period",
            f"Claim {claim}. Interruption beginning {d} has now continued for 20 months "
            f"with operations still not fully restored. Cumulative claimed loss to date: "
            f"GBP {amt}.",
        )

    # -- 16. Late notification of a BI claim (30-day clause), ambiguous consequence --
    for _ in range(4):
        claim, d, amt = _claim_no(rng), _date(rng), _amount(rng, 40_000, 250_000)
        add(
            "bi_late_notification",
            f"Claim {claim}. Business interruption event on {d}; the insured's risk "
            f"manager only notified the Insurer 52 days later. Claimed amount GBP {amt}. "
            f"Insured asks whether the late notice affects the claim.",
        )

    # -- 17. Exclusion E-17: second flood at the same site within the ed. 03-24 window --
    for _ in range(6):
        claim, amt = _claim_no(rng), _amount(rng, 40_000, 300_000)
        gap = rng.randint(10, 58)
        d2 = rng.choice(_2026_DATES)
        d1 = d2 - timedelta(days=gap)
        add(
            "e17_within_window",
            f"Claim {claim}. Second flood event at the same insured location on "
            f"{d2.strftime('%d %B %Y')}, {gap} days after an earlier flood event on "
            f"{d1.strftime('%d %B %Y')} at the same location. Estimated damage GBP {amt}. "
            f"Form on file for this policy is HO-0304 edition 03-24.",
        )

    # -- 18. Exclusion E-17 with the flood-defence carve-out: barriers were tested --
    for _ in range(4):
        claim, amt = _claim_no(rng), _amount(rng, 40_000, 300_000)
        gap = rng.randint(10, 55)
        d2 = rng.choice(_2026_DATES)
        d1 = d2 - timedelta(days=gap)
        add(
            "e17_defence_carveout",
            f"Claim {claim}. Second flood event at the same insured location on "
            f"{d2.strftime('%d %B %Y')}, {gap} days after an earlier flood event on "
            f"{d1.strftime('%d %B %Y')} at the same location. Estimated damage GBP {amt}. "
            f"Insured's flood-defence test log shows the sump pumps and non-return valves "
            f"were tested and working 20 days before this second event. Form on file is "
            f"HO-0304 edition 03-24.",
        )

    # -- 19. Out-of-corpus: a peril this pack does not address at all --
    for _ in range(4):
        claim, d, amt = _claim_no(rng), _date(rng), _amount(rng, 5_000, 80_000)
        add(
            "out_of_corpus",
            f"Claim {claim}. Insured reports a company vehicle involved in a collision on "
            f"{d} while making a delivery; driver cited for careless driving. Claimed "
            f"repair cost GBP {amt}. Insured asks whether this is covered under this policy.",
        )

    rng.shuffle(scenarios)
    return scenarios


def build_scenarios() -> list[Scenario]:
    rng = random.Random(SEED)
    return _build(rng)


if __name__ == "__main__":
    scenarios = build_scenarios()
    print(f"{len(scenarios)} scenarios")
    from collections import Counter

    for key, count in Counter(s.key for s in scenarios).most_common():
        print(f"  {key:28} {count}")

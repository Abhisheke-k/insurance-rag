"""Generate a synthetic insurance endorsement pack as a PDF.

Used by the test suite and by ``chunk_size_comparison.py`` so that both run
without shipping a binary fixture (and without needing a real, confidential
policy document). The content deliberately exercises every structural feature
the parser claims to handle:

* ``ENDORSEMENT NO. n`` headers
* ``SECTION n`` / all-caps headings
* numbered clauses at several depths (``3.``, ``4.2``, ``4.2.1``)
* a clause label sharing its line with body text
* sub-clause markers ``(a)`` ... ``(h)``
* one clause far larger than any sensible chunk target, to force the
  recursive fallback
* running page footers, which must be filtered out

Run directly to write ``data/sample_endorsement_pack.pdf``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pymupdf

__all__ = ["EVAL_QUESTIONS", "build_sample_pdf"]

PAGE_WIDTH, PAGE_HEIGHT = 595.0, 842.0
MARGIN = 60.0
BODY_SIZE = 10.5
HEADING_SIZE = 11.5
TITLE_SIZE = 15.0
LEADING = 14.0
PARAGRAPH_GAP = 10.0
WRAP_WIDTH = 92

# (style, text) -- style drives font and spacing only; structure comes from the text.
CONTENT: list[tuple[str, str]] = [
    ("title", "GLOBAL MANUFACTURING LTD"),
    ("title", "COMMERCIAL PROPERTY AND LIABILITY ENDORSEMENT PACK"),
    (
        "body",
        "Policy Number GML-2024-88213. Period of Insurance: 1 April 2024 to 31 March 2025, "
        "both dates inclusive at the address of the Insured. This pack records every "
        "endorsement attached to the policy and supersedes any endorsement schedule issued "
        "previously.",
    ),
    ("heading", "ENDORSEMENT NO. 1 - FLOOD SUB-LIMIT AMENDMENT"),
    (
        "body",
        "Effective from 1 June 2024. In consideration of the additional premium of GBP 42,750 "
        "the following amendments are made to the Property Damage section of the policy.",
    ),
    ("heading", "1. DEFINITIONS"),
    (
        "body",
        "1.1 Flood means the escape of water from the normal confines of any natural or "
        "artificial watercourse, lake, reservoir, canal or dam, or inundation from the sea, "
        "whether driven by storm or otherwise.",
    ),
    (
        "body",
        "1.2 Flood Zone means any location identified as Zone 2 or Zone 3 on the Environment "
        "Agency Flood Map for Planning current at the inception of this endorsement.",
    ),
    ("heading", "2. AMENDMENT TO LIMIT"),
    (
        "body",
        "The sub-limit applicable to loss or damage caused by Flood is amended from "
        "GBP 2,500,000 to GBP 5,000,000 in the annual aggregate. The revised sub-limit applies "
        "to all insured locations combined and is part of, and not in addition to, the policy "
        "limit of GBP 40,000,000 any one occurrence.",
    ),
    ("heading", "3. DEDUCTIBLE"),
    (
        "body",
        "A deductible of GBP 250,000 each and every loss applies to loss or damage caused by "
        "Flood at any location within a Flood Zone. At all other locations the standard property "
        "deductible of GBP 50,000 each and every loss continues to apply.",
    ),
    ("heading", "SECTION 4 - CONDITIONS APPLYING TO THIS ENDORSEMENT"),
    ("heading", "4.1 Flood Defence Maintenance"),
    (
        "body",
        "The Insured shall maintain all demountable flood barriers, non-return valves and sump "
        "pumps at each insured location in good and effective working order, and shall test them "
        "at intervals of not more than six months. A written record of each test shall be kept "
        "for a period of three years and produced to the Insurer on request.",
    ),
    ("heading", "4.2 Exclusions"),
    (
        "body",
        "Notwithstanding anything to the contrary in the policy, this endorsement does not cover "
        "loss, damage, cost or expense arising directly or indirectly out of:",
    ),
    (
        "body",
        "(a) subsidence, ground heave or landslip, whether or not caused or contributed to by "
        "Flood; (b) loss of or damage to growing crops, standing timber, or livestock in the "
        "open; (c) any property in the course of construction, erection or installation where "
        "the contract value exceeds GBP 1,000,000; (d) loss or damage to stock stored below "
        "ground level unless raised at least 150 millimetres above the finished floor level; "
        "(e) any fine, penalty or punitive damages levied by a regulator following a Flood event.",
    ),
    ("heading", "4.2.1 Erosion of the sub-limit"),
    (
        "body",
        "Each payment made under this endorsement, including any payment for professional fees "
        "and debris removal, erodes the annual aggregate sub-limit stated in clause 2. Once the "
        "annual aggregate sub-limit is exhausted no further payment shall be made under this "
        "endorsement for the remainder of the Period of Insurance.",
    ),
    ("heading", "ENDORSEMENT NO. 2 - CYBER AND DATA EXCLUSION"),
    (
        "body",
        "Effective from inception. This endorsement applies to every section of the policy and "
        "replaces any cyber exclusion previously attached.",
    ),
    ("heading", "SECTION 1 - SCOPE OF THE EXCLUSION"),
    (
        "body",
        "1. This exclusion applies to all coverage afforded by the policy, including any "
        "extension, endorsement or memorandum, and applies irrespective of any other cause or "
        "event contributing concurrently or in any other sequence to the loss.",
    ),
    ("heading", "SECTION 2 - EXCLUSION WORDING"),
    (
        "body",
        "2. Notwithstanding any provision to the contrary within this policy or any endorsement "
        "thereto, this policy excludes any loss, damage, liability, claim, cost or expense of "
        "whatsoever nature directly or indirectly caused by, contributed to by, resulting from, "
        "arising out of or in connection with any of the following, regardless of any other "
        "cause or event contributing concurrently or in any other sequence thereto:",
    ),
    (
        "body",
        "(a) any Cyber Act or Cyber Incident, including but not limited to any action taken in "
        "controlling, preventing, suppressing or remediating any Cyber Act or Cyber Incident, "
        "and including the cost of any forensic investigation commissioned by the Insured or by "
        "any regulator, and including any sum paid or payable to a third party by way of "
        "settlement, judgment, award or contractual indemnity in respect of such an act or "
        "incident, whether or not the Insured admitted liability;",
    ),
    (
        "body",
        "(b) any loss of use, reduction in functionality, repair, replacement, restoration or "
        "reproduction of any Data, including any amount pertaining to the value of such Data, "
        "and including the cost of re-keying, re-entering or otherwise reconstituting Data from "
        "source documents where those documents themselves remain undamaged, and including any "
        "loss of profit consequent upon such loss of use;",
    ),
    (
        "body",
        "(c) the failure, malfunction, degradation, unavailability or interruption of any "
        "Computer System, whether owned or operated by the Insured or by any third party service "
        "provider, cloud host, managed service provider or telecommunications carrier upon which "
        "the Insured relies, and whether or not the Insured had a contractual right of recourse "
        "against that party;",
    ),
    (
        "body",
        "(d) the receipt, transmission, distribution or storage of any Malicious Code, whether "
        "or not that Malicious Code was introduced deliberately, negligently or inadvertently, "
        "and whether or not the Insured had in place anti-malware controls conforming to any "
        "recognised standard including ISO 27001 or the National Institute of Standards and "
        "Technology Cybersecurity Framework;",
    ),
    (
        "body",
        "(e) any regulatory investigation, enquiry, examination, inspection, hearing or "
        "proceeding brought by any data protection authority, supervisory authority, information "
        "commissioner or equivalent body in any jurisdiction, together with the costs of "
        "responding to any request for information from such a body, and together with any "
        "administrative fine or penalty however described;",
    ),
    (
        "body",
        "(f) any extortion demand, ransom payment, cryptocurrency transfer or negotiation fee "
        "made or incurred in response to a threat to a Computer System or to Data, and including "
        "the fees of any incident response firm, ransomware negotiator or public relations "
        "consultant engaged in connection with that threat;",
    ),
    (
        "body",
        "(g) any breach of contract, warranty, guarantee, service level agreement or licence "
        "term relating to the performance, security, availability or integrity of any Computer "
        "System or Data, whether that contract was entered into by the Insured as supplier or as "
        "customer;",
    ),
    (
        "body",
        "(h) the deliberate act of any employee, contractor or agent of the Insured taken with "
        "intent to cause a Cyber Incident, whether or not that person was acting within the "
        "scope of their employment or engagement, and whether or not the Insured had performed "
        "pre-engagement screening on that person.",
    ),
    ("heading", "SECTION 3 - WRITE-BACK FOR PHYSICAL DAMAGE"),
    (
        "body",
        "3. Subject to all other terms of the policy, this exclusion shall not apply to physical "
        "loss of or physical damage to insured property arising from fire or explosion which "
        "itself results from a Cyber Incident, provided that the fire or explosion is not the "
        "result of a Cyber Act committed with intent to cause physical damage.",
    ),
    ("heading", "ENDORSEMENT NO. 3 - BUSINESS INTERRUPTION AMENDMENTS"),
    (
        "body",
        "Effective from 1 October 2024. The Business Interruption section is amended as set out "
        "below. All other terms, conditions and exclusions remain unaltered.",
    ),
    ("heading", "1. WAITING PERIOD"),
    (
        "body",
        "A waiting period of 72 consecutive hours applies to each and every claim under the "
        "Business Interruption section. No indemnity is payable in respect of any interruption "
        "that is remedied within the waiting period.",
    ),
    ("heading", "2. INDEMNITY PERIOD"),
    (
        "body",
        "The maximum indemnity period is extended from 12 months to 18 months from the date on "
        "which the interruption begins.",
    ),
    ("heading", "3. NOTIFICATION OF CLAIM"),
    (
        "body",
        "3. The Insured shall give written notice to the Insurer of any event likely to give "
        "rise to a claim under this section within 30 days of that event becoming known to a "
        "director or to the risk manager of the Insured. Notice given to the broker alone does "
        "not constitute notice to the Insurer.",
    ),
    ("heading", "GENERAL CONDITIONS"),
    (
        "body",
        "1. Cancellation. Either party may cancel this policy by giving 60 days written notice "
        "to the other at its last known address. Where the Insurer cancels, a pro rata return of "
        "premium shall be made provided no claim has been notified during the Period of "
        "Insurance.",
    ),
    (
        "body",
        "2. Governing law. This policy shall be governed by and construed in accordance with the "
        "law of England and Wales, and the parties submit to the exclusive jurisdiction of the "
        "courts of England and Wales.",
    ),
    (
        "body",
        "3. Premium payment. The premium is due within 45 days of inception. Failure to pay "
        "within that period entitles the Insurer to cancel in accordance with General Condition "
        "1 above.",
    ),
]

#: Question -> (expected substring in the answering chunk, expected clause label fragment).
#: Shared by the test suite and the chunk-size comparison so both measure the
#: same notion of "retrieved the right passage".
EVAL_QUESTIONS: list[dict[str, str]] = [
    {
        "question": "What is the flood sub-limit in the annual aggregate after the endorsement?",
        "expects": "GBP 5,000,000",
        "clause": "2. AMENDMENT TO LIMIT",
    },
    {
        "question": "What deductible applies to flood losses inside a flood zone?",
        "expects": "GBP 250,000",
        "clause": "3. DEDUCTIBLE",
    },
    {
        "question": "How long is the business interruption waiting period?",
        "expects": "72 consecutive hours",
        "clause": "1. WAITING PERIOD",
    },
    {
        "question": "How much written notice is needed to cancel the policy?",
        "expects": "60 days written notice",
        "clause": "GENERAL CONDITIONS",
    },
    {
        "question": "How often must flood barriers and sump pumps be tested?",
        "expects": "six months",
        "clause": "4.1 Flood Defence Maintenance",
    },
    {
        "question": "What is the maximum indemnity period for business interruption?",
        "expects": "18 months",
        "clause": "2. INDEMNITY PERIOD",
    },
    {
        "question": "Does the cyber exclusion write back cover for fire damage?",
        "expects": "fire or explosion",
        "clause": "SECTION 3 - WRITE-BACK FOR PHYSICAL DAMAGE",
    },
    {
        "question": "Within how many days must a business interruption claim be notified?",
        "expects": "within 30 days",
        "clause": "3. NOTIFICATION OF CLAIM",
    },
]

#: A question the pack cannot answer -- used to prove the not-found path.
OUT_OF_SCOPE_QUESTION = "What is the average annual rainfall in the Amazon basin?"


class _PageWriter:
    """Minimal top-down text layout: wrap, place, paginate."""

    def __init__(self, document: pymupdf.Document) -> None:
        self._document = document
        self._page: pymupdf.Page | None = None
        self._y = 0.0
        self._new_page()

    def _new_page(self) -> None:
        self._page = self._document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        self._y = MARGIN

    def _ensure_space(self, needed: float) -> None:
        if self._y + needed > PAGE_HEIGHT - MARGIN - 30:
            self._new_page()

    def write(self, style: str, text: str) -> None:
        size = {"title": TITLE_SIZE, "heading": HEADING_SIZE}.get(style, BODY_SIZE)
        font = "hebo" if style in {"title", "heading"} else "helv"
        wrap = int(WRAP_WIDTH * BODY_SIZE / size)
        lines = textwrap.wrap(text, width=wrap) or [""]

        self._ensure_space(len(lines) * LEADING)
        for line in lines:
            assert self._page is not None
            self._page.insert_text((MARGIN, self._y), line, fontname=font, fontsize=size)
            self._y += LEADING
        self._y += PARAGRAPH_GAP if style == "body" else PARAGRAPH_GAP / 2

    def add_footers(self) -> None:
        total = self._document.page_count
        for index, page in enumerate(self._document, start=1):
            page.insert_text(
                (PAGE_WIDTH / 2 - 30, PAGE_HEIGHT - 40),
                f"Page {index} of {total}",
                fontname="helv",
                fontsize=8,
            )


def build_sample_pdf(path: str | Path) -> Path:
    """Write the sample endorsement pack to ``path`` and return it."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    document = pymupdf.open()
    try:
        writer = _PageWriter(document)
        for style, text in CONTENT:
            writer.write(style, text)
        writer.add_footers()
        document.save(str(destination))
    finally:
        document.close()
    return destination


if __name__ == "__main__":
    output = build_sample_pdf(Path(__file__).resolve().parent.parent / "data" / "sample_endorsement_pack.pdf")
    print(f"wrote {output}")

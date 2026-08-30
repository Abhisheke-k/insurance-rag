"""Trace capture, redaction, and replay for the claim-summary feature.

Every call to ``RagService.summarize_claim`` can be recorded as one
:class:`TraceRecord` -- everything needed to read the call back later without
re-running the app: the input, what was retrieved (chunk ids, scores, and the
retrieved text itself, so replay never needs the live vector store), the
model and generation params, and the raw output. This is the record Week 5's
error analysis reads and Week 6 pulls regression cases from.

**Redaction happens before a record is ever constructed**, not as a
post-processing pass over the trace file -- :func:`build_trace_record` is the
only place a :class:`TraceRecord` gets built, and it redacts the notes text
and the claim number before either touches the object that gets serialised.
There is no "redact the log" step to forget to run.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.claims import ClaimSummary, ClaimSummaryGenerator
from app.models import RetrievedChunk

__all__ = [
    "PROMPT_VERSION",
    "TraceRecord",
    "build_trace_record",
    "read_traces",
    "redact_notes",
    "replay_trace",
    "write_trace",
]

#: Bumped whenever SYSTEM_PROMPT or CLAIM_SUMMARY_SCHEMA changes shape -- traces
#: record which version produced them, per the Week 5 replay requirement.
PROMPT_VERSION = "claim-summary-v1"

#: Synthetic claimant surnames used across scripts/w5_scenarios.py. A real
#: deployment would run a NER model or a name-gazetteer here; a fixed list is
#: enough for this corpus because every name in it is one this codebase wrote.
_SYNTHETIC_SURNAMES = [
    "Okafor", "Bianchi", "Nakamura", "Kowalski", "Delacroix", "Farrow",
    "Odusanya", "Marchetti", "Fitzgerald", "Ibrahim", "Larsson", "Whitfield",
]
_NAME_RE = re.compile(r"\b(?:Mr|Mrs|Ms|Mx|Dr)\.?\s+(?:" + "|".join(_SYNTHETIC_SURNAMES) + r")\b")
_CLAIM_NUMBER_RE = re.compile(r"CLM-\d{4}-\d{5}")


def redact_notes(text: str) -> tuple[str, list[str]]:
    """Mask claimant names and the claim number. Returns (redacted_text, what_was_masked)."""
    masked: list[str] = []

    def _mask_name(m: re.Match) -> str:
        masked.append(f"name:{m.group(0)}")
        return "[CLAIMANT NAME REDACTED]"

    def _mask_claim(m: re.Match) -> str:
        masked.append(f"claim_number:{m.group(0)}")
        return "[CLAIM NUMBER REDACTED]"

    text = _NAME_RE.sub(_mask_name, text)
    text = _CLAIM_NUMBER_RE.sub(_mask_claim, text)
    return text, masked


def _redact_claim_number_field(value: str) -> str:
    return "[CLAIM NUMBER REDACTED]" if _CLAIM_NUMBER_RE.fullmatch(value.strip()) else value


@dataclass(frozen=True, slots=True)
class TraceRecord:
    trace_id: str
    created_at: str  # ISO 8601 UTC
    feature: str  # "claim_summary"
    prompt_version: str
    input_redacted: str
    redacted_fields: list[str]
    retrieval_mode: str
    retrieved: list[dict[str, Any]]  # full RetrievedChunk data, so replay needs no live store
    model: str | None
    params: dict[str, Any]
    output: dict[str, Any]  # the ClaimSummary fields, redacted
    output_notes: list[str]

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, line: str) -> "TraceRecord":
        return cls(**json.loads(line))


def build_trace_record(
    *,
    trace_id: str,
    adjuster_notes: str,
    retrieval_mode: str,
    retrieved: list[RetrievedChunk],
    summary: ClaimSummary,
    effort: str,
    max_tokens: int,
) -> TraceRecord:
    """The one place a TraceRecord is constructed -- redaction is not optional here."""
    redacted_input, redacted_fields = redact_notes(adjuster_notes)
    output = {
        "claim_number": _redact_claim_number_field(summary.claim_number),
        "date_of_loss": summary.date_of_loss,
        "coverage_decision": summary.coverage_decision,
        "cited_exclusion_id": summary.cited_exclusion_id,
        "excess_amount": summary.excess_amount,
        "summary": summary.summary,
        "citations": [c.chunk_id for c in summary.citations],
    }
    return TraceRecord(
        trace_id=trace_id,
        created_at=datetime.now(timezone.utc).isoformat(),
        feature="claim_summary",
        prompt_version=PROMPT_VERSION,
        input_redacted=redacted_input,
        redacted_fields=redacted_fields,
        retrieval_mode=retrieval_mode,
        retrieved=[
            {
                "chunk_id": c.chunk_id,
                "doc_id": c.doc_id,
                "filename": c.filename,
                "page": c.page,
                "page_end": c.page_end,
                "section_path": c.section_path,
                "clause_label": c.clause_label,
                "score": round(c.score, 4),
                "text": c.text,
            }
            for c in retrieved
        ],
        model=summary.model,
        params={"effort": effort, "max_tokens": max_tokens},
        output=output,
        output_notes=summary.notes,
    )


def write_trace(record: TraceRecord, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(record), indent=2))
        fh.write("\n---\n")


def read_traces(path: Path) -> list[TraceRecord]:
    """Parse a trace file written by :func:`write_trace` (pretty-printed, ``---``-delimited)."""
    if not path.exists():
        return []
    blocks = path.read_text(encoding="utf-8").split("\n---\n")
    return [TraceRecord(**json.loads(block)) for block in blocks if block.strip()]


def replay_trace(record: TraceRecord, *, generator: ClaimSummaryGenerator) -> dict[str, Any]:
    """Regenerate a claim summary from the trace's stored fields alone.

    No live vector store, no original PDFs -- the retrieved chunks are
    reconstructed from what the trace itself stored. Because the input was
    redacted before the trace was written, the claim number the input
    extraction depends on is gone from what replay can see; the caller should
    expect ``output["claim_number"]`` to come back ``"UNKNOWN"`` rather than
    matching the (also-redacted) original, and that gap is itself the honest
    answer to "what could you not reconstruct".
    """
    reconstructed = [
        RetrievedChunk(
            chunk_id=c["chunk_id"],
            doc_id=c["doc_id"],
            filename=c["filename"],
            text=c["text"],
            page=c["page"],
            page_end=c["page_end"],
            section_path=c["section_path"],
            clause_label=c["clause_label"],
            score=c["score"],
        )
        for c in record.retrieved
    ]
    summary = generator.summarize(record.input_redacted, reconstructed)
    return {
        "claim_number": summary.claim_number,
        "date_of_loss": summary.date_of_loss,
        "coverage_decision": summary.coverage_decision,
        "cited_exclusion_id": summary.cited_exclusion_id,
        "excess_amount": summary.excess_amount,
        "summary": summary.summary,
        "citations": [c.chunk_id for c in summary.citations],
    }

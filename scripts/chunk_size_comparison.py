"""Re-run ingestion at several chunk sizes and measure what changes.

Chunk size is the one knob in a RAG system that trades two things off against
each other, and neither is visible from the code:

* **precision of the citation** -- small chunks point at exactly the clause that
  answers the question, but lose the surrounding context that makes the clause
  interpretable (and split more clauses in the first place);
* **recall** -- large chunks almost always contain the answer somewhere, but the
  citation then points at half a page and the reader has to search it.

So measure. This script ingests the same document at each requested size and
reports clause integrity, storage overhead, and retrieval accuracy against the
eval set in ``make_sample_pdf.py``.

    python -m scripts.chunk_size_comparison
    python -m scripts.chunk_size_comparison --sizes 300 600 1000 --overlap 120
    python -m scripts.chunk_size_comparison --provider sentence-transformers

Retrieval numbers are embedder-specific; the default (hashing) is deterministic
and needs no API key or model download, which makes the comparison reproducible.
Re-run with ``--provider`` to see how the same trade-off looks under semantic
embeddings.
"""

from __future__ import annotations

import argparse
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from app.chunking import StructureAwareChunker, build_chunker
from app.config import Settings
from app.models import Chunk, RetrievedChunk
from app.parsing import parse_pdf_bytes
from app.rag import RagService
from app.registry import DocumentRegistry
from app.tokenizer import estimate_tokens
from scripts.make_sample_pdf import EVAL_QUESTIONS, OUT_OF_SCOPE_QUESTION, build_sample_pdf

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PDF = ROOT / "data" / "sample_endorsement_pack.pdf"


class _NullGenerator:
    """Retrieval-only: the comparison never needs an answer from Claude."""

    def status(self) -> dict[str, object]:
        return {"configured": False, "model": "n/a", "effort": "n/a", "error": None}

    def generate(self, question: str, retrieved: Sequence[RetrievedChunk]):  # pragma: no cover
        raise NotImplementedError("the comparison script measures retrieval only")


@dataclass(frozen=True, slots=True)
class Measurement:
    chunk_size: int
    chunk_overlap: int
    chunks: int
    mean_tokens: float
    median_tokens: float
    max_tokens: int
    stored_tokens: int
    duplication: float
    clauses_total: int
    clauses_intact: int
    chunks_with_split_clause: int
    recall_at_5: float
    mrr: float
    mean_rank: float
    mean_top1_score: float
    out_of_scope_top1: float

    @property
    def clause_integrity(self) -> float:
        return self.clauses_intact / self.clauses_total if self.clauses_total else 0.0


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _clause_integrity(chunker: StructureAwareChunker, blocks, chunks: Sequence[Chunk]) -> tuple[int, int]:
    """How many clauses that *could* have been kept whole actually were."""
    haystacks = [_normalise(chunk.text) for chunk in chunks]
    total = intact = 0
    for unit in chunker._group_into_clauses(blocks):  # noqa: SLF001
        text = unit.text()
        if not text or estimate_tokens(text) > chunker.chunk_size:
            continue  # too big to keep whole -- not a fair test of integrity
        total += 1
        needle = _normalise(text)
        if any(needle in haystack for haystack in haystacks):
            intact += 1
    return total, intact


def measure(
    pdf_bytes: bytes,
    *,
    chunk_size: int,
    chunk_overlap: int,
    provider: str,
    workdir: Path,
) -> Measurement:
    settings = Settings(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        min_chunk_size=0,
        embedding_provider=provider,  # type: ignore[arg-type]
        force_in_memory_store=True,
        chroma_dir=workdir / f"size-{chunk_size}",
        top_k=5,
    )
    service = RagService(
        settings,
        registry=DocumentRegistry(settings.registry_path),
        generator=_NullGenerator(),  # type: ignore[arg-type]
    )
    result = service.ingest(pdf_bytes, "sample_endorsement_pack.pdf")

    blocks = parse_pdf_bytes(pdf_bytes, filename="sample.pdf", doc_id="measure").blocks
    chunker = build_chunker(settings)
    chunks = chunker.chunk(blocks)
    document_tokens = sum(estimate_tokens(block.text) for block in blocks)

    sizes = [chunk.tokens for chunk in chunks]
    clauses_total, clauses_intact = _clause_integrity(chunker, blocks, chunks)

    ranks: list[int | None] = []
    top1_scores: list[float] = []
    for case in EVAL_QUESTIONS:
        hits = service.retrieve(case["question"], top_k=5)
        top1_scores.append(hits[0].score if hits else 0.0)
        wanted = _normalise(case["expects"])
        rank = next(
            (index for index, hit in enumerate(hits, start=1) if wanted in _normalise(hit.text)),
            None,
        )
        ranks.append(rank)

    found = [rank for rank in ranks if rank is not None]
    out_hits = service.retrieve(OUT_OF_SCOPE_QUESTION, top_k=1)

    return Measurement(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        chunks=result.chunks,
        mean_tokens=statistics.fmean(sizes),
        median_tokens=statistics.median(sizes),
        max_tokens=max(sizes),
        stored_tokens=sum(sizes),
        duplication=sum(sizes) / document_tokens if document_tokens else 0.0,
        clauses_total=clauses_total,
        clauses_intact=clauses_intact,
        chunks_with_split_clause=sum(1 for chunk in chunks if chunk.is_partial_clause),
        recall_at_5=len(found) / len(ranks),
        mrr=sum(1 / rank for rank in found) / len(ranks),
        mean_rank=statistics.fmean(found) if found else float("nan"),
        mean_top1_score=statistics.fmean(top1_scores),
        out_of_scope_top1=out_hits[0].score if out_hits else 0.0,
    )


def render_markdown(measurements: Sequence[Measurement], provider: str) -> str:
    header = (
        "| Metric | " + " | ".join(f"**{m.chunk_size} tokens**" for m in measurements) + " |\n"
        "|---|" + "---|" * len(measurements) + "\n"
    )

    def row(label: str, values: Sequence[str]) -> str:
        return f"| {label} | " + " | ".join(values) + " |\n"

    body = "".join(
        [
            row("Chunks produced", [str(m.chunks) for m in measurements]),
            row("Mean tokens/chunk", [f"{m.mean_tokens:.0f}" for m in measurements]),
            row("Median tokens/chunk", [f"{m.median_tokens:.0f}" for m in measurements]),
            row("Largest chunk", [str(m.max_tokens) for m in measurements]),
            row("Tokens stored", [f"{m.stored_tokens:,}" for m in measurements]),
            row("Storage vs document", [f"{m.duplication:.2f}x" for m in measurements]),
            row(
                "Clauses kept whole",
                [f"{m.clauses_intact}/{m.clauses_total} ({m.clause_integrity:.0%})" for m in measurements],
            ),
            row("Chunks holding a split clause", [str(m.chunks_with_split_clause) for m in measurements]),
            row("Recall@5", [f"{m.recall_at_5:.0%}" for m in measurements]),
            row("MRR", [f"{m.mrr:.2f}" for m in measurements]),
            row("Mean rank of answer", [f"{m.mean_rank:.2f}" for m in measurements]),
            row("Mean top-1 similarity", [f"{m.mean_top1_score:.3f}" for m in measurements]),
            row("Out-of-scope top-1", [f"{m.out_of_scope_top1:.3f}" for m in measurements]),
        ]
    )

    overlap = measurements[0].chunk_overlap if measurements else 0
    caption = (
        f"\nOverlap {overlap} tokens, {len(EVAL_QUESTIONS)} eval questions, "
        f"embeddings: `{provider}`.\n"
    )
    return header + body + caption


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF, help="document to ingest")
    parser.add_argument("--sizes", type=int, nargs="+", default=[300, 600, 1000])
    parser.add_argument("--overlap", type=int, default=120)
    parser.add_argument(
        "--provider",
        default="hashing",
        choices=["hashing", "sentence-transformers", "voyage", "auto"],
        help="embedding backend (default: hashing, deterministic and offline)",
    )
    parser.add_argument("--out", type=Path, help="also write the markdown table here")
    arguments = parser.parse_args()

    pdf_path = arguments.pdf
    if not pdf_path.exists():
        print(f"{pdf_path} not found; generating the sample pack")
        build_sample_pdf(pdf_path)
    pdf_bytes = pdf_path.read_bytes()

    workdir = ROOT / ".chunk-comparison"
    measurements = [
        measure(
            pdf_bytes,
            chunk_size=size,
            chunk_overlap=min(arguments.overlap, size - 1),
            provider=arguments.provider,
            workdir=workdir,
        )
        for size in sorted(arguments.sizes)
    ]

    table = render_markdown(measurements, arguments.provider)
    print(f"\nDocument: {pdf_path.name}\n")
    print(table)

    if arguments.out:
        arguments.out.parent.mkdir(parents=True, exist_ok=True)
        arguments.out.write_text(table, encoding="utf-8")
        print(f"written to {arguments.out}")


if __name__ == "__main__":
    main()

"""Chunking guarantees.

The headline requirement: **a numbered clause is never split in half**. Everything
downstream (citation accuracy, answer correctness) depends on it, so it is tested
against the real parsed output of the sample endorsement pack rather than against
a hand-written string.
"""

from __future__ import annotations

import re

import pytest

from app.chunking import StructureAwareChunker, build_chunker, chunk_statistics
from app.config import Settings
from app.models import Block, Chunk
from app.tokenizer import estimate_tokens

from .conftest import CHUNK_OVERLAP, CHUNK_SIZE


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _clause_units(chunker: StructureAwareChunker, blocks: list[Block]):
    return chunker._group_into_clauses(blocks)  # noqa: SLF001 -- the unit under test


# --------------------------------------------------------------------------- #
# (a) never split a numbered clause
# --------------------------------------------------------------------------- #
def test_no_clause_that_fits_is_ever_split(chunker: StructureAwareChunker, blocks, chunks):
    """Every clause smaller than the target survives intact inside one chunk."""
    haystacks = [_normalise(chunk.text) for chunk in chunks]

    checked = 0
    for unit in _clause_units(chunker, blocks):
        text = unit.text()
        if estimate_tokens(text) > CHUNK_SIZE:
            continue  # oversized clauses are allowed to split -- covered below
        checked += 1
        needle = _normalise(text)
        assert any(needle in haystack for haystack in haystacks), (
            f"clause {unit.clause_label!r} was split across chunks:\n{needle[:200]}"
        )

    assert checked > 10, "sanity: the sample pack should contain many small clauses"


def test_named_numbered_clauses_stay_whole(chunker: StructureAwareChunker, blocks, chunks):
    """Spot-check the clauses a reader would actually cite."""
    haystacks = [_normalise(chunk.text) for chunk in chunks]

    for label in ("2. AMENDMENT TO LIMIT", "3. DEDUCTIBLE", "4.2 Exclusions", "4.1 Flood Defence"):
        unit = next(
            (
                unit
                for unit in _clause_units(chunker, blocks)
                if unit.section_path and unit.section_path[-1].startswith(label.split()[0])
            ),
            None,
        )
        assert unit is not None, f"clause {label!r} was not parsed out of the sample document"
        needle = _normalise(unit.text())
        assert any(needle in haystack for haystack in haystacks), f"{label!r} was split"


def test_exclusion_subclauses_stay_with_their_clause(chunks: list[Chunk]):
    """(a)...(e) of clause 4.2 must live in the same chunk as the 4.2 stem."""
    stem = "this endorsement does not cover loss, damage, cost or expense arising"
    owner = next((chunk for chunk in chunks if stem in _normalise(chunk.text)), None)
    assert owner is not None

    body = _normalise(owner.text)
    for marker in ("(a) subsidence", "(b) loss of or damage", "(e) any fine, penalty"):
        assert marker in body, f"{marker!r} was separated from its parent clause 4.2"


# --------------------------------------------------------------------------- #
# size, overlap and metadata
# --------------------------------------------------------------------------- #
def test_chunks_respect_the_target_size(chunks: list[Chunk]):
    assert chunks
    for chunk in chunks:
        assert chunk.tokens <= CHUNK_SIZE, f"{chunk.chunk_id} is {chunk.tokens} tokens"


def test_adjacent_chunks_overlap(chunks: list[Chunk]):
    """Overlap is real text, sized to the budget and snapped to a boundary."""
    assert len(chunks) > 2

    for earlier, later in zip(chunks, chunks[1:]):
        before, after = earlier.text.split(), later.text.split()
        shared = max(
            (n for n in range(1, min(len(before), len(after)) + 1) if before[-n:] == after[:n]),
            default=0,
        )
        assert shared > 0, f"no overlap between {earlier.chunk_id} and {later.chunk_id}"

        overlap_tokens = estimate_tokens(" ".join(after[:shared]))
        assert overlap_tokens <= CHUNK_OVERLAP + 5, (
            f"overlap between {earlier.chunk_id} and {later.chunk_id} is {overlap_tokens} tokens, "
            f"over the {CHUNK_OVERLAP} budget"
        )
        assert overlap_tokens >= CHUNK_OVERLAP * 0.5, (
            f"overlap between {earlier.chunk_id} and {later.chunk_id} is only {overlap_tokens} tokens"
        )


def test_every_chunk_carries_full_citation_metadata(chunks: list[Chunk]):
    seen_ids = set()
    for chunk in chunks:
        metadata = chunk.to_metadata()
        for key in ("doc_id", "filename", "page", "section_path", "chunk_id"):
            assert key in metadata, f"{key} missing from chunk metadata"
        assert metadata["doc_id"]
        assert metadata["filename"].endswith(".pdf")
        assert metadata["page"] >= 1
        assert metadata["chunk_id"] not in seen_ids, "chunk ids must be unique"
        seen_ids.add(metadata["chunk_id"])
        # section_path may legitimately be empty only for pre-heading front matter.
        assert isinstance(metadata["section_path"], str)


def test_pages_are_monotonic_and_within_the_document(chunks: list[Chunk], parsed_document):
    for chunk in chunks:
        assert 1 <= chunk.page <= parsed_document.page_count
        assert chunk.page <= chunk.page_end <= parsed_document.page_count


# --------------------------------------------------------------------------- #
# recursive fallback
# --------------------------------------------------------------------------- #
def test_oversized_clause_falls_back_to_recursive_splitting(blocks):
    """The 500+ token cyber exclusion cannot fit; it must split, not be dropped."""
    small = StructureAwareChunker(chunk_size=200, chunk_overlap=40, min_chunk_size=0)
    produced = small.chunk(blocks)

    assert any(chunk.is_partial_clause for chunk in produced), "expected at least one split clause"
    assert all(chunk.tokens <= 200 for chunk in produced)

    # No content is lost: a distinctive phrase from deep inside the long clause
    # still appears somewhere.
    needle = "cryptocurrency transfer or negotiation fee"
    assert any(needle in _normalise(chunk.text) for chunk in produced)


def test_split_points_prefer_subclause_boundaries(blocks):
    """When a clause must be cut, the cut lands on a sub-clause marker."""
    small = StructureAwareChunker(chunk_size=200, chunk_overlap=0, min_chunk_size=0)
    produced = small.chunk(blocks)

    starts = [chunk.text.lstrip() for chunk in produced if chunk.is_partial_clause]
    assert any(re.match(r"^\([a-h]\)", start) for start in starts), (
        "expected at least one chunk to start exactly at a sub-clause marker"
    )


# --------------------------------------------------------------------------- #
# configurability
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("size", [300, 600, 1000])
def test_chunk_size_is_env_configurable(blocks, size: int):
    settings = Settings(chunk_size=size, chunk_overlap=min(120, size // 4), min_chunk_size=0)
    produced = build_chunker(settings).chunk(blocks)

    assert produced
    assert all(chunk.tokens <= size for chunk in produced)

    stats = chunk_statistics(produced)
    assert stats["count"] == len(produced)
    assert stats["max_tokens"] <= size


def test_larger_chunks_mean_fewer_chunks(blocks):
    counts = []
    for size in (300, 600, 1000):
        settings = Settings(chunk_size=size, chunk_overlap=100, min_chunk_size=0)
        counts.append(len(build_chunker(settings).chunk(blocks)))
    assert counts == sorted(counts, reverse=True), f"expected monotonically fewer chunks, got {counts}"


def test_overlap_must_be_smaller_than_chunk_size():
    with pytest.raises(ValueError):
        StructureAwareChunker(chunk_size=100, chunk_overlap=100)
    with pytest.raises(ValueError):
        Settings(chunk_size=100, chunk_overlap=150)


def test_empty_input_produces_no_chunks(chunker: StructureAwareChunker):
    assert chunker.chunk([]) == []

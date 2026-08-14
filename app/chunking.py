"""Structure-aware recursive chunking.

Why not a plain sliding window? In an endorsement pack the unit of meaning is
the numbered clause. A window that cuts "4.2 Exclusions" in half produces two
chunks that each look plausible and are each wrong: one carries the exclusion
without its trigger, the other the trigger without its exclusion. Citation
correctness starts here.

The splitter therefore works top-down and only ever descends when it has to:

1. **Clause boundaries first.** Consecutive blocks sharing a section path are
   glued into one clause unit. A clause that fits inside the target is never
   split -- several small clauses are packed together instead.
2. **Recursive fallback for oversized clauses only.** paragraph (PDF block) ->
   sub-clause marker ``(a)``/``(ii)`` -> sentence -> hard word window. Each
   level is tried in turn and only on the segments that are still too large.
3. **Overlap by whole pieces.** When a chunk is flushed, trailing pieces worth
   up to ``chunk_overlap`` tokens are carried into the next chunk. Overlap is
   real (the text is repeated) but it never starts mid-sentence.

Both ``chunk_size`` and ``chunk_overlap`` come from the environment
(``CHUNK_SIZE`` / ``CHUNK_OVERLAP``) so ingestion can be re-run and compared at
300 / 600 / 1000 tokens without touching this file.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from app.config import Settings
from app.models import Block, Chunk, Piece
from app.parsing import SUBCLAUSE_RE
from app.tokenizer import TokenCounter, estimate_tokens

logger = logging.getLogger(__name__)

__all__ = ["StructureAwareChunker", "build_chunker", "chunk_statistics"]

#: Split immediately before a sub-clause marker such as " (a) " or " (ii) ".
_SUBCLAUSE_SPLIT_RE = re.compile(r"(?=(?:^|\s)\((?:[a-z]{1,3}|[ivxIVX]{1,4}|\d{1,2})\)\s)")

#: Split on any run of newlines (paragraph boundaries inside a clause).
_PARAGRAPH_SPLIT_RE = re.compile(r"\n+")

#: Split after sentence-final punctuation followed by an opening character.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;:!?])\s+(?=[\"'(\[A-Z0-9])")


@dataclass(slots=True)
class _ClauseUnit:
    """All the blocks that live under one section path, in reading order."""

    blocks: list[Block]
    section_path: tuple[str, ...]
    clause_label: str | None

    @property
    def page_start(self) -> int:
        return min(block.page for block in self.blocks)

    @property
    def page_end(self) -> int:
        return max(block.page for block in self.blocks)

    def text(self) -> str:
        parts: list[str] = []
        for index, block in enumerate(self.blocks):
            if index and self.blocks[index - 1].kind == "heading":
                parts.append("\n")  # keep a heading attached to its first paragraph
            elif index:
                parts.append("\n\n")
            parts.append(block.text)
        return "".join(parts).strip()


class StructureAwareChunker:
    """Turns structure-tagged blocks into overlapping, clause-respecting chunks."""

    def __init__(
        self,
        *,
        chunk_size: int,
        chunk_overlap: int,
        min_chunk_size: int = 0,
        token_counter: TokenCounter = estimate_tokens,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if not 0 <= chunk_overlap < chunk_size:
            raise ValueError("chunk_overlap must be >= 0 and smaller than chunk_size")

        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.min_chunk_size = min_chunk_size
        self._count = token_counter

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def chunk(self, blocks: Sequence[Block]) -> list[Chunk]:
        """Chunk one document's blocks. Blocks must be in reading order."""
        if not blocks:
            return []

        units = self._group_into_clauses(blocks)
        pieces: list[Piece] = []
        for unit in units:
            pieces.extend(self._expand(unit))

        chunks = self._pack(pieces, doc_id=blocks[0].doc_id, filename=blocks[0].filename)
        chunks = self._merge_undersized(chunks)
        return self._renumber(chunks)

    # ------------------------------------------------------------------ #
    # Step 1 -- clause grouping
    # ------------------------------------------------------------------ #
    @staticmethod
    def _group_into_clauses(blocks: Sequence[Block]) -> list[_ClauseUnit]:
        units: list[_ClauseUnit] = []
        for block in blocks:
            if units and units[-1].section_path == block.section_path:
                units[-1].blocks.append(block)
            else:
                units.append(
                    _ClauseUnit(
                        blocks=[block],
                        section_path=block.section_path,
                        clause_label=block.clause_label,
                    )
                )
        return units

    # ------------------------------------------------------------------ #
    # Step 2 -- expand only what does not fit
    # ------------------------------------------------------------------ #
    def _expand(self, unit: _ClauseUnit) -> list[Piece]:
        text = unit.text()
        if not text:
            return []

        tokens = self._count(text)
        if tokens <= self.chunk_size:
            # The common case: the whole clause survives as one atom.
            return [
                Piece(
                    text=text,
                    tokens=tokens,
                    page_start=unit.page_start,
                    page_end=unit.page_end,
                    section_path=unit.section_path,
                    clause_label=unit.clause_label,
                    is_partial=False,
                )
            ]

        logger.debug(
            "clause %s is %d tokens (> %d); falling back to recursive splitting",
            unit.clause_label,
            tokens,
            self.chunk_size,
        )

        pieces: list[Piece] = []
        for block in unit.blocks:
            for fragment in self._split_text(block.text):
                fragment = fragment.strip()
                if not fragment:
                    continue
                pieces.append(
                    Piece(
                        text=fragment,
                        tokens=self._count(fragment),
                        page_start=block.page,
                        page_end=block.page,
                        section_path=unit.section_path,
                        clause_label=unit.clause_label,
                        is_partial=True,
                    )
                )
        return pieces

    def _split_text(self, text: str, depth: int = 0) -> list[str]:
        """Recursively split ``text`` until every fragment fits the target."""
        if self._count(text) <= self.chunk_size:
            return [text]

        splitters = (self._split_subclauses, self._split_paragraphs, self._split_sentences)
        if depth < len(splitters):
            parts = [part for part in splitters[depth](text) if part.strip()]
            if len(parts) > 1:
                fragments: list[str] = []
                for part in parts:
                    fragments.extend(self._split_text(part, depth + 1))
                return fragments
            return self._split_text(text, depth + 1)

        return self._window_split(text)

    @staticmethod
    def _split_subclauses(text: str) -> list[str]:
        return [part.strip() for part in _SUBCLAUSE_SPLIT_RE.split(text)]

    @staticmethod
    def _split_paragraphs(text: str) -> list[str]:
        return [part.strip() for part in _PARAGRAPH_SPLIT_RE.split(text)]

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        return [part.strip() for part in _SENTENCE_SPLIT_RE.split(text)]

    def _window_split(self, text: str) -> list[str]:
        """Last resort: fixed word windows with token overlap."""
        words = text.split()
        if not words:
            return []

        windows: list[str] = []
        start = 0
        while start < len(words):
            end = start
            tokens = 0
            while end < len(words):
                cost = self._count(words[end])
                if tokens + cost > self.chunk_size and end > start:
                    break
                tokens += cost
                end += 1
            windows.append(" ".join(words[start:end]))
            if end >= len(words):
                break
            # Step back by roughly `chunk_overlap` tokens worth of words.
            back = 0
            cursor = end
            while cursor > start and back < self.chunk_overlap:
                cursor -= 1
                back += self._count(words[cursor])
            start = max(cursor, start + 1)
        return windows

    # ------------------------------------------------------------------ #
    # Step 3 -- pack pieces into chunks with overlap
    # ------------------------------------------------------------------ #
    def _pack(self, pieces: Sequence[Piece], *, doc_id: str, filename: str) -> list[Chunk]:
        chunks: list[Chunk] = []
        buffer: list[Piece] = []
        buffer_tokens = 0

        for piece in pieces:
            if buffer and buffer_tokens + piece.tokens > self.chunk_size:
                chunks.append(self._materialise(buffer, doc_id, filename, len(chunks)))
                buffer = self._carry_over(buffer, piece.tokens)
                buffer_tokens = sum(item.tokens for item in buffer)
            buffer.append(piece)
            buffer_tokens += piece.tokens

        if buffer:
            chunks.append(self._materialise(buffer, doc_id, filename, len(chunks)))
        return chunks

    def _carry_over(self, buffer: Sequence[Piece], next_tokens: int) -> list[Piece]:
        """Trailing text of the flushed chunk that seeds the next one.

        Whole pieces are preferred, so overlap normally begins at a clause or
        paragraph boundary. When the final piece is on its own larger than the
        overlap budget -- common, since clauses run to several hundred tokens --
        we fall back to a sentence-aligned tail of that piece, because an
        overlap of zero would defeat the point of having one.
        """
        if self.chunk_overlap == 0 or not buffer:
            return []

        #: Below this fraction of the budget an overlap is too thin to help, so
        #: we top it up with a sentence-aligned tail of the preceding piece.
        top_up_threshold = self.chunk_overlap * 0.6

        carry: list[Piece] = []
        total = 0
        index = len(buffer) - 1
        while index >= 0 and total < self.chunk_overlap:
            piece = buffer[index]
            remaining = self.chunk_overlap - total
            if piece.tokens <= remaining:
                carry.insert(0, piece)
                total += piece.tokens
                index -= 1
                continue
            if total < top_up_threshold:
                tail = self._tail_slice(piece, remaining)
                if tail is not None:
                    carry.insert(0, tail)
                    total += tail.tokens
            break

        # Never let overlap crowd out the piece that triggered the flush.
        while carry and total + next_tokens > self.chunk_size:
            dropped = carry.pop(0)
            total -= dropped.tokens
        return carry

    def _tail_slice(self, piece: Piece, budget: int) -> Piece | None:
        """The last ``budget`` tokens of ``piece``, snapped to a sentence boundary."""
        if budget <= 0:
            return None

        sentences = [part for part in self._split_sentences(piece.text) if part]
        selected: list[str] = []
        total = 0
        for sentence in reversed(sentences):
            cost = self._count(sentence)
            if selected and total + cost > budget:
                break
            selected.insert(0, sentence)
            total += cost
            if total >= budget:
                break

        if not selected:
            return None

        text = " ".join(selected)
        if total > budget:
            # A single sentence longer than the budget: fall back to its last words.
            keep: list[str] = []
            total = 0
            for word in reversed(text.split()):
                cost = self._count(word)
                if keep and total + cost > budget:
                    break
                keep.insert(0, word)
                total += cost
            text = " ".join(keep)

        text = text.strip()
        if not text or text == piece.text:
            return None

        return Piece(
            text=text,
            tokens=self._count(text),
            page_start=piece.page_end,
            page_end=piece.page_end,
            section_path=piece.section_path,
            clause_label=piece.clause_label,
            is_partial=False,
            is_overlap=True,
        )

    def _materialise(
        self, pieces: Sequence[Piece], doc_id: str, filename: str, index: int
    ) -> Chunk:
        parts: list[str] = []
        for position, piece in enumerate(pieces):
            if position:
                same_clause = piece.section_path == pieces[position - 1].section_path
                parts.append("\n" if same_clause else "\n\n")
            parts.append(piece.text)
        text = "".join(parts).strip()

        # Carried-over overlap belongs to the *previous* chunk's clause, so it
        # must not decide this chunk's citation label or its page range.
        own = [piece for piece in pieces if not piece.is_overlap] or list(pieces)

        return Chunk(
            chunk_id=f"{doc_id}::{index:04d}",
            doc_id=doc_id,
            filename=filename,
            text=text,
            tokens=self._count(text),
            page=min(piece.page_start for piece in own),
            page_end=max(piece.page_end for piece in own),
            section_path=" > ".join(own[0].section_path),
            clause_label=own[0].clause_label,
            chunk_index=index,
            is_partial_clause=any(piece.is_partial for piece in own),
        )

    # ------------------------------------------------------------------ #
    # Step 4 -- tidy-up
    # ------------------------------------------------------------------ #
    def _merge_undersized(self, chunks: list[Chunk]) -> list[Chunk]:
        """Fold stub chunks into their predecessor when there is room.

        Only ever *joins* text, so it cannot break the never-split-a-clause
        guarantee. The merged chunk keeps the earlier chunk's section path.
        """
        if self.min_chunk_size <= 0 or len(chunks) < 2:
            return chunks

        ceiling = self.chunk_size + self.chunk_overlap
        merged: list[Chunk] = [chunks[0]]
        for chunk in chunks[1:]:
            previous = merged[-1]
            if chunk.tokens < self.min_chunk_size and previous.tokens + chunk.tokens <= ceiling:
                text = f"{previous.text}\n\n{chunk.text}"
                merged[-1] = Chunk(
                    chunk_id=previous.chunk_id,
                    doc_id=previous.doc_id,
                    filename=previous.filename,
                    text=text,
                    tokens=self._count(text),
                    page=min(previous.page, chunk.page),
                    page_end=max(previous.page_end, chunk.page_end),
                    section_path=previous.section_path,
                    clause_label=previous.clause_label,
                    chunk_index=previous.chunk_index,
                    is_partial_clause=previous.is_partial_clause or chunk.is_partial_clause,
                )
            else:
                merged.append(chunk)
        return merged

    @staticmethod
    def _renumber(chunks: Sequence[Chunk]) -> list[Chunk]:
        """Re-index after merging so chunk ids stay dense and ordered."""
        renumbered: list[Chunk] = []
        for index, chunk in enumerate(chunks):
            renumbered.append(
                Chunk(
                    chunk_id=f"{chunk.doc_id}::{index:04d}",
                    doc_id=chunk.doc_id,
                    filename=chunk.filename,
                    text=chunk.text,
                    tokens=chunk.tokens,
                    page=chunk.page,
                    page_end=chunk.page_end,
                    section_path=chunk.section_path,
                    clause_label=chunk.clause_label,
                    chunk_index=index,
                    is_partial_clause=chunk.is_partial_clause,
                )
            )
        return renumbered


def build_chunker(settings: Settings, token_counter: TokenCounter = estimate_tokens) -> StructureAwareChunker:
    """Construct a chunker from environment-driven settings."""
    return StructureAwareChunker(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        min_chunk_size=settings.min_chunk_size,
        token_counter=token_counter,
    )


def chunk_statistics(chunks: Iterable[Chunk]) -> dict[str, float]:
    """Summary numbers used by the chunk-size comparison script."""
    materialised = list(chunks)
    sizes = [chunk.tokens for chunk in materialised]
    if not sizes:
        return {"count": 0, "mean_tokens": 0.0, "median_tokens": 0.0, "max_tokens": 0.0, "split_clause_ratio": 0.0}

    ordered = sorted(sizes)
    middle = len(ordered) // 2
    median = (
        float(ordered[middle])
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2
    )
    partial = sum(1 for chunk in materialised if chunk.is_partial_clause)
    return {
        "count": float(len(sizes)),
        "mean_tokens": sum(sizes) / len(sizes),
        "median_tokens": median,
        "max_tokens": float(max(sizes)),
        "split_clause_ratio": partial / len(sizes),
    }

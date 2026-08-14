"""Offline token estimation used to size chunks.

Chunk sizing has to be deterministic, fast and network-free: ingestion runs over
every block of every page, and the whole point of the CHUNK_SIZE/CHUNK_OVERLAP
knobs is that you can re-run ingestion at several settings and compare them.

Two things we deliberately do *not* do:

* ``tiktoken`` -- that is OpenAI's tokenizer. It undercounts Claude tokens by
  roughly 15-20% on prose and much more on code or identifiers, so sizing
  against it would silently produce oversized prompts.
* ``client.messages.count_tokens`` -- accurate for Claude, but it is a network
  round-trip per call, which makes ingestion slow, non-deterministic and
  dependent on an API key.

Instead we use a word-aware character heuristic: whitespace/punctuation-split
pieces, with long pieces charged extra at ~4 characters per token — the usual
rule of thumb for English prose under BPE.

This is an approximation and has not been calibrated against Claude's own
counter, so treat every number it produces as "estimated tokens" (which is how
they are labelled in `/health` and in the comparison script). That is fine for
its actual job: a chunk *target* only has to be consistent, and 500-600 has
plenty of headroom against any context limit. If you need exact counts — for
billing, or for packing a prompt right up to a limit — call
`client.messages.count_tokens` on the assembled prompt instead, and swap this
function out via the `token_counter` argument to `StructureAwareChunker`.
"""

from __future__ import annotations

import math
import re
from typing import Callable

__all__ = ["TokenCounter", "estimate_tokens"]

TokenCounter = Callable[[str], int]

#: Words (including digits/underscores) or a single punctuation character.
_PIECE_RE = re.compile(r"\w+|[^\w\s]")

#: Average characters per BPE token for English prose.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Return an approximate Claude token count for ``text``.

    Short pieces cost one token; longer pieces are charged one token per
    ~4 characters, which is how BPE tends to break up long words.
    """
    if not text:
        return 0

    total = 0
    for match in _PIECE_RE.finditer(text):
        length = len(match.group())
        total += 1 if length <= _CHARS_PER_TOKEN else math.ceil(length / _CHARS_PER_TOKEN)
    return total

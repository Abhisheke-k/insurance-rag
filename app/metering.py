"""Week 10: measure what a pipeline spends -- calls, tokens, seconds, dollars.

Week 7 compared the agent and the fixed workflow on LLM *call count* because
``LLMResponse`` carried no token usage. The Week 10 brief asks for four
numbers -- quality, speed, tokens, cost -- so this module supplies the three
that are not quality, at the one seam every pipeline already goes through:
``LLMProvider.complete()``.

:class:`MeteredProvider` wraps any provider (Anthropic, OpenAI or the stub),
times each call, and records its tokens against a *role* label, so a team's
cost can be read per agent (manager vs specialists) as well as in total.
Usage is the backend's own figure when it reports one; otherwise it is
estimated from the real prompt and reply text with the same
``estimate_tokens`` heuristic the chunker uses, and flagged ``estimated`` so a
report can say so. Cost is tokens x list price; see :data:`PRICES_USD_PER_MTOK`.

Two things this deliberately does not model, and the report must say so:
wall-clock here is the time the *provider call* took (a stub's is ~0, a live
model's is not -- the structural measure, sequential calls on the critical
path, is what predicts live latency), and prompt caching (none of this app's
calls set ``cache_control``, so cache tokens are zero in practice).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.llm import LLMProvider, LLMResponse, LLMUsage
from app.tokenizer import estimate_tokens

__all__ = [
    "PRICES_USD_PER_MTOK",
    "PRICE_SOURCE",
    "CallRecord",
    "Meter",
    "MeteredProvider",
    "Totals",
    "UnknownModelPrice",
    "cost_usd",
    "summarise",
]

#: model id -> (input, output) USD per million tokens. First-party list prices for prompts
#: up to 100K tokens, from the Claude API skill's model table (cached 2026-10-06). Check
#: https://platform.claude.com/docs/en/about-claude/pricing before relying on them for billing.
PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
PRICE_SOURCE = "Claude API model table (cached 2026-10-06), first-party list prices, prompts <= 100K tokens"

#: Anthropic's documented multipliers on the input price (not used by this app today).
_CACHE_READ_MULTIPLIER = 0.1
_CACHE_WRITE_MULTIPLIER = 1.25


class UnknownModelPrice(KeyError):
    """No price on file for the model asked about."""


@dataclass(frozen=True, slots=True)
class CallRecord:
    role: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    seconds: float
    estimated: bool


@dataclass(frozen=True, slots=True)
class Totals:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    llm_seconds: float = 0.0
    estimated_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    def __add__(self, other: Totals) -> Totals:
        return Totals(
            self.calls + other.calls,
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.llm_seconds + other.llm_seconds,
            self.estimated_calls + other.estimated_calls,
        )


def summarise(records: Iterable[CallRecord]) -> Totals:
    total = Totals()
    for r in records:
        total += Totals(1, r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens, r.seconds, int(r.estimated))
    return total


def cost_usd(totals: Totals, model: str) -> float:
    """Dollar cost of ``totals`` at ``model``'s list price."""
    try:
        price_in, price_out = PRICES_USD_PER_MTOK[model]
    except KeyError as exc:
        raise UnknownModelPrice(f"no price on file for {model!r}; known: {sorted(PRICES_USD_PER_MTOK)}") from exc
    return (
        totals.input_tokens * price_in
        + totals.cache_read_tokens * price_in * _CACHE_READ_MULTIPLIER
        + totals.cache_write_tokens * price_in * _CACHE_WRITE_MULTIPLIER
        + totals.output_tokens * price_out
    ) / 1_000_000


class Meter:
    """A thread-safe ledger of :class:`CallRecord`s."""

    def __init__(self) -> None:
        self._records: list[CallRecord] = []
        self._lock = threading.Lock()

    def add(self, record: CallRecord) -> None:
        with self._lock:
            self._records.append(record)

    def mark(self) -> int:
        with self._lock:
            return len(self._records)

    def since(self, mark: int) -> list[CallRecord]:
        with self._lock:
            return list(self._records[mark:])

    def by_role(self, records: Iterable[CallRecord]) -> dict[str, Totals]:
        grouped: dict[str, list[CallRecord]] = defaultdict(list)
        for record in records:
            grouped[record.role].append(record)
        return {role: summarise(group) for role, group in grouped.items()}


class MeteredProvider(LLMProvider):
    """Wrap a provider; record every ``complete()`` against ``role`` on a shared :class:`Meter`."""

    def __init__(self, inner: LLMProvider, meter: Meter, *, role: str = "agent") -> None:
        self._inner = inner
        self._meter = meter
        self._role = role
        self.name = inner.name

    def with_role(self, role: str) -> MeteredProvider:
        return MeteredProvider(self._inner, self._meter, role=role)

    def is_configured(self) -> bool:
        return self._inner.is_configured()

    def status(self) -> dict[str, Any]:
        return self._inner.status()

    def complete(
        self,
        *,
        system: str,
        user_message: str,
        json_schema: dict[str, Any],
        max_tokens: int,
        effort: str = "medium",
    ) -> LLMResponse:
        started = time.perf_counter()
        response = self._inner.complete(
            system=system, user_message=user_message, json_schema=json_schema, max_tokens=max_tokens, effort=effort
        )
        seconds = time.perf_counter() - started
        usage = response.usage or LLMUsage(
            input_tokens=estimate_tokens(system) + estimate_tokens(user_message),
            output_tokens=estimate_tokens(response.text),
            estimated=True,
        )
        self._meter.add(
            CallRecord(
                role=self._role,
                model=response.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                seconds=seconds,
                estimated=usage.estimated,
            )
        )
        return response

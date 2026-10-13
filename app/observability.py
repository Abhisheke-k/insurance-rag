"""Week 11: per-request traces with per-step time and cost, written so a past answer can be found.

Week 5's ``app/tracing.py`` captured one record per claim summary: enough to read a
trace and replay it, but one lump -- no per-step latency, no tokens, no cost, nothing
for the agent loop or the MCP hop. This module is the production version of the idea.

**A trace is one request end to end; a span is one operation inside it.** A claim
summary is not one line in a log, it is a retrieval span, a generation span, a
verification span; an agent request is those plus a tool span per tool call. Each
span carries its own start offset, duration and attributes (generation spans: model,
prompt version, input/output tokens, cost, stop reason; retrieval spans: the retrieved
context ids and scores; tool spans: tool name, a digest of the arguments, outcome).
That is what lets a p99 be attributed to a stage and a cost to a step.

One trace is written as one JSON line, with these deliberate properties:

* **Redaction on write.** ``Trace`` redacts every free-text field as it is stored
  (``app/redaction.py``); there is no later cleaning pass. The input is kept in
  redacted form, plus a digest of the raw text for exact-match lookup. Claim and
  policy numbers are pseudonymised so traces of the same claim still join.
* **Findable from the complaint.** The fields a vague complaint can hang on --
  time, user, feature, input type, prompt version, the decision/exclusion in the
  *output*, cost, the retrieved context ids -- are top-level keys of the record, not
  buried in a blob (see ``app/logquery.py`` for the slices built on them).
* **Replayable.** A trace records the prompt versions and the context ids/scores and
  model that produced the answer, which is what "reproduce that exact answer eighteen
  months from now" starts from.
* **Safe by default.** With no sink configured nothing is written; the span helpers
  are no-ops outside a trace. Observability can never be the reason a request fails.

Trace ids are 32 hex characters and span ids 16, so a trace id can travel to another
process as a W3C ``traceparent`` (:func:`make_traceparent`) -- how Week 12 keeps one id
across the MCP hop.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.llm import LLMProvider, LLMResponse, LLMUsage
from app.redaction import Redactor, get_redactor
from app.tokenizer import estimate_tokens

logger = logging.getLogger(__name__)

__all__ = [
    "SCHEMA",
    "JsonlSink",
    "Span",
    "Trace",
    "TracedProvider",
    "Tracer",
    "annotate",
    "current_trace",
    "make_traceparent",
    "parse_traceparent",
    "propagating_context",
    "span",
]

SCHEMA = "trace/1"
_INPUT_CHARS = 600
_SUMMARY_CHARS = 600

_TRACE: contextvars.ContextVar[Trace | None] = contextvars.ContextVar("current_trace", default=None)
_PARENT: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_span", default=None)


# --------------------------------------------------------------------------- #
# W3C trace context
# --------------------------------------------------------------------------- #
def make_traceparent(trace_id: str, span_id: str) -> str:
    return f"00-{trace_id}-{span_id}-01"


def parse_traceparent(value: str | None) -> tuple[str, str] | None:
    """``(trace_id, parent_span_id)`` from a ``traceparent`` header, or ``None`` if malformed."""
    if not value:
        return None
    parts = value.strip().split("-")
    if len(parts) != 4 or len(parts[1]) != 32 or len(parts[2]) != 16:
        return None
    try:
        int(parts[1], 16)
        int(parts[2], 16)
    except ValueError:
        return None
    return parts[1], parts[2]


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass
class Span:
    span_id: str
    parent_id: str | None
    name: str
    kind: str  # "retrieval" | "generation" | "tool" | "verify" | "stage"
    start_ms: float = 0.0
    duration_ms: float = 0.0
    status: str = "ok"
    error: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)

    def set(self, **attrs: Any) -> None:
        self.attrs.update(attrs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "kind": self.kind,
            "start_ms": round(self.start_ms, 3),
            "duration_ms": round(self.duration_ms, 3),
            "status": self.status,
            "error": self.error,
            "attrs": self.attrs,
        }


class Trace:
    """One request. Created by :meth:`Tracer.trace`; spans attach to it through a context variable."""

    def __init__(
        self,
        tracer: Tracer,
        *,
        trace_id: str,
        feature: str,
        user: str | None,
        input_type: str,
        input_text: str,
        started_at: datetime,
        root_span_id: str,
    ) -> None:
        self.tracer = tracer
        self.trace_id = trace_id
        self.feature = feature
        self.input_type = input_type
        self.started_at = started_at
        self.root_span_id = root_span_id
        self._t0 = time.perf_counter()
        self._lock = threading.Lock()
        self.spans: list[Span] = []
        self.output: dict[str, Any] = {}
        self.tags: dict[str, Any] = {}
        self.error: str | None = None

        redactor = tracer.redactor
        self.user = redactor.pseudonym("user", user) if user else None
        redacted = redactor.redact(input_text)
        self.input = {
            "redacted": redacted.text[:_INPUT_CHARS],
            "redactions": redacted.kinds,
            "chars": len(input_text),
            "digest": hashlib.sha256(input_text.encode("utf-8")).hexdigest()[:16],
        }

    # -- spans ---------------------------------------------------------------
    def now_ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000

    def _add(self, span_: Span) -> None:
        with self._lock:
            self.spans.append(span_)

    def traceparent(self, span_id: str | None = None) -> str:
        return make_traceparent(self.trace_id, span_id or _PARENT.get() or self.root_span_id)

    # -- output --------------------------------------------------------------
    def set_output(self, **fields: Any) -> None:
        """Record what the request produced. Free-text fields are redacted as they are stored."""
        for key in ("summary", "rationale"):
            if isinstance(fields.get(key), str):
                fields[key] = self.tracer.redactor.redact(fields[key]).text[:_SUMMARY_CHARS]
        self.output.update(fields)

    def tag(self, **tags: Any) -> None:
        self.tags.update(tags)

    # -- serialisation ---------------------------------------------------------
    def to_record(self, duration_ms: float) -> dict[str, Any]:
        spans = sorted(self.spans, key=lambda s: s.start_ms)
        generations = [s for s in spans if s.kind == "generation"]
        retrievals = [s for s in spans if s.kind == "retrieval"]
        versions = sorted({s.attrs["prompt_version"] for s in generations if s.attrs.get("prompt_version")})
        cost = sum(float(s.attrs.get("cost_usd") or 0.0) for s in generations)
        context_ids: list[str] = []
        for r in retrievals:
            context_ids.extend(i for i in r.attrs.get("context_ids", []) if i not in context_ids)
        return {
            "schema": SCHEMA,
            "trace_id": self.trace_id,
            "started_at": self.started_at.isoformat(),
            "duration_ms": round(duration_ms, 3),
            "feature": self.feature,
            "input_type": self.input_type,
            "user": self.user,
            "prompt_versions": versions,
            "input": self.input,
            "output": self.output,
            "context_ids": context_ids,
            "cost": {
                "usd": round(cost, 6),
                "basis": next((s.attrs.get("cost_basis") for s in generations if s.attrs.get("cost_basis")), None),
                "input_tokens": sum(int(s.attrs.get("input_tokens") or 0) for s in generations),
                "output_tokens": sum(int(s.attrs.get("output_tokens") or 0) for s in generations),
                "llm_calls": len(generations),
            },
            "tags": self.tags,
            "error": self.error,
            "spans": [s.to_dict() for s in spans],
        }


# --------------------------------------------------------------------------- #
# Sink and tracer
# --------------------------------------------------------------------------- #
class JsonlSink:
    """Append-only, one JSON object per line, safe to share between threads."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


class Tracer:
    """Starts traces and owns where they go. ``sink=None`` turns writing off; spans still work."""

    def __init__(
        self,
        sink: JsonlSink | None = None,
        *,
        redactor: Redactor | None = None,
        price_model: str = "claude-opus-5",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sink = sink
        self.redactor = redactor or get_redactor()
        self.price_model = price_model
        #: Supplies a trace's start time when the caller gives none. Production leaves it ``None``
        #: (wall clock); the Week 11 traffic generator sets it to backdate a few days of logs.
        self.clock = clock

    @contextmanager
    def trace(
        self,
        feature: str,
        *,
        input_text: str = "",
        user: str | None = None,
        input_type: str = "unknown",
        started_at: datetime | None = None,
        traceparent: str | None = None,
    ) -> Iterator[Trace]:
        """Open a trace for one request.

        Re-entrant: if a trace is already active (the triage service calls the agent, which
        calls retrieval), the inner call becomes a ``stage`` span of the outer trace and does
        not write a second record. ``traceparent`` continues a trace started in another process.
        """
        outer = _TRACE.get()
        if outer is not None:
            with span(f"stage:{feature}", "stage", feature=feature):
                yield outer
            return

        inherited = parse_traceparent(traceparent)
        trace_id = inherited[0] if inherited else uuid.uuid4().hex
        root_span_id = secrets.token_hex(8)
        active = Trace(
            self,
            trace_id=trace_id,
            feature=feature,
            user=user,
            input_type=input_type,
            input_text=input_text,
            started_at=started_at or (self.clock() if self.clock else datetime.now(timezone.utc)),
            root_span_id=root_span_id,
        )
        trace_token = _TRACE.set(active)
        parent_token = _PARENT.set(root_span_id)
        try:
            yield active
        except BaseException as exc:
            active.error = f"{type(exc).__name__}: {exc}"[:300]
            raise
        finally:
            _PARENT.reset(parent_token)
            _TRACE.reset(trace_token)
            self._finish(active)

    def _finish(self, active: Trace) -> None:
        if self.sink is None:
            return
        try:
            self.sink.write(active.to_record(active.now_ms()))
        except Exception:  # noqa: BLE001 -- observability must never fail the request it observes
            logger.exception("could not write trace %s", active.trace_id)


def current_trace() -> Trace | None:
    return _TRACE.get()


@contextmanager
def span(name: str, kind: str, **attrs: Any) -> Iterator[Span | None]:
    """A timed operation inside the current trace; a no-op (yields ``None``) outside one."""
    active = _TRACE.get()
    if active is None:
        yield None
        return
    current = Span(
        span_id=secrets.token_hex(8),
        parent_id=_PARENT.get(),
        name=name,
        kind=kind,
        start_ms=active.now_ms(),
        attrs=dict(attrs),
    )
    token = _PARENT.set(current.span_id)
    started = time.perf_counter()
    try:
        yield current
    except BaseException as exc:
        current.status = "error"
        current.error = f"{type(exc).__name__}: {exc}"[:200]
        raise
    finally:
        current.duration_ms = (time.perf_counter() - started) * 1000
        _PARENT.reset(token)
        active._add(current)  # noqa: SLF001 -- same module; Trace owns its lock


def annotate(**attrs: Any) -> None:
    """Attach attributes to the trace itself (tags), if one is active."""
    active = _TRACE.get()
    if active is not None:
        active.tag(**attrs)


def propagating_context() -> contextvars.Context:
    """A copy of the current context for running work on another thread under the same trace."""
    return contextvars.copy_context()


# --------------------------------------------------------------------------- #
# The LLM seam
# --------------------------------------------------------------------------- #
class TracedProvider(LLMProvider):
    """Wrap any provider so every ``complete()`` becomes a generation span on the active trace.

    Applied once, in ``app.llm.build_llm_provider``, so every call site (answers, claim summaries,
    the agent loops, the judge) is traced without knowing about tracing. Outside a trace it is a
    pass-through.
    """

    def __init__(self, inner: LLMProvider, *, price_model: str = "claude-opus-5") -> None:
        self._inner = inner
        self._price_model = price_model
        self.name = inner.name

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
        if _TRACE.get() is None:
            return self._inner.complete(
                system=system, user_message=user_message, json_schema=json_schema, max_tokens=max_tokens, effort=effort
            )

        from app.metering import PRICES_USD_PER_MTOK, Totals, cost_usd  # noqa: PLC0415 -- avoids an import cycle
        from app.prompts import version_for_system  # noqa: PLC0415

        with span("llm.complete", "generation") as current:
            response = self._inner.complete(
                system=system, user_message=user_message, json_schema=json_schema, max_tokens=max_tokens, effort=effort
            )
            usage = response.usage or LLMUsage(
                input_tokens=estimate_tokens(system) + estimate_tokens(user_message),
                output_tokens=estimate_tokens(response.text),
                estimated=True,
            )
            model_for_price = response.model if response.model in PRICES_USD_PER_MTOK else self._price_model
            totals = Totals(1, usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens)
            basis = "api" if response.model in PRICES_USD_PER_MTOK else f"projected@{model_for_price}"
            if current is not None:
                current.set(
                    model=response.model,
                    prompt_version=version_for_system(system),
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    tokens_estimated=usage.estimated,
                    cost_usd=round(cost_usd(totals, model_for_price), 6),
                    cost_basis=basis,
                    stop_reason=response.stop_reason,
                    effort=effort,
                )
            return response

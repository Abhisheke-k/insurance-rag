"""Week 9: who may call which tool on the claims desk's MCP server.

An MCP server that only ever runs as a subprocess of its one host (stdio) is
protected by the process boundary: whoever can launch it already owns the
machine. The moment it listens on a socket for *other people's* agents, that
stops being true, and "any client that can reach the port can call every tool"
is the default. This module closes that default with two small, separate checks:

1. **Authentication** (HTTP only): a static bearer token per caller, compared
   in constant time. No token, or an unknown one, is a ``401`` before MCP sees
   the request. Tokens are held as SHA-256 digests, so the in-memory table is
   not a list of usable secrets.
2. **Authorisation** (per tool): each caller (a :class:`Principal`) has a set
   of tools it may use. ``tools/list`` shows it only those -- a tool you may
   not call is not advertised -- and ``tools/call`` on anything else is refused.
   A narrow scope is the point: a partner team's agent that only needs to
   search policy wording does not get ``lookup_exclusion_code`` too.

Fail closed: on the HTTP transport a request that reaches the tool layer with
no resolvable principal is refused, never waved through as "local".
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_REQUEST, CallToolResult, TextContent

from app.observability import JsonlSink, parse_traceparent

logger = logging.getLogger(__name__)

__all__ = [
    "CALL_PRINCIPAL",
    "CALL_TRACE",
    "LOCAL_PRINCIPAL",
    "AccessPolicy",
    "BearerAuthMiddleware",
    "Principal",
    "ToolScopeMiddleware",
]

#: Who is making the tool call that is running, and the (trace id, parent span id) the caller sent in ``_meta.traceparent``.
#: Set by :class:`ToolScopeMiddleware` for the duration of one call so a tool that writes (``refer_to_adjuster``) can record
#: both without every handler taking a context argument.
CALL_PRINCIPAL: ContextVar[str | None] = ContextVar("mcp_call_principal", default=None)
CALL_TRACE: ContextVar[tuple[str, str] | None] = ContextVar("mcp_call_trace", default=None)


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller and the tools it is allowed to use."""

    name: str
    #: ``None`` means every tool the server has enabled; a set narrows it.
    tools: frozenset[str] | None = None

    def allows(self, tool: str) -> bool:
        return self.tools is None or tool in self.tools


#: The single caller of a stdio server: whoever launched the process.
LOCAL_PRINCIPAL = Principal("local-stdio")


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


@dataclass(slots=True)
class AccessPolicy:
    """Bearer token -> :class:`Principal`, held as digests."""

    _by_digest: dict[bytes, Principal] = field(default_factory=dict)

    @classmethod
    def from_tokens(cls, tokens: Mapping[str, Principal]) -> AccessPolicy:
        for token in tokens:
            if len(token) < 16:
                raise ValueError("bearer tokens must be at least 16 characters; use secrets.token_urlsafe(32)")
        return cls({_digest(token): principal for token, principal in tokens.items()})

    @classmethod
    def from_json(cls, raw: str) -> AccessPolicy:
        """Parse ``{"<token>": {"name": "partner-a", "tools": ["search_policy"]}, ...}``.

        ``"tools": null`` (or omitted) grants every enabled tool.
        """
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not payload:
            raise ValueError("MCP_AUTH_TOKENS must be a non-empty JSON object of token -> principal")
        tokens: dict[str, Principal] = {}
        for token, spec in payload.items():
            tools = spec.get("tools")
            tokens[token] = Principal(
                name=str(spec.get("name") or "unnamed"),
                tools=frozenset(tools) if tools is not None else None,
            )
        return cls.from_tokens(tokens)

    def authenticate(self, authorization: str | None) -> Principal | None:
        """The principal for an ``Authorization: Bearer <token>`` header, or ``None``."""
        if not authorization:
            return None
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        presented = _digest(token.strip())
        match: Principal | None = None
        # Compare against every entry (no early exit) so timing does not reveal
        # which stored digest a near-miss resembled.
        for stored, principal in self._by_digest.items():
            if hmac.compare_digest(stored, presented):
                match = principal
        return match


class BearerAuthMiddleware:
    """ASGI wrapper: reject any HTTP request without a valid bearer token with 401."""

    def __init__(self, app: Any, policy: AccessPolicy) -> None:
        self._app = app
        self._policy = policy

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":  # lifespan (and websocket) pass straight through
            await self._app(scope, receive, send)
            return

        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope["headers"]}
        if self._policy.authenticate(headers.get("authorization")) is None:
            body = json.dumps({"error": "unauthorized"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"www-authenticate", b'Bearer realm="claims-desk-mcp"'),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            logger.warning("mcp.audit unauthenticated request rejected path=%s", scope.get("path"))
            return
        await self._app(scope, receive, send)


class ToolScopeMiddleware:
    """MCP-level middleware: scope ``tools/list`` and ``tools/call`` to the caller.

    Also the audit log -- one line per tool call with who, what, outcome and
    duration, never the arguments (they may carry claim details).
    """

    def __init__(self, policy: AccessPolicy | None, *, transport: str, trace_sink: JsonlSink | None = None) -> None:
        if transport == "http" and policy is None:
            raise ValueError("the HTTP transport requires an AccessPolicy (refusing to serve unauthenticated)")
        self._policy = policy
        self._transport = transport
        self._trace_sink = trace_sink

    def _principal(self, ctx: Any) -> Principal | None:
        if self._transport == "stdio":
            return LOCAL_PRINCIPAL
        request = ctx.request
        if request is None or self._policy is None:
            return None  # fail closed
        return self._policy.authenticate(request.headers.get("authorization"))

    @staticmethod
    def _visible(principal: Principal, tool: str) -> bool:
        return principal.allows(tool)

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        if ctx.method not in ("tools/list", "tools/call"):
            return await call_next(ctx)

        principal = self._principal(ctx)
        if principal is None:
            raise MCPError(INVALID_REQUEST, "unauthenticated")

        if ctx.method == "tools/call":
            name = str((ctx.params or {}).get("name", ""))
            if not self._visible(principal, name):
                logger.warning("mcp.audit DENIED principal=%s tool=%s", principal.name, name)
                # Byte-for-byte what the SDK answers for a tool that does not exist, so a
                # caller cannot tell "not yours" from "not there" and probe its way to
                # the names of tools outside its scope.
                return CallToolResult(content=[TextContent(type="text", text=f"Unknown tool: {name}")], is_error=True)
            started = time.perf_counter()
            started_at = datetime.now(timezone.utc)
            outcome = "error"
            meta = ctx.meta or {}
            inherited = parse_traceparent(meta.get("traceparent") if hasattr(meta, "get") else None)
            tokens = (CALL_PRINCIPAL.set(principal.name), CALL_TRACE.set(inherited))
            try:
                result = await call_next(ctx)
                outcome = "tool_error" if _is_error_result(result) else "ok"
                return result
            finally:
                CALL_PRINCIPAL.reset(tokens[0])
                CALL_TRACE.reset(tokens[1])
                elapsed_ms = (time.perf_counter() - started) * 1000
                logger.info("mcp.audit principal=%s tool=%s outcome=%s ms=%.1f", principal.name, name, outcome, elapsed_ms)
                self._write_span(ctx, name, principal.name, outcome, started_at, elapsed_ms)

        result = await call_next(ctx)
        return _filter_tool_listing(result, lambda tool_name: self._visible(principal, tool_name))


    def _write_span(self, ctx: Any, tool: str, principal: str, outcome: str, started_at: datetime, ms: float) -> None:
        """The server's half of a trace: same trace id as the caller's, never the arguments."""
        if self._trace_sink is None:
            return
        meta = ctx.meta or {}
        inherited = parse_traceparent(meta.get("traceparent") if hasattr(meta, "get") else None)
        self._trace_sink.write(
            {
                "schema": "span/1",
                "side": "server",
                "trace_id": inherited[0] if inherited else None,
                "parent_id": inherited[1] if inherited else None,
                "span_id": secrets.token_hex(8),
                "name": f"mcp.tools/call:{tool}",
                "kind": "tool",
                "tool": tool,
                "principal": principal,
                "started_at": started_at.isoformat(),
                "duration_ms": round(ms, 3),
                "status": "ok" if outcome == "ok" else "error",
                "outcome": outcome,
            }
        )


def _is_error_result(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("isError") or result.get("is_error"))
    return bool(getattr(result, "is_error", False))


def _filter_tool_listing(result: Any, keep: Any) -> Any:
    """Drop tools the caller may not use from a ``tools/list`` result (model or dict)."""
    if isinstance(result, dict):
        filtered = dict(result)
        filtered["tools"] = [tool for tool in result.get("tools", []) if keep(tool.get("name", ""))]
        return filtered
    tools = [tool for tool in result.tools if keep(tool.name)]
    return result.model_copy(update={"tools": tools})

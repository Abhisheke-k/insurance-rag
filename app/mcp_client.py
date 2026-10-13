"""Week 9: the host's side of the Model Context Protocol -- a blocking client.

The agent loop in this app is synchronous (one ``LLMProvider.complete()`` call
per step, no event loop anywhere), while the MCP SDK is asyncio-only. Rather
than make the whole app async, :class:`McpToolClient` owns one background
thread running one event loop for the lifetime of the connection and exposes
plain blocking methods. Calls may come from several threads at once (the
Week 10 team runs its specialists in parallel over one shared connection); the
protocol multiplexes them.

A connection is entered and exited by a single coroutine: anyio's cancel
scopes, which the stdio and HTTP transports are built on, must be closed by the
task that opened them.

Nothing here knows what any tool *does*. It lists definitions and relays calls;
whether a tool is trustworthy is :mod:`app.mcp_vetting`'s question, and which
tool to call is the model's.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED, Tool

logger = logging.getLogger(__name__)

__all__ = ["McpConnectionError", "McpToolClient", "ToolDefinition", "ToolOutcome"]

#: A server cannot make us page forever through a listing.
_MAX_LISTING_PAGES = 20


class McpConnectionError(RuntimeError):
    """The tool server could not be reached, or the connection dropped."""


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One tool as a server advertises it -- the only thing the host knows about it up front."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    title: str | None = None
    #: Annotations are the server's *claims* about itself -- hints, not guarantees.
    read_only: bool | None = None
    destructive: bool | None = None

    @classmethod
    def from_mcp(cls, tool: Tool) -> ToolDefinition:
        annotations = tool.annotations
        return cls(
            name=tool.name,
            description=tool.description or "",
            input_schema=dict(tool.input_schema),
            output_schema=dict(tool.output_schema) if tool.output_schema else None,
            title=tool.title,
            read_only=annotations.read_only_hint if annotations else None,
            destructive=annotations.destructive_hint if annotations else None,
        )

    def fingerprint(self) -> str:
        """Stable hash of everything the model will be shown about this tool.

        Used to pin a definition: a server that changes a description *after*
        you reviewed it (a "rug pull") changes this value.
        """
        canonical = json.dumps(
            {
                "name": self.name,
                "title": self.title,
                "description": self.description,
                "input_schema": self.input_schema,
                "output_schema": self.output_schema,
                "read_only": self.read_only,
                "destructive": self.destructive,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def for_prompt(self) -> dict[str, Any]:
        """The compact form placed in the model's prompt."""
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """A tool call's result, reduced to what the host acts on."""

    text: str
    structured: Any | None
    is_error: bool


Target = StdioServerParameters | str | MCPServer


class McpToolClient:
    """Blocking MCP client: ``connect()``, ``list_tools()``, ``call_tool()``, ``close()``.

    ``target`` is a :class:`~mcp.StdioServerParameters` (spawn a server
    subprocess), a ``http(s)://`` URL (streamable HTTP; pass ``headers`` for a
    bearer token) or, in tests, an in-process ``MCPServer``.
    """

    def __init__(
        self,
        target: Target,
        *,
        headers: dict[str, str] | None = None,
        client_name: str = "claims-desk-host",
        call_timeout_seconds: float = 30.0,
        connect_timeout_seconds: float = 60.0,
    ) -> None:
        self._target = target
        self._headers = headers
        self._client_name = client_name
        self._call_timeout = call_timeout_seconds
        self._connect_timeout = connect_timeout_seconds

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._client: Client | None = None
        self._error: BaseException | None = None
        self._count_lock = threading.Lock()
        self._counts = {"tools/list": 0, "tools/call": 0}

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #
    def connect(self) -> McpToolClient:
        if self._thread is not None:
            raise RuntimeError("already connected")
        self._thread = threading.Thread(target=self._run, name="mcp-client", daemon=True)
        self._thread.start()
        if not self._ready.wait(self._connect_timeout):
            raise McpConnectionError(f"timed out after {self._connect_timeout:.0f}s connecting to the tool server")
        if self._error is not None:
            raise McpConnectionError(f"could not connect to the tool server: {self._error}") from self._error
        return self

    def close(self) -> None:
        if self._thread is None:
            return
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=15)
        self._thread = None

    def __enter__(self) -> McpToolClient:
        return self.connect()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:  # noqa: BLE001 -- surfaced to the caller via connect()/call errors
            self._error = exc
            logger.debug("mcp client thread ended: %r", exc)
        finally:
            self._ready.set()

    async def _serve(self) -> None:
        from mcp_types import Implementation  # noqa: PLC0415

        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        transport: Any = self._target
        if isinstance(self._target, str):
            import httpx2  # noqa: PLC0415 -- the SDK's own HTTP stack
            from mcp.client.streamable_http import streamable_http_client  # noqa: PLC0415

            http_client = httpx2.AsyncClient(headers=self._headers or {}, timeout=self._call_timeout)
            transport = streamable_http_client(self._target, http_client=http_client)
        async with Client(
            transport,
            client_info=Implementation(name=self._client_name, version="1.0.0"),
            cache=None,  # always ask the server: a stale listing would hide a changed definition
            read_timeout_seconds=self._call_timeout,
        ) as client:
            self._client = client
            self._ready.set()
            await self._stop.wait()

    # ------------------------------------------------------------------ #
    # Calls
    # ------------------------------------------------------------------ #
    @property
    def server_name(self) -> str:
        info = self._require().server_info
        return info.name if info else "(anonymous server)"

    @property
    def protocol_version(self) -> str:
        return self._require().protocol_version

    @property
    def round_trips(self) -> dict[str, int]:
        """Requests made so far, by method -- the host-side view of what the server was asked."""
        with self._count_lock:
            return dict(self._counts)

    def _count(self, method: str) -> None:
        with self._count_lock:
            self._counts[method] += 1

    def _require(self) -> Client:
        if self._client is None or self._thread is None:
            raise McpConnectionError("not connected to a tool server")
        return self._client

    def _submit(self, coroutine: Any) -> Any:
        if self._loop is None:
            raise McpConnectionError("not connected to a tool server")
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout=self._call_timeout + 5)
        except TimeoutError as exc:
            future.cancel()
            raise McpConnectionError(f"tool server did not answer within {self._call_timeout:.0f}s") from exc

    def list_tools(self) -> list[ToolDefinition]:
        """Ask the server what it offers *now* (never cached), following pagination."""
        client = self._require()
        self._count("tools/list")

        async def collect() -> list[ToolDefinition]:
            tools: list[ToolDefinition] = []
            cursor: str | None = None
            for _ in range(_MAX_LISTING_PAGES):
                page = await client.list_tools(cursor=cursor, cache_mode="refresh")
                tools.extend(ToolDefinition.from_mcp(tool) for tool in page.tools)
                cursor = page.next_cursor
                if cursor is None:
                    break
            return tools

        try:
            return self._submit(collect())
        except MCPError as exc:
            raise McpConnectionError(f"tools/list failed: {exc.message}") from exc

    def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        """Call one tool. Tool-level and protocol-level refusals come back as ``is_error`` outcomes
        (so the model can see them and recover); only a dead connection raises."""
        client = self._require()
        self._count("tools/call")

        async def call() -> ToolOutcome:
            try:
                result = await client.call_tool(name, arguments)
            except MCPError as exc:
                if exc.code == CONNECTION_CLOSED:
                    raise McpConnectionError("the tool server closed the connection") from exc
                return ToolOutcome(text=f"MCP error {exc.code}: {exc.message}", structured=None, is_error=True)
            text = "\n".join(block.text for block in result.content if getattr(block, "type", None) == "text")
            return ToolOutcome(text=text, structured=result.structured_content, is_error=bool(result.is_error))

        return self._submit(call())

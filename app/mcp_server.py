"""Week 9: the claims desk's capabilities, exposed as an MCP server.

Until now every tool the claims agent could use was wired into
``app/agent.py`` by hand -- ``search_policy`` and ``check_assertions`` exist
only as branches inside one class, and no other application can use them. This
module publishes the same two capabilities (plus one exact-match lookup) over
the Model Context Protocol, so any MCP-speaking host -- this app's own agent,
another team's, Claude Code, Claude Desktop -- can discover and call them
without a line of integration code.

**Where the AI runs: not here.** This process has no language model. A tool
handler receives arguments, runs ordinary code (a vector search, four regexes,
a string scan) and returns data. It never sees the adjuster's notes unless a
caller chooses to put them in a query, never sees the agent's reasoning or
scratchpad, and cannot tell which model -- or whether any model -- is on the
other end; all it learns about its caller is the client name in the handshake.
The model lives in the *host* (``app/mcp_agent.py``), which decides which tool
to call and what to make of the answer. ``tests/test_mcp.py`` pins this: every
tool is exercised with every LLM backend patched to raise.

Everything exposed here is read-only on purpose. ``POST /ingest`` is **not** a
tool: Week 8 traced the prompt-injection attack to "anyone can add a document
to the corpus", and publishing ingestion to other people's agents would hand
them that capability directly.

Run it::

    python -m app.mcp_server                                   # stdio, for a host to spawn
    python -m app.mcp_server --transport http --port 8765      # needs MCP_AUTH_TOKENS
    python -m app.mcp_server --tools search_policy,check_assertions,lookup_exclusion_code
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from app.assertions import run_assertions
from app.injection_guard import quarantine
from app.mcp_access import AccessPolicy, BearerAuthMiddleware, ToolScopeMiddleware
from app.models import RetrievedChunk

logger = logging.getLogger(__name__)

__all__ = [
    "ALL_TOOLS",
    "DEFAULT_TOOLS",
    "Passage",
    "PassageResult",
    "PolicyCorpus",
    "build_server",
    "build_server_for_service",
]

#: Enabled unless the operator says otherwise: the two capabilities the Week 7 agent had.
DEFAULT_TOOLS: tuple[str, ...] = ("search_policy", "check_assertions")
#: Everything this module can serve. ``lookup_exclusion_code`` is the "second tool":
#: enabling it changes the *server's* configuration and nothing in any agent.
ALL_TOOLS: tuple[str, ...] = (*DEFAULT_TOOLS, "lookup_exclusion_code")

_READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
#: Enforced by the tool's own input schema, so a caller sees the constraint in `tools/list`.
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,31}$"
_MAX_LOOKUP_RESULTS = 10


class Passage(BaseModel):
    """One retrieved policy passage -- everything a caller needs to cite it."""

    chunk_id: str
    doc_id: str
    filename: str
    page: int
    page_end: int
    section_path: str
    clause_label: str | None = None
    score: float
    text: str


class Withheld(BaseModel):
    """A passage the server declined to return. Provenance and reason only, never its text."""

    chunk_id: str
    filename: str
    page: int
    reason: str


class PassageResult(BaseModel):
    passages: list[Passage]
    withheld: list[Withheld] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PolicyCorpus:
    """The only two things the server needs from the app. No LLM, no generator, no agent."""

    retrieve: Callable[[str, int | None], list[RetrievedChunk]]
    all_chunks: Callable[[], list[RetrievedChunk]]


def _to_result(chunks: Sequence[RetrievedChunk], *, guard: bool) -> PassageResult:
    """Shape retrieved chunks for the wire, withholding any that read as instructions.

    Defence in depth: a caller should not have to trust that *its* host
    screens tool output. Flagged passages are reported by provenance and
    reason only (the suspect text is not repeated back).
    """
    withheld: list[Withheld] = []
    kept = list(chunks)
    if guard:
        kept, flags = quarantine(chunks)
        withheld = [Withheld(chunk_id=f.chunk_id, filename=f.filename, page=f.page, reason=f.reason) for f in flags]
    passages = [
        Passage(
            chunk_id=c.chunk_id,
            doc_id=c.doc_id,
            filename=c.filename,
            page=c.page,
            page_end=c.page_end,
            section_path=c.section_path,
            clause_label=c.clause_label,
            score=round(c.score, 4),
            text=c.text,
        )
        for c in kept
    ]
    return PassageResult(passages=passages, withheld=withheld)


def _register_search_policy(server: MCPServer, corpus: PolicyCorpus, *, guard: bool) -> None:
    @server.tool(
        name="search_policy",
        title="Search policy wording",
        description=(
            "Search the policy and endorsement corpus for passages about a phrase or topic. "
            "Returns up to top_k passages, best match first, each with its filename, page, "
            "section path and full text so it can be cited. If the results look off-topic or "
            "do not settle the question, search again with a more specific query. Read-only."
        ),
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def search_policy(
        query: Annotated[str, Field(min_length=1, max_length=2000, description="What to look for.")],
        top_k: Annotated[int, Field(ge=1, le=20, description="How many passages to return.")] = 5,
    ) -> PassageResult:
        return _to_result(corpus.retrieve(query, top_k), guard=guard)


def _register_check_assertions(server: MCPServer) -> None:
    @server.tool(
        name="check_assertions",
        title="Check a draft claim summary",
        description=(
            "Run four deterministic checks on a draft claim summary: claim_number matches "
            "CLM-YYYY-NNNNN; date_of_loss is present and parses; excess_amount, if given, is a "
            "non-negative number; and a 'denied' coverage_decision names a cited_exclusion_id. "
            "Arguments are the draft's fields. Returns one `name=ok|FAIL (detail)` entry per "
            "check, separated by '; '. Read-only; changes nothing."
        ),
        annotations=_READ_ONLY,
        structured_output=False,
    )
    def check_assertions(
        claim_number: Annotated[str, Field(max_length=64)] = "UNKNOWN",
        date_of_loss: Annotated[str, Field(max_length=64)] = "",
        coverage_decision: Annotated[str, Field(max_length=32)] = "needs_review",
        cited_exclusion_id: Annotated[str | None, Field(max_length=64)] = None,
        excess_amount: float | None = None,
    ) -> str:
        results = run_assertions(
            {
                "claim_number": claim_number,
                "date_of_loss": date_of_loss,
                "coverage_decision": coverage_decision,
                "cited_exclusion_id": cited_exclusion_id,
                "excess_amount": excess_amount,
            }
        )
        return "; ".join(f"{r.name}={'ok' if r.passed else 'FAIL'} ({r.detail})" for r in results)


def _register_lookup_exclusion_code(server: MCPServer, corpus: PolicyCorpus, *, guard: bool) -> None:
    @server.tool(
        name="lookup_exclusion_code",
        title="Look up an exact identifier",
        description=(
            "Find every passage that mentions an exact identifier -- an exclusion code such as "
            "'E-17', a form number such as 'HO-0304', or an edition string. Exact, case-insensitive "
            "match on the whole identifier. Use it when a topical search keeps returning "
            "near-miss editions of the same clause. Same result shape as search_policy. Read-only."
        ),
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def lookup_exclusion_code(
        code: Annotated[
            str,
            Field(
                pattern=_IDENTIFIER_PATTERN,
                description="The identifier, e.g. E-17: 1-32 letters, digits, '.' or '-'.",
            ),
        ],
    ) -> PassageResult:
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(code)}(?![A-Za-z0-9])", re.IGNORECASE)
        hits = [chunk for chunk in corpus.all_chunks() if pattern.search(f"{chunk.section_path}\n{chunk.text}")]
        exact = [
            RetrievedChunk(
                chunk_id=c.chunk_id,
                doc_id=c.doc_id,
                filename=c.filename,
                text=c.text,
                page=c.page,
                page_end=c.page_end,
                section_path=c.section_path,
                clause_label=c.clause_label,
                score=1.0,
            )
            for c in hits[:_MAX_LOOKUP_RESULTS]
        ]
        return _to_result(exact, guard=guard)


def build_server(
    corpus: PolicyCorpus,
    *,
    enabled: Sequence[str] = DEFAULT_TOOLS,
    access: AccessPolicy | None = None,
    transport: str = "stdio",
    guard_outputs: bool = True,
) -> MCPServer:
    """Assemble the MCP server for ``corpus``.

    ``enabled`` is the operator's list of tools to publish -- the single place a
    new capability is switched on. ``access`` is required for the HTTP
    transport and ignored for stdio, where the launching process is the only
    possible caller.
    """
    unknown = [name for name in enabled if name not in ALL_TOOLS]
    if unknown:
        raise ValueError(f"unknown tool(s) {unknown}; this server can publish {list(ALL_TOOLS)}")

    server = MCPServer(
        "claims-desk-policy",
        title="Claims desk policy tools",
        instructions=(
            "Read-only tools over an insurance policy and endorsement corpus: topical search, "
            "an exact-identifier lookup, and deterministic checks on a draft claim summary. "
            "Nothing here calls a language model or modifies the corpus."
        ),
        version="1.0.0",
        middleware=[ToolScopeMiddleware(access, transport=transport)],
    )
    if "search_policy" in enabled:
        _register_search_policy(server, corpus, guard=guard_outputs)
    if "check_assertions" in enabled:
        _register_check_assertions(server)
    if "lookup_exclusion_code" in enabled:
        _register_lookup_exclusion_code(server, corpus, guard=guard_outputs)
    return server


def build_server_for_service(service: Any, **kwargs: Any) -> MCPServer:
    """Bind the server to a :class:`~app.rag.RagService` -- retrieval only; nothing else is passed in."""
    corpus = PolicyCorpus(retrieve=service.retrieve, all_chunks=service.store.all_chunks)
    return build_server(corpus, **kwargs)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--tools",
        default=os.environ.get("MCP_TOOLS", ",".join(DEFAULT_TOOLS)),
        help=f"comma-separated tools to publish (env MCP_TOOLS); available: {', '.join(ALL_TOOLS)}",
    )
    parser.add_argument(
        "--ingest",
        action="append",
        type=Path,
        default=[],
        metavar="PDF",
        help="ingest this PDF at startup (repeatable); operator-side only, never exposed as a tool",
    )
    parser.add_argument(
        "--no-output-guard",
        action="store_true",
        help="DEMONSTRATION ONLY: do not withhold injection-shaped passages (reproduces an unscreened third-party server)",
    )
    arguments = parser.parse_args(argv)

    # stdout belongs to the protocol on stdio: every log line goes to stderr.
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )

    from app.config import get_settings  # noqa: PLC0415 -- keep `import app.mcp_server` cheap
    from app.rag import RagService  # noqa: PLC0415

    # The server never calls a model, so it never holds model credentials: scrub any
    # key inherited from the environment and pin the (unused) LLM backend to the
    # offline stub, so no real client is even constructed in this process.
    settings = get_settings().model_copy(
        update={"llm_provider": "stub", "anthropic_api_key": None, "openai_api_key": None}
    )
    service = RagService(settings)
    for pdf in arguments.ingest:
        result = service.ingest(pdf.read_bytes(), pdf.name)
        logger.info("ingested %s: %d chunks", pdf.name, result.chunks)

    enabled = [name.strip() for name in arguments.tools.split(",") if name.strip()]

    if arguments.transport == "stdio":
        build_server_for_service(
            service, enabled=enabled, transport="stdio", guard_outputs=not arguments.no_output_guard
        ).run("stdio")
        return

    raw_tokens = os.environ.get("MCP_AUTH_TOKENS")
    if not raw_tokens:
        parser.error("--transport http requires MCP_AUTH_TOKENS (a JSON object of bearer token -> principal)")
    policy = AccessPolicy.from_json(raw_tokens)
    if not _is_loopback(arguments.host):
        logger.warning("binding %s: terminate TLS in front of this server; bearer tokens are sent in clear", arguments.host)

    import uvicorn  # noqa: PLC0415

    server = build_server_for_service(
        service, enabled=enabled, access=policy, transport="http", guard_outputs=not arguments.no_output_guard
    )
    app = BearerAuthMiddleware(server.streamable_http_app(host=arguments.host), policy)
    uvicorn.run(app, host=arguments.host, port=arguments.port, log_level="warning")


if __name__ == "__main__":
    main()

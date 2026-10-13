"""A stand-in for *someone else's agent* calling the claims desk's MCP server.

Week 9 asks that another person's agent be able to call the server you built.
This script plays that part, and it is written to be a fair test of it: it
imports **nothing from ``app/``** -- only the MCP SDK and the standard
library -- so everything it knows about the server comes from the protocol
(``tools/list``), exactly as it would for a partner team that has never seen
this repository.

It is a deliberately small "agent": it discovers the tools, picks the one
whose description says it searches, calls it with a question, and prints the
top passages with their citations. ``--probe`` additionally calls a tool by
name whether or not it was advertised, to show what a scoped token does.

    # a server it spawns itself (stdio)
    python scripts/w9_peer_agent.py --stdio -- python -m app.mcp_server --ingest data/sample_endorsement_pack.pdf

    # a shared server (HTTP + bearer token, which scopes what it may call)
    python scripts/w9_peer_agent.py --http http://127.0.0.1:8765/mcp --token "$PARTNER_TOKEN" --probe check_assertions

To use the same server from a real MCP host, no code is needed -- see
``docs/COMMANDS.md`` ("MCP server") for the Claude Code / Claude Desktop configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from mcp import Client, StdioServerParameters


def first_sentence(text: str) -> str:
    return text.strip().split(". ")[0].rstrip(".") + "."


async def run(client: Client, question: str, probe: str | None) -> int:
    server = client.server_info
    print(f"[peer] connected to {server.name if server else '(anonymous)'!r}, protocol {client.protocol_version}")

    listing = await client.list_tools()
    print(f"[peer] discovered {len(listing.tools)} tool(s):")
    for tool in listing.tools:
        print(f"[peer]   - {tool.name}: {first_sentence(tool.description or '')}")

    # My own selection logic: I know nothing about this server's tool names.
    searcher = next((t for t in listing.tools if "search" in (t.description or "").lower()), None)
    if searcher is None:
        print("[peer] no search-like tool offered to me; nothing to do")
        return 1
    required = (searcher.input_schema.get("required") or ["query"])[0]
    print(f"[peer] question: {question!r} -> calling {searcher.name}({required}=...)")
    result = await client.call_tool(searcher.name, {required: question, "top_k": 2})
    if result.is_error:
        print(f"[peer] tool error: {result.content[0].text[:200]}")
        return 1
    for passage in result.structured_content["passages"]:
        section = passage["section_path"][:60]
        snippet = " ".join(passage["text"].split())[:110]
        print(f"[peer]   cite: {passage['filename']} p.{passage['page']} | {section} | {snippet}...")

    if probe:
        print(f"[peer] probing a tool I was not necessarily told about: {probe}")
        try:
            outcome = await client.call_tool(probe, {})
            verdict = "refused" if outcome.is_error else "ALLOWED"
            print(f"[peer]   {probe} -> {verdict}: {outcome.content[0].text[:120]}")
        except Exception as exc:  # noqa: BLE001 -- a transport-level refusal is also an answer
            print(f"[peer]   {probe} -> refused ({type(exc).__name__}: {str(exc)[:100]})")
    return 0


async def main_async(arguments: argparse.Namespace) -> int:
    if arguments.http:
        import httpx2  # noqa: PLC0415 -- the SDK's own HTTP stack
        from mcp.client.streamable_http import streamable_http_client  # noqa: PLC0415

        headers = {"Authorization": f"Bearer {arguments.token}"} if arguments.token else {}
        http = httpx2.AsyncClient(headers=headers)
        transport = streamable_http_client(arguments.http, http_client=http)
    else:
        command, *args = arguments.server_command
        # The SDK hands a spawned server only a minimal, safe environment; anything else is opt-in.
        env = dict(item.split("=", 1) for item in arguments.env)
        transport = StdioServerParameters(command=command, args=args, env=env or None)
    async with Client(transport, cache=None) as client:
        return await run(client, arguments.question, arguments.probe)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--http", metavar="URL", help="streamable-HTTP endpoint of a running server")
    target.add_argument("--stdio", action="store_true", help="spawn the server command given after --")
    parser.add_argument("--token", help="bearer token for --http")
    parser.add_argument("--question", default="What is the flood sub-limit in the annual aggregate?")
    parser.add_argument("--probe", metavar="TOOL", help="also call TOOL by name, advertised or not")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="(with --stdio) pass this variable to the spawned server; repeatable")
    parser.add_argument("server_command", nargs="*", help="(with --stdio) the server command, after --")
    arguments = parser.parse_args()
    if arguments.stdio and not arguments.server_command:
        parser.error("--stdio needs the server command after --")
    return asyncio.run(main_async(arguments))


if __name__ == "__main__":
    sys.exit(main())

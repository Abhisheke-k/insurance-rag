"""Week 9: read the raw MCP messages once, so the protocol stops being a mystery.

Two traces, both of the real ``app/mcp_server.py`` running as a subprocess:

A. **By hand.** A ~30-line client written with the standard library only -- no
   MCP SDK -- performs the classic handshake and one tool call, printing every
   line it sends and receives. This is the protocol with nothing hidden: it is
   JSON-RPC 2.0, one message per line, over the server's stdin/stdout.

B. **Through the tap.** The real SDK client runs the real agent on one claim
   while ``scripts/w9_wire_tap.py`` records every line crossing between them.
   The point of B is the last table: the complete list of things the server
   was ever asked. The model, the system prompt, the adjuster's notes as such
   and the agent's reasoning are not among them -- they never leave the host.

    python -m scripts.w9_wire_trace            # writes coursework/w9/wire_trace.md
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from mcp import StdioServerParameters

from app.config import Settings
from app.mcp_agent import McpClaimsAgent
from app.mcp_client import McpToolClient
from scripts.make_sample_pdf import build_sample_pdf

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "coursework" / "w9" / "wire_trace.md"
SCENARIOS = ROOT / "coursework" / "w7" / "claim_scenarios.jsonl"
TAP = ROOT / "scripts" / "w9_wire_tap.py"


def _server_env(workdir: Path) -> dict[str, str]:
    return {
        "EMBEDDING_PROVIDER": "hashing",
        "FORCE_IN_MEMORY_STORE": "1",
        "CHROMA_DIR": str(workdir / "server-store"),
    }


def _clip(text: str, limit: int = 420) -> str:
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more chars]"


def _pretty(message: dict[str, Any], limit: int = 420) -> str:
    return _clip(json.dumps(message, indent=2), limit)


# --------------------------------------------------------------------------- #
# A. by hand, standard library only
# --------------------------------------------------------------------------- #
def trace_by_hand(pdf: Path, workdir: Path) -> list[str]:
    env = {**os.environ, **_server_env(workdir)}
    process = subprocess.Popen(
        [sys.executable, "-m", "app.mcp_server", "--ingest", str(pdf)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    assert process.stdin is not None and process.stdout is not None
    lines: list[str] = []

    def send(message: dict[str, Any], note: str) -> None:
        process.stdin.write(json.dumps(message) + "\n")  # type: ignore[union-attr]
        process.stdin.flush()  # type: ignore[union-attr]
        lines.extend([f"**client -> server** ({note})", "```json", _pretty(message), "```", ""])

    def receive(note: str, limit: int = 900) -> dict[str, Any]:
        message = json.loads(process.stdout.readline())  # type: ignore[union-attr]
        lines.extend([f"**server -> client** ({note})", "```json", _pretty(message, limit), "```", ""])
        return message

    send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "hand-rolled-client", "version": "0"},
            },
        },
        "handshake: who I am and what I support",
    )
    receive("handshake reply: what the server is and can do -- note `tools`, and no model anywhere")
    send({"jsonrpc": "2.0", "method": "notifications/initialized"}, "a notification: no `id`, so no reply")
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, "DISCOVERY: what can you do?")
    listing = receive("the catalogue: each tool's name, description and JSON-Schema for its arguments", limit=1500)
    first = listing["result"]["tools"][0]["name"]
    send(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": first, "arguments": {"query": "flood sub-limit annual aggregate", "top_k": 1}},
        },
        "USE: call a tool by name, arguments as the schema said",
    )
    receive("the result: `content` for a model to read, `structuredContent` for code to parse", limit=1100)
    send(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}},
        },
        "a mistake",
    )
    receive("failure is data, not a crash: `isError: true` and a message the model can read")
    process.stdin.close()
    process.wait(timeout=15)
    return lines


# --------------------------------------------------------------------------- #
# B. the SDK client + the real agent, recorded by the tap
# --------------------------------------------------------------------------- #
def trace_through_tap(pdf: Path, workdir: Path) -> list[str]:
    scenario = json.loads(SCENARIOS.read_text(encoding="utf-8").splitlines()[1])  # sequential_flood_e17
    log = workdir / "wire.jsonl"
    log.unlink(missing_ok=True)
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(TAP), sys.executable, "-m", "app.mcp_server", "--ingest", str(pdf)],
        env={**_server_env(workdir), "WIRE_TAP_LOG": str(log)},
        cwd=str(ROOT),
    )
    settings = Settings(embedding_provider="hashing", force_in_memory_store=True, chroma_dir=workdir / "host", llm_provider="stub")
    with McpToolClient(params, client_name="claims-desk-host") as client:
        agent = McpClaimsAgent(settings, tools=client)
        result = agent.process(scenario["adjuster_notes"])

    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows: list[str] = []
    methods_seen: Counter[str] = Counter()
    for index, record in enumerate(records, start=1):
        try:
            message = json.loads(record["line"])
        except ValueError:
            continue
        if record["dir"] == "->server" and "method" in message:
            methods_seen[message["method"]] += 1
            params_text = _clip(json.dumps(message.get("params", {})), 150)
            rows.append(f"| {index} | client -> server | `{message['method']}` | `{params_text}` |")
        elif record["dir"] == "<-server":
            body = message.get("result", message.get("error", {}))
            rows.append(f"| {index} | server -> client | (reply to id {message.get('id')}) | `{_clip(json.dumps(body), 150)}` |")

    lines = [
        f"The real agent processed `{scenario['id']}`; its own steps were "
        f"`{[s.action for s in result.steps]}`, decision `{result.summary.coverage_decision}`. "
        "Every line that crossed the tap, in order (clipped):",
        "",
        "| # | direction | method | content |",
        "|---|---|---|---|",
        *rows,
        "",
        "### Everything the server was ever asked",
        "",
        "| method | times | what it is |",
        "|---|---|---|",
    ]
    meanings = {
        "initialize": "handshake (legacy era)",
        "server/discover": "handshake (current era): who are you, what do you speak",
        "tools/list": "discovery: what tools do you have",
        "tools/call": "use one",
        "notifications/initialized": "handshake complete",
    }
    for method, count in methods_seen.items():
        lines.append(f"| `{method}` | {count} | {meanings.get(method, '')} |")
    tool_calls = methods_seen["tools/call"]
    lines += [
        "",
        f"The agent made {len(result.steps)} model calls (its steps); the server saw {tool_calls} tool "
        f"calls. The model's {len(result.steps)} decisions -- including which tool to call and the final "
        "`finalize` -- happened on the host and nothing about them crossed this wire. There is no "
        "`messages`, no `prompt`, no `system`, no `model` in any request: **the AI runs in the host**, and "
        "the server cannot tell whether a model, a script or a person is calling it. The one thing it "
        "learns about its caller is the `clientInfo.name` it chose to announce.",
        "",
        "What the server *does* see is whatever the host puts in tool arguments. Here the agent's search "
        "query is a slice of the adjuster's notes, so a fragment of the notes did cross the wire as a "
        "`query`. Treat tool arguments as data you are sharing with the tool's operator.",
    ]
    return lines


def main() -> None:
    pdf = ROOT / "data" / "sample_endorsement_pack.pdf"
    if not pdf.exists():
        build_sample_pdf(pdf)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        by_hand = trace_by_hand(pdf, workdir)
        tapped = trace_through_tap(pdf, workdir)

    document = [
        "# Week 9 -- the MCP wire, read once",
        "",
        "Generated by `python -m scripts.w9_wire_trace` from a real run of `app/mcp_server.py`. "
        "Nothing below is hand-written except this framing.",
        "",
        "## A. By hand: the protocol with nothing hidden",
        "",
        "A client written with the Python standard library only (`subprocess` + `json`), speaking to the server's "
        "stdin/stdout. MCP is **JSON-RPC 2.0, one message per line**: a request has an `id` and expects a reply "
        "with the same `id`; a notification has no `id`. Three methods carry almost everything: `initialize` "
        "(handshake), `tools/list` (discovery), `tools/call` (use).",
        "",
        *by_hand,
        "## B. The real agent, recorded in flight",
        "",
        "The SDK client (current protocol era, so the handshake is `server/discover` rather than `initialize`) "
        "runs `McpClaimsAgent` on one claim, through `scripts/w9_wire_tap.py`, a proxy that copies bytes "
        "unchanged and logs them.",
        "",
        *tapped,
        "",
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(document), encoding="utf-8")
    print(f"written to {OUT}")


if __name__ == "__main__":
    main()

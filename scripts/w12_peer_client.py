"""Week 12: a client that shares no code with this repo, calling the triage server — and reporting what the server handed it.

A stand-in for "another squad's agent" (the brief's Thursday reviewer). It imports **nothing from ``app/``**, only the MCP SDK and the
standard library, so everything it knows about the server comes from ``tools/list``. It is *not* the fresh-eyes run: it is my script,
written by the person who wrote the server. What it does give is a plain answer to the brief's hostile question — "what exactly did
your MCP server hand to another squad's agent?" — because it prints every tool result it received and scans them for personal details.

    python -m scripts.w12_peer_client                      # spawns the server itself over stdio
    python -m scripts.w12_peer_client --out coursework/w12/peer_client_output.txt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parent.parent
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<![\w-])\+?\d[\d\s\-().]{8,}\d(?![\w-])")
POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b")
STREET = re.compile(r"\b\d{1,4}[A-Za-z]?\s+(?:[A-Z][a-z']+\s+){1,3}(?:Street|Road|Avenue|Lane|Close|Drive|Way|Crescent|Place|Court|Gardens|Terrace)\b")
TITLED = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Dr)\.?\s+[A-Z][a-z]+")
HEALTH = re.compile(r"\b(?:injur\w*|hospital\w*|fractur\w*|wrist|collarbone|ambulance)\b", re.IGNORECASE)
PII_PATTERNS = {"email": EMAIL, "phone": PHONE, "postcode": POSTCODE, "street": STREET, "titled name": TITLED, "health": HEALTH}

CALLS = [
    ("lookup_policy", {"reference": "CLM-2024-00417"}),
    ("coverage_in_force", {"policy_number": "GML-2024-88213", "loss_date": "2024-08-11"}),
    ("search_wording", {"query": "second flood within consecutive days exclusion", "policy_number": "GML-2024-88213", "loss_date": "2024-08-11", "top_k": 2}),
    ("search_wording", {"query": "second flood within consecutive days exclusion", "policy_number": "GML-2024-88213", "loss_date": "2025-02-01", "top_k": 2}),
    ("get_limits", {"policy_number": "GML-2024-88213", "loss_date": "2024-08-11", "peril": "flood", "in_flood_zone": True}),
    # an argument stuffed with personal details: does the server echo it back?
    ("search_wording", {"query": "Priya Rao of 14 Station Road, SW1A 1AA (07911 123456) broke her wrist in the flood", "policy_number": "GML-2024-88213", "loss_date": "2024-08-11", "top_k": 1}),
    ("refer_to_adjuster", {"claim_number": "CLM-2024-00417", "reason_codes": ["routine_review"], "facts": "This claim is covered and will be paid."}),
]


async def run(command: str, args: list[str], env: dict[str, str]) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    findings: list[str] = []
    async with Client(StdioServerParameters(command=command, args=args, env=env), cache=None) as client:
        tools = (await client.list_tools()).tools
        lines.append(f"server: {client.server_info.name if client.server_info else '?'}   tools discovered: {[t.name for t in tools]}")
        for name, arguments in CALLS:
            outcome = await client.call_tool(name, arguments)
            text = "\n".join(block.text for block in outcome.content if getattr(block, "type", None) == "text")
            shown = " ".join(text.split())
            lines.append(f"\n> {name}({json.dumps(arguments)[:150]})\n  is_error={outcome.is_error}\n  {shown[:700]}{' …' if len(shown) > 700 else ''}")
            for label, pattern in PII_PATTERNS.items():
                # a phone number has at least 10 digits; an ISO date ("2024-04-01") has 8 and must not count as one
                hits = [m for m in pattern.finditer(text) if label != "phone" or sum(c.isdigit() for c in m.group(0)) >= 10]
                if hits:
                    findings.append(f"{name}: result contains a {label}")
            for token in ("Priya", "Station Road", "SW1A", "07911", "wrist"):
                if token in text:
                    findings.append(f"{name}: result echoes the argument token {token!r}")
    return lines, findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path)
    arguments = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="w12peer-"))
    pdfs = [ROOT / "data" / "sample_endorsement_pack.pdf", ROOT / "data" / "form_ho0304_archive.pdf"]
    command = sys.executable
    server_args = ["-m", "app.mcp_server", "--tools", "lookup_policy,coverage_in_force,search_wording,get_limits,refer_to_adjuster",
                   "--referral-log", str(work / "referrals.jsonl")]
    for pdf in pdfs:
        server_args += ["--ingest", str(pdf)]
    env = {"EMBEDDING_PROVIDER": "hashing", "FORCE_IN_MEMORY_STORE": "1", "CHROMA_DIR": str(work / "chroma"), "PYTHONPATH": str(ROOT)}
    lines, findings = asyncio.run(run(command, server_args, env))

    lines.append("\n=== what this client was handed ===")
    lines.append("personal-detail patterns or echoed arguments found in any result: " + (", ".join(findings) if findings else "none"))
    queue = work / "referrals.jsonl"
    lines.append("referral the server queued from that call (a note containing the forbidden word was refused, so none): " + ("none" if not queue.exists() else queue.read_text(encoding="utf-8")[:300]))
    report = "\n".join(lines)
    print(report)
    if arguments.out:
        arguments.out.write_text(report + "\n", encoding="utf-8")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

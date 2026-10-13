"""Week 9: decide whether a discovered tool is fit to put in front of the model.

Discovery makes it easy to plug in someone else's server -- which is exactly
why it needs a gate. A tool's name, description and parameter descriptions are
*text the model reads and obeys*, written by whoever runs the server. That is
the same untrusted-text-reaches-the-prompt problem Week 8 found in retrieved
passages, one hop earlier: a "tool poisoning" description can instruct the
model to skip checks, hide its behaviour from the user, or send data to a URL,
before a single call is made. And because definitions can change between
connections, a description reviewed on Monday can say something else on
Tuesday (a "rug pull").

:func:`vet_tools` applies, to every discovered definition and with every
reason recorded rather than just the first:

* a valid, non-reserved name (a tool called ``finalize`` would shadow the
  agent's own terminal action);
* an operator **allowlist** -- for a server you do not own, which tools may be
  used at all is the operator's decision, not the server's;
* a **read-only** declaration. Annotations are the server's own claim and prove
  nothing against a hostile one; this check only keeps honest-but-sloppy tools
  out and documents intent;
* a description that exists and is a sane size (a 40 KB description is both a
  prompt-stuffing vector and a per-turn token bill);
* the **injection scan** (:mod:`app.injection_guard`, plus tool-specific
  patterns) over the description and every string in the parameter schemas;
* **pinning**: a definition whose hash differs from the one reviewed is
  rejected until a human re-approves it.

None of this makes a malicious server safe. It narrows what an *honest* host
exposes to the model and catches the cheap, common attacks; the durable
controls remain least privilege (read-only tools, a narrow allowlist) and not
connecting to servers you have no reason to trust.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.injection_guard import scan_text
from app.mcp_client import ToolDefinition

__all__ = [
    "ToolPins",
    "VetPolicy",
    "Verdict",
    "scan_tool_output",
    "scan_tool_text",
    "vet_tools",
]

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,63}$")

#: Patterns specific to poisoned *tool definitions* (on top of app.injection_guard's).
_TOOL_POISON_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "asks the model to conceal something from the user",
        re.compile(r"\bdo not (?:tell|inform|mention|reveal|show|alert)\b[^.]{0,60}\b(?:user|human|operator|adjuster)\b", re.IGNORECASE),
    ),
    (
        "gives instructions about how to use other tools",
        re.compile(r"\b(?:before|instead of|after|whenever)\b[^.]{0,30}\b(?:using|calling|invoking)\b[^.]{0,20}\b(?:other|any other|every|all)\b[^.]{0,10}\btools?\b", re.IGNORECASE),
    ),
    (
        "tells the model to send data to a URL",
        re.compile(r"\b(?:send|post|upload|forward|transmit|exfiltrate)\b[^.]{0,80}\bhttps?://", re.IGNORECASE),
    ),
    (
        "refers to credential or secret files",
        re.compile(r"(?:~|/)\.(?:ssh|aws|env|netrc)\b|\bid_rsa\b|/etc/passwd|\bcredentials?\.json\b", re.IGNORECASE),
    ),
    (
        "wraps instructions in a hidden-looking tag",
        re.compile(r"<\s*(?:important|system|instructions?|secret|hidden)\s*>", re.IGNORECASE),
    ),
)


def scan_tool_text(text: str) -> list[str]:
    """Why ``text`` (a description, a schema string) reads as instructions to the model, or ``[]``."""
    reasons = scan_text(text)
    reasons.extend(reason for reason, pattern in _TOOL_POISON_PATTERNS if pattern.search(text))
    return reasons


def scan_tool_output(text: str) -> list[str]:
    """The same scan applied to what a tool *returns* -- indirect injection via a result."""
    return scan_tool_text(text)


def _strings(node: Any) -> Iterator[str]:
    """Every key and string value in a JSON-schema-like structure."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from _strings(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _strings(item)


class ToolPins:
    """name -> fingerprint of the definition a human approved; optionally backed by a JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._pins: dict[str, str] = {}
        if path is not None and path.exists():
            self._pins = dict(json.loads(path.read_text(encoding="utf-8")))

    def status(self, tool: ToolDefinition) -> str:
        """``"new"`` (never pinned), ``"match"`` or ``"changed"``."""
        pinned = self._pins.get(tool.name)
        if pinned is None:
            return "new"
        return "match" if pinned == tool.fingerprint() else "changed"

    def pin(self, tool: ToolDefinition) -> None:
        self._pins[tool.name] = tool.fingerprint()

    def save(self) -> None:
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._pins, indent=2, sort_keys=True), encoding="utf-8")


@dataclass(frozen=True, slots=True)
class VetPolicy:
    """What the operator is willing to put in front of the model."""

    #: Tools that may be used at all. ``None`` = no allowlist (acceptable for a server you run yourself).
    allowed: frozenset[str] | None = None
    require_read_only: bool = True
    max_description_chars: int = 2000
    #: Names the agent itself uses; a server may not claim them.
    reserved_names: frozenset[str] = frozenset({"finalize", "report", "delegate"})
    pins: ToolPins | None = None
    #: Trust-on-first-use: pin tools never seen before instead of rejecting them. Use once, then turn off.
    learn_pins: bool = False

    @classmethod
    def third_party(cls, allowed: Iterable[str], pins: ToolPins | None = None) -> VetPolicy:
        """The strict posture for a server you do not run."""
        return cls(allowed=frozenset(allowed), pins=pins)


@dataclass(frozen=True, slots=True)
class Verdict:
    tool: str
    accepted: bool
    reasons: list[str] = field(default_factory=list)


def _check(tool: ToolDefinition, policy: VetPolicy) -> list[str]:
    reasons: list[str] = []
    if not _NAME_RE.match(tool.name):
        reasons.append(f"invalid tool name {tool.name!r}")
    if tool.name in policy.reserved_names:
        reasons.append(f"name {tool.name!r} is reserved by the agent")
    if policy.allowed is not None and tool.name not in policy.allowed:
        reasons.append("not on the operator's allowlist")
    if policy.require_read_only and tool.read_only is not True:
        reasons.append("does not declare itself read-only")
    if tool.destructive is True:
        reasons.append("declares itself destructive")
    if not tool.description.strip():
        reasons.append("has no description, so the model cannot use it safely")
    if len(tool.description) > policy.max_description_chars:
        reasons.append(f"description is {len(tool.description)} chars (limit {policy.max_description_chars})")
    if tool.input_schema.get("type") != "object":
        reasons.append("input schema is not an object")

    text_sources = [tool.name, tool.title or "", tool.description, *_strings(tool.input_schema)]
    for text in text_sources:
        for reason in scan_tool_text(text):
            note = f"definition text {reason}"
            if note not in reasons:
                reasons.append(note)

    if policy.pins is not None:
        status = policy.pins.status(tool)
        if status == "changed":
            reasons.append("definition changed since it was pinned (possible rug pull)")
        elif status == "new" and not policy.learn_pins:
            reasons.append("not pinned: its definition has never been reviewed")
    return reasons


def vet_tools(tools: Iterable[ToolDefinition], policy: VetPolicy) -> tuple[list[ToolDefinition], list[Verdict]]:
    """Split ``tools`` into those safe to offer the model and a verdict for every one examined."""
    accepted: list[ToolDefinition] = []
    verdicts: list[Verdict] = []
    seen: set[str] = set()
    for tool in tools:
        reasons = _check(tool, policy)
        if tool.name in seen:
            reasons.append("duplicate tool name in the listing")
        seen.add(tool.name)
        verdicts.append(Verdict(tool.name, not reasons, reasons))
        if not reasons:
            accepted.append(tool)
            if policy.pins is not None and policy.learn_pins and policy.pins.status(tool) == "new":
                policy.pins.pin(tool)
    if policy.pins is not None and policy.learn_pins:
        policy.pins.save()
    return accepted, verdicts

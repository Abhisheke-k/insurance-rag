# Week 9 — MCP: the standard way agents reach tools & data

The Week 7/8 claims agent had its two tools wired in by hand, and no other
application could use them. This week the same capabilities moved behind the
Model Context Protocol: a server that publishes them, and a host-side agent
that **discovers** them.

| Piece | File |
|---|---|
| Our MCP server (`search_policy`, `check_assertions`, `lookup_exclusion_code`) | [`app/mcp_server.py`](../../app/mcp_server.py) |
| Who may call what (bearer tokens, per-tool scopes, audit log) | [`app/mcp_access.py`](../../app/mcp_access.py) |
| Blocking MCP client for the (synchronous) agent loop | [`app/mcp_client.py`](../../app/mcp_client.py) |
| Vetting a tool you didn't write (allowlist, injection scan, pinning) | [`app/mcp_vetting.py`](../../app/mcp_vetting.py) |
| The agent with discovered tools | [`app/mcp_agent.py`](../../app/mcp_agent.py) |
| Evidence generators | `scripts/w9_mcp_demo.py`, `scripts/w9_wire_trace.py` |
| A stand-in for someone else's agent / a hostile server | `scripts/w9_peer_agent.py`, `scripts/w9_untrusted_server.py` |
| Tests | [`tests/test_mcp.py`](../../tests/test_mcp.py) (49) |

```bash
python -m scripts.w9_mcp_demo      # -> evidence.md, evidence_raw.json   (18 s, no API key)
python -m scripts.w9_wire_trace    # -> wire_trace.md
```

Everything below that is a number comes from [`evidence.md`](evidence.md) or the
tests. The model in every run is the offline deterministic stub (as in Weeks
4–8: no API key exists in this checkout), except where a section says a
*scripted* model was used to force a specific choice.

---

## The four Friday-check questions

### 1. Does the agent use a tool through MCP, discovered rather than hard-coded?

**Yes.** `McpClaimsAgent` calls `tools/list` at the start of every run and
builds three things from the answer — nothing about tools is typed into it:

1. the `action` enum of its per-turn JSON schema (`build_step_schema`),
2. the tool section of its system prompt (descriptions and input schemas, verbatim),
3. dispatch: a tool call goes to the server by name, with arguments validated
   against the *discovered* schema first (a malformed call is reported to the
   model, never sent).

[`evidence.md` §1](evidence.md) shows the catalog it found and all five Week 7
claims run through it. `tests/test_mcp.py::test_action_enum_and_prompt_are_built_from_the_server_listing`
pins it.

What is still the host's decision, deliberately, because it is policy rather
than wiring: `required_tools` (the Week 7 rule "search and check before you
finalize", now a constructor argument that fails loudly if the server doesn't
offer them), the vetting policy, and two result conventions — a structured
result with a `passages` list is citable evidence and is **numbered by the
host** (a server can never forge a citation number), and a tool whose
parameters are named after claim-summary fields has them filled from the
running draft. Any other tool is observed as plain text.

### 2. Can they add a second tool without changing the agent's code?

**Yes — demonstrated three ways.**

- [`evidence.md` §3](evidence.md): two real server subprocesses, one with
  `--tools search_policy,check_assertions`, one adding `lookup_exclusion_code`.
  The agent discovers 2 tools, then 3. The SHA-256 of `mcp_agent.py`,
  `mcp_client.py` and `mcp_vetting.py` is identical before and after.
- A scripted model calls `lookup_exclusion_code("E-17")`; the observation shows
  the E-17 clause text, the host numbers it `[1]`, and the final summary cites it.
- `test_a_tool_the_agent_has_never_heard_of_is_still_offered_and_callable`
  defines a `count_words` tool *inside the test*, so nothing in `app/` knows it
  exists, and the agent offers and calls it.

**What this does not show.** The offline stub is a fixed-policy simulator: it
was offered the new tool and never chose it (its run: search → check →
finalize). Whether a *real* model takes up a new tool from its description is a
property of that model and is unmeasured here.

### 3. Did they build their own server that another person's agent could call?

**Yes, with an honest caveat about "another person".** `app/mcp_server.py`
speaks stdio (for a host that spawns it) and streamable HTTP (for a shared
server). `scripts/w9_peer_agent.py` plays the other team's agent and is built
to be a fair test: it **imports nothing from `app/`** — only the MCP SDK — so
everything it knows about the server comes from `tools/list`.
[`evidence.md` §4](evidence.md) shows it working over stdio and over HTTP.

Access control, in the same section:

| Caller | Result |
|---|---|
| No token / a made-up token | HTTP **401**, before MCP sees the request |
| `partner-a` (scoped to `search_policy`) | is *advertised* one tool; `check_assertions` by name → `Unknown tool: check_assertions` |
| `claims-desk` (all tools) | sees all three; the same probe is **allowed** |

A scoped caller's refusal is byte-identical to the answer for a tool that
doesn't exist (`test_http_server_requires_a_token_and_scopes_each_caller`), so a
partner can't probe its way to the names of tools outside its scope. Every call
is audit-logged by principal, tool, outcome and duration — never arguments.

**The caveat:** the "other person" is a script I wrote, so this proves the
server is self-describing and that scoping works, not that someone else's real
agent has been through it. Connecting a real MCP host needs no code — see
[`docs/COMMANDS.md`](../../docs/COMMANDS.md#mcp-server-week-9) for the Claude
Code / Claude Desktop configuration — but I have not run that and do not claim to.

### 4. Can they explain, in plain words, where the AI runs and where it doesn't?

**The model runs in the host — the process that holds the agent loop,
`LLMProvider`, and the API key. The MCP server is a tool shelf: it receives
`tools/call` with arguments, runs ordinary code (a vector search, four regexes,
a string scan) and hands back data. It never sees the model, the system prompt
or the agent's reasoning, and it can't tell whether a model, a script or a
person is calling.**

Evidence rather than assertion:

- [`wire_trace.md`](wire_trace.md): the raw JSON-RPC, read two ways (a
  30-line stdlib-only client, and the real agent through a logging proxy). For
  a whole claim the server was asked exactly: one handshake, one `tools/list`,
  two `tools/call`. No `prompt`, `system`, `messages` or `model` field appears
  anywhere. The three model decisions that claim took happened entirely on the host.
- `test_every_tool_works_with_every_llm_backend_forbidden`: patches
  `complete()` on the stub, Anthropic and OpenAI providers to raise, then calls
  every tool. Nothing on the server side needs a model.
- The server CLI overrides its own settings to the offline stub backend with
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` cleared, so (by reading
  `build_llm_provider`) no real LLM client is constructed in that process even
  if the parent's environment holds a key. A source-level test pins the override
  and `test_agent_over_a_real_stdio_subprocess_server` runs end to end with a
  dummy key in the child's environment — but no test inspects the live server's
  objects, so treat "no client constructed" as code-reading, not measurement.

One precision, because "the server sees nothing" would be an overclaim: the
server sees **whatever the host puts in tool arguments**. This agent's search
query is a slice of the adjuster's notes, so a fragment of the notes does cross
the wire. Treat tool arguments as data you are sharing with the tool's operator.

---

## Does MCP make the agent smarter? No — and here is what it costs

The syllabus says to be honest that MCP is plumbing. Measured
([`evidence.md` §2](evidence.md)):

- **Same answers.** 33/33 inputs (5 Week 7 scenarios + 28 Week 6 notes) give an
  identical result from the in-process agent and the MCP agent: decision, claim
  number, date, exclusion id, excess, citations (by chunk id) and action sequence.
- **Time.** Over stdio the hop adds ≈2.5 ms to `search_policy` (0.24 → 2.76 ms),
  ≈1.2 ms to `check_assertions`, and ≈1.3 ms for the per-run `tools/list`. A whole
  claim with the stub model goes from 6.1 ms to 17.2 ms (2.8×) — against a model
  call measured in seconds, noise; it is the price of the process boundary.
- **Tokens (a cost the syllabus doesn't mention).** Discovery puts every tool's
  description and JSON Schema in the prompt of *every turn*. The MCP agent's
  system prompt is ≈1,400 estimated tokens against ≈606 for Week 7's hand-written
  one, and each further tool adds ≈280 per turn, for ever. Auto-generated schemas
  are verbose; trimming them is an obvious lever that I did **not** apply, so the
  Week 10 numbers include this overhead for both contestants.

## What building it taught me (things that went wrong)

1. **My own safety cap changed a decision.** The first parity run was 32/33. The
   one divergence traced to a `MAX_OBSERVATION_CHARS = 6000` cap I had put on tool
   results: a 5-passage search result is ≈9.4 k characters, so the cap silently cut
   the later passages, the stub weighed a different clause, and the cited exclusion
   changed. A host-side guard that alters tool output is not neutral. The fix bounds
   passage results by *count* (drop whole passages, say so in the observation) and
   keeps a character cap for free text only. My original parity test used only the
   five short scenarios and missed it; it now runs all 33 inputs and compares the
   first observation the model sees, and two regression tests pin the behaviour.
2. **Denial leaked existence.** A scope check that raised a protocol error while
   a missing tool returned a tool-level error let a caller tell "not yours" from
   "not there". A test caught it; denial now returns exactly the SDK's own
   unknown-tool result.
3. **The SDK gives a spawned server a minimal environment**, not yours. My demo
   peer hung until it was told to pass `EMBEDDING_PROVIDER` and friends explicitly.
   That is the right default (the child doesn't inherit your keys) but surprising.
4. **This is MCP SDK 2.x.** `FastMCP` is now `MCPServer`, the handshake is
   `server/discover` rather than `initialize` by default, and most examples online
   are v1. `requirements.txt` pins `mcp>=2.3,<3`.
5. **Port 8765 was already taken on this machine** by something I didn't
   investigate (the server's default port; use `--port`). The tests and demos bind
   OS-assigned free ports.

## Keeping it safe

**Built and tested:**

- *Access control.* HTTP requires a bearer token (compared in constant time,
  held as SHA-256 digests, ≥16 chars); each token maps to a principal with a
  tool allowlist; `tools/list` shows only what you may call; fail-closed if no
  principal resolves; the server refuses to start on HTTP without tokens, warns
  on a non-loopback bind, and **does not publish `ingest`** (Week 8 traced its
  attack to "anyone can add a document").
- *Vetting someone else's tool* ([`evidence.md` §5](evidence.md)) against a
  deliberately hostile server. The table there maps each behaviour to the control
  that catches it: hidden instructions in a description (injection scan), a tool
  named `finalize` (reserved names), a destructive tool (read-only requirement), a
  clean definition with a hostile **output** (output scan — definition checks
  cannot see it), a definition edited after review (**pinning**: a rug pull), and
  anything unapproved (operator allowlist).
- *Week 8's attack, replayed through MCP* ([`evidence.md` §6](evidence.md)):
  `sequential_flood_e17` flips to *covered* when the poisoned PDF is served by an
  unscreened server to an unguarded host, and is *denied* (correct) if either end
  screens. Putting the corpus behind a protocol didn't move the trust boundary.

**Not built, and what would be needed:** TLS (terminate it in front; tokens
travel in clear on plain HTTP); token expiry/rotation and per-token rate limits;
OAuth (the SDK supports it; static tokens were enough here and an OAuth issuer
would be theatre); handling `notifications/tools/list_changed` (the agent simply
re-lists per run); sandboxing the server process. Annotations such as
`readOnlyHint` are a server's *claim*, not a guarantee — vetting narrows what an
honest host exposes and catches cheap attacks, but cannot make a hostile server
safe. The durable control is not connecting to servers you have no reason to trust.

## Limits of this work, stated plainly

- **No real model.** Everything ran on the deterministic stub. Decisions,
  parity and the wire trace are properties of the plumbing and would hold for a
  real model; model uptake of new tools, and whether a real model resists a
  poisoned tool description the vetting layer didn't catch, are untested.
- **The "someone else's agent" is my script**, as above.
- **The FastAPI app doesn't use the MCP agent.** `POST /agent/process-claim`
  still runs the Week 7 in-process agent; `McpClaimsAgent` is used as a library by
  the scripts and tests. Wiring it to an endpoint is a config change I chose not to
  make unasked.
- The vetting patterns are a heuristic list; rewording defeats them.

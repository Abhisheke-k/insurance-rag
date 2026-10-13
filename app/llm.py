"""Generic LLM provider abstraction.

Mirrors the pattern in :mod:`app.embeddings`: one interface (`LLMProvider`),
several interchangeable backends, resolved by ``LLM_PROVIDER``. Nothing above
this module imports ``anthropic`` or ``openai`` directly, and nothing above it
knows the stub backend exists -- every caller (answer generation, claim
summarisation, the eval judge) is written against ``LLMProvider.complete()``.

Why this exists as a separate module rather than the Claude-specific code that
used to live in ``app/generation.py``: Week 5's trace replay and Week 6's
judge need to run the exact same call shape against whichever backend is
configured, including a deterministic, network-free stub when no API key is
present -- which is the normal state of this checkout (see
``coursework/README.md``). Provider-specific request/response handling is
confined to the three ``_Provider`` classes below; everything else is written
once, against the interface.

The stub is a real (if simple) reading-comprehension simulator, not a fixed
string. It parses the numbered passages out of the prompt, scores them against
the question by lexical overlap, and answers from whichever passage that
scoring -- and therefore retrieval's ranking -- handed it. It also injects a
small, deterministic (seeded by a hash of the prompt) rate of realistic
failure: reading the right passage and drawing the wrong conclusion, or
refusing despite good context. That is what gives Weeks 4/5/6 real failures to
find instead of a suite that trivially passes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.config import Settings
from app.tokenizer import estimate_tokens

logger = logging.getLogger(__name__)

__all__ = [
    "AnthropicProvider",
    "LLMProvider",
    "LLMResponse",
    "LLMUnavailableError",
    "LLMUsage",
    "OpenAIProvider",
    "StubProvider",
    "build_llm_provider",
]


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """Tokens one call consumed -- what Week 10's cost comparison is built from.

    ``input_tokens`` is the *uncached* input only (Anthropic's own accounting);
    cache reads/writes are separate so a cost model can price them differently.
    ``estimated`` is True when the backend reported nothing and the count comes
    from ``app.tokenizer.estimate_tokens`` over the real prompt and reply text
    (the stub, or a provider response without a usage block): the *prompt* is
    exactly what a real model would be sent, but the count is the same
    uncalibrated ~4-chars-per-token heuristic the chunker uses.
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated: bool = False


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Normalised shape every backend returns, whatever its native SDK gives back."""

    text: str
    model: str
    stop_reason: str  # "end" | "max_tokens" | "refusal"
    refusal_category: str | None = None
    #: None when the backend gave no usage block (see :class:`LLMUsage`); callers
    #: that need a number regardless should go through ``app.metering``.
    usage: LLMUsage | None = None


class LLMUnavailableError(RuntimeError):
    """Raised when the configured backend cannot be reached or is not configured."""


class LLMProvider(ABC):
    """Common interface every backend implements."""

    #: Stable identifier surfaced by /health and recorded on traces.
    name: str

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    def status(self) -> dict[str, Any]: ...

    @abstractmethod
    def complete(
        self,
        *,
        system: str,
        user_message: str,
        json_schema: dict[str, Any],
        max_tokens: int,
        effort: str = "medium",
    ) -> LLMResponse:
        """One structured-output call. Must return JSON matching ``json_schema`` in ``.text``."""


# --------------------------------------------------------------------------- #
# Anthropic
# --------------------------------------------------------------------------- #
class AnthropicProvider(LLMProvider):
    """Claude via the ``anthropic`` SDK, structured output with a prompted-JSON fallback."""

    def __init__(self, *, api_key: str | None, model: str) -> None:
        self.name = f"anthropic:{model}"
        self._model = model
        self._client: Any | None = None
        self._init_error: str | None = None
        self._supports_structured_output = True

        try:
            import anthropic  # noqa: PLC0415 -- optional at import time

            self._anthropic = anthropic
            self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        except ImportError as exc:
            self._anthropic = None
            self._init_error = f"the 'anthropic' package is not installed ({exc})"
        except Exception as exc:  # pragma: no cover - depends on SDK version
            self._init_error = f"could not construct the Anthropic client ({exc})"

        if self._client is not None and not self._has_credentials():
            self._init_error = (
                "no Anthropic credentials found. Set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN, "
                "or run `ant auth login`) to enable this backend."
            )

    def _has_credentials(self) -> bool:
        return any(
            getattr(self._client, attribute, None) for attribute in ("api_key", "auth_token", "credentials")
        )

    def is_configured(self) -> bool:
        return self._client is not None and self._init_error is None

    def status(self) -> dict[str, Any]:
        return {"provider": "anthropic", "model": self._model, "configured": self.is_configured(), "error": self._init_error}

    def complete(
        self,
        *,
        system: str,
        user_message: str,
        json_schema: dict[str, Any],
        max_tokens: int,
        effort: str = "medium",
    ) -> LLMResponse:
        if not self.is_configured():
            raise LLMUnavailableError(self._init_error or "Anthropic is not configured")
        response = self._call(system, user_message, json_schema, max_tokens, effort)
        usage = self._usage(response)

        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            return LLMResponse(
                text="", model=self._model, stop_reason="refusal", refusal_category=category, usage=usage
            )

        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        stop_reason = "max_tokens" if getattr(response, "stop_reason", None) == "max_tokens" else "end"
        return LLMResponse(
            text=text, model=getattr(response, "model", self._model), stop_reason=stop_reason, usage=usage
        )

    @staticmethod
    def _usage(response: Any) -> LLMUsage | None:
        raw = getattr(response, "usage", None)
        if raw is None:
            return None
        return LLMUsage(
            input_tokens=int(getattr(raw, "input_tokens", 0) or 0),
            output_tokens=int(getattr(raw, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(raw, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(raw, "cache_creation_input_tokens", 0) or 0),
        )

    def _call(
        self, system: str, user_message: str, json_schema: dict[str, Any], max_tokens: int, effort: str
    ) -> Any:
        output_config: dict[str, Any] = {"effort": effort}
        if self._supports_structured_output:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}

        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "output_config": output_config,
            "messages": [{"role": "user", "content": user_message}],
        }
        if not self._supports_structured_output:
            request["messages"][0]["content"] += (
                "\n\nReply with a single JSON object matching this schema and nothing else: "
                + json.dumps(json_schema)
            )

        try:
            return self._client.messages.create(**request)
        except self._anthropic.BadRequestError as exc:
            if self._supports_structured_output and "output_config" in str(exc):
                logger.warning("structured outputs rejected (%s); retrying with prompted JSON", exc)
                self._supports_structured_output = False
                return self._call(system, user_message, json_schema, max_tokens, effort)
            raise LLMUnavailableError(f"Claude rejected the request: {exc}") from exc
        except self._anthropic.AuthenticationError as exc:
            raise LLMUnavailableError(f"Anthropic authentication failed: {exc}") from exc
        except self._anthropic.RateLimitError as exc:
            raise LLMUnavailableError(f"Anthropic rate limit reached: {exc}") from exc
        except self._anthropic.APIConnectionError as exc:
            raise LLMUnavailableError(f"could not reach the Anthropic API: {exc}") from exc
        except self._anthropic.APIStatusError as exc:
            raise LLMUnavailableError(f"Anthropic API error {exc.status_code}: {exc}") from exc
        except TypeError as exc:
            raise LLMUnavailableError(f"Anthropic client is not usable: {exc}") from exc


# --------------------------------------------------------------------------- #
# OpenAI
# --------------------------------------------------------------------------- #
class OpenAIProvider(LLMProvider):
    """Any OpenAI-compatible chat-completions endpoint, structured output via json_schema."""

    def __init__(self, *, api_key: str | None, model: str) -> None:
        self.name = f"openai:{model}"
        self._model = model
        self._client: Any | None = None
        self._init_error: str | None = None
        self._supports_structured_output = True

        if not api_key:
            self._init_error = "no OpenAI credentials found. Set OPENAI_API_KEY to enable this backend."
            return

        try:
            import openai  # noqa: PLC0415

            self._openai = openai
            self._client = openai.OpenAI(api_key=api_key)
        except ImportError as exc:
            self._openai = None
            self._init_error = f"the 'openai' package is not installed ({exc})"

    def is_configured(self) -> bool:
        return self._client is not None and self._init_error is None

    def status(self) -> dict[str, Any]:
        return {"provider": "openai", "model": self._model, "configured": self.is_configured(), "error": self._init_error}

    def complete(
        self,
        *,
        system: str,
        user_message: str,
        json_schema: dict[str, Any],
        max_tokens: int,
        effort: str = "medium",
    ) -> LLMResponse:
        if not self.is_configured():
            raise LLMUnavailableError(self._init_error or "OpenAI is not configured")
        response = self._call(system, user_message, json_schema, max_tokens)
        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", "stop")
        stop_reason = {"length": "max_tokens", "content_filter": "refusal"}.get(finish_reason, "end")
        text = choice.message.content or ""
        raw_usage = getattr(response, "usage", None)
        usage = (
            LLMUsage(
                input_tokens=int(getattr(raw_usage, "prompt_tokens", 0) or 0),
                output_tokens=int(getattr(raw_usage, "completion_tokens", 0) or 0),
            )
            if raw_usage is not None
            else None
        )
        return LLMResponse(
            text=text,
            model=getattr(response, "model", self._model),
            stop_reason=stop_reason,
            refusal_category="content_filter" if stop_reason == "refusal" else None,
            usage=usage,
        )

    def _call(self, system: str, user_message: str, json_schema: dict[str, Any], max_tokens: int) -> Any:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user_message}]
        kwargs: dict[str, Any] = {"model": self._model, "messages": messages, "max_completion_tokens": max_tokens}
        if self._supports_structured_output:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": json_schema, "strict": True},
            }
        else:
            messages[0]["content"] += "\n\nReply with a single JSON object matching this schema and nothing else: " + json.dumps(
                json_schema
            )
            kwargs["response_format"] = {"type": "json_object"}

        try:
            return self._client.chat.completions.create(**kwargs)
        except self._openai.BadRequestError as exc:
            if self._supports_structured_output:
                logger.warning("structured outputs rejected (%s); retrying with json_object mode", exc)
                self._supports_structured_output = False
                return self._call(system, user_message, json_schema, max_tokens)
            raise LLMUnavailableError(f"OpenAI rejected the request: {exc}") from exc
        except self._openai.AuthenticationError as exc:
            raise LLMUnavailableError(f"OpenAI authentication failed: {exc}") from exc
        except self._openai.RateLimitError as exc:
            raise LLMUnavailableError(f"OpenAI rate limit reached: {exc}") from exc
        except self._openai.APIConnectionError as exc:
            raise LLMUnavailableError(f"could not reach the OpenAI API: {exc}") from exc
        except self._openai.APIStatusError as exc:
            raise LLMUnavailableError(f"OpenAI API error {exc.status_code}: {exc}") from exc


# --------------------------------------------------------------------------- #
# Deterministic stub -- no network, no key, seeded so replay is exact
# --------------------------------------------------------------------------- #
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-.]*")
_STOPWORDS = frozenset(
    "a about after again against all also an and any are as at be because been before being "
    "below between both but by can cannot could did do does doing down during each few for "
    "from further had has have having he her here hers him his how however i if in into is it "
    "its itself just me more most much must my no nor not now of off on once only or other our "
    "out over own please same shall she should so some such than that the their them then there "
    "these they this those through to too under until up very was we were what when where "
    "which while who why will with within would you your".split()
)
#: Identifier-shaped tokens (exclusion codes, form numbers, edition strings, claim/policy/
#: endorsement numbers) that a real dense embedder blurs but exact string matching should not.
_TOKEN_RE = re.compile(
    r"\bE-\d+\b|\b[A-Z]{2,4}-\d{4}-\d{3,6}\b|\bHO-\d{4}\b|\bed\.?\s?\d{2}-\d{2}\b|\bEND-\d{4}-\d+\b",
    re.IGNORECASE,
)
_NEGATION_CUES = ("does not cover", "excludes", "shall not be liable", "not liable", "no indemnity", "not cover")
_PASSAGE_RE = re.compile(r"^\[(\d+)\]\s*(.*?)\n(.*?)(?=\n\[\d+\]|\Z)", re.DOTALL | re.MULTILINE)


def _seed(text: str) -> int:
    """Stable 0-99 bucket from a hash of ``text``, so behaviour is deterministic and replayable."""
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big") % 100


def _content_tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 2}


def _parse_passages(block: str) -> list[tuple[int, str, str]]:
    """Parse the ``[n] header\\nbody`` passages out of a context block."""
    return [(int(num), header.strip(), body.strip()) for num, header, body in _PASSAGE_RE.findall(block)]


#: Bounds each search_policy step's <observation>...</observation> body exactly,
#: so a later check_assertions/finalize step's text (also in the scratchpad the
#: agent turn persona re-parses each call) can never bleed into a passage body.
_SEARCH_STEP_RE = re.compile(
    r'<step number="\d+" action="search_policy">.*?<observation>\n(.*?)\n</observation>', re.DOTALL
)


def _search_observations(scratchpad: str) -> str:
    """Concatenate just the search_policy observations out of an agent scratchpad."""
    return "\n\n".join(_SEARCH_STEP_RE.findall(scratchpad))


_CHUNK_ID_IN_HEADER_RE = re.compile(r"chunk_id:\s*(\S+)")


def _passages_by_chunk_id(block: str) -> dict[str, str]:
    """Map chunk_id -> body text, keyed the way ``ClaimSummary.citations`` actually
    stores citations (chunk_id strings, not passage numbers). ``build_context_block``
    prints ``chunk_id: <id>`` in every passage header, so it's always recoverable."""
    result: dict[str, str] = {}
    for _num, header, body in _parse_passages(block):
        match = _CHUNK_ID_IN_HEADER_RE.search(header)
        if match:
            result[match.group(1)] = body
    return result


def _best_sentence(body: str, wanted: set[str]) -> str:
    sentences = re.split(r"(?<=[.;])\s+", body)
    if not sentences:
        return body[:280]
    scored = sorted(sentences, key=lambda s: len(_content_tokens(s) & wanted), reverse=True)
    return scored[0].strip()


@dataclass(frozen=True, slots=True)
class _ScoredPassage:
    number: int
    header: str
    body: str
    score: float
    exact_hit: bool


def _score_passages(passages: Sequence[tuple[int, str, str]], question: str) -> list[_ScoredPassage]:
    wanted = _content_tokens(question)
    exact_tokens = {t.lower() for t in _TOKEN_RE.findall(question)}
    scored: list[_ScoredPassage] = []
    for number, header, body in passages:
        haystack = f"{header}\n{body}"
        overlap = len(wanted & _content_tokens(haystack))
        exact_hit = any(tok in haystack.lower() for tok in exact_tokens) if exact_tokens else False
        score = float(overlap) + (2.0 if exact_hit else 0.0)
        scored.append(_ScoredPassage(number, header, body, score, exact_hit))
    # Stable sort: ties keep retrieval's own rank order (lower passage number = higher-ranked),
    # which is what makes the stub faithfully inherit retrieval's mistakes rather than fixing them.
    return sorted(scored, key=lambda p: p.score, reverse=True)


def _infer_claim_fields(notes: str, scored: Sequence[_ScoredPassage]) -> dict[str, Any]:
    """Read a claim number/date/decision/exclusion/excess/summary off ``notes``
    and the best-matching passage. Shared by Persona 2 (claim summary) and
    Persona 3 (agent turn) -- the agent's version just runs this again each
    turn against whatever passages its own searches have surfaced so far."""
    claim_number_match = re.search(r"\bCLM-\d{4}-\d{5}\b", notes)
    claim_number = claim_number_match.group(0) if claim_number_match else "UNKNOWN"

    date_match = re.search(
        r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|"
        r"November|December)\s+\d{4}|\d{4}-\d{2}-\d{2})\b",
        notes,
    )
    date_of_loss = date_match.group(0) if date_match else ""

    top = scored[0] if scored and scored[0].score > 0 else None
    excess_amount: float | None = None
    cited_exclusion_id: str | None = None
    citations: list[int] = []
    coverage_decision = "needs_review"
    summary_bits: list[str] = []

    if top is not None:
        citations.append(top.number)
        excess_pattern = r"deductible of GBP\s?([\d,]+)|excess of GBP\s?([\d,]+)"
        excess_search = re.search(excess_pattern, top.body, re.IGNORECASE)
        if excess_search:
            raw = next(g for g in excess_search.groups() if g)
            excess_amount = float(raw.replace(",", ""))

        has_negation = any(cue in top.body.lower() for cue in _NEGATION_CUES)
        clause_id_match = re.search(r"\(([a-h])\)|(\d\.\d(?:\.\d)?)", top.header + " " + top.body)
        best = _best_sentence(top.body, _content_tokens(notes))

        if has_negation:
            coverage_decision = "denied"
            if clause_id_match:
                cited_exclusion_id = clause_id_match.group(1) or clause_id_match.group(2)
            summary_bits.append(f"Notes describe an excluded scenario: {best}")
        else:
            coverage_decision = "covered"
            summary_bits.append(f"Notes match an insured peril: {best}")
    else:
        summary_bits.append("No policy passage in context matches the notes' circumstances.")

    summary = " ".join(summary_bits) or "Unable to determine coverage from the notes and context."
    return {
        "claim_number": claim_number,
        "date_of_loss": date_of_loss,
        "coverage_decision": coverage_decision,
        "cited_exclusion_id": cited_exclusion_id,
        "excess_amount": excess_amount,
        "summary": summary,
        "citations": citations,
    }


class StubProvider(LLMProvider):
    """Deterministic reading-comprehension simulator. No network call, ever.

    Dispatches on the requested JSON schema's property names, since every
    call site in this app already declares one -- ``found``/``citations`` is
    the Q&A answer shape, ``coverage_decision`` is the claim-summary shape,
    ``pass_criterion`` is the judge shape. Behaviour for each is grounded in
    the actual passages supplied in ``user_message``; nothing is templated
    independent of the prompt.
    """

    name = "stub:reading-comprehension-v1"

    def is_configured(self) -> bool:
        return True

    def status(self) -> dict[str, Any]:
        return {"provider": "stub", "model": self.name, "configured": True, "error": None}

    def complete(
        self,
        *,
        system: str,
        user_message: str,
        json_schema: dict[str, Any],
        max_tokens: int,
        effort: str = "medium",
    ) -> LLMResponse:
        properties = set((json_schema or {}).get("properties", {}).keys())
        if "pass_criterion" in properties:
            payload = self._judge(system, user_message)
        elif "delegations" in properties and "thought" in properties:
            payload = self._manager_turn(user_message)
        elif "tool_input" in properties and "thought" in properties:
            payload = self._tool_agent_turn(system, user_message, json_schema)
        elif "action" in properties and "thought" in properties:
            payload = self._agent_turn(user_message)
        elif "coverage_decision" in properties:
            payload = self._summarize_claim(user_message)
        elif "found" in properties and "citations" in properties:
            payload = self._answer_question(user_message)
        else:  # pragma: no cover - defensive; every call site declares a known shape
            raise LLMUnavailableError(f"stub provider has no persona for schema {sorted(properties)!r}")
        text = json.dumps(payload)
        # Estimated, not measured: the prompt is exactly what a real model would be
        # sent, but the count is the offline heuristic (see LLMUsage).
        usage = LLMUsage(
            input_tokens=estimate_tokens(system) + estimate_tokens(user_message),
            output_tokens=estimate_tokens(text),
            estimated=True,
        )
        return LLMResponse(text=text, model=self.name, stop_reason="end", usage=usage)

    # ------------------------------------------------------------------ #
    # Persona 1: Q&A answer (matches app.generation.ANSWER_SCHEMA)
    # ------------------------------------------------------------------ #
    def _answer_question(self, user_message: str) -> dict[str, Any]:
        context_match = re.search(r"<context>\n(.*?)\n</context>", user_message, re.DOTALL)
        question_match = re.search(r"Question:\s*(.*)\s*\Z", user_message, re.DOTALL)
        question = question_match.group(1).strip() if question_match else ""
        passages = _parse_passages(context_match.group(1)) if context_match else []
        scored = _score_passages(passages, question)

        if not scored or scored[0].score <= 0:
            return {"found": False, "answer": "I couldn't find this in the documents.", "citations": []}

        bucket = _seed(question)
        top = scored[0]
        # Second citation only when a runner-up is genuinely close (multi-passage grounding).
        runner_up = scored[1] if len(scored) > 1 and scored[1].score >= top.score * 0.7 and scored[1].score > 0 else None

        # Deterministic G-failure #1: right passage, wrong conclusion. Only fires on
        # coverage/exclusion-shaped questions where the top passage actually carries a
        # negation cue to invert -- otherwise there is nothing to misread.
        has_negation = any(cue in top.body.lower() for cue in _NEGATION_CUES)
        is_coverage_question = any(
            kw in question.lower() for kw in ("cover", "exclu", "deny", "denied", "apply", "applies", "liable")
        )
        if is_coverage_question and has_negation and bucket < 18:
            answer = (
                f"Yes, this is covered. {_best_sentence(top.body, _content_tokens(question))}"
                " (read against the passage's stated scope, not its exclusion clause)"
            )
            return {"found": True, "answer": answer, "citations": [top.number]}

        # Deterministic G-failure #2: over-cautious refusal despite adequate context.
        if 18 <= bucket < 24 and top.score < 4:
            return {
                "found": False,
                "answer": "I couldn't find this in the documents.",
                "citations": [],
            }

        sentence = _best_sentence(top.body, _content_tokens(question))
        citations = [top.number] + ([runner_up.number] if runner_up else [])
        return {"found": True, "answer": sentence, "citations": citations}

    # ------------------------------------------------------------------ #
    # Persona 2: claim summary (matches app.claims.CLAIM_SUMMARY_SCHEMA)
    # ------------------------------------------------------------------ #
    def _summarize_claim(self, user_message: str) -> dict[str, Any]:
        notes_match = re.search(r"<adjuster_notes>\n(.*?)\n</adjuster_notes>", user_message, re.DOTALL)
        context_match = re.search(r"<policy_context>\n(.*?)\n</policy_context>", user_message, re.DOTALL)
        notes = notes_match.group(1).strip() if notes_match else user_message
        passages = _parse_passages(context_match.group(1)) if context_match else []

        scored = _score_passages(passages, notes)
        bucket = _seed(notes)
        fields = _infer_claim_fields(notes, scored)
        top = scored[0] if scored and scored[0].score > 0 else None

        # Deterministic G-failure: flips the coverage read on notes that DO carry a
        # negation cue in the matched clause -- same "right context, wrong conclusion"
        # pattern as the Q&A stub, tuned to a different, independent hash band.
        if top is not None:
            has_negation = any(cue in top.body.lower() for cue in _NEGATION_CUES)
            if has_negation and 40 <= bucket < 55:
                best = _best_sentence(top.body, _content_tokens(notes))
                fields["coverage_decision"] = "covered"
                fields["cited_exclusion_id"] = None
                fields["summary"] = f"Notes match an insured peril: {best}"

        return fields

    # ------------------------------------------------------------------ #
    # Persona 3: agent turn (matches app.agent.AGENT_STEP_SCHEMA)
    #
    # Stateless like every stub call, so "what step am I on" is read back out
    # of the scratchpad the loop re-sends each turn rather than tracked here:
    # search once, draft + check assertions once, then finalize. No injected
    # failure modes -- the interesting difference to observe against Persona 2
    # is the agent's architecture (re-search, self-check), not a second copy
    # of the same seeded imperfection.
    # ------------------------------------------------------------------ #
    def _agent_turn(self, user_message: str) -> dict[str, Any]:
        notes_pattern = r"<adjuster_notes>\n(.*?)\n</adjuster_notes>"
        notes_match = re.search(notes_pattern, user_message, re.DOTALL)
        scratchpad_match = re.search(r"<scratchpad>\n(.*?)\n</scratchpad>", user_message, re.DOTALL)
        notes = notes_match.group(1).strip() if notes_match else user_message
        scratchpad = scratchpad_match.group(1) if scratchpad_match else ""

        blank = {
            "draft_claim_number": None,
            "draft_date_of_loss": None,
            "draft_coverage_decision": None,
            "draft_cited_exclusion_id": None,
            "draft_excess_amount": None,
            "draft_summary": None,
            "draft_citations": [],
        }

        if 'action="search_policy"' not in scratchpad:
            return {
                "thought": "search the policy corpus for the circumstances in the notes",
                "action": "search_policy",
                "search_query": notes[:200],
                **blank,
            }

        passages = _parse_passages(_search_observations(scratchpad))
        scored = _score_passages(passages, notes)
        fields = _infer_claim_fields(notes, scored)
        draft = {
            "draft_claim_number": fields["claim_number"],
            "draft_date_of_loss": fields["date_of_loss"],
            "draft_coverage_decision": fields["coverage_decision"],
            "draft_cited_exclusion_id": fields["cited_exclusion_id"],
            "draft_excess_amount": fields["excess_amount"],
            "draft_summary": fields["summary"],
            "draft_citations": fields["citations"],
        }

        if 'action="check_assertions"' not in scratchpad:
            return {
                "thought": "check the draft against the deterministic assertions before finalizing",
                "action": "check_assertions",
                "search_query": None,
                **draft,
            }

        return {
            "thought": "assertions checked; finalize",
            "action": "finalize",
            "search_query": None,
            **draft,
        }

    # ------------------------------------------------------------------ #
    # Persona 3b: agent turn over DISCOVERED tools (matches
    # app.mcp_agent.build_step_schema).
    #
    # Same simulated reasoning as Persona 3, but it works out which tool is
    # which by reading the <tools> catalog in the system prompt -- the way a
    # real model would -- rather than being handed names. A search tool is "a
    # tool whose description says search and which takes a required string"; a
    # check tool is "a tool that takes claim_number". A tool it does not
    # recognise as either is simply never called: the stub is a fixed-policy
    # simulator, not a general reasoner, so it will NOT make use of a brand-new
    # kind of tool (tests drive that with a scripted model instead). Role limits
    # what it reports: an analyst leaves the identifying fields to the clerk.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _is_search_tool(tool: dict[str, Any]) -> str | None:
        """The name of the required string parameter if ``tool`` looks like a topical search, else None."""
        schema = tool.get("input_schema") or {}
        properties = schema.get("properties") or {}
        label = f"{tool.get('name', '')} {tool.get('description', '')}".lower()
        if "search" not in label or "lookup" in str(tool.get("name", "")).lower():
            return None
        for name in schema.get("required") or []:
            if (properties.get(name) or {}).get("type") == "string":
                return str(name)
        return None

    def _tool_agent_turn(self, system: str, user_message: str, json_schema: dict[str, Any]) -> dict[str, Any]:
        tools_match = re.search(r"<tools>\n(.*?)\n</tools>", system, re.DOTALL)
        catalog: list[dict[str, Any]] = json.loads(tools_match.group(1)) if tools_match else []
        role_match = re.search(r"^ROLE:\s*(\S+)", system, re.MULTILINE)
        role = role_match.group(1) if role_match else "claims_agent"
        terminal = json_schema["properties"]["action"]["enum"][-1]

        notes_match = re.search(r"<adjuster_notes>\n(.*?)\n</adjuster_notes>", user_message, re.DOTALL)
        scratchpad_match = re.search(r"<scratchpad>\n(.*?)\n</scratchpad>", user_message, re.DOTALL)
        notes = notes_match.group(1).strip() if notes_match else user_message
        scratchpad = scratchpad_match.group(1) if scratchpad_match else ""

        steps = re.findall(r'<step number="\d+" action="([^"]*)">.*?<observation>\n(.*?)\n</observation>', scratchpad, re.DOTALL)
        failed_prefixes = ("rejected:", "tool error:", "unrecognized action")
        succeeded = {action for action, observation in steps if not observation.startswith(failed_prefixes)}

        if role == "claims_triage":
            return self._triage_turn(notes, steps, terminal)

        search_tool = next((t for t in catalog if self._is_search_tool(t)), None)
        check_tool = next((t for t in catalog if "claim_number" in ((t.get("input_schema") or {}).get("properties") or {})), None)

        blank = {
            "draft_claim_number": None,
            "draft_date_of_loss": None,
            "draft_coverage_decision": None,
            "draft_cited_exclusion_id": None,
            "draft_excess_amount": None,
            "draft_summary": None,
            "draft_citations": [],
        }

        def turn(thought: str, action: str, tool_input: dict[str, Any], draft: dict[str, Any] | None = None) -> dict[str, Any]:
            return {"thought": thought, "action": action, "tool_input": json.dumps(tool_input), **(draft or blank)}

        if search_tool and role in ("claims_agent", "coverage_analyst") and search_tool["name"] not in succeeded:
            parameter = self._is_search_tool(search_tool)
            return turn(
                "search the policy corpus for the circumstances in the notes",
                search_tool["name"],
                {str(parameter): notes[:200]},
            )

        evidence = "\n\n".join(
            observation
            for action, observation in steps
            if action in succeeded and action != terminal and not (check_tool and action == check_tool["name"])
        )
        scored = _score_passages(_parse_passages(evidence), notes)
        fields = _infer_claim_fields(notes, scored)
        draft = {
            "draft_claim_number": fields["claim_number"],
            "draft_date_of_loss": fields["date_of_loss"],
            "draft_coverage_decision": fields["coverage_decision"],
            "draft_cited_exclusion_id": fields["cited_exclusion_id"],
            "draft_excess_amount": fields["excess_amount"],
            "draft_summary": fields["summary"],
            "draft_citations": fields["citations"],
        }
        if role == "coverage_analyst":
            draft["draft_claim_number"] = draft["draft_date_of_loss"] = None
        elif role == "claims_clerk":
            draft = {**blank, "draft_claim_number": fields["claim_number"], "draft_date_of_loss": fields["date_of_loss"]}

        if check_tool and role in ("claims_agent", "claims_clerk") and check_tool["name"] not in succeeded:
            return turn("check the draft against the deterministic assertions before finishing", check_tool["name"], {}, draft)
        return turn("required tools used; hand back the result", terminal, {}, draft)


    # ------------------------------------------------------------------ #
    # Persona 3d: claims triage (role "claims_triage", Week 12).
    #
    # Works the way a careful but fallible junior would, over the five triage tools: look the policy up, check it was in
    # force, search the wording, get the three figures, refer. It never sets a coverage decision -- there is no field for
    # one -- and it is deliberately NAIVE in four places, each a plausible real failure that the integrated eval has to find
    # (rates are artefacts of how many such FNOLs the cases contain, not measurements of any real model):
    #   1. dates: it takes the FIRST date in the text as the loss date, so "Reported 3 March 2025 ... loss on 11 August 2024"
    #      is resolved against the wrong day;
    #   2. trajectory: it skips coverage_in_force when the FNOL says the policy "is active";
    #   3. wording choice: among the passages it is shown it relies on the best-matching one, whichever edition that is;
    #   4. tone: asked to "confirm it is covered" it writes the word, in its referral note and in its closing note.
    # It also puts the start of the narrative into its wording query, which is how personal details reach a tool argument
    # when nothing upstream removes them.
    # ------------------------------------------------------------------ #
    _TRIAGE_MONTHS = (
        "January|February|March|April|May|June|July|August|September|October|November|December"
    )
    _TRIAGE_DATE_RE = re.compile(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + _TRIAGE_MONTHS + r")\s+(\d{4})\b|\b(\d{4})-(\d{2})-(\d{2})\b"
    )

    @classmethod
    def _first_date(cls, text: str) -> str | None:
        match = cls._TRIAGE_DATE_RE.search(text)
        if not match:
            return None
        if match.group(4):
            return f"{match.group(4)}-{match.group(5)}-{match.group(6)}"
        month = cls._TRIAGE_MONTHS.split("|").index(match.group(2)) + 1
        return f"{int(match.group(3)):04d}-{month:02d}-{int(match.group(1)):02d}"

    @staticmethod
    def _json_or_none(text: str) -> dict[str, Any] | None:
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _triage_turn(self, notes: str, steps: list[tuple[str, str]], terminal: str) -> dict[str, Any]:
        failed = ("rejected:", "tool error:", "unrecognized action")
        attempted = [action for action, _ in steps]
        ok = {action: obs for action, obs in steps if not obs.startswith(failed)}
        lowered = notes.lower()

        claim = (re.search(r"\bCLM-\d{4}-\d{5}\b", notes) or [None])[0]
        policy_in_text = (re.search(r"\b(?!CLM-)[A-Z]{3}-\d{4}-\d{5}\b", notes) or [None])[0]
        reference = claim or policy_in_text
        loss_date = self._first_date(notes)
        peril = "flood" if "flood" in lowered else "other"
        in_zone: bool | None = None
        if re.search(r"(?:not|outside)\s+(?:in\s+|within\s+|of\s+)?(?:a\s+|the\s+)?flood zone|zone 1\b", lowered):
            in_zone = False
        elif re.search(r"flood zone|zone [23]\b", lowered):
            in_zone = True
        asserts_active = bool(re.search(r"policy (?:is|remains|was) (?:still )?(?:active|live|valid|current|in force)", lowered))
        wants_confirmation = bool(re.search(r"confirm\w*[^.]{0,40}cover|tell (?:me|us|them|the claimant)[^.]{0,40}cover", lowered))

        blank = {
            "draft_claim_number": None, "draft_date_of_loss": None, "draft_coverage_decision": None,
            "draft_cited_exclusion_id": None, "draft_excess_amount": None, "draft_summary": None, "draft_citations": [],
        }

        def call(thought: str, action: str, arguments: dict[str, Any], draft: dict[str, Any] | None = None) -> dict[str, Any]:
            return {"thought": thought, "action": action, "tool_input": json.dumps(arguments), **(draft or blank)}

        lookup = self._json_or_none(ok.get("lookup_policy", ""))
        in_force = self._json_or_none(ok.get("coverage_in_force", ""))
        limits = self._json_or_none(ok.get("get_limits", ""))
        wording_obs = ok.get("search_wording", "")
        policy_number = (lookup or {}).get("policy_number")

        # --- the wording the persona relies on: the best match among whatever it was shown -------------
        passages = _parse_passages(wording_obs) if wording_obs else []
        chosen = None
        if passages:
            scored = _score_passages(passages, notes)
            with_window = [p for p in scored if re.search(r"within\s+\d+\s+consecutive\s+days", p.body)]
            chosen = (with_window or scored)[0]
        window = None
        edition_label = None
        if chosen is not None:
            found_window = re.search(r"within\s+(\d+)\s+consecutive\s+days", chosen.body)
            window = int(found_window.group(1)) if found_window else None
            tagged = re.search(r"\| wording: ([^|]+?)(?:;|$|\n)", chosen.header)
            in_text = re.search(r"edition\s+(\d{2}-\d{2})", chosen.body)
            edition_label = tagged.group(1).strip() if tagged else (f"edition {in_text.group(1)} (as named in the text)" if in_text else None)
        gap = None
        gap_match = re.search(r"(\d+)\s+days?\s+(?:after|since|following)\s+(?:the\s+|an\s+)?(?:earlier|previous|first|prior)", lowered)
        if gap_match:
            gap = int(gap_match.group(1))

        def referral(codes: list[str], facts: str, refs: list[str] | None = None, priority: str = "routine") -> dict[str, Any]:
            arguments: dict[str, Any] = {"claim_number": claim, "reason_codes": codes, "priority": priority, "facts": facts}
            if policy_number:
                arguments["policy_number"] = policy_number
            if loss_date:
                arguments["loss_date"] = loss_date
            if refs:
                arguments["wording_refs"] = refs
            return call("refer the claim to an adjuster with the facts gathered", "refer_to_adjuster", arguments)

        referred = "refer_to_adjuster" in ok
        last_refer_failed = any(a == "refer_to_adjuster" and obs.startswith(failed) for a, obs in steps) and not referred
        note = "Surfaced the wording and figures for the adjuster."
        if wants_confirmation and not last_refer_failed:
            note += " The loss is covered."  # slip 4: this is what the compliance guard exists to catch

        def closing_draft() -> dict[str, Any]:
            cited = [chosen.number] if chosen is not None else []
            parts = [f"Policy {policy_number or 'unknown'}"] if policy_number else []
            if edition_label:
                parts.append(f"wording relied on: {edition_label}")
            if window is not None and gap is not None:
                parts.append(f"earlier flood stated {gap} days before; E-17 window in that wording is {window} days")
            text = "; ".join(parts) + ". " if parts else ""
            return {
                **blank,
                "draft_claim_number": claim,
                "draft_date_of_loss": loss_date,
                "draft_cited_exclusion_id": "E-17" if chosen is not None and "E-17" in chosen.body else None,
                "draft_summary": text + note,
                "draft_citations": cited,
            }

        if referred:
            return call("referral queued; hand back the result", terminal, {}, closing_draft())

        if "lookup_policy" not in attempted and reference:
            return call("find the policy this claim sits on", "lookup_policy", {"reference": reference})
        if lookup is None or lookup.get("found") is False:
            return referral(["policy_not_found"], "The claim or policy reference did not resolve to a policy in the registry.")
        if loss_date is None:
            return referral(["insufficient_information"], "The notice gives no date of loss that could be read.")
        if any(obs.startswith("rejected:") and "date_ambiguous" in obs for _, obs in steps):
            # told by the host that the notice has no single loss date: stop searching and refer, as the brief says to
            return referral(["date_ambiguous"], "The notice gives several dates and the date of loss could not be determined.")

        if "coverage_in_force" not in attempted and not asserts_active:  # slip 2
            return call("check the policy was in force on the date of loss", "coverage_in_force", {"policy_number": policy_number, "loss_date": loss_date})
        if in_force is not None and in_force.get("in_force") is False:
            return referral(["not_in_force"], f"The policy record shows it was not in force on {loss_date}: {in_force.get('detail')}.", priority="routine")
        if in_force is not None and in_force.get("in_force") is None:
            return referral(["policy_not_found"], "The in-force check could not be completed for this policy.")

        if not lookup.get("wording_on_file"):
            return referral(["wording_not_on_file"], "No policy wording is on file for this policy, so none was searched.")

        if "search_wording" not in attempted:
            query = f"{notes[:160]} flood exclusion sequential water intrusion E-17 consecutive days" if peril == "flood" else notes[:200]
            return call(
                "retrieve the wording in force on the date of loss", "search_wording",
                {"query": query, "policy_number": policy_number, "loss_date": loss_date, "top_k": 4},
            )
        if "get_limits" not in attempted:
            arguments = {"policy_number": policy_number, "loss_date": loss_date, "peril": peril}
            if in_zone is not None:
                arguments["in_flood_zone"] = in_zone
            return call("get the deductible, sub-limit and policy limit as three separate figures", "get_limits", arguments)

        codes: list[str] = []
        if window is not None and gap is not None and gap <= window:
            codes.append("exclusion_arguable")
        if limits and limits.get("unresolved"):
            codes.append("limits_unresolved")
        codes = codes or ["routine_review"]
        facts = f"Date of loss {loss_date}."
        if edition_label:
            facts += f" Wording relied on: {edition_label}."
        if window is not None and gap is not None:
            facts += f" Earlier flood stated {gap} days before; the window in that wording is {window} days."
        if wants_confirmation and not last_refer_failed:
            facts += " This loss is covered."  # slip 4, first attempt: the server refuses it and the persona retries clean
        return referral(codes, facts, [edition_label] if edition_label else None)

    # ------------------------------------------------------------------ #
    # Persona 3c: team manager (matches app.team.MANAGER_SCHEMA). Delegates to
    # both specialists in its first turn, then merges their reports.
    # ------------------------------------------------------------------ #
    def _manager_turn(self, user_message: str) -> dict[str, Any]:
        reports: dict[str, dict[str, Any]] = {}
        for specialist, body in re.findall(r'<report specialist="(\w+)">\n(.*?)\n</report>', user_message, re.DOTALL):
            try:
                reports[specialist] = json.loads(body)
            except ValueError:
                continue

        blank = {
            "draft_claim_number": None,
            "draft_date_of_loss": None,
            "draft_coverage_decision": None,
            "draft_cited_exclusion_id": None,
            "draft_excess_amount": None,
            "draft_summary": None,
            "draft_citations": [],
        }
        if not reports:
            return {
                "thought": "two independent jobs: policy research and claim-data validation; hand each to its specialist",
                "action": "delegate",
                "delegations": [
                    {"specialist": "analyst", "task": "Find the policy wording that governs this claim and decide coverage, citing passages."},
                    {"specialist": "clerk", "task": "Extract and validate the claim number and date of loss from the notes."},
                ],
                **blank,
            }

        analyst = (reports.get("analyst") or {}).get("fields") or {}
        clerk = (reports.get("clerk") or {}).get("fields") or {}
        return {
            "thought": "both reports are in; merge them into the final summary",
            "action": "finalize",
            "delegations": [],
            "draft_claim_number": clerk.get("claim_number") or analyst.get("claim_number"),
            "draft_date_of_loss": clerk.get("date_of_loss") or analyst.get("date_of_loss"),
            "draft_coverage_decision": analyst.get("coverage_decision"),
            "draft_cited_exclusion_id": analyst.get("cited_exclusion_id"),
            "draft_excess_amount": analyst.get("excess_amount"),
            "draft_summary": analyst.get("summary"),
            "draft_citations": list(analyst.get("citations") or []),
        }

    # ------------------------------------------------------------------ #
    # Persona 4: judge (matches app.judge.JUDGE_SCHEMA)
    # ------------------------------------------------------------------ #
    #: v2's prompt (coursework/w6/judge_v2.txt) adds this instruction, in these
    #: words, after the two disagreement examples. Its presence is how the stub
    #: "learns" the lesson those examples teach -- a real model would pick it up
    #: from the instruction and the few-shot pair together; the stub has no
    #: weights to update, so it keys off the sentence a v2 prompt actually
    #: contains. This is a simulation detail specific to the stub, not a
    #: contract the real Anthropic/OpenAI providers need to know about.
    _TOPICAL_CHECK_MARKER = "same clause topic"

    #: Topic vocabularies used only by the (optional) topical-relevance check
    #: below -- a cheap stand-in for "is the cited passage even about the same
    #: kind of claim as the notes", which a pure negation-cue check cannot see.
    #: Separate word lists for the passage side (policy jargon) and the notes
    #: side (how a claim in that category actually gets described) -- a first
    #: pass used one shared list and produced false positives whenever the
    #: notes described the situation without repeating the clause's own
    #: wording (e.g. "production interruption" never says "waiting period").
    _TOPIC_MARKERS: dict[str, dict[str, tuple[str, ...]]] = {
        "cyber": {
            "passage": ("cyber", "computer system", "ransom", "malicious code", "hacking", "cryptocurrency"),
            "notes": ("cyber", "ransomware", "hack", "malicious", "computer", "cloud", "encrypt", "data"),
        },
        "flood_sublimit": {
            "passage": ("sub-limit", "annual aggregate"),
            "notes": ("flood", "sub-limit", "aggregate"),
        },
        "business_interruption": {
            "passage": ("waiting period", "indemnity period"),
            "notes": ("interruption", "business interruption", "lost revenue", "lost sales", "outage"),
        },
    }

    #: A clause that DENIES coverage.
    _PRIMARY_EXCLUSION_CUES = ("does not cover", "excludes", "shall not be liable", "not liable", "no indemnity", "not cover")
    #: A clause that switches a primary exclusion back OFF (a write-back / carve-out)
    #: -- also negatively worded, but the opposite implication for coverage. v1
    #: does not know this distinction exists; that is its main blind spot.
    _CARVEOUT_CUES = ("does not apply", "shall not apply")
    #: This corpus enumerates exclusion items as "(a) ... (h)"; a rationale that
    #: quotes one verbatim is quoting an exclusion even with no cue word in view.
    #: Not anchored to the start: the stub's own rationale text is templated as
    #: "Notes match an insured peril: (f) any extortion demand ...", so the
    #: lettered item is a few words in, not at position zero.
    _LETTERED_ITEM_RE = re.compile(r"\(([a-h])\)\s+\S")

    def _judge(self, system: str, user_message: str) -> dict[str, Any]:
        if "<triage_card>" in user_message:
            return self._judge_fit(system, user_message)
        # v1: checks only that the summary's coverage_decision agrees with whether
        # the CITED PASSAGE carries a negation cue -- it never independently
        # re-derives the answer from the notes, cannot tell a correctly-negated
        # WRONG clause from a correctly-negated RIGHT one, and treats any
        # negative-sounding clause as a denial even when it is actually a
        # carve-out restoring cover. Those blind spots are what Week 6's
        # judge/human disagreement is built to surface.
        notes_match = re.search(r"<adjuster_notes>\n(.*?)\n</adjuster_notes>", user_message, re.DOTALL)
        summary_match = re.search(r"<claim_summary>\n(.*?)\n</claim_summary>", user_message, re.DOTALL)
        context_match = re.search(r"<policy_context>\n(.*?)\n</policy_context>", user_message, re.DOTALL)
        notes = notes_match.group(1).strip() if notes_match else ""
        summary_json = summary_match.group(1).strip() if summary_match else "{}"
        try:
            summary = json.loads(summary_json)
        except ValueError:
            summary = {}
        by_chunk_id = _passages_by_chunk_id(context_match.group(1)) if context_match else {}
        cited_ids = summary.get("citations") or []
        cited_bodies = " ".join(by_chunk_id[cid] for cid in cited_ids if cid in by_chunk_id)
        decision = summary.get("coverage_decision")
        rationale_text = str(summary.get("summary", ""))

        plausible = bool(cited_ids) and bool(rationale_text.strip())
        v2_active = self._TOPICAL_CHECK_MARKER in system.lower()

        if not v2_active:
            has_negation = any(cue in cited_bodies.lower() for cue in _NEGATION_CUES)
            polarity_ok = not (has_negation and decision == "covered") and not (
                not has_negation and decision == "denied" and cited_bodies
            )
            polarity_reason = "cited passage(s) contain a negation/exclusion cue that conflicts with the stated decision"
        else:
            # v2: read the MODEL'S OWN STATED RATIONALE, not the whole cited chunk
            # (which routinely contains other, unrelated clauses packed alongside
            # the one actually relied on) -- and distinguish a primary exclusion
            # from a carve-out that restores cover instead of collapsing both into
            # one "has a negative word" bucket.
            check_text = rationale_text.lower().strip()
            has_primary = any(cue in check_text for cue in self._PRIMARY_EXCLUSION_CUES) or bool(
                self._LETTERED_ITEM_RE.search(rationale_text)
            )
            has_carveout = (not has_primary) and any(cue in check_text for cue in self._CARVEOUT_CUES)
            if has_carveout:
                polarity_ok = decision != "denied"
                polarity_reason = "the quoted rationale is a carve-out (the exclusion 'does not apply') -- that supports 'covered', not 'denied'"
            elif has_primary:
                polarity_ok = decision != "covered"
                polarity_reason = "the quoted rationale is an exclusion item -- that supports 'denied', not 'covered'"
            else:
                polarity_ok = True
                polarity_reason = ""

        # v2 only: does the cited passage even belong to the same topic as the
        # notes? Catches "right polarity, wrong clause" (e.g. a flood sub-limit
        # question denied on the cyber exclusion, which IS negatively worded).
        topical_ok = True
        topical_reason = ""
        if v2_active and notes:
            # Checked against the RATIONALE, not the whole cited chunk, for the same
            # reason as the polarity check above: one retrieved chunk in this corpus
            # routinely packs several unrelated clauses together (e.g. a cyber
            # write-back paragraph sitting right next to the business-interruption
            # waiting-period clause), and the wider chunk's topic mix is not what is
            # actually being relied on -- only the quoted sentence is.
            notes_lower = notes.lower()
            rationale_lower = rationale_text.lower()
            for topic, sides in self._TOPIC_MARKERS.items():
                passage_has_topic = any(m in rationale_lower for m in sides["passage"])
                notes_has_topic = any(m in notes_lower for m in sides["notes"])
                if passage_has_topic and not notes_has_topic:
                    topical_ok = False
                    topical_reason = f"cited passage is about {topic!r} but the notes never mention it"
                    break

        pass_criterion = plausible and polarity_ok and topical_ok
        if not plausible:
            rationale = "summary lacks a citation or a summary sentence to check"
        elif not polarity_ok:
            rationale = polarity_reason
        elif not topical_ok:
            rationale = topical_reason
        else:
            rationale = "cited passage(s) support the stated coverage decision"
        return {"pass_criterion": pass_criterion, "rationale": rationale}


    # ------------------------------------------------------------------ #
    # Persona 4: the integrated-system judge (Week 12). Criterion FIT -- see coursework/w12/judge_integrated.txt.
    #
    # Reads the notice and the clause topic of each cited excerpt, the way the prompt tells it to. Version 1 of the prompt
    # reasons about PERIL and CLAUSE TOPIC only. A later version adds a paragraph about earlier events; the stub switches that
    # rule on when the paragraph is in the prompt it is given, so "iterating the judge" is editing the prompt file, as in Week 6.
    # ------------------------------------------------------------------ #
    _PRIOR_EVENT_MARKER = "engaged only if the notice states an earlier event"
    _SECOND_EVENT_RE = re.compile(
        r"second (?:flood|time)|earlier flood|previous flood|prior flood|\bagain\b|after the earlier|flooded twice|twice (?:this|in)", re.IGNORECASE
    )

    def _judge_fit(self, system: str, user_message: str) -> dict[str, Any]:
        notice_match = re.search(r"<fnol>\n(.*?)\n</fnol>", user_message, re.DOTALL)
        card_match = re.search(r"<triage_card>\n(.*?)\n</triage_card>", user_message, re.DOTALL)
        notice = (notice_match.group(1) if notice_match else "").lower()
        raw_notice = notice_match.group(1) if notice_match else ""
        card = self._json_or_none(card_match.group(1) if card_match else "") or {}
        excerpts = [str(w.get("excerpt", "")) for w in card.get("wording", [])]
        joined = " ".join(excerpts).lower()
        if not excerpts:
            return {"pass_criterion": False, "rationale": "nothing is cited, so there is no wording to fit to the notice"}

        has_e17 = "e-17" in joined or "second or subsequent" in joined
        has_bi = "waiting period" in joined or "indemnity period" in joined
        has_flood = "flood" in joined
        notice_second = bool(self._SECOND_EVENT_RE.search(raw_notice))
        notice_flood = "flood" in notice
        notice_bi = bool(re.search(r"business interruption|waiting period|interruption|shut for", notice))
        notice_fire = bool(re.search(r"\bfire\b", notice)) and not notice_flood

        if notice_second:
            ok = has_e17
            why = "a second-flood notice is engaged by the sequential-flood clause" if ok else "a second-flood notice, but no sequential-flood clause is cited"
        elif notice_bi:
            ok = has_bi
            why = "a business-interruption notice is engaged by the waiting-period clause" if ok else "a business-interruption notice, but no business-interruption clause is cited"
        elif notice_flood:
            ok = has_flood
            why = "a flood notice with flood wording cited" if ok else "a flood notice, but no flood wording is cited"
            if ok and has_e17 and self._PRIOR_EVENT_MARKER in " ".join(system.lower().split()):
                ok = False  # the version-2 paragraph: a clause about a later event is not engaged without a stated earlier event
                why = "the cited clause concerns a second or subsequent event but the notice states no earlier event"
        elif notice_fire:
            ok = "fire" in joined or "explosion" in joined or not has_flood
            why = "a fire notice with non-flood wording cited" if ok else "a fire notice, but only flood wording is cited"
        else:
            ok = True
            why = "no specific topic in the notice to contradict the citation"
        return {"pass_criterion": ok, "rationale": why}


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def build_llm_provider(settings: Settings) -> LLMProvider:
    """Resolve the backend named by ``LLM_PROVIDER``, wrapped so every call is traceable.

    The wrapper (``app.observability.TracedProvider``) records a generation span -- tokens,
    cost, prompt version -- on whatever trace is active and is a pass-through otherwise.
    """
    from app.observability import TracedProvider  # noqa: PLC0415 -- observability imports this module

    return TracedProvider(_resolve_llm_provider(settings), price_model=settings.answer_model)


def _resolve_llm_provider(settings: Settings) -> LLMProvider:
    """Resolve the backend named by ``LLM_PROVIDER``.

    'auto' tries Anthropic, then OpenAI, then falls back to the deterministic
    stub -- which is what makes this whole app (ask, claim summaries, the eval
    judge, trace replay) runnable with zero API keys and zero network calls.
    """
    provider = settings.llm_provider

    if provider == "anthropic":
        return AnthropicProvider(api_key=settings.anthropic_api_key, model=settings.answer_model)
    if provider == "openai":
        return OpenAIProvider(api_key=settings.openai_api_key, model=settings.openai_model)
    if provider == "stub":
        return StubProvider()

    # auto
    if settings.anthropic_api_key:
        candidate = AnthropicProvider(api_key=settings.anthropic_api_key, model=settings.answer_model)
        if candidate.is_configured():
            logger.info("llm: %s", candidate.name)
            return candidate
    if settings.openai_api_key:
        candidate = OpenAIProvider(api_key=settings.openai_api_key, model=settings.openai_model)
        if candidate.is_configured():
            logger.info("llm: %s", candidate.name)
            return candidate

    logger.warning("no LLM credentials found; using the deterministic stub provider (no network calls).")
    return StubProvider()

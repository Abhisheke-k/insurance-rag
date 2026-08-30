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

logger = logging.getLogger(__name__)

__all__ = [
    "AnthropicProvider",
    "LLMProvider",
    "LLMResponse",
    "LLMUnavailableError",
    "OpenAIProvider",
    "StubProvider",
    "build_llm_provider",
]


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Normalised shape every backend returns, whatever its native SDK gives back."""

    text: str
    model: str
    stop_reason: str  # "end" | "max_tokens" | "refusal"
    refusal_category: str | None = None


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

        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            return LLMResponse(text="", model=self._model, stop_reason="refusal", refusal_category=category)

        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        stop_reason = "max_tokens" if getattr(response, "stop_reason", None) == "max_tokens" else "end"
        return LLMResponse(text=text, model=getattr(response, "model", self._model), stop_reason=stop_reason)

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
        return LLMResponse(
            text=text,
            model=getattr(response, "model", self._model),
            stop_reason=stop_reason,
            refusal_category="content_filter" if stop_reason == "refusal" else None,
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
        elif "coverage_decision" in properties:
            payload = self._summarize_claim(user_message)
        elif "found" in properties and "citations" in properties:
            payload = self._answer_question(user_message)
        else:  # pragma: no cover - defensive; every call site declares a known shape
            raise LLMUnavailableError(f"stub provider has no persona for schema {sorted(properties)!r}")
        return LLMResponse(text=json.dumps(payload), model=self.name, stop_reason="end")

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

        claim_number_match = re.search(r"\bCLM-\d{4}-\d{5}\b", notes)
        claim_number = claim_number_match.group(0) if claim_number_match else "UNKNOWN"

        date_match = re.search(
            r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|"
            r"November|December)\s+\d{4}|\d{4}-\d{2}-\d{2})\b",
            notes,
        )
        date_of_loss = date_match.group(0) if date_match else ""

        amount_match = re.search(r"GBP\s?([\d,]+(?:\.\d{2})?)", notes)
        claimed_amount = float(amount_match.group(1).replace(",", "")) if amount_match else None

        scored = _score_passages(passages, notes)
        bucket = _seed(notes)
        top = scored[0] if scored and scored[0].score > 0 else None

        excess_amount: float | None = None
        cited_exclusion_id: str | None = None
        citations: list[int] = []
        coverage_decision = "needs_review"
        summary_bits: list[str] = []

        if top is not None:
            citations.append(top.number)
            excess_search = re.search(r"deductible of GBP\s?([\d,]+)|excess of GBP\s?([\d,]+)", top.body, re.IGNORECASE)
            if excess_search:
                raw = next(g for g in excess_search.groups() if g)
                excess_amount = float(raw.replace(",", ""))

            has_negation = any(cue in top.body.lower() for cue in _NEGATION_CUES)
            clause_id_match = re.search(r"\(([a-h])\)|(\d\.\d(?:\.\d)?)", top.header + " " + top.body)

            if has_negation:
                coverage_decision = "denied"
                if clause_id_match:
                    cited_exclusion_id = clause_id_match.group(1) or clause_id_match.group(2)
                summary_bits.append(f"Notes describe circumstances matching an excluded scenario: {_best_sentence(top.body, _content_tokens(notes))}")
            else:
                coverage_decision = "covered"
                summary_bits.append(f"Notes match an insured peril: {_best_sentence(top.body, _content_tokens(notes))}")

            # Deterministic G-failure: flips the coverage read on notes that DO carry a
            # negation cue in the matched clause -- same "right context, wrong conclusion"
            # pattern as the Q&A stub, tuned to a different, independent hash band.
            if has_negation and 40 <= bucket < 55:
                coverage_decision = "covered"
                cited_exclusion_id = None
                summary_bits[-1] = f"Notes match an insured peril: {_best_sentence(top.body, _content_tokens(notes))}"
        else:
            summary_bits.append("No policy passage in context matches the circumstances described in the notes.")

        summary = " ".join(summary_bits) or "Unable to determine coverage from the supplied notes and context."
        return {
            "claim_number": claim_number,
            "date_of_loss": date_of_loss,
            "coverage_decision": coverage_decision,
            "cited_exclusion_id": cited_exclusion_id,
            "excess_amount": excess_amount,
            "summary": summary,
            "citations": citations,
        }

    # ------------------------------------------------------------------ #
    # Persona 3: judge (matches app.judge.JUDGE_SCHEMA)
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
    _TOPIC_MARKERS = {
        "cyber": ["cyber", "computer system", "ransom", "malicious", "hacking", "encrypt", "cryptocurrency"],
        "flood_sublimit": ["sub-limit", "annual aggregate"],
        "business_interruption": ["waiting period", "indemnity period"],
    }

    def _judge(self, system: str, user_message: str) -> dict[str, Any]:
        # v1: checks only that the summary's coverage_decision agrees with whether
        # the cited passage carries a negation cue -- it never independently
        # re-derives the answer from the notes, and it cannot tell a correctly-
        # negated WRONG clause from a correctly-negated RIGHT one. That blind spot
        # is what Week 6's judge/human disagreement is built to surface.
        notes_match = re.search(r"<adjuster_notes>\n(.*?)\n</adjuster_notes>", user_message, re.DOTALL)
        summary_match = re.search(r"<claim_summary>\n(.*?)\n</claim_summary>", user_message, re.DOTALL)
        context_match = re.search(r"<policy_context>\n(.*?)\n</policy_context>", user_message, re.DOTALL)
        notes = notes_match.group(1).strip() if notes_match else ""
        summary_json = summary_match.group(1).strip() if summary_match else "{}"
        try:
            summary = json.loads(summary_json)
        except ValueError:
            summary = {}
        passages = _parse_passages(context_match.group(1)) if context_match else []
        cited = summary.get("citations") or []
        cited_bodies = " ".join(body for num, _header, body in passages if num in cited)
        has_negation = any(cue in cited_bodies.lower() for cue in _NEGATION_CUES)
        decision = summary.get("coverage_decision")

        plausible = bool(cited) and bool(str(summary.get("summary", "")).strip())
        polarity_ok = not (has_negation and decision == "covered") and not (
            not has_negation and decision == "denied" and cited_bodies
        )

        # v2 only: does the cited passage even belong to the same topic as the
        # notes? Catches "right polarity, wrong clause" (e.g. a flood sub-limit
        # question denied on the cyber exclusion, which IS negatively worded).
        topical_ok = True
        topical_reason = ""
        if self._TOPICAL_CHECK_MARKER in system.lower() and notes:
            notes_lower = cited_bodies and notes.lower()
            for topic, markers in self._TOPIC_MARKERS.items():
                passage_has_topic = any(m in cited_bodies.lower() for m in markers)
                notes_has_topic = any(m in notes_lower for m in markers)
                if passage_has_topic and not notes_has_topic:
                    topical_ok = False
                    topical_reason = f"cited passage is about {topic!r} but the notes never mention it"
                    break

        pass_criterion = plausible and polarity_ok and topical_ok
        if not plausible:
            rationale = "summary lacks a citation or a summary sentence to check"
        elif not polarity_ok:
            rationale = "cited passage(s) contain a negation/exclusion cue that conflicts with the stated decision"
        elif not topical_ok:
            rationale = topical_reason
        else:
            rationale = "cited passage(s) support the stated coverage decision"
        return {"pass_criterion": pass_criterion, "rationale": rationale}


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def build_llm_provider(settings: Settings) -> LLMProvider:
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

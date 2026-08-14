"""Answering behaviour and the guard rails that keep answers traceable.

The headline requirement: **an out-of-scope question returns the not-found
response rather than a fabricated answer**. That is enforced in two independent
places, and both are tested here without needing an API key:

* the relevance gate, which refuses to call Claude at all when nothing in the
  corpus is close enough to the question;
* citation resolution, which discards passage numbers that do not map to a
  retrieved chunk and withholds any answer left with no citation.

A live end-to-end check against the real model runs too, but only when
``ANTHROPIC_API_KEY`` is set.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.generation import (
    NOT_FOUND_MESSAGE,
    SYSTEM_PROMPT,
    AnswerGenerator,
    build_context_block,
)
from app.models import GeneratedAnswer
from app.rag import RagService

from .conftest import EVAL_QUESTIONS, OUT_OF_SCOPE_QUESTION, StubGenerator

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_FAKE_RESPONSE = SimpleNamespace(model="claude-opus-5", stop_reason="end_turn")


# --------------------------------------------------------------------------- #
# (c) out-of-scope questions are not answered
# --------------------------------------------------------------------------- #
def test_out_of_scope_question_returns_not_found_without_calling_the_model(
    ingested_service: RagService, stub_generator: StubGenerator
):
    outcome = ingested_service.ask(OUT_OF_SCOPE_QUESTION)

    assert outcome.answer.answer == NOT_FOUND_MESSAGE
    assert outcome.answer.found is False
    assert outcome.answer.citations == []
    assert outcome.answer.chunks_used == 0
    assert outcome.gated is True
    assert stub_generator.call_count == 0, "the model must not be asked to reason over noise"


def test_in_scope_question_is_not_gated(ingested_service: RagService, stub_generator: StubGenerator):
    """Control case: the gate must not simply reject everything."""
    outcome = ingested_service.ask(EVAL_QUESTIONS[0]["question"])

    assert outcome.gated is False
    assert stub_generator.call_count == 1
    assert outcome.answer.found is True
    assert outcome.answer.citations


def test_asking_with_no_documents_returns_not_found(service: RagService, stub_generator: StubGenerator):
    outcome = service.ask("What is the flood sub-limit?")

    assert outcome.answer.answer == NOT_FOUND_MESSAGE
    assert outcome.answer.citations == []
    assert stub_generator.call_count == 0


def test_empty_question_is_rejected(ingested_service: RagService):
    with pytest.raises(ValueError):
        ingested_service.ask("   ")


# --------------------------------------------------------------------------- #
# citation resolution
# --------------------------------------------------------------------------- #
@pytest.fixture
def generator(settings: Settings) -> AnswerGenerator:
    return AnswerGenerator(settings)


@pytest.fixture
def hits(ingested_service: RagService):
    return ingested_service.retrieve(EVAL_QUESTIONS[0]["question"], top_k=5)


def _verify(generator: AnswerGenerator, payload: dict, hits) -> GeneratedAnswer:
    return generator._verify(payload, hits, _FAKE_RESPONSE, [])  # noqa: SLF001


def test_fabricated_citation_is_discarded(generator: AnswerGenerator, hits):
    """A passage number that was never supplied cannot survive verification."""
    answer = _verify(
        generator,
        {"found": True, "answer": "The sub-limit is GBP 5,000,000.", "citations": [99]},
        hits,
    )

    assert answer.found is False
    assert answer.answer == NOT_FOUND_MESSAGE
    assert answer.citations == []
    assert any("out-of-range" in note for note in answer.notes)


def test_only_real_citations_survive(generator: AnswerGenerator, hits):
    answer = _verify(
        generator,
        {"found": True, "answer": "The sub-limit is GBP 5,000,000.", "citations": [1, 42, "x"]},
        hits,
    )

    assert answer.found is True
    assert len(answer.citations) == 1
    assert answer.citations[0].chunk_id == hits[0].chunk_id
    assert answer.citations[0].filename.endswith(".pdf")
    assert answer.citations[0].page >= 1
    assert answer.citations[0].section


def test_uncited_answer_is_withheld(generator: AnswerGenerator, hits):
    """An answer with nothing to point at is not shippable, however plausible."""
    answer = _verify(
        generator, {"found": True, "answer": "The sub-limit is GBP 9,999,999.", "citations": []}, hits
    )

    assert answer.found is False
    assert answer.answer == NOT_FOUND_MESSAGE
    assert any("withheld" in note for note in answer.notes)


def test_duplicate_citations_are_collapsed(generator: AnswerGenerator, hits):
    answer = _verify(
        generator, {"found": True, "answer": "Answer.", "citations": [1, 1, 1]}, hits
    )
    assert len(answer.citations) == 1


def test_model_not_found_is_normalised_to_the_exact_message(generator: AnswerGenerator, hits):
    answer = _verify(
        generator,
        {"found": False, "answer": "I am not sure, sorry!", "citations": [1]},
        hits,
    )

    assert answer.answer == NOT_FOUND_MESSAGE
    assert answer.citations == []


# --------------------------------------------------------------------------- #
# prompt construction
# --------------------------------------------------------------------------- #
def test_context_block_labels_every_passage(hits):
    block = build_context_block(hits)

    for index, hit in enumerate(hits, start=1):
        assert f"[{index}]" in block
        assert hit.chunk_id in block
        assert hit.filename in block
    assert "page:" in block and "section:" in block


def test_system_prompt_states_the_not_found_contract():
    assert NOT_FOUND_MESSAGE in SYSTEM_PROMPT
    assert "only from the passages" in SYSTEM_PROMPT.lower()
    assert "never invent" in SYSTEM_PROMPT.lower()


def test_generator_reports_configuration(generator: AnswerGenerator):
    status = generator.status()
    assert set(status) == {"configured", "model", "effort", "error"}
    assert status["model"]


# --------------------------------------------------------------------------- #
# live model (skipped without credentials)
# --------------------------------------------------------------------------- #
requires_api_key = pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY is not set; skipping live Claude calls",
)


@pytest.fixture
def live_service(tmp_path, sample_pdf_bytes: bytes) -> RagService:
    """A service wired to the real model, with the relevance gate disabled.

    Disabling the gate is deliberate: it forces the out-of-scope question all
    the way through to Claude, so the test measures the *model's* grounding
    rather than our short-circuit.
    """
    settings = Settings(
        chunk_size=550,
        chunk_overlap=120,
        embedding_provider="hashing",
        force_in_memory_store=True,
        chroma_dir=tmp_path / "live",
        min_relevance=-1.0,
    )
    service = RagService(settings)
    service.ingest(sample_pdf_bytes, "sample_endorsement_pack.pdf")
    return service


@requires_api_key
@pytest.mark.integration
def test_live_model_declines_an_out_of_scope_question(live_service: RagService):
    outcome = live_service.ask(OUT_OF_SCOPE_QUESTION)

    assert outcome.gated is False, "the gate should be off, so the model really was asked"
    assert outcome.answer.found is False
    assert outcome.answer.answer == NOT_FOUND_MESSAGE
    assert outcome.answer.citations == []


@requires_api_key
@pytest.mark.integration
def test_live_model_answers_an_in_scope_question_with_citations(live_service: RagService):
    outcome = live_service.ask(EVAL_QUESTIONS[0]["question"])

    assert outcome.answer.found is True
    assert "5,000,000" in outcome.answer.answer
    assert outcome.answer.citations

    retrieved_ids = {hit.chunk_id for hit in outcome.retrieved}
    for citation in outcome.answer.citations:
        assert citation.chunk_id in retrieved_ids, "every citation must point at a retrieved chunk"

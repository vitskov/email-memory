"""Tests for Stage 4 retrieval engine and answer synthesis."""

from __future__ import annotations

import subprocess
import sys
import types
from datetime import datetime, timezone
from typing import Any

import pytest

_embedder_module = types.ModuleType("email_memory_store.retrieval.embedder")
_vector_store_module = types.ModuleType("email_memory_store.retrieval.vector_store")


class _StubDefaultDependency:
    pass


def _unexpected_default_dependency() -> _StubDefaultDependency:
    raise AssertionError("tests must inject retrieval dependencies")


_embedder_module.Embedder = _StubDefaultDependency
_embedder_module.get_default_embedder = _unexpected_default_dependency
_vector_store_module.COLLECTION_NAMES = (
    "holographic_facts",
    "action_items",
    "deadlines",
    "calendar_events",
    "decisions",
    "thread_summaries",
    "message_chunks",
)
_vector_store_module.VectorStore = _StubDefaultDependency
_vector_store_module.get_default_store = _unexpected_default_dependency
sys.modules.setdefault("email_memory_store.retrieval.embedder", _embedder_module)
sys.modules.setdefault("email_memory_store.retrieval.vector_store", _vector_store_module)

from email_memory_store.promotion.llm import LLMProviderSpec
from email_memory_store.retrieval.answerer import Answerer, REFUSAL_TEXT
from email_memory_store.retrieval.engine import (
    EFFORT_COLLECTIONS,
    RetrievalEngine,
    RetrievalResult,
)
from email_memory_store.retrieval.filters import RetrievalFilters


class FakeChromaCollection:
    def __init__(
        self,
        *,
        semantic: dict[str, list[list[Any]]] | None = None,
        lexical: dict[str, list[list[Any]]] | None = None,
        raise_on_lexical: bool = False,
    ) -> None:
        self.semantic = semantic or _chroma_response()
        self.lexical = lexical or _chroma_response()
        self.raise_on_lexical = raise_on_lexical
        self.query_calls: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, list[list[Any]]]:
        self.query_calls.append(kwargs)
        if "where_document" in kwargs:
            if self.raise_on_lexical:
                raise RuntimeError("where_document unsupported")
            return self.lexical
        return self.semantic


class FakeVectorStore:
    def __init__(self, collections: dict[str, FakeChromaCollection] | None = None) -> None:
        self.collections = collections or {}
        self.requested_collections: list[str] = []
        self.closed = False

    def collection(self, name: str) -> FakeChromaCollection:
        self.requested_collections.append(name)
        if name not in self.collections:
            self.collections[name] = FakeChromaCollection()
        return self.collections[name]

    def close(self) -> None:
        self.closed = True


class FakeEmbedder:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
        return [0.0] * 4


class RaisingEngine:
    def search(self, *_args: Any, **_kwargs: Any) -> list[RetrievalResult]:
        raise AssertionError("search should not be called")


class StaticEngine:
    def __init__(self, results: list[RetrievalResult]) -> None:
        self.results = results
        self.queries: list[str] = []

    def search(self, query: str, **_kwargs: Any) -> list[RetrievalResult]:
        self.queries.append(query)
        return self.results


def _chroma_response(
    ids: list[str] | None = None,
    documents: list[str] | None = None,
    metadatas: list[dict[str, Any]] | None = None,
    distances: list[float] | None = None,
) -> dict[str, list[list[Any]]]:
    row_ids = ids or []
    return {
        "ids": [row_ids],
        "documents": [documents or [f"document {id_}" for id_ in row_ids]],
        "metadatas": [metadatas or [{} for _ in row_ids]],
        "distances": [distances or [0.1 for _ in row_ids]],
    }


def _engine(
    collections: dict[str, FakeChromaCollection] | None = None,
    embedder: FakeEmbedder | None = None,
) -> tuple[RetrievalEngine, FakeVectorStore, FakeEmbedder]:
    fake_store = FakeVectorStore(collections)
    fake_embedder = embedder or FakeEmbedder()
    return (
        RetrievalEngine(vector_store=fake_store, embedder=fake_embedder),
        fake_store,
        fake_embedder,
    )


def _result(
    *,
    collection: str = "holographic_facts",
    id: str = "42",
    document: str = "stored fact",
    metadata: dict[str, Any] | None = None,
) -> RetrievalResult:
    return RetrievalResult(
        collection=collection,
        id=id,
        score=1.0,
        document=document,
        metadata=metadata or {},
        semantic_rank=0,
        lexical_rank=None,
        semantic_distance=0.1,
    )


def test_engine_rejects_unknown_effort() -> None:
    engine, _, _ = _engine()

    with pytest.raises(ValueError):
        engine.search("query", effort="unknown")


def test_engine_returns_empty_for_blank_query_without_embedding() -> None:
    embedder = FakeEmbedder()
    engine, _, _ = _engine(embedder=embedder)

    assert engine.search("   ") == []
    assert embedder.queries == []


def test_light_effort_only_queries_holographic_facts() -> None:
    engine, store, _ = _engine()

    engine.search("parking", effort="light")

    assert store.requested_collections == ["holographic_facts"]


def test_heavy_effort_queries_all_stage_four_collections() -> None:
    engine, store, _ = _engine()

    engine.search("parking", effort="heavy")

    assert store.requested_collections == list(EFFORT_COLLECTIONS["heavy"])


def test_engine_rrf_boosts_semantic_and_lexical_overlap_and_sorts_descending() -> None:
    collection = FakeChromaCollection(
        semantic=_chroma_response(ids=["semantic-only", "both"]),
        lexical=_chroma_response(ids=["both"]),
    )
    engine, _, _ = _engine({"holographic_facts": collection})

    results = engine.search("shared", effort="light", limit=10)
    by_id = {result.id: result for result in results}

    assert results == sorted(results, key=lambda result: result.score, reverse=True)
    assert by_id["both"].score > by_id["semantic-only"].score
    assert results[0].id == "both"


def test_engine_returns_semantic_results_when_lexical_query_raises() -> None:
    collection = FakeChromaCollection(
        semantic=_chroma_response(ids=["semantic-only"]),
        raise_on_lexical=True,
    )
    engine, _, _ = _engine({"holographic_facts": collection})

    results = engine.search("query", effort="light")

    assert [result.id for result in results] == ["semantic-only"]


def test_engine_applies_date_post_filter_to_collection_results() -> None:
    action_items = FakeChromaCollection(
        semantic=_chroma_response(
            ids=["inside", "outside", "missing"],
            metadatas=[
                {"due_date": "2026-04-15T00:00:00+00:00"},
                {"due_date": "2026-03-15T00:00:00+00:00"},
                {},
            ],
        )
    )
    engine, _, _ = _engine({"action_items": action_items})
    filters = RetrievalFilters(
        date_from=datetime(2026, 4, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 4, 30, 23, 59, 59, tzinfo=timezone.utc),
    )

    results = engine.search("deadline", effort="medium", limit=10, filters=filters)

    assert [result.id for result in results] == ["inside"]


def test_engine_applies_date_post_filter_to_calendar_events() -> None:
    calendar_events = FakeChromaCollection(
        semantic=_chroma_response(
            ids=["inside", "outside"],
            metadatas=[
                {"starts_at": "2026-04-09T09:00:00+00:00"},
                {"starts_at": "2026-05-09T09:00:00+00:00"},
            ],
        )
    )
    engine, _, _ = _engine({"calendar_events": calendar_events})
    filters = RetrievalFilters(
        date_from=datetime(2026, 4, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 4, 30, 23, 59, 59, tzinfo=timezone.utc),
    )

    results = engine.search("meeting", effort="medium", limit=10, filters=filters)

    assert [result.id for result in results] == ["inside"]


def test_answerer_refuses_blank_query_without_search_or_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("subprocess should not be called")

    monkeypatch.setattr("email_memory_store.retrieval.answerer.subprocess.run", fail_run)
    answerer = Answerer(engine=RaisingEngine(), provider_spec=LLMProviderSpec())

    result = answerer.answer("")

    assert result.answer == REFUSAL_TEXT
    assert result.citations == []
    assert result.retrieved == []
    assert result.used_handles == []


def test_answerer_refuses_when_engine_returns_no_results_without_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("subprocess should not be called")

    monkeypatch.setattr("email_memory_store.retrieval.answerer.subprocess.run", fail_run)
    answerer = Answerer(engine=StaticEngine([]), provider_spec=LLMProviderSpec())

    result = answerer.answer("what happened?")

    assert result.answer == REFUSAL_TEXT
    assert result.citations == []
    assert result.retrieved == []
    assert result.used_handles == []


def test_build_prompt_uses_collection_prefix_handles() -> None:
    answerer = Answerer(engine=StaticEngine([]), provider_spec=LLMProviderSpec())

    prompt, citations = answerer._build_prompt(
        "question",
        [
            _result(collection="holographic_facts", id="42"),
            _result(collection="calendar_events", id="77"),
            _result(collection="message_chunks", id="1234:0"),
        ],
    )

    assert "[F:42]" in prompt
    assert "[CE:77]" in prompt
    assert "STRICT JSON" in prompt
    assert [citation.handle for citation in citations] == ["F:42", "CE:77", "M:1234:0"]


def test_build_prompt_truncates_context_but_keeps_full_citation_document() -> None:
    answerer = Answerer(engine=StaticEngine([]), provider_spec=LLMProviderSpec())
    long_document = "x" * 5000

    prompt, citations = answerer._build_prompt(
        "question", [_result(id="42", document=long_document)]
    )

    assert len(prompt) < 1500
    assert long_document not in prompt
    assert "..." in prompt
    assert citations[0].document == long_document


def test_parse_response_reads_strict_json() -> None:
    answerer = Answerer(engine=StaticEngine([]), provider_spec=LLMProviderSpec())

    assert answerer._parse_response(
        '{"answer": "yes [F:42]", "used_citations": ["F:42"]}',
        valid_handles={"F:42"},
    ) == (
        "yes [F:42]",
        ["F:42"],
    )


def test_parse_response_finds_embedded_json_and_uses_inline_citations() -> None:
    answerer = Answerer(engine=StaticEngine([]), provider_spec=LLMProviderSpec())

    assert answerer._parse_response(
        'preamble {"answer": "ok [F:42]"} trailing',
        valid_handles={"F:42"},
    ) == ("ok [F:42]", ["F:42"])


def test_parse_response_refuses_uncited_answer_text() -> None:
    answerer = Answerer(engine=StaticEngine([]), provider_spec=LLMProviderSpec())

    assert answerer._parse_response(
        '{"answer": "not cited", "used_citations": ["F:42"]}',
        valid_handles={"F:42"},
    ) == (REFUSAL_TEXT, [])


def test_parse_response_refuses_raw_text() -> None:
    answerer = Answerer(engine=StaticEngine([]), provider_spec=LLMProviderSpec())

    assert answerer._parse_response("not json at all") == (REFUSAL_TEXT, [])


def test_answerer_end_to_end_with_mocked_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["hermes"],
            returncode=0,
            stdout='{"answer": "use the chunk [M:1234:0]", "used_citations": ["M:1234:0"]}',
            stderr="",
        )

    monkeypatch.setattr("email_memory_store.retrieval.answerer.subprocess.run", fake_run)
    answerer = Answerer(
        engine=StaticEngine(
            [_result(collection="message_chunks", id="1234:0", document="chunk text")]
        ),
        provider_spec=LLMProviderSpec(executable="/opt/bin/hermes-current"),
    )

    result = answerer.answer("test query")

    assert result.answer == "use the chunk [M:1234:0]"
    assert result.used_handles == ["M:1234:0"]
    assert result.citations[0].handle == "M:1234:0"


def test_answerer_refuses_malformed_model_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["hermes"],
            returncode=0,
            stdout="totally malformed raw model text",
            stderr="",
        )

    monkeypatch.setattr("email_memory_store.retrieval.answerer.subprocess.run", fake_run)
    answerer = Answerer(
        engine=StaticEngine(
            [_result(collection="message_chunks", id="1234:0", document="chunk text")]
        ),
        provider_spec=LLMProviderSpec(executable="/opt/bin/hermes-current"),
    )

    result = answerer.answer("test query")

    assert result.answer == REFUSAL_TEXT
    assert result.used_handles == []
    assert result.citations == []

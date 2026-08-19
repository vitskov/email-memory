"""RAG answer synthesis for retrieval results with inline citation handles."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from email_memory_store.promotion.llm import (
    BaseLLMProvider,
    LLMProviderSpec,
    create_provider,
)
from email_memory_store.retrieval.engine import RetrievalEngine, RetrievalResult
from email_memory_store.retrieval.filters import RetrievalFilters


CITATION_PREFIX_BY_COLLECTION: dict[str, str] = {
    "holographic_facts": "F",
    "action_items": "A",
    "deadlines": "D",
    "calendar_events": "CE",
    "decisions": "DC",
    "thread_summaries": "S",
    "message_chunks": "M",
}
REFUSAL_TEXT: str = "I don't have enough information in the email memory store to answer that."
DEFAULT_LIMIT: int = 10
_PROMPT_DOCUMENT_CHARS = 600
_CITATION_HANDLE_PATTERN = re.compile(r"\[((?:F|A|D|CE|DC|S|M):[^\]\s]+)\]")


@dataclass(frozen=True)
class Citation:
    handle: str
    collection: str
    id: str
    metadata: dict[str, Any]
    document: str


@dataclass(frozen=True)
class AnswerResult:
    query: str
    answer: str
    citations: list[Citation]
    retrieved: list[RetrievalResult]
    used_handles: list[str]


class Answerer:
    def __init__(
        self,
        *,
        engine: RetrievalEngine | None = None,
        provider_spec: LLMProviderSpec | None = None,
    ) -> None:
        self._engine = engine if engine is not None else RetrievalEngine()
        self._provider: BaseLLMProvider = create_provider(provider_spec or LLMProviderSpec())

    def answer(
        self,
        query: str,
        *,
        effort: str = "medium",
        limit: int = DEFAULT_LIMIT,
        filters: RetrievalFilters | None = None,
    ) -> AnswerResult:
        if not query.strip():
            return AnswerResult(
                query=query,
                answer=REFUSAL_TEXT,
                citations=[],
                retrieved=[],
                used_handles=[],
            )

        retrieved = self._engine.search(query, effort=effort, limit=limit, filters=filters)
        if not retrieved:
            return AnswerResult(
                query=query,
                answer=REFUSAL_TEXT,
                citations=[],
                retrieved=[],
                used_handles=[],
            )

        prompt, citations = self._build_prompt(query, retrieved)
        completed = subprocess.run(
            self._provider.build_command(prompt),
            text=True,
            capture_output=True,
            check=True,
        )
        answer_text, used_handles = self._parse_response(
            completed.stdout,
            valid_handles={citation.handle for citation in citations},
        )
        return AnswerResult(
            query=query,
            answer=answer_text,
            citations=[citation for citation in citations if citation.handle in used_handles],
            retrieved=retrieved,
            used_handles=used_handles,
        )

    def _build_prompt(
        self, query: str, retrieved: list[RetrievalResult]
    ) -> tuple[str, list[Citation]]:
        citations: list[Citation] = []
        context_lines: list[str] = []

        for result in retrieved:
            handle = f"{CITATION_PREFIX_BY_COLLECTION[result.collection]}:{result.id}"
            citation = Citation(
                handle=handle,
                collection=result.collection,
                id=result.id,
                metadata=result.metadata,
                document=result.document,
            )
            citations.append(citation)
            rendered_document = _truncate_document(result.document)
            context_lines.append(f"[{handle}] ({result.collection}) {rendered_document}")

        context = '\n'.join(context_lines)
        return (
            'Answer the question using ONLY the retrieved context.\n'
            'Cite every factual claim with the matching bracketed handle inline, '
            '"for example: \"The deadline is May 15 [D:88].\"\n'
            f'If context is insufficient, reply exactly with: {REFUSAL_TEXT}\n'
            'When refusing, include no citations.\n'
            'Return STRICT JSON only: {\"answer\": \"...\", \"used_citations\": [\"<handle>\", ...]} '
            'and nothing outside the JSON.\n\n'
            f'Question:\n{query}\n\n'
            f'Retrieved context:\n{context}'
        ), citations

    def _parse_response(
        self,
        raw: str,
        *,
        valid_handles: set[str] | None = None,
    ) -> tuple[str, list[str]]:
        text = raw.strip()
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text):
            try:
                payload, _ = decoder.raw_decode(text[match.start() :])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and "answer" in payload:
                answer = str(payload["answer"]).strip()
                if not answer or answer == REFUSAL_TEXT:
                    return REFUSAL_TEXT, []
                used_handles = _normalize_handles(
                    _extract_inline_handles(answer),
                    valid_handles=valid_handles,
                )
                if not used_handles:
                    return REFUSAL_TEXT, []
                return answer, used_handles
        return REFUSAL_TEXT, []


def _extract_inline_handles(answer: str) -> list[str]:
    return [match.group(1) for match in _CITATION_HANDLE_PATTERN.finditer(answer)]


def _normalize_handles(
    handles: list[str],
    *,
    valid_handles: set[str] | None = None,
) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for handle in handles:
        if valid_handles is not None and handle not in valid_handles:
            continue
        if handle in seen:
            continue
        seen.add(handle)
        normalized.append(handle)
    return normalized


def _truncate_document(document: str) -> str:
    normalized = " ".join(document.split())
    if len(normalized) <= _PROMPT_DOCUMENT_CHARS:
        return normalized
    return normalized[: _PROMPT_DOCUMENT_CHARS - 3].rstrip() + "..."

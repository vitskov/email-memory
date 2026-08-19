"""Incremental embed-on-write hooks for ingestion, extraction, and promotion.

These wrap the idempotent backfill helpers so they can be called as a
post-step after the relevant pipeline stage completes. Each call only
embeds rows whose IDs are not already present in the vector store.
"""

from __future__ import annotations

from typing import Any

from .embed_backfill import (
    backfill_action_items,
    backfill_calendar_events,
    backfill_deadlines,
    backfill_decisions,
    backfill_holographic_facts,
    backfill_message_chunks,
    backfill_thread_summaries,
)
from .embedder import Embedder, get_default_embedder
from .vector_store import VectorStore, get_default_store
from ..store import EmailMemoryStore


def embed_after_ingestion(
    store: EmailMemoryStore,
    *,
    vector_store: VectorStore | None = None,
    embedder: Embedder | None = None,
    batch_size: int = 64,
) -> dict[str, int]:
    vs = vector_store if vector_store is not None else get_default_store()
    em = embedder if embedder is not None else get_default_embedder()
    return {
        "message_chunks": backfill_message_chunks(
            store=store, batch_size=batch_size, vector_store=vs, embedder=em
        ),
        "calendar_events": backfill_calendar_events(
            store=store, batch_size=batch_size, vector_store=vs, embedder=em
        ),
    }


def embed_after_extraction(
    store: EmailMemoryStore,
    *,
    vector_store: VectorStore | None = None,
    embedder: Embedder | None = None,
    batch_size: int = 64,
) -> dict[str, int]:
    vs = vector_store if vector_store is not None else get_default_store()
    em = embedder if embedder is not None else get_default_embedder()
    return {
        "action_items": backfill_action_items(
            store=store, batch_size=batch_size, vector_store=vs, embedder=em
        ),
        "deadlines": backfill_deadlines(
            store=store, batch_size=batch_size, vector_store=vs, embedder=em
        ),
        "decisions": backfill_decisions(
            store=store, batch_size=batch_size, vector_store=vs, embedder=em
        ),
        "thread_summaries": backfill_thread_summaries(
            store=store, batch_size=batch_size, vector_store=vs, embedder=em
        ),
    }


def embed_after_promotion(
    *,
    vector_store: VectorStore | None = None,
    embedder: Embedder | None = None,
    batch_size: int = 64,
) -> int:
    vs = vector_store if vector_store is not None else get_default_store()
    em = embedder if embedder is not None else get_default_embedder()
    return backfill_holographic_facts(
        batch_size=batch_size, vector_store=vs, embedder=em
    )


def embed_for_pipeline_event(
    store: EmailMemoryStore,
    *,
    event: str,
    vector_store: VectorStore | None = None,
    embedder: Embedder | None = None,
    batch_size: int = 64,
) -> dict[str, Any]:
    if event == "ingestion":
        return embed_after_ingestion(
            store, vector_store=vector_store, embedder=embedder, batch_size=batch_size
        )
    if event == "extraction":
        return embed_after_extraction(
            store, vector_store=vector_store, embedder=embedder, batch_size=batch_size
        )
    if event == "promotion":
        return {"holographic_facts": embed_after_promotion(
            vector_store=vector_store, embedder=embedder, batch_size=batch_size
        )}
    raise ValueError(f"unknown pipeline event: {event!r}")

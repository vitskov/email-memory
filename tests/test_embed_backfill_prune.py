"""Tests for self-healing vector reconciliation in embed_backfill.

Row deletions elsewhere in the pipeline (retention purges, duplicate collapse,
the delete-then-insert replace_message_* extraction paths) remove DB rows but
never their vectors. These tests pin the reconciliation that prunes the
resulting orphans, and the safety guard that refuses to wipe a collection when
the source read comes back empty.
"""
from __future__ import annotations

import sys
import types

_embedder_module = types.ModuleType("email_memory_store.retrieval.embedder")


class _StubDefaultDependency:
    pass


def _unexpected_default_dependency(*_args, **_kwargs):
    raise AssertionError("tests must inject retrieval dependencies")


_embedder_module.Embedder = _StubDefaultDependency
_embedder_module.get_default_embedder = _unexpected_default_dependency
sys.modules["email_memory_store.retrieval.embedder"] = _embedder_module

from email_memory_store.retrieval import embed_backfill, incremental
from email_memory_store.retrieval.embed_backfill import (
    _reconcile_deletions,
    backfill_action_items,
    backfill_all,
    backfill_calendar_events,
    backfill_message_chunks,
)
from email_memory_store.retrieval.incremental import embed_for_pipeline_event
from email_memory_store.retrieval.vector_store import COLLECTION_NAMES
from email_memory_store.store import EmailMemoryStore


class FakeVectorStore:
    """Implements the high-level surface the backfills use."""

    def __init__(self, ids_by_collection: dict[str, set[str]] | None = None) -> None:
        self.ids: dict[str, set[str]] = {k: set(v) for k, v in (ids_by_collection or {}).items()}
        self.deleted: dict[str, list[str]] = {}
        self.upserted: dict[str, list[str]] = {}

    def existing_ids(self, collection: str) -> set[str]:
        return set(self.ids.get(collection, set()))

    def upsert(self, collection: str, *, ids, embeddings, metadatas, documents) -> None:
        self.ids.setdefault(collection, set()).update(ids)
        self.upserted.setdefault(collection, []).extend(ids)

    def delete(self, collection: str, ids: list[str]) -> None:
        self.deleted.setdefault(collection, []).extend(ids)
        self.ids.setdefault(collection, set()).difference_update(ids)


class FakeEmbedder:
    def embed_documents(self, documents):
        return [[0.0] * 4 for _ in documents]


# ---------------------------------------------------------------------------
# _reconcile_deletions unit tests
# ---------------------------------------------------------------------------

def test_reconcile_deletes_only_orphans():
    vs = FakeVectorStore({"action_items": {"1", "2", "3"}})
    removed = _reconcile_deletions(vs, "action_items", existing={"1", "2", "3"}, current_ids={"1", "3"})
    assert removed == 1
    assert vs.deleted["action_items"] == ["2"]


def test_reconcile_noop_when_all_present():
    vs = FakeVectorStore({"deadlines": {"1", "2"}})
    removed = _reconcile_deletions(vs, "deadlines", existing={"1", "2"}, current_ids={"1", "2", "9"})
    assert removed == 0
    assert "deadlines" not in vs.deleted


def test_reconcile_guards_against_empty_source_read():
    """An empty current-id set must NOT wipe the collection — that is a failed
    read, not a legitimate 'every row was deleted'."""
    vs = FakeVectorStore({"action_items": {"1", "2", "3"}})
    removed = _reconcile_deletions(vs, "action_items", existing={"1", "2", "3"}, current_ids=set())
    assert removed == 0
    assert "action_items" not in vs.deleted


def test_reconcile_noop_on_empty_collection():
    vs = FakeVectorStore()
    assert _reconcile_deletions(vs, "action_items", existing=set(), current_ids=set()) == 0


# ---------------------------------------------------------------------------
# Integration: backfill prunes orphaned fact vectors
# ---------------------------------------------------------------------------

def _seed_action_item(store: EmailMemoryStore, action_item_id: int, text: str) -> None:
    store.conn.execute(
        """
        INSERT INTO action_items (action_item_id, thread_id, message_pk, owner, action_text, status)
        VALUES (?, NULL, NULL, 'example-owner', ?, 'open')
        """,
        [action_item_id, text],
    )


def test_backfill_action_items_prunes_vector_whose_row_was_deleted(tmp_path):
    store = EmailMemoryStore(tmp_path / "em")
    store.initialize()
    _seed_action_item(store, 1, "Example task")
    # Vector 99 has no corresponding row — an orphan left by a prior deletion.
    vs = FakeVectorStore({"action_items": {"1", "99"}})

    backfill_action_items(store=store, vector_store=vs, embedder=FakeEmbedder())

    assert vs.deleted.get("action_items") == ["99"]
    assert "1" in vs.ids["action_items"]  # live vector kept
    store.close()


def test_backfill_prune_false_leaves_orphans(tmp_path):
    store = EmailMemoryStore(tmp_path / "em")
    store.initialize()
    _seed_action_item(store, 1, "Example task")
    vs = FakeVectorStore({"action_items": {"1", "99"}})

    backfill_action_items(store=store, vector_store=vs, embedder=FakeEmbedder(), prune=False)

    assert "action_items" not in vs.deleted
    assert "99" in vs.ids["action_items"]
    store.close()


def test_backfill_action_items_guard_holds_when_table_empty(tmp_path):
    """No rows in the table must not wipe existing vectors — the empty-source guard."""
    store = EmailMemoryStore(tmp_path / "em")
    store.initialize()
    vs = FakeVectorStore({"action_items": {"1", "2"}})

    backfill_action_items(store=store, vector_store=vs, embedder=FakeEmbedder())

    assert "action_items" not in vs.deleted
    assert vs.ids["action_items"] == {"1", "2"}
    store.close()


def _seed_calendar_event(store: EmailMemoryStore, event_id: int, summary: str) -> None:
    store.conn.execute(
        """
        INSERT INTO messages (message_pk, account_id, folder_id, mailbox_message_id,
            stable_message_id, thread_key, subject, normalized_subject, from_addr,
            direction, cleaned_text)
        VALUES (?, 1, 1, ?, ?, 'fixture-thread', 'Example event', 'example event', 'sender@example.test', 'incoming', 'example body')
        """,
        [900 + event_id, f"message-{event_id}", f"fixture-{event_id}"],
    )
    store.conn.execute(
        """
        INSERT INTO calendar_events (
            calendar_event_id, message_pk, summary, organizer, location,
            starts_at, ends_at, raw_ics
        ) VALUES (?, ?, ?, 'Example organizer', 'Example location', TIMESTAMP '2026-09-01 15:00:00', TIMESTAMP '2026-09-01 16:00:00', 'BEGIN:VCALENDAR\nEND:VCALENDAR')
        """,
        [event_id, 900 + event_id, summary],
    )


def test_backfill_calendar_events_prunes_vector_whose_row_was_deleted(tmp_path):
    store = EmailMemoryStore(tmp_path / "em")
    store.initialize()
    _seed_calendar_event(store, 1, "Example calendar entry")
    vs = FakeVectorStore({"calendar_events": {"1", "99"}})

    backfill_calendar_events(store=store, vector_store=vs, embedder=FakeEmbedder())

    assert vs.deleted.get("calendar_events") == ["99"]
    assert "1" in vs.ids["calendar_events"]
    store.close()


def test_embed_for_pipeline_event_ingestion_embeds_message_chunks_and_calendar_events(tmp_path):
    store = EmailMemoryStore(tmp_path / "em")
    store.initialize()
    _seed_message(store, 1, "example content for retrieval")
    store.conn.execute(
        """
        INSERT INTO calendar_events (
            calendar_event_id, message_pk, summary, organizer, location,
            starts_at, ends_at, raw_ics
        ) VALUES (1, 1, 'Example calendar entry', 'Example organizer', 'Example location', TIMESTAMP '2026-09-02 10:00:00', TIMESTAMP '2026-09-02 11:00:00', 'BEGIN:VCALENDAR\nEND:VCALENDAR')
        """
    )
    vs = FakeVectorStore()

    embedded = embed_for_pipeline_event(store, event="ingestion", vector_store=vs, embedder=FakeEmbedder())

    assert embedded == {"message_chunks": 1, "calendar_events": 1}
    assert "1:0" in vs.ids["message_chunks"]
    assert "1" in vs.ids["calendar_events"]
    store.close()


# ---------------------------------------------------------------------------
# message_chunks: composite pk:index IDs, shrink case
# ---------------------------------------------------------------------------

def _seed_message(store: EmailMemoryStore, message_pk: int, body: str) -> None:
    store.conn.execute(
        """
        INSERT INTO messages (message_pk, account_id, folder_id, mailbox_message_id,
            stable_message_id, thread_key, subject, normalized_subject, from_addr,
            direction, cleaned_text)
        VALUES (?, 1, 1, ?, ?, 'fixture-thread', 'Example subject', 'example subject', 'sender@example.test', 'incoming', ?)
        """,
        [message_pk, f"mid{message_pk}", f"stable{message_pk}", body],
    )


def test_backfill_message_chunks_prunes_excess_chunks_after_shrink(tmp_path):
    """A message re-bodied to fewer chunks must have its now-excess chunk
    vectors pruned; a deleted message's chunks must be pruned entirely."""
    store = EmailMemoryStore(tmp_path / "em")
    store.initialize()
    # Message 1 now has a short body -> a single chunk "1:0".
    _seed_message(store, 1, "short body")
    # Chroma still holds a stale second chunk 1:1 (message used to be longer),
    # plus chunks for message 2 which no longer exists at all.
    vs = FakeVectorStore({"message_chunks": {"1:0", "1:1", "2:0", "2:1"}})

    backfill_message_chunks(store=store, vector_store=vs, embedder=FakeEmbedder())

    assert set(vs.deleted.get("message_chunks", [])) == {"1:1", "2:0", "2:1"}
    assert vs.ids["message_chunks"] == {"1:0"}
    store.close()


def test_backfill_all_forwards_the_explicit_fact_store_path(monkeypatch, tmp_path):
    vector_store = object()
    embedder = object()
    fact_store_db = tmp_path / "facts.db"
    captured: dict[str, object] = {}

    monkeypatch.setattr(embed_backfill, "_deps", lambda *_: (vector_store, embedder))

    def fake_backfill_holographic_facts(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(embed_backfill, "backfill_holographic_facts", fake_backfill_holographic_facts)
    for name in (
        "backfill_action_items",
        "backfill_deadlines",
        "backfill_calendar_events",
        "backfill_decisions",
        "backfill_thread_summaries",
        "backfill_message_chunks",
    ):
        monkeypatch.setattr(embed_backfill, name, lambda **_kwargs: 0)

    result = backfill_all(store=object(), fact_store_db_path=fact_store_db)
    assert result == {
        "holographic_facts": 0,
        "action_items": 0,
        "deadlines": 0,
        "calendar_events": 0,
        "decisions": 0,
        "thread_summaries": 0,
        "message_chunks": 0,
    }
    assert set(result) == set(COLLECTION_NAMES)
    assert captured["hologr_db_path"] == fact_store_db
    assert captured["vector_store"] is vector_store
    assert captured["embedder"] is embedder


def test_promotion_event_forwards_the_explicit_fact_store_path(monkeypatch, tmp_path):
    fact_store_db = tmp_path / "facts.db"
    captured: dict[str, object] = {}

    def fake_backfill_holographic_facts(**kwargs):
        captured.update(kwargs)
        return 4

    monkeypatch.setattr(
        incremental, "backfill_holographic_facts", fake_backfill_holographic_facts,
    )
    result = embed_for_pipeline_event(
        object(),
        event="promotion",
        vector_store=object(),
        embedder=object(),
        fact_store_db_path=fact_store_db,
    )

    assert result == {"holographic_facts": 4}
    assert captured["hologr_db_path"] == fact_store_db

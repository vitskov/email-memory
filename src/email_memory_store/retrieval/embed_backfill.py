"""Idempotent embedding backfill for retrieval source collections."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable

from .embedder import get_default_embedder
from .vector_store import COLLECTION_NAMES, get_default_store
from ..holographic import default_holographic_db_path
from ..store import EmailMemoryStore


def chunk_text(text: str, chunk_size: int = 1600, overlap: int = 200) -> list[str]:
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    step = chunk_size - overlap
    while start < len(text):
        chunk = text[start : start + chunk_size]
        if chunk:
            chunks.append(chunk)
        if start + chunk_size >= len(text):
            break
        start += step
    return chunks


def _iso(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _deps(vector_store, embedder):
    return (
        vector_store if vector_store is not None else get_default_store(),
        embedder if embedder is not None else get_default_embedder(),
    )


def _reconcile_deletions(
    vector_store,
    collection: str,
    existing: set[str],
    current_ids: set[str],
) -> int:
    """Delete vectors whose source row no longer exists (orphans), and return the count.

    Row deletions elsewhere in the pipeline — retention purges, duplicate
    collapse, and the delete-then-insert `replace_message_*` extraction paths —
    remove DB rows but never their vectors, since nothing else calls
    ``VectorStore.delete``. Left alone, retrieval keeps surfacing facts whose
    rows are gone and ``ask`` cites dangling handles. Reconciling here makes the
    backfill self-healing rather than append-only.

    Safety guard: if ``current_ids`` is empty while ``existing`` is not, treat it
    as a failed/empty source read rather than "every row was deleted", and prune
    nothing. Wiping a whole collection on a bad read is far worse than leaving a
    few orphans for the next run.
    """
    if not existing:
        return 0
    if not current_ids:
        print(
            f"[backfill] {collection}: skipped pruning — source returned 0 ids "
            f"(guard against wiping {len(existing)} vectors)",
            flush=True,
        )
        return 0
    orphans = existing - current_ids
    if not orphans:
        return 0
    vector_store.delete(collection, list(orphans))
    print(f"[backfill] {collection}: pruned {len(orphans)} orphaned vector(s)", flush=True)
    return len(orphans)


def _upsert_batches(vector_store, embedder, collection: str, items: list[tuple[str, str, dict]], batch_size: int) -> int:
    if collection not in COLLECTION_NAMES:
        raise ValueError(f"Unknown collection: {collection}")
    count = 0
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        ids = [item[0] for item in batch]
        documents = [item[1] for item in batch]
        metadatas = [item[2] for item in batch]
        embeddings = embedder.embed_documents(documents)
        vector_store.upsert(
            collection,
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=documents,
        )
        count += len(batch)
    return count


def backfill_holographic_facts(
    *,
    batch_size: int = 64,
    hologr_db_path: Path | None = None,
    vector_store=None,
    embedder=None,
    prune: bool = True,
) -> int:
    vector_store, embedder = _deps(vector_store, embedder)
    collection = "holographic_facts"
    existing = set(vector_store.existing_ids(collection))
    db_path = hologr_db_path if hologr_db_path is not None else default_holographic_db_path()
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT fact_id, content, category, tags FROM facts").fetchall()
    conn.close()
    items = [
        (
            str(fact_id),
            content,
            {"fact_id": fact_id, "category": category or "general", "tags": tags or ""},
        )
        for fact_id, content, category, tags in rows
        if content and str(fact_id) not in existing
    ]
    count = _upsert_batches(vector_store, embedder, collection, items, batch_size)
    if prune:
        _reconcile_deletions(vector_store, collection, existing, {str(r[0]) for r in rows})
    return count


def backfill_action_items(*, store: EmailMemoryStore, batch_size=64, vector_store=None, embedder=None, prune: bool = True) -> int:
    vector_store, embedder = _deps(vector_store, embedder)
    collection = "action_items"
    existing = set(vector_store.existing_ids(collection))
    rows = store.conn.execute(
        """
        SELECT action_item_id, thread_id, message_pk, owner, action_text, due_date, status
        FROM action_items
        """
    ).fetchall()
    items = [
        (
            str(action_item_id),
            action_text + (f" — owner: {owner}" if owner else ""),
            {
                "action_item_id": action_item_id,
                "thread_id": thread_id or 0,
                "message_pk": message_pk or 0,
                "owner": owner or "",
                "status": status or "",
                "due_date": _iso(due_date),
            },
        )
        for action_item_id, thread_id, message_pk, owner, action_text, due_date, status in rows
        if action_text and str(action_item_id) not in existing
    ]
    count = _upsert_batches(vector_store, embedder, collection, items, batch_size)
    if prune:
        _reconcile_deletions(vector_store, collection, existing, {str(r[0]) for r in rows})
    return count


def backfill_deadlines(*, store: EmailMemoryStore, batch_size=64, vector_store=None, embedder=None, prune: bool = True) -> int:
    vector_store, embedder = _deps(vector_store, embedder)
    collection = "deadlines"
    existing = set(vector_store.existing_ids(collection))
    rows = store.conn.execute(
        """
        SELECT deadline_id, thread_id, message_pk, label, due_date, related_project, status
        FROM deadlines
        """
    ).fetchall()
    items = [
        (
            str(deadline_id),
            label + (f" — project: {related_project}" if related_project else ""),
            {
                "deadline_id": deadline_id,
                "thread_id": thread_id or 0,
                "message_pk": message_pk or 0,
                "status": status or "",
                "due_date": _iso(due_date),
            },
        )
        for deadline_id, thread_id, message_pk, label, due_date, related_project, status in rows
        if label and str(deadline_id) not in existing
    ]
    count = _upsert_batches(vector_store, embedder, collection, items, batch_size)
    if prune:
        _reconcile_deletions(vector_store, collection, existing, {str(r[0]) for r in rows})
    return count


def _render_calendar_event_document(
    *,
    summary,
    description,
    organizer,
    organizer_email,
    location,
    status,
    method,
    starts_at,
    ends_at,
    attendees_json,
    uid,
) -> str:
    parts = []
    if summary:
        parts.append(summary)
    if description:
        parts.append(description)
    if organizer:
        parts.append(f"organizer: {organizer}")
    if organizer_email and organizer_email != organizer:
        parts.append(f"organizer email: {organizer_email}")
    if location:
        parts.append(f"location: {location}")
    if status:
        parts.append(f"status: {status}")
    if method:
        parts.append(f"method: {method}")
    if starts_at:
        parts.append(f"starts: {_iso(starts_at)}")
    if ends_at:
        parts.append(f"ends: {_iso(ends_at)}")
    if attendees_json:
        parts.append(f"attendees: {attendees_json}")
    if uid:
        parts.append(f"uid: {uid}")
    return " — ".join(str(part) for part in parts if part)


def backfill_calendar_events(*, store: EmailMemoryStore, batch_size=64, vector_store=None, embedder=None, prune: bool = True) -> int:
    vector_store, embedder = _deps(vector_store, embedder)
    collection = "calendar_events"
    existing = set(vector_store.existing_ids(collection))
    rows = store.conn.execute(
        """
        SELECT
            c.calendar_event_id,
            t.thread_id,
            m.thread_key,
            c.message_pk,
            c.summary,
            c.description,
            c.organizer,
            c.organizer_email,
            c.location,
            c.status,
            c.method,
            c.starts_at,
            c.ends_at,
            c.uid,
            c.attendees_json
        FROM calendar_events c
        LEFT JOIN messages m ON m.message_pk = c.message_pk
        LEFT JOIN threads t ON t.thread_key = m.thread_key AND t.account_id = m.account_id
        """
    ).fetchall()
    items = []
    for (
        calendar_event_id,
        thread_id,
        thread_key,
        message_pk,
        summary,
        description,
        organizer,
        organizer_email,
        location,
        status,
        method,
        starts_at,
        ends_at,
        uid,
        attendees_json,
    ) in rows:
        vector_id = str(calendar_event_id)
        if vector_id in existing:
            continue
        document = _render_calendar_event_document(
            summary=summary,
            description=description,
            organizer=organizer,
            organizer_email=organizer_email,
            location=location,
            status=status,
            method=method,
            starts_at=starts_at,
            ends_at=ends_at,
            attendees_json=attendees_json,
            uid=uid,
        )
        if not document:
            continue
        items.append(
            (
                vector_id,
                document,
                {
                    "calendar_event_id": calendar_event_id,
                    "thread_id": thread_id or 0,
                    "thread_key": thread_key or "",
                    "message_pk": message_pk or 0,
                    "uid": uid or "",
                    "status": status or "",
                    "method": method or "",
                    "organizer": organizer or "",
                    "organizer_email": organizer_email or "",
                    "location": location or "",
                    "starts_at": _iso(starts_at),
                    "ends_at": _iso(ends_at),
                },
            )
        )
    count = _upsert_batches(vector_store, embedder, collection, items, batch_size)
    if prune:
        _reconcile_deletions(vector_store, collection, existing, {str(r[0]) for r in rows})
    return count


def backfill_decisions(*, store: EmailMemoryStore, batch_size=64, vector_store=None, embedder=None, prune: bool = True) -> int:
    vector_store, embedder = _deps(vector_store, embedder)
    collection = "decisions"
    existing = set(vector_store.existing_ids(collection))
    rows = store.conn.execute(
        """
        SELECT decision_id, thread_id, title, decision_text, decided_at, status
        FROM decisions
        """
    ).fetchall()
    items = [
        (
            str(decision_id),
            (f"{title} — " if title else "") + decision_text,
            {
                "decision_id": decision_id,
                "thread_id": thread_id or 0,
                "status": status or "",
                "decided_at": _iso(decided_at),
            },
        )
        for decision_id, thread_id, title, decision_text, decided_at, status in rows
        if decision_text and str(decision_id) not in existing
    ]
    count = _upsert_batches(vector_store, embedder, collection, items, batch_size)
    if prune:
        _reconcile_deletions(vector_store, collection, existing, {str(r[0]) for r in rows})
    return count


def backfill_thread_summaries(*, store: EmailMemoryStore, batch_size=64, vector_store=None, embedder=None, prune: bool = True) -> int:
    vector_store, embedder = _deps(vector_store, embedder)
    collection = "thread_summaries"
    existing = set(vector_store.existing_ids(collection))
    rows = store.conn.execute(
        """
        SELECT summary_id, thread_id, summary_type, summary_text, generated_at
        FROM thread_summaries
        """
    ).fetchall()
    items = [
        (
            str(summary_id),
            summary_text,
            {
                "summary_id": summary_id,
                "thread_id": thread_id or 0,
                "summary_type": summary_type or "",
                "generated_at": _iso(generated_at),
            },
        )
        for summary_id, thread_id, summary_type, summary_text, generated_at in rows
        if summary_text and str(summary_id) not in existing
    ]
    count = _upsert_batches(vector_store, embedder, collection, items, batch_size)
    if prune:
        _reconcile_deletions(vector_store, collection, existing, {str(r[0]) for r in rows})
    return count


def backfill_message_chunks(*, store: EmailMemoryStore, batch_size=64, vector_store=None, embedder=None, prune: bool = True) -> int:
    vector_store, embedder = _deps(vector_store, embedder)
    collection = "message_chunks"
    existing = set(vector_store.existing_ids(collection))
    rows = store.conn.execute(
        """
        SELECT message_pk, thread_key, subject, from_addr, sent_at, cleaned_text
        FROM messages
        WHERE cleaned_text IS NOT NULL AND cleaned_text <> ''
        """
    ).fetchall()
    items = []
    current_ids: set[str] = set()
    for message_pk, thread_key, subject, from_addr, sent_at, cleaned_text in rows:
        chunks = chunk_text(cleaned_text)
        chunk_ids = [f"{message_pk}:{chunk_index}" for chunk_index in range(len(chunks))]
        # Every chunk this message *should* have, so reconciliation can prune
        # both deleted messages and chunks a re-bodied message no longer needs.
        current_ids.update(chunk_ids)
        if chunk_ids and all(chunk_id in existing for chunk_id in chunk_ids):
            continue
        for chunk_index, chunk in enumerate(chunks):
            vector_id = f"{message_pk}:{chunk_index}"
            if vector_id in existing:
                continue
            items.append(
                (
                    vector_id,
                    chunk,
                    {
                        "message_pk": message_pk,
                        "chunk_index": chunk_index,
                        "thread_key": thread_key,
                        "subject": subject or "",
                        "from_addr": from_addr or "",
                        "sent_at": _iso(sent_at),
                    },
                )
            )
    count = _upsert_batches(vector_store, embedder, collection, items, batch_size)
    if prune:
        _reconcile_deletions(vector_store, collection, existing, current_ids)
    return count


def backfill_all(
    *,
    store: EmailMemoryStore,
    batch_size=64,
    vector_store=None,
    embedder=None,
    prune: bool = True,
    fact_store_db_path: Path | None = None,
) -> dict[str, int]:
    vector_store, embedder = _deps(vector_store, embedder)
    counts = {}

    start = time.perf_counter()
    holographic_count = backfill_holographic_facts(
        batch_size=batch_size,
        hologr_db_path=fact_store_db_path,
        vector_store=vector_store,
        embedder=embedder,
        prune=prune,
    )
    elapsed = time.perf_counter() - start
    print(f"[backfill] holographic_facts: {holographic_count} new in {elapsed:.1f}s", flush=True)
    counts["holographic_facts"] = holographic_count

    funcs: list[tuple[str, Callable[..., int]]] = [
        ("action_items", backfill_action_items),
        ("deadlines", backfill_deadlines),
        ("decisions", backfill_decisions),
        ("thread_summaries", backfill_thread_summaries),
        ("message_chunks", backfill_message_chunks),
    ]
    for name, func in funcs:
        start = time.perf_counter()
        count = func(store=store, batch_size=batch_size, vector_store=vector_store, embedder=embedder, prune=prune)
        elapsed = time.perf_counter() - start
        print(f"[backfill] {name}: {count} new in {elapsed:.1f}s", flush=True)
        counts[name] = count
    return counts

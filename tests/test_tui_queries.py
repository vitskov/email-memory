"""Tests for email_memory_store.tui.queries."""
from __future__ import annotations

from pathlib import Path

from email_memory_store.store import EmailMemoryStore
from email_memory_store.tui import queries


def _make_store(tmp_path: Path) -> EmailMemoryStore:
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    return store


def _insert_thread(conn, subject: str = "Test Subject") -> int:
    """Insert a thread and return its thread_id."""
    conn.execute(
        """
        INSERT INTO threads (account_id, canonical_subject, thread_key,
            first_message_at, last_message_at, message_count, participant_count)
        VALUES (1, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1, 1)
        """,
        [subject, f"key_{subject}"],
    )
    row = conn.execute(
        "SELECT thread_id FROM threads WHERE canonical_subject = ? ORDER BY thread_id DESC LIMIT 1",
        [subject],
    ).fetchone()
    return row[0]


def _insert_action_item(conn, thread_id: int, action_text: str, status: str = "open", owner: str = "alice") -> int:
    conn.execute(
        """
        INSERT INTO action_items (thread_id, owner, action_text, status, confidence)
        VALUES (?, ?, ?, ?, 0.8)
        """,
        [thread_id, owner, action_text, status],
    )
    row = conn.execute(
        "SELECT action_item_id FROM action_items WHERE action_text = ? ORDER BY action_item_id DESC LIMIT 1",
        [action_text],
    ).fetchone()
    return row[0]


def _insert_deadline(conn, thread_id: int, label: str, status: str = "pending") -> int:
    conn.execute(
        """
        INSERT INTO deadlines (thread_id, label, status, confidence)
        VALUES (?, ?, ?, 0.9)
        """,
        [thread_id, label, status],
    )
    row = conn.execute(
        "SELECT deadline_id FROM deadlines WHERE label = ? ORDER BY deadline_id DESC LIMIT 1",
        [label],
    ).fetchone()
    return row[0]


def _insert_decision(conn, thread_id: int, title: str, status: str = "active") -> int:
    conn.execute(
        """
        INSERT INTO decisions (thread_id, title, decision_text, status, confidence)
        VALUES (?, ?, 'Some decision text', ?, 0.95)
        """,
        [thread_id, title, status],
    )
    row = conn.execute(
        "SELECT decision_id FROM decisions WHERE title = ? ORDER BY decision_id DESC LIMIT 1",
        [title],
    ).fetchone()
    return row[0]


def _insert_thread_summary(conn, thread_id: int, summary_type: str = "brief") -> int:
    conn.execute(
        """
        INSERT INTO thread_summaries (thread_id, summary_type, summary_text, source_message_count)
        VALUES (?, ?, 'Summary text here', 3)
        """,
        [thread_id, summary_type],
    )
    row = conn.execute(
        "SELECT summary_id FROM thread_summaries WHERE thread_id = ? ORDER BY summary_id DESC LIMIT 1",
        [thread_id],
    ).fetchone()
    return row[0]


def _insert_promotion(conn, promoted_text: str, status: str = "pending") -> int:
    conn.execute(
        """
        INSERT INTO promotion_log (source_object_type, source_object_id, promoted_text, status, promoted_category)
        VALUES ('action_items', '1', ?, ?, 'task')
        """,
        [promoted_text, status],
    )
    row = conn.execute(
        "SELECT promotion_id FROM promotion_log WHERE promoted_text = ? ORDER BY promotion_id DESC LIMIT 1",
        [promoted_text],
    ).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# action_items
# ---------------------------------------------------------------------------

def test_fetch_action_items_returns_all(tmp_path):
    store = _make_store(tmp_path)
    conn = store.conn
    tid = _insert_thread(conn, "Thread A")
    _insert_action_item(conn, tid, "Do task one")
    _insert_action_item(conn, tid, "Do task two")
    rows = queries.fetch_action_items(conn)
    assert len(rows) >= 2
    texts = [r["action_text"] for r in rows]
    assert "Do task one" in texts
    assert "Do task two" in texts
    store.close()


def test_fetch_action_items_search_filter(tmp_path):
    store = _make_store(tmp_path)
    conn = store.conn
    tid = _insert_thread(conn)
    _insert_action_item(conn, tid, "Write the report")
    _insert_action_item(conn, tid, "Schedule the meeting")
    rows = queries.fetch_action_items(conn, search="report")
    texts = [r["action_text"] for r in rows]
    assert any("report" in t.lower() for t in texts)
    assert not any("meeting" in t.lower() for t in texts)
    store.close()


def test_fetch_action_items_status_filter(tmp_path):
    store = _make_store(tmp_path)
    conn = store.conn
    tid = _insert_thread(conn)
    _insert_action_item(conn, tid, "Open task", status="open")
    _insert_action_item(conn, tid, "Done task", status="done")
    rows = queries.fetch_action_items(conn, status="done")
    assert all(r["status"] == "done" for r in rows)
    texts = [r["action_text"] for r in rows]
    assert "Done task" in texts
    assert "Open task" not in texts
    store.close()


# ---------------------------------------------------------------------------
# deadlines
# ---------------------------------------------------------------------------

def test_fetch_deadlines_returns_rows(tmp_path):
    store = _make_store(tmp_path)
    conn = store.conn
    tid = _insert_thread(conn, "Deadline Thread")
    _insert_deadline(conn, tid, "Submit form")
    _insert_deadline(conn, tid, "File taxes")
    rows = queries.fetch_deadlines(conn)
    labels = [r["label"] for r in rows]
    assert "Submit form" in labels
    assert "File taxes" in labels
    store.close()


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------

def test_fetch_decisions_returns_rows(tmp_path):
    store = _make_store(tmp_path)
    conn = store.conn
    tid = _insert_thread(conn, "Decision Thread")
    _insert_decision(conn, tid, "Adopt new stack")
    rows = queries.fetch_decisions(conn)
    titles = [r["title"] for r in rows]
    assert "Adopt new stack" in titles
    store.close()


# ---------------------------------------------------------------------------
# thread_summaries
# ---------------------------------------------------------------------------

def test_fetch_thread_summaries_joins_subject(tmp_path):
    store = _make_store(tmp_path)
    conn = store.conn
    tid = _insert_thread(conn, "My Important Thread")
    _insert_thread_summary(conn, tid, "brief")
    rows = queries.fetch_thread_summaries(conn)
    subjects = [r["thread_subject"] for r in rows]
    assert "My Important Thread" in subjects
    store.close()


# ---------------------------------------------------------------------------
# promotions
# ---------------------------------------------------------------------------

def test_fetch_promotions_returns_rows(tmp_path):
    store = _make_store(tmp_path)
    conn = store.conn
    _insert_promotion(conn, "Important fact to remember")
    rows = queries.fetch_promotions(conn)
    texts = [r["promoted_text"] for r in rows]
    assert "Important fact to remember" in texts
    store.close()


# ---------------------------------------------------------------------------
# fetch_thread_id_for_fact
# ---------------------------------------------------------------------------

def test_fetch_thread_id_for_fact_action_items(tmp_path):
    store = _make_store(tmp_path)
    conn = store.conn
    tid = _insert_thread(conn, "Action Thread")
    aid = _insert_action_item(conn, tid, "Retrieve me")
    result = queries.fetch_thread_id_for_fact(conn, "action_items", aid)
    assert result == tid
    store.close()


def test_fetch_thread_id_for_fact_deadlines(tmp_path):
    store = _make_store(tmp_path)
    conn = store.conn
    tid = _insert_thread(conn, "Deadline Thread 2")
    did = _insert_deadline(conn, tid, "Big deadline")
    result = queries.fetch_thread_id_for_fact(conn, "deadlines", did)
    assert result == tid
    store.close()


# ---------------------------------------------------------------------------
# list_status_values
# ---------------------------------------------------------------------------

def test_list_status_values(tmp_path):
    store = _make_store(tmp_path)
    conn = store.conn
    tid = _insert_thread(conn)
    _insert_action_item(conn, tid, "Task A", status="open")
    _insert_action_item(conn, tid, "Task B", status="done")
    _insert_action_item(conn, tid, "Task C", status="open")
    statuses = queries.list_status_values(conn, "action_items")
    assert "open" in statuses
    assert "done" in statuses
    # Should be sorted
    assert statuses == sorted(statuses)
    store.close()


# ---------------------------------------------------------------------------
# people (entity DB)
# ---------------------------------------------------------------------------

def _insert_person(
    entity_conn,
    canonical_name: str,
    organization_hint: str = "example.test",
    message_count: int = 1,
    emails: tuple[str, ...] = (),
    aliases: tuple[str, ...] = (),
) -> int:
    entity_conn.execute(
        """
        INSERT INTO people (canonical_name, normalized_name, organization_hint,
            disambiguation_status, email_count, message_count, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, 'unique', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        [canonical_name, canonical_name.lower(), organization_hint, len(emails), message_count],
    )
    person_id = entity_conn.execute(
        "SELECT person_id FROM people WHERE canonical_name = ? ORDER BY person_id DESC LIMIT 1",
        [canonical_name],
    ).fetchone()[0]
    for email in emails:
        entity_conn.execute(
            "INSERT INTO person_emails (person_id, email_address) VALUES (?, ?)",
            [person_id, email],
        )
    for alias in aliases:
        entity_conn.execute(
            "INSERT INTO person_aliases (person_id, alias_name, normalized_alias) VALUES (?, ?, ?)",
            [person_id, alias, alias.lower()],
        )
    return person_id


def test_fetch_people_aggregates_emails_in_one_row(tmp_path):
    store = _make_store(tmp_path)
    econn = store.entity_store.conn
    _insert_person(econn, "Example Person", emails=("person-a@example.test", "person-b@example.test"))

    rows = queries.fetch_people(econn)

    assert len(rows) == 1
    assert rows[0]["canonical_name"] == "Example Person"
    # Emails are aggregated in the same query, not fetched per row.
    assert rows[0]["emails"] == "person-a@example.test, person-b@example.test"
    store.close()


def test_fetch_people_orders_by_message_count(tmp_path):
    store = _make_store(tmp_path)
    econn = store.entity_store.conn
    _insert_person(econn, "Quiet Person", message_count=2)
    _insert_person(econn, "Frequent Person", message_count=99)

    rows = queries.fetch_people(econn)

    assert [r["canonical_name"] for r in rows] == ["Frequent Person", "Quiet Person"]
    store.close()


def test_fetch_people_search_matches_name_alias_and_email(tmp_path):
    store = _make_store(tmp_path)
    econn = store.entity_store.conn
    _insert_person(econn, "Example Contact", emails=("contact@sample.test",), aliases=("Sample Alias",))
    _insert_person(econn, "Unrelated Person", emails=("nobody@example.test",))

    assert [r["canonical_name"] for r in queries.fetch_people(econn, search="contact")] == ["Example Contact"]
    assert [r["canonical_name"] for r in queries.fetch_people(econn, search="Sample")] == ["Example Contact"]
    assert [r["canonical_name"] for r in queries.fetch_people(econn, search="sample.test")] == ["Example Contact"]
    assert queries.fetch_people(econn, search="zzz-no-match") == []
    store.close()


# ---------------------------------------------------------------------------
# calendar_events
# ---------------------------------------------------------------------------

def _insert_message(conn, thread_key: str, subject: str = "Msg") -> int:
    conn.execute(
        """
        INSERT INTO messages (account_id, folder_id, mailbox_message_id, stable_message_id,
            thread_key, subject, normalized_subject, from_addr, direction)
        VALUES (1, 1, ?, ?, ?, ?, ?, 'sender@example.test', 'incoming')
        """,
        [f"mid_{subject}", f"stable_{subject}", thread_key, subject, subject.lower()],
    )
    return conn.execute(
        "SELECT message_pk FROM messages WHERE stable_message_id = ?",
        [f"stable_{subject}"],
    ).fetchone()[0]


def _insert_calendar_event(conn, message_pk: int, summary: str, organizer: str = "Chair",
                           location: str = "Room 101") -> int:
    conn.execute(
        """
        INSERT INTO calendar_events (message_pk, summary, organizer, organizer_email,
            location, status, method, uid, starts_at, ends_at, raw_ics)
        VALUES (?, ?, ?, 'chair@example.test', ?, 'CONFIRMED', 'REQUEST', ?,
            TIMESTAMP '2026-05-01 10:00:00', TIMESTAMP '2026-05-01 11:00:00',
            'BEGIN:VCALENDAR\nEND:VCALENDAR')
        """,
        [message_pk, summary, organizer, location, f"uid_{summary}"],
    )
    return conn.execute(
        "SELECT calendar_event_id FROM calendar_events WHERE summary = ? ORDER BY calendar_event_id DESC LIMIT 1",
        [summary],
    ).fetchone()[0]


def test_fetch_calendar_events_resolves_thread_via_thread_key(tmp_path):
    store = _make_store(tmp_path)
    conn = store.conn
    tid = _insert_thread(conn, "Seminar Thread")
    # threads.thread_key is 'key_<subject>' per _insert_thread
    mpk = _insert_message(conn, thread_key="key_Seminar Thread", subject="Invite")
    _insert_calendar_event(conn, mpk, "Weekly Seminar")

    rows = queries.fetch_calendar_events(conn)

    assert len(rows) == 1
    assert rows[0]["summary"] == "Weekly Seminar"
    # messages joins threads on thread_key, not a thread_id column.
    assert rows[0]["thread_id"] == tid
    assert rows[0]["thread_subject"] == "Seminar Thread"
    store.close()


def test_fetch_calendar_events_search_matches_summary_organizer_location(tmp_path):
    store = _make_store(tmp_path)
    conn = store.conn
    _insert_thread(conn, "T")
    mpk = _insert_message(conn, thread_key="key_T", subject="Invite")
    _insert_calendar_event(conn, mpk, "Team Meeting", organizer="Example Organizer", location="Example Location")
    mpk2 = _insert_message(conn, thread_key="key_T", subject="Other")
    _insert_calendar_event(conn, mpk2, "Lunch", organizer="Nobody", location="Cafe")

    assert [r["summary"] for r in queries.fetch_calendar_events(conn, search="team")] == ["Team Meeting"]
    assert [r["summary"] for r in queries.fetch_calendar_events(conn, search="Organizer")] == ["Team Meeting"]
    assert [r["summary"] for r in queries.fetch_calendar_events(conn, search="Location")] == ["Team Meeting"]
    store.close()


def test_fetch_thread_id_for_fact_calendar_events(tmp_path):
    store = _make_store(tmp_path)
    conn = store.conn
    tid = _insert_thread(conn, "Cal Thread")
    mpk = _insert_message(conn, thread_key="key_Cal Thread", subject="Invite")
    eid = _insert_calendar_event(conn, mpk, "Drill Down Event")

    assert queries.fetch_thread_id_for_fact(conn, "calendar_events", eid) == tid
    store.close()


# ---------------------------------------------------------------------------
# pipeline health
# ---------------------------------------------------------------------------

def _insert_failure(conn, folder_name: str, failure_kind: str = "body_export",
                    status: str = "pending", error: str = "boom") -> None:
    conn.execute(
        """
        INSERT INTO failed_message_ingestions (account_name, folder_name, mailbox_message_id,
            stable_message_id, failure_kind, error, retry_count, status, first_failed_at, last_failed_at)
        VALUES ('primary-account', ?, ?, ?, ?, ?, 1, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        [folder_name, f"mid_{folder_name}_{failure_kind}", f"stable_{folder_name}_{failure_kind}",
         failure_kind, error, status],
    )


def _insert_sync_state(conn, folder_name: str, sync_kind: str, status: str = "complete") -> None:
    conn.execute(
        """
        INSERT INTO ingest_sync_state (account_name, folder_name, sync_kind,
            next_page, last_completed_page, status, last_run_at)
        VALUES ('primary-account', ?, ?, 3, 2, ?, CURRENT_TIMESTAMP)
        """,
        [folder_name, sync_kind, status],
    )


def test_fetch_pipeline_health_hides_resolved_failures_by_default(tmp_path):
    store = _make_store(tmp_path)
    conn = store.conn
    _insert_failure(conn, "INBOX", failure_kind="body_export", status="pending")
    _insert_failure(conn, "Archive", failure_kind="body_persist", status="resolved")

    rows = queries.fetch_pipeline_health(conn)
    failures = [r for r in rows if r["kind"] == "failure"]

    assert [f["folder_name"] for f in failures] == ["INBOX"]

    with_resolved = queries.fetch_pipeline_health(conn, include_resolved=True)
    assert sorted(f["folder_name"] for f in with_resolved if f["kind"] == "failure") == ["Archive", "INBOX"]
    store.close()


def test_fetch_pipeline_health_lists_both_envelope_and_body_sync_kinds(tmp_path):
    store = _make_store(tmp_path)
    conn = store.conn
    _insert_sync_state(conn, "INBOX", "nightly_envelopes")
    _insert_sync_state(conn, "INBOX", "nightly_bodies", status="pending")

    rows = queries.fetch_pipeline_health(conn)
    kinds = {r["detail"] for r in rows if r["kind"] == "sync"}

    # Envelope completion alone does not prove a folder is body-complete,
    # so both stages must be visible.
    assert kinds == {"nightly_envelopes", "nightly_bodies"}
    store.close()


def test_fetch_pipeline_health_unions_failures_and_sync_rows(tmp_path):
    store = _make_store(tmp_path)
    conn = store.conn
    _insert_failure(conn, "INBOX")
    _insert_sync_state(conn, "Archive", "initial_bodies")

    rows = queries.fetch_pipeline_health(conn)

    assert {r["kind"] for r in rows} == {"failure", "sync"}
    failure = next(r for r in rows if r["kind"] == "failure")
    sync = next(r for r in rows if r["kind"] == "sync")
    # Columns that only apply to one kind are NULL on the other.
    assert failure["error"] == "boom" and failure["next_page"] is None
    assert sync["next_page"] == 3 and sync["error"] is None
    store.close()


def test_fetch_pipeline_health_search_filters_both_kinds(tmp_path):
    store = _make_store(tmp_path)
    conn = store.conn
    _insert_failure(conn, "INBOX")
    _insert_failure(conn, "Archive")
    _insert_sync_state(conn, "INBOX", "nightly_bodies")
    _insert_sync_state(conn, "Archive", "nightly_bodies")

    rows = queries.fetch_pipeline_health(conn, search="INBOX")

    assert len(rows) == 2
    assert {r["folder_name"] for r in rows} == {"INBOX"}
    store.close()

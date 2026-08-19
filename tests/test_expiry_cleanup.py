"""Tests for principled cleanup of expired, purely time-tracking memories.

The mechanism removes deadlines and calendar events whose reference time has
passed beyond a user-set grace period, but preserves anything durable: promoted
facts, topic-anchored deadlines, and recurring events.
"""
from __future__ import annotations

from email_memory_store.store import EmailMemoryStore


def _store(tmp_path) -> EmailMemoryStore:
    store = EmailMemoryStore(tmp_path / "em")
    store.initialize()
    return store


def _add_deadline(store, deadline_id, label, days_from_today, *, related_project=None):
    store.conn.execute(
        """
        INSERT INTO deadlines (deadline_id, thread_id, message_pk, label, due_date, related_project, confidence, status)
        VALUES (?, NULL, NULL, ?, CURRENT_DATE + CAST(? AS INTEGER), ?, 0.9, 'open')
        """,
        [deadline_id, label, days_from_today, related_project],
    )


def _promote_deadline(store, deadline_id):
    store.conn.execute(
        """
        INSERT INTO promotion_log (source_object_type, source_object_id, promoted_text, status, promoted_category)
        VALUES ('deadline', ?, 'x', 'fact_store_written', 'deadline')
        """,
        [str(deadline_id)],
    )


def _add_action_item(store, action_item_id, text, days_from_today):
    """days_from_today=None -> undated task (must never be selected)."""
    if days_from_today is None:
        store.conn.execute(
            """
            INSERT INTO action_items (action_item_id, thread_id, message_pk, owner, action_text, due_date, confidence, status)
            VALUES (?, NULL, NULL, 'alice', ?, NULL, 0.9, 'open')
            """,
            [action_item_id, text],
        )
    else:
        store.conn.execute(
            """
            INSERT INTO action_items (action_item_id, thread_id, message_pk, owner, action_text, due_date, confidence, status)
            VALUES (?, NULL, NULL, 'alice', ?, CURRENT_DATE + CAST(? AS INTEGER), 0.9, 'open')
            """,
            [action_item_id, text, days_from_today],
        )


def _promote_action_item(store, action_item_id):
    store.conn.execute(
        """
        INSERT INTO promotion_log (source_object_type, source_object_id, promoted_text, status, promoted_category)
        VALUES ('action_item', ?, 'x', 'fact_store_written', 'task')
        """,
        [str(action_item_id)],
    )


def _add_event(store, event_id, summary, days_from_today, *, recurrence_rule=None):
    # calendar_events.message_pk is NOT NULL, so seed a parent message stub.
    store.conn.execute(
        """
        INSERT INTO messages (message_pk, account_id, folder_id, mailbox_message_id,
            stable_message_id, thread_key, subject, normalized_subject, from_addr, direction)
        VALUES (?, 1, 1, ?, ?, 'tk', 'S', 's', 'a@example.test', 'incoming')
        """,
        [900 + event_id, f"mid{event_id}", f"stable{event_id}"],
    )
    store.conn.execute(
        """
        INSERT INTO calendar_events (calendar_event_id, message_pk, summary, status, method, uid,
            starts_at, ends_at, recurrence_rule, raw_ics)
        VALUES (?, ?, ?, 'CONFIRMED', 'REQUEST', ?,
            CURRENT_TIMESTAMP + CAST(? AS INTEGER) * INTERVAL '1 day',
            CURRENT_TIMESTAMP + CAST(? AS INTEGER) * INTERVAL '1 day',
            ?, 'BEGIN:VCALENDAR\nEND:VCALENDAR')
        """,
        [event_id, 900 + event_id, summary, f"uid{event_id}", days_from_today, days_from_today, recurrence_rule],
    )


# ---------------------------------------------------------------------------
# grace config
# ---------------------------------------------------------------------------

def test_grace_defaults_to_one_year_and_is_settable(tmp_path):
    store = _store(tmp_path)
    assert store.get_expiry_grace_days() == 365
    assert store.set_expiry_grace_days(7) == 7
    assert store.get_expiry_grace_days() == 7
    store.close()


def test_grace_rejects_negative(tmp_path):
    store = _store(tmp_path)
    try:
        store.set_expiry_grace_days(-1)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for negative grace")
    store.close()


# ---------------------------------------------------------------------------
# selection: what is / isn't eligible
# ---------------------------------------------------------------------------

def test_grace_period_protects_recently_passed_deadlines(tmp_path):
    store = _store(tmp_path)
    store.set_expiry_grace_days(30)
    _add_deadline(store, 1, "long past", -40)     # beyond grace -> eligible
    _add_deadline(store, 2, "just passed", -5)    # inside grace -> kept
    _add_deadline(store, 3, "upcoming", +10)      # future -> kept

    sel = store.select_expired_time_anchors()
    assert sel["deadline_ids"] == [1]
    store.close()


def test_promoted_deadline_is_preserved(tmp_path):
    store = _store(tmp_path)
    _add_deadline(store, 1, "promoted, long past", -100)
    _promote_deadline(store, 1)
    _add_deadline(store, 2, "unpromoted, long past", -100)

    sel = store.select_expired_time_anchors(grace_days=0)
    assert sel["deadline_ids"] == [2]
    store.close()


def test_topic_anchored_deadline_is_preserved(tmp_path):
    store = _store(tmp_path)
    _add_deadline(store, 1, "milestone", -100, related_project="Sample project")
    _add_deadline(store, 2, "pure reminder", -100)

    sel = store.select_expired_time_anchors(grace_days=0)
    assert sel["deadline_ids"] == [2]
    store.close()


def test_undated_action_item_is_never_selected(tmp_path):
    store = _store(tmp_path)
    _add_action_item(store, 1, "someday task", None)      # no due_date -> never eligible
    _add_action_item(store, 2, "expired dated task", -100)
    sel = store.select_expired_time_anchors(grace_days=0)
    assert sel["action_item_ids"] == [2]
    store.close()


def test_promoted_action_item_is_preserved(tmp_path):
    store = _store(tmp_path)
    _add_action_item(store, 1, "expired promoted", -100)
    _promote_action_item(store, 1)
    _add_action_item(store, 2, "expired unpromoted", -100)
    sel = store.select_expired_time_anchors(grace_days=0)
    assert sel["action_item_ids"] == [2]
    store.close()


def test_action_item_grace_protects_recent(tmp_path):
    store = _store(tmp_path)
    _add_action_item(store, 1, "long past", -40)
    _add_action_item(store, 2, "recent", -5)
    _add_action_item(store, 3, "future", +10)
    sel = store.select_expired_time_anchors(grace_days=30)
    assert sel["action_item_ids"] == [1]
    store.close()


def test_recurring_event_is_preserved_but_oneoff_is_eligible(tmp_path):
    store = _store(tmp_path)
    _add_event(store, 1, "weekly standup", -100, recurrence_rule="FREQ=WEEKLY")
    _add_event(store, 2, "one-off talk", -100)
    _add_event(store, 3, "upcoming", +5)

    sel = store.select_expired_time_anchors(grace_days=0)
    assert sel["calendar_event_ids"] == [2]
    store.close()


# ---------------------------------------------------------------------------
# cleanup: dry-run vs apply
# ---------------------------------------------------------------------------

def test_dry_run_deletes_nothing(tmp_path):
    store = _store(tmp_path)
    _add_deadline(store, 1, "past", -100)
    _add_action_item(store, 5, "past task", -100)
    _add_event(store, 2, "past", -100)

    result = store.cleanup_expired_time_anchors(grace_days=0, dry_run=True)
    assert result["deadlines_matched"] == 1
    assert result["action_items_matched"] == 1
    assert result["calendar_events_matched"] == 1
    assert result["deadlines_deleted"] == 0
    assert result["action_items_deleted"] == 0
    assert result["calendar_events_deleted"] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM deadlines").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM action_items").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0] == 1
    store.close()


def test_apply_deletes_only_eligible_and_returns_ids(tmp_path):
    store = _store(tmp_path)
    _add_deadline(store, 1, "expired reminder", -100)
    _add_deadline(store, 2, "promoted", -100)
    _promote_deadline(store, 2)
    _add_deadline(store, 3, "topical", -100, related_project="grant")
    _add_deadline(store, 4, "future", +10)
    _add_event(store, 5, "expired one-off", -100)
    _add_event(store, 6, "recurring", -100, recurrence_rule="FREQ=MONTHLY")
    _add_action_item(store, 7, "expired dated task", -100)
    _add_action_item(store, 8, "undated task", None)
    _add_action_item(store, 9, "promoted expired task", -100)
    _promote_action_item(store, 9)

    result = store.cleanup_expired_time_anchors(grace_days=0, dry_run=False)

    assert result["deadlines_deleted"] == 1
    assert result["deleted_deadline_ids"] == [1]
    assert result["action_items_deleted"] == 1
    assert result["deleted_action_item_ids"] == [7]
    assert result["calendar_events_deleted"] == 1
    # Durable and future rows survive.
    remaining_dl = {r[0] for r in store.conn.execute("SELECT deadline_id FROM deadlines").fetchall()}
    assert remaining_dl == {2, 3, 4}
    remaining_ev = {r[0] for r in store.conn.execute("SELECT calendar_event_id FROM calendar_events").fetchall()}
    assert remaining_ev == {6}
    # Undated task (8) and promoted task (9) survive; only the plain expired one (7) went.
    remaining_ai = {r[0] for r in store.conn.execute("SELECT action_item_id FROM action_items").fetchall()}
    assert remaining_ai == {8, 9}
    store.close()

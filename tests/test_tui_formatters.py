"""Tests for email_memory_store.tui.formatters."""
from __future__ import annotations

import json
from datetime import datetime

from email_memory_store.tui.formatters import (
    format_confidence,
    format_date,
    format_detail,
    format_json_list,
    truncate,
)


# ---------------------------------------------------------------------------
# truncate
# ---------------------------------------------------------------------------

def test_truncate_short_text_unchanged():
    assert truncate("hello", 10) == "hello"


def test_truncate_exact_length_unchanged():
    assert truncate("hello", 5) == "hello"


def test_truncate_long_text_ellipsis():
    result = truncate("hello world", 8)
    assert len(result) == 8
    assert result.endswith("\u2026")


def test_truncate_none():
    assert truncate(None, 10) == ""


# ---------------------------------------------------------------------------
# format_date
# ---------------------------------------------------------------------------

def test_format_date_datetime():
    dt = datetime(2024, 3, 15, 10, 30, 0)
    assert format_date(dt) == "2024-03-15"


def test_format_date_none():
    assert format_date(None) == ""


def test_format_date_string_iso():
    assert format_date("2025-06-01T12:00:00") == "2025-06-01"


def test_format_date_string_date_only():
    assert format_date("2025-06-01") == "2025-06-01"


# ---------------------------------------------------------------------------
# format_confidence
# ---------------------------------------------------------------------------

def test_format_confidence():
    assert format_confidence(0.87) == "87%"


def test_format_confidence_zero():
    assert format_confidence(0.0) == "0%"


def test_format_confidence_one():
    assert format_confidence(1.0) == "100%"


def test_format_confidence_none():
    assert format_confidence(None) == ""


# ---------------------------------------------------------------------------
# format_json_list
# ---------------------------------------------------------------------------

def test_format_json_list_valid():
    result = format_json_list(json.dumps(["point one", "point two"]), "Key Points")
    assert "Key Points" in result
    assert "point one" in result
    assert "point two" in result
    assert "- " in result


def test_format_json_list_none():
    result = format_json_list(None, "Items")
    assert result == ""


def test_format_json_list_empty_array():
    result = format_json_list(json.dumps([]), "Items")
    assert result == ""


def test_format_json_list_python_list():
    result = format_json_list(["a", "b", "c"], "My Label")
    assert "My Label" in result
    assert "- a" in result


# ---------------------------------------------------------------------------
# format_detail
# ---------------------------------------------------------------------------

def test_format_detail_action_item():
    row = {
        "thread_subject": "Project Alpha",
        "owner": "bob@example.test",
        "action_text": "Send the final report",
        "due_date": datetime(2025, 5, 1),
        "status": "open",
        "confidence": 0.9,
        "extracted_at": None,
    }
    result = format_detail(row, "action_items")
    assert "Project Alpha" in result
    assert "bob@example.test" in result
    assert "Send the final report" in result
    assert "2025-05-01" in result
    assert "90%" in result


def test_format_detail_decision():
    row = {
        "thread_subject": "Engineering Meeting",
        "title": "Use Python 3.12",
        "decision_text": "We decided to upgrade to Python 3.12.",
        "decided_by_json": json.dumps(["alice", "bob"]),
        "decided_at": "2025-04-01",
        "status": "active",
        "confidence": 0.95,
        "superseded_by": None,
    }
    result = format_detail(row, "decisions")
    assert "Engineering Meeting" in result
    assert "Use Python 3.12" in result
    assert "We decided to upgrade" in result
    assert "alice" in result
    assert "95%" in result


def test_format_detail_thread_summary():
    row = {
        "thread_subject": "Weekly Sync",
        "summary_type": "brief",
        "summary_text": "The team discussed project status.",
        "key_points_json": json.dumps(["Status update", "Next steps"]),
        "action_items_json": None,
        "decisions_json": None,
        "deadlines_json": None,
        "participants_json": None,
        "generated_at": datetime(2025, 4, 10),
        "source_message_count": 5,
    }
    result = format_detail(row, "thread_summaries")
    assert "Weekly Sync" in result
    assert "The team discussed project status." in result
    assert "Status update" in result
    assert "2025-04-10" in result
    assert "5" in result


def test_format_detail_promotion():
    row = {
        "candidate_type": "action_items",
        "promoted_category": "task",
        "status": "pending",
        "created_at": "2025-01-01",
        "promoted_text": "Remember to call Alice",
        "promoted_tags": "urgent",
    }
    result = format_detail(row, "promotions")
    assert "Remember to call Alice" in result
    assert "task" in result
    assert "pending" in result

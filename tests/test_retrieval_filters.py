"""Tests for Stage 4 retrieval filter helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from email_memory_store.retrieval.filters import (
    RetrievalFilters,
    combine_filters,
    parse_natural_date_range,
)


UTC = timezone.utc


def test_chroma_where_uses_collection_specific_thread_metadata() -> None:
    assert RetrievalFilters().chroma_where("holographic_facts") is None
    assert RetrievalFilters(thread_id=42).chroma_where("action_items") == {
        "thread_id": {"$eq": 42}
    }
    assert RetrievalFilters(thread_id=42).chroma_where("holographic_facts") is None
    assert RetrievalFilters(thread_id=42).chroma_where("message_chunks") is None
    assert RetrievalFilters(thread_key="abc").chroma_where("message_chunks") == {
        "thread_key": {"$eq": "abc"}
    }
    assert RetrievalFilters(thread_key="abc").chroma_where("action_items") is None
    assert RetrievalFilters(thread_id=42, thread_key="abc").chroma_where("action_items") == {
        "thread_id": {"$eq": 42}
    }


def test_date_filter_detection_is_collection_aware() -> None:
    assert RetrievalFilters().has_date_filter("holographic_facts") is False
    assert RetrievalFilters().has_date_filter("action_items") is False
    assert (
        RetrievalFilters(date_from=datetime(2026, 4, 1, tzinfo=UTC)).has_date_filter(
            "action_items"
        )
        is True
    )


def test_post_filter_applies_date_bounds_when_collection_has_date_field() -> None:
    assert RetrievalFilters().post_filter("holographic_facts", {}) is True
    assert RetrievalFilters().post_filter("action_items", {}) is True
    assert (
        RetrievalFilters(date_from=datetime(2026, 4, 1, tzinfo=UTC)).post_filter(
            "action_items", {}
        )
        is False
    )

    april_filter = RetrievalFilters(
        date_from=datetime(2026, 4, 1, tzinfo=UTC),
        date_to=datetime(2026, 4, 30, tzinfo=UTC),
    )
    assert (
        april_filter.post_filter(
            "action_items", {"due_date": "2026-04-15T00:00:00+00:00"}
        )
        is True
    )
    assert (
        RetrievalFilters(date_from=datetime(2026, 4, 1, tzinfo=UTC)).post_filter(
            "action_items", {"due_date": "2026-03-15T00:00:00+00:00"}
        )
        is False
    )


def test_parse_natural_date_range_returns_none_when_no_date_phrase() -> None:
    assert parse_natural_date_range("just give me everything") == (None, None)


def test_parse_natural_date_range_handles_relative_day_phrases() -> None:
    now = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)

    assert parse_natural_date_range("today", now=now) == (
        datetime(2026, 4, 29, 0, 0, tzinfo=UTC),
        datetime(2026, 4, 29, 23, 59, 59, tzinfo=UTC),
    )
    assert parse_natural_date_range("yesterday", now=now) == (
        datetime(2026, 4, 28, 0, 0, tzinfo=UTC),
        datetime(2026, 4, 28, 23, 59, 59, tzinfo=UTC),
    )


def test_parse_natural_date_range_handles_week_and_rolling_days() -> None:
    now = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)

    assert parse_natural_date_range("this week", now=now) == (
        datetime(2026, 4, 27, 0, 0, tzinfo=UTC),
        datetime(2026, 5, 3, 23, 59, 59, tzinfo=UTC),
    )
    assert parse_natural_date_range("last 7 days", now=now) == (
        now - timedelta(days=7),
        now,
    )


def test_parse_natural_date_range_handles_explicit_year() -> None:
    assert parse_natural_date_range("in 2024") == (
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC),
    )


def test_combine_filters_prefers_non_none_override_values() -> None:
    assert combine_filters(None, None) == RetrievalFilters()
    assert combine_filters(
        RetrievalFilters(thread_id=1), RetrievalFilters(thread_id=2)
    ) == RetrievalFilters(thread_id=2)
    assert combine_filters(RetrievalFilters(thread_id=1), RetrievalFilters()) == (
        RetrievalFilters(thread_id=1)
    )

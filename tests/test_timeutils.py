from __future__ import annotations

from datetime import datetime, timezone

from email_memory_store.timeutils import normalize_timestamp


def test_normalize_timestamp_preserves_seconds_before_timezone_offset() -> None:
    assert normalize_timestamp("2026-04-15T09:30:45+00:00") == datetime(
        2026, 4, 15, 9, 30, 45, tzinfo=timezone.utc
    )


def test_normalize_timestamp_accepts_minutes_before_timezone_offset() -> None:
    assert normalize_timestamp("2026-04-15T09:30+00:00") == datetime(
        2026, 4, 15, 9, 30, tzinfo=timezone.utc
    )

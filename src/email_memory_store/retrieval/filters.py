"""Typed retrieval filters and natural-language date range helpers."""

from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


DATE_FIELD_BY_COLLECTION: dict[str, str | None] = {
    "holographic_facts": None,
    "action_items": "due_date",
    "deadlines": "due_date",
    "calendar_events": "starts_at",
    "decisions": "decided_at",
    "thread_summaries": "generated_at",
    "message_chunks": "sent_at",
}

_THREAD_ID_COLLECTIONS = frozenset(
    collection
    for collection in DATE_FIELD_BY_COLLECTION
    if collection not in {"holographic_facts", "message_chunks"}
)
_UTC = timezone.utc


@dataclass(frozen=True)
class RetrievalFilters:
    date_from: datetime | None = None
    date_to: datetime | None = None
    thread_id: int | None = None
    thread_key: str | None = None

    def chroma_where(self, collection: str) -> dict[str, Any] | None:
        clauses: list[dict[str, Any]] = []
        if collection in _THREAD_ID_COLLECTIONS and self.thread_id is not None:
            clauses.append({"thread_id": {"$eq": self.thread_id}})
        if collection == "message_chunks" and self.thread_key is not None:
            clauses.append({"thread_key": {"$eq": self.thread_key}})
        if not clauses:
            return None
        if len(clauses) == 1:
            return clauses[0]
        return {"$and": clauses}

    def has_date_filter(self, collection: str) -> bool:
        date_field = DATE_FIELD_BY_COLLECTION.get(collection)
        if not date_field:
            return False
        return self.date_from is not None or self.date_to is not None

    def post_filter(self, collection: str, metadata: dict[str, Any]) -> bool:
        date_field = DATE_FIELD_BY_COLLECTION.get(collection)
        if not date_field:
            return True
        if self.date_from is None and self.date_to is None:
            return True
        raw = metadata.get(date_field)
        if not raw:
            return False
        if self.date_from is not None and raw < self.date_from.isoformat():
            return False
        if self.date_to is not None and raw > self.date_to.isoformat():
            return False
        return True

    def applies_to(self, collection: str) -> bool:
        if self.chroma_where(collection) is not None:
            return True
        return self.has_date_filter(collection)


def parse_natural_date_range(
    query: str, *, now: datetime | None = None
) -> tuple[datetime | None, datetime | None]:
    base = _as_utc(now or datetime.now(_UTC))
    normalized_query = query.lower()

    if _contains_phrase(normalized_query, "today"):
        return _day_bounds(base)
    if _contains_phrase(normalized_query, "yesterday"):
        return _day_bounds(base - timedelta(days=1))
    if _contains_any_phrase(normalized_query, ("this week", "current week")):
        return _week_bounds(base)
    if _contains_any_phrase(normalized_query, ("last week", "past week", "previous week")):
        return _week_bounds(base - timedelta(weeks=1))
    if _contains_any_phrase(normalized_query, ("this month", "current month")):
        return _month_bounds(base.year, base.month)
    if _contains_any_phrase(normalized_query, ("last month", "past month", "previous month")):
        return _previous_month_bounds(base)
    if _contains_phrase(normalized_query, "this year"):
        return _year_bounds(base.year)

    last_days_match = re.search(r"\b(?:last|past)\s+([1-9]\d*)\s+days\b", normalized_query)
    if last_days_match:
        days = int(last_days_match.group(1))
        return base - timedelta(days=days), base

    year_match = re.search(r"\bin\s+(\d{4})\b", normalized_query)
    if year_match:
        return _year_bounds(int(year_match.group(1)))

    return None, None


def combine_filters(
    base: RetrievalFilters | None, override: RetrievalFilters | None
) -> RetrievalFilters:
    return RetrievalFilters(
        date_from=_prefer_override("date_from", base, override),
        date_to=_prefer_override("date_to", base, override),
        thread_id=_prefer_override("thread_id", base, override),
        thread_key=_prefer_override("thread_key", base, override),
    )


def _prefer_override(
    field_name: str, base: RetrievalFilters | None, override: RetrievalFilters | None
) -> Any:
    if override is not None:
        override_value = getattr(override, field_name)
        if override_value is not None:
            return override_value
    if base is not None:
        return getattr(base, field_name)
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=_UTC)
    return value.astimezone(_UTC)


def _contains_phrase(query: str, phrase: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}\b", query) is not None


def _contains_any_phrase(query: str, phrases: tuple[str, ...]) -> bool:
    return any(_contains_phrase(query, phrase) for phrase in phrases)


def _day_bounds(value: datetime) -> tuple[datetime, datetime]:
    start = value.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start.replace(hour=23, minute=59, second=59)


def _week_bounds(value: datetime) -> tuple[datetime, datetime]:
    start = (value - timedelta(days=value.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start, start + timedelta(days=6, hours=23, minutes=59, seconds=59)


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=_UTC)
    last_day = monthrange(year, month)[1]
    end = datetime(year, month, last_day, 23, 59, 59, tzinfo=_UTC)
    return start, end


def _previous_month_bounds(value: datetime) -> tuple[datetime, datetime]:
    if value.month == 1:
        return _month_bounds(value.year - 1, 12)
    return _month_bounds(value.year, value.month - 1)


def _year_bounds(year: int) -> tuple[datetime, datetime]:
    return (
        datetime(year, 1, 1, tzinfo=_UTC),
        datetime(year, 12, 31, 23, 59, 59, tzinfo=_UTC),
    )

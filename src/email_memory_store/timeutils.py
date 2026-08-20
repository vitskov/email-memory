from __future__ import annotations

from datetime import datetime
from typing import Literal, overload


def normalize_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.strip())


@overload
def normalize_date_only(value: str | None, as_datetime: Literal[False] = False) -> str | None: ...


@overload
def normalize_date_only(value: str | None, as_datetime: Literal[True]) -> datetime | None: ...


def normalize_date_only(value: str | None, as_datetime: bool = False) -> str | datetime | None:
    if not value:
        return None
    text = value.strip()
    normalized = datetime.fromisoformat(text).date().isoformat()
    if as_datetime:
        return datetime.fromisoformat(normalized)
    return normalized

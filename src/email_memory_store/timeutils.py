from __future__ import annotations

from datetime import datetime
from typing import Literal, overload


def normalize_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if len(text) >= 6 and (text[-6] in ['+', '-']) and text[-3] == ':':
        text = text[:-6] + ':00' + text[-6:]
    return datetime.fromisoformat(text)


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

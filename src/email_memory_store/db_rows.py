from __future__ import annotations

from typing import Protocol, TypeVar


RowT = TypeVar("RowT")
ValueT = TypeVar("ValueT", covariant=True)


class _IndexableRow(Protocol[ValueT]):
    def __getitem__(self, index: int, /) -> ValueT: ...


def require_row(row: RowT | None, *, operation: str) -> RowT:
    """Return a query row or fail explicitly when an invariant is broken."""
    if row is None:
        raise RuntimeError(f"database query returned no row: {operation}")
    return row


def require_scalar(row: _IndexableRow[ValueT] | None, *, operation: str) -> ValueT:
    """Return the first column from a query that must produce one row."""
    return require_row(row, operation=operation)[0]

import pytest

from email_memory_store.db_rows import require_row, require_scalar


def test_require_row_preserves_present_row() -> None:
    row = (7, "value")

    assert require_row(row, operation="load test row") is row


def test_require_row_reports_impossible_missing_row() -> None:
    with pytest.raises(
        RuntimeError, match="database query returned no row: load test row"
    ):
        require_row(None, operation="load test row")


def test_require_scalar_returns_first_column() -> None:
    assert require_scalar((7,), operation="count test rows") == 7

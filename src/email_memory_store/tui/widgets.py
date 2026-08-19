"""TUI widgets for the email-memory-store browser."""
from __future__ import annotations

from textual.message import Message
from textual.widgets import DataTable

from .formatters import format_date, format_confidence, truncate


class FactTable(DataTable):
    """DataTable subclass that stores the full row dicts alongside display rows."""

    class ShowThread(Message):
        """Posted when the user presses 't' on a row."""
        def __init__(self, row_dict: dict, tab_key: str) -> None:
            super().__init__()
            self.row_dict = row_dict
            self.tab_key = tab_key

    BINDINGS = [("t", "show_thread", "Thread")]

    # Per-tab column specs: tab_key -> list of (col_name, display_name, width)
    COLUMN_SPECS: dict[str, list[tuple[str, str, int]]] = {
        'all': [
            ('fact_type', 'Type', 10),
            ('text', 'Text', 50),
            ('date', 'Date', 12),
            ('status', 'Status', 10),
            ('confidence', 'Conf', 6),
            ('thread_subject', 'Thread', 30),
        ],
        'action_items': [
            ('owner', 'Owner', 18),
            ('action_text', 'Action', 45),
            ('due_date', 'Due', 12),
            ('status', 'Status', 10),
            ('confidence', 'Conf', 6),
        ],
        'deadlines': [
            ('label', 'Label', 25),
            ('due_date', 'Due', 12),
            ('related_project', 'Project', 22),
            ('status', 'Status', 10),
            ('confidence', 'Conf', 6),
        ],
        'decisions': [
            ('title', 'Title', 28),
            ('decided_at', 'Date', 12),
            ('status', 'Status', 10),
            ('confidence', 'Conf', 6),
        ],
        'thread_summaries': [
            ('thread_subject', 'Thread', 35),
            ('summary_type', 'Type', 14),
            ('last_message_at', 'Date', 14),
            ('source_message_count', 'Msgs', 5),
        ],
        'promotions': [
            ('candidate_type', 'Type', 16),
            ('promoted_category', 'Category', 14),
            ('status', 'Status', 16),
            ('created_at', 'Date', 14),
        ],
        'people': [
            ('canonical_name', 'Name', 28),
            ('organization_hint', 'Org', 22),
            ('emails', 'Emails', 34),
            ('message_count', 'Msgs', 6),
            ('disambiguation_status', 'Ident', 12),
            ('last_seen_at', 'Last Seen', 14),
        ],
        'calendar_events': [
            ('summary', 'Event', 34),
            ('starts_at', 'Starts', 14),
            ('ends_at', 'Ends', 14),
            ('organizer', 'Organizer', 22),
            ('location', 'Location', 20),
            ('status', 'Status', 10),
        ],
        'pipeline_health': [
            ('kind', 'Kind', 8),
            ('folder_name', 'Folder', 26),
            ('detail', 'Detail', 20),
            ('status', 'Status', 12),
            ('error', 'Error', 40),
            ('last_activity_at', 'Last', 14),
        ],
    }

    # Columns that should be formatted as dates
    _DATE_COLS = {
        'due_date', 'decided_at', 'generated_at', 'extracted_at', 'created_at',
        'updated_at', 'promoted_at', 'starts_at', 'ends_at', 'first_seen_at',
        'last_seen_at', 'last_activity_at',
    }
    # Columns that should be formatted as confidence
    _CONF_COLS = {'confidence'}

    def __init__(self, tab_key: str, **kwargs):
        kwargs.setdefault("cursor_type", "row")
        super().__init__(**kwargs)
        self._tab_key = tab_key
        self._row_dicts: list[dict] = []
        self._sort_col: str | None = None
        self._sort_asc: bool = True

    def setup_columns(self) -> None:
        """Call once after mounting to add columns."""
        specs = self.COLUMN_SPECS.get(self._tab_key, [])
        for col_name, display_name, width in specs:
            self.add_column(display_name, width=width, key=col_name)

    def load_rows(self, rows: list[dict]) -> None:
        """Clear and repopulate. Reapplies current sort if one is active."""
        self._row_dicts = list(rows)
        if self._sort_col:
            self._apply_sort()
        else:
            self._render_rows()

    def _sort_key(self, row: dict):
        v = row.get(self._sort_col)
        if v is None:
            return (1, "")
        return (0, str(v).lower())

    def _apply_sort(self) -> None:
        self._row_dicts.sort(key=self._sort_key, reverse=not self._sort_asc)
        self._render_rows()

    def _render_rows(self) -> None:
        self.clear()
        specs = self.COLUMN_SPECS.get(self._tab_key, [])
        for row_dict in self._row_dicts:
            cells = []
            for col_name, _display_name, width in specs:
                value = row_dict.get(col_name)
                if col_name in self._DATE_COLS:
                    cell = format_date(value)
                elif col_name in self._CONF_COLS:
                    cell = format_confidence(value)
                else:
                    cell = truncate(str(value) if value is not None else "", width)
                cells.append(cell)
            self.add_row(*cells)

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        col = event.column_key.value
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc  # toggle direction
        else:
            self._sort_col = col
            self._sort_asc = True
        self._apply_sort()

    def focused_row_dict(self) -> dict | None:
        """Return the full dict for the currently highlighted row."""
        if not self._row_dicts:
            return None
        row_index = self.cursor_row
        if row_index < 0 or row_index >= len(self._row_dicts):
            return None
        return self._row_dicts[row_index]

    def action_show_thread(self) -> None:
        row = self.focused_row_dict()
        if row is not None:
            self.post_message(self.ShowThread(row, self._tab_key))

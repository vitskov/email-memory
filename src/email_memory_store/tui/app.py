"""BrowserApp — Textual TUI browser for email-memory-store."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Input, TabbedContent, TabPane

from .widgets import FactTable
from . import queries


class BrowserApp(App):
    CSS_PATH = "app.tcss"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("j,down", "cursor_down", "Down"),
        ("k,up", "cursor_up", "Up"),
        ("slash", "focus_search", "Search"),
        ("question_mark", "semantic_search", "Sem-Search"),
        ("colon", "ask", "Ask"),
        ("escape", "escape", "Back/Clear"),
    ]

    _search_text: reactive[str] = reactive("", init=False)

    def __init__(self, store, *, vector_store=None):
        super().__init__()
        self._store = store
        self._vector_store = vector_store

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Search by keyword, name, or email  (/ to focus, Esc to clear)", id="search-input")
        with TabbedContent(initial="all"):
            with TabPane("All", id="all"):
                yield FactTable("all", id="table_all")
            with TabPane("Action Items", id="action_items"):
                yield FactTable("action_items", id="table_action_items")
            with TabPane("Deadlines", id="deadlines"):
                yield FactTable("deadlines", id="table_deadlines")
            with TabPane("Decisions", id="decisions"):
                yield FactTable("decisions", id="table_decisions")
            with TabPane("Summaries", id="thread_summaries"):
                yield FactTable("thread_summaries", id="table_thread_summaries")
            with TabPane("Promoted", id="promotions"):
                yield FactTable("promotions", id="table_promotions")
            with TabPane("People", id="people"):
                yield FactTable("people", id="table_people")
            with TabPane("Calendar", id="calendar_events"):
                yield FactTable("calendar_events", id="table_calendar_events")
            with TabPane("Health", id="pipeline_health"):
                yield FactTable("pipeline_health", id="table_pipeline_health")
        yield Footer()

    def on_mount(self) -> None:
        for tab_key in FactTable.COLUMN_SPECS:
            table_id = f"table_{tab_key}"
            try:
                table = self.query_one(f"#{table_id}", FactTable)
                table.setup_columns()
            except Exception:
                pass
        self._load_active_tab()

    def on_tabbed_content_tab_activated(self, event) -> None:
        self._load_active_tab()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._search_text = event.value.strip()
        self._load_active_tab()
        try:
            self._active_table().focus()
        except Exception:
            pass

    def _load_active_tab(self) -> None:
        try:
            tc = self.query_one(TabbedContent)
            tab_id = tc.active
            if not tab_id:
                return
            table = self._active_table()
            rows = self._fetch_rows(tab_id)
            table.load_rows(rows)
            table.focus()
        except Exception:
            pass

    def _fetch_rows(self, tab_id: str) -> list[dict]:
        conn = self._store.conn
        s = self._search_text or None
        if tab_id == "all":
            return queries.fetch_all_facts(conn, search=s)
        elif tab_id == "action_items":
            return queries.fetch_action_items(conn, search=s)
        elif tab_id == "deadlines":
            return queries.fetch_deadlines(conn, search=s)
        elif tab_id == "decisions":
            return queries.fetch_decisions(conn, search=s)
        elif tab_id == "thread_summaries":
            return queries.fetch_thread_summaries(conn, search=s)
        elif tab_id == "promotions":
            return queries.fetch_promotions(conn, search=s)
        elif tab_id == "people":
            # People live in the companion entity DB, not the email one.
            return queries.fetch_people(self._store.entity_store.conn, search=s)
        elif tab_id == "calendar_events":
            return queries.fetch_calendar_events(conn, search=s)
        elif tab_id == "pipeline_health":
            return queries.fetch_pipeline_health(conn, search=s)
        return []

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_semantic_search(self) -> None:
        from .screens import SemanticSearchScreen
        self.push_screen(SemanticSearchScreen(vector_store=self._vector_store))

    def action_ask(self) -> None:
        from .screens import AskScreen
        self.push_screen(AskScreen(vector_store=self._vector_store))

    def action_cursor_down(self) -> None:
        try:
            self._active_table().action_cursor_down()
        except Exception:
            pass

    def action_cursor_up(self) -> None:
        try:
            self._active_table().action_cursor_up()
        except Exception:
            pass

    def action_escape(self) -> None:
        # If a modal is open, close it
        if len(self.screen_stack) > 1:
            self.pop_screen()
            return
        # If search is active, clear it
        search_input = self.query_one("#search-input", Input)
        if search_input.value:
            search_input.value = ""
            self._search_text = ""
            self._load_active_tab()
        else:
            try:
                self._active_table().focus()
            except Exception:
                pass

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        from .screens import DetailScreen
        table = event.control
        if not isinstance(table, FactTable):
            return
        row_dict = table.focused_row_dict()
        if row_dict is None:
            return
        tc = self.query_one(TabbedContent)
        tab_id = tc.active
        if tab_id == 'all':
            orig_fact_type = str(row_dict.get('fact_type', ''))
            full = queries.fetch_full_fact(
                self._store.conn,
                orig_fact_type,
                int(row_dict.get('fact_id', 0)),
            )
            if full:
                row_dict = dict(full)
                row_dict['fact_type'] = orig_fact_type
        self.push_screen(DetailScreen(row_dict, tab_id))

    def on_fact_table_show_thread(self, event: FactTable.ShowThread) -> None:
        from .screens import ThreadSummaryScreen
        row_dict = event.row_dict
        tab_key = event.tab_key
        thread_id = row_dict.get("thread_id")
        if thread_id is None:
            fact_id = (
                row_dict.get("action_item_id")
                or row_dict.get("deadline_id")
                or row_dict.get("decision_id")
                or row_dict.get("summary_id")
                or row_dict.get("calendar_event_id")
                or row_dict.get("fact_id")
            )
            if fact_id is not None:
                thread_id = queries.fetch_thread_id_for_fact(
                    self._store.conn, tab_key, int(fact_id)
                )
        if thread_id is not None:
            self.push_screen(ThreadSummaryScreen(int(thread_id), self._store.conn))

    def _active_table(self) -> FactTable:
        tc = self.query_one(TabbedContent)
        tab_id = tc.active
        return self.query_one(f"#table_{tab_id}", FactTable)

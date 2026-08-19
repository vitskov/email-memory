"""Modal screens for the TUI browser."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Markdown


class DetailScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close"), ("q", "dismiss", "Close")]

    def __init__(self, row_dict: dict, fact_type: str):
        super().__init__()
        self._row_dict = row_dict
        self._fact_type = fact_type

    def compose(self) -> ComposeResult:
        from .formatters import format_detail
        with VerticalScroll(id="detail-scroll"):
            yield Markdown(format_detail(self._row_dict, self._fact_type))


class ThreadSummaryScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close"), ("q", "dismiss", "Close")]

    def __init__(self, thread_id: int, conn):
        super().__init__()
        self._thread_id = thread_id
        self._conn = conn

    def compose(self) -> ComposeResult:
        from . import queries
        from .formatters import format_detail
        detail = queries.fetch_thread_detail(self._conn, self._thread_id)
        with VerticalScroll(id="thread-scroll"):
            if detail:
                yield Markdown(format_detail(detail, 'thread_summaries'))
            else:
                yield Markdown("_Thread summary not found._")


class SemanticSearchScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close"), ("q", "dismiss", "Close")]

    def __init__(self, effort: str = "medium", limit: int = 10, *, vector_store=None) -> None:
        super().__init__()
        self._effort = effort
        self._limit = limit
        self._vector_store = vector_store

    def compose(self) -> ComposeResult:
        with Vertical(id="semsearch-root"):
            yield Label(f"Semantic search (effort={self._effort})  Enter to run, Esc to close")
            yield Input(placeholder="query", id="semsearch-input")
            with VerticalScroll(id="semsearch-results"):
                yield Markdown("_Type a query and press Enter._", id="semsearch-output")

    def on_mount(self) -> None:
        self.query_one("#semsearch-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        output = self.query_one("#semsearch-output", Markdown)
        if not query:
            output.update("_Empty query._")
            return
        output.update("_Searching..._")
        try:
            from email_memory_store.retrieval.engine import RetrievalEngine
            from email_memory_store.retrieval.filters import RetrievalFilters, parse_natural_date_range
            engine = RetrievalEngine(vector_store=self._vector_store)
            date_from, date_to = parse_natural_date_range(query)
            results = engine.search(
                query,
                effort=self._effort,
                limit=self._limit,
                filters=RetrievalFilters(date_from=date_from, date_to=date_to),
            )
        except Exception as exc:
            output.update(f"**Error:** `{exc}`")
            return
        if not results:
            output.update("_No results._")
            return
        lines = [f"### {len(results)} results for: `{query}`", ""]
        for r in results:
            doc = (r.document or "").replace("\n", " ").strip()
            if len(doc) > 240:
                doc = doc[:237] + "..."
            lines.append(f"- **[{r.collection}:{r.id}]** _(score {r.score:.4f})_  {doc}")
        output.update("\n".join(lines))


class AskScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close"), ("q", "dismiss", "Close")]

    def __init__(self, effort: str = "medium", limit: int = 10, *, vector_store=None) -> None:
        super().__init__()
        self._effort = effort
        self._limit = limit
        self._vector_store = vector_store

    def compose(self) -> ComposeResult:
        with Vertical(id="ask-root"):
            yield Label(f"Ask (effort={self._effort})  Enter to ask, Esc to close")
            yield Input(placeholder="question", id="ask-input")
            with VerticalScroll(id="ask-results"):
                yield Markdown("_Type a question and press Enter._", id="ask-output")

    def on_mount(self) -> None:
        self.query_one("#ask-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        output = self.query_one("#ask-output", Markdown)
        if not query:
            output.update("_Empty question._")
            return
        output.update("_Asking the LLM (this may take a moment)..._")
        try:
            from email_memory_store.retrieval.answerer import Answerer
            from email_memory_store.retrieval.engine import RetrievalEngine
            from email_memory_store.retrieval.filters import RetrievalFilters, parse_natural_date_range
            answerer = Answerer(engine=RetrievalEngine(vector_store=self._vector_store))
            date_from, date_to = parse_natural_date_range(query)
            result = answerer.answer(
                query,
                effort=self._effort,
                limit=self._limit,
                filters=RetrievalFilters(date_from=date_from, date_to=date_to),
            )
        except Exception as exc:
            output.update(f"**Error:** `{exc}`")
            return
        lines = ["### Answer", "", result.answer, ""]
        if result.used_handles:
            lines.append("**Used citations:** " + ", ".join(f"`{h}`" for h in result.used_handles))
            lines.append("")
        if result.citations:
            lines.append("### Retrieved context")
            for c in result.citations:
                doc = (c.document or "").replace("\n", " ").strip()
                if len(doc) > 200:
                    doc = doc[:197] + "..."
                lines.append(f"- **[{c.handle}]**  {doc}")
        output.update("\n".join(lines))

"""Headless smoke tests for BrowserApp using Textual's run_test()."""
from __future__ import annotations

import sys
import types

_embedder_module = types.ModuleType("email_memory_store.retrieval.embedder")
_vector_store_module = types.ModuleType("email_memory_store.retrieval.vector_store")


class _StubDefaultDependency:
    pass


def _unexpected_default_dependency(*_args, **_kwargs):
    raise AssertionError("tests must inject retrieval dependencies")


_embedder_module.Embedder = _StubDefaultDependency
_embedder_module.get_default_embedder = _unexpected_default_dependency
_vector_store_module.COLLECTION_NAMES = (
    "holographic_facts",
    "action_items",
    "deadlines",
    "decisions",
    "thread_summaries",
    "message_chunks",
)
_vector_store_module.VectorStore = _StubDefaultDependency
_vector_store_module.get_default_store = _unexpected_default_dependency
sys.modules.setdefault("email_memory_store.retrieval.embedder", _embedder_module)
sys.modules.setdefault("email_memory_store.retrieval.vector_store", _vector_store_module)

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.asyncio


@pytest.mark.asyncio
async def test_browse_app_starts(tmp_path):
    from email_memory_store.store import EmailMemoryStore
    from email_memory_store.tui.app import BrowserApp

    store = EmailMemoryStore(tmp_path / 'em')
    store.initialize()
    app = BrowserApp(store)
    async with app.run_test():
        assert app.is_running
    store.close()


@pytest.mark.asyncio
async def test_browse_app_has_tabs(tmp_path):
    from email_memory_store.store import EmailMemoryStore
    from email_memory_store.tui.app import BrowserApp
    from textual.widgets import TabbedContent

    store = EmailMemoryStore(tmp_path / 'em')
    store.initialize()
    app = BrowserApp(store)
    async with app.run_test():
        tc = app.query_one(TabbedContent)
        assert tc is not None
    store.close()


@pytest.mark.asyncio
async def test_fact_table_cursor_type_is_row(tmp_path):
    """Regression test: FactTable must have cursor_type='row' so that
    pressing Enter posts a DataTable.RowSelected message (rather than
    CellSelected, which BrowserApp does not handle)."""
    from email_memory_store.store import EmailMemoryStore
    from email_memory_store.tui.app import BrowserApp
    from email_memory_store.tui.widgets import FactTable

    store = EmailMemoryStore(tmp_path / 'em')
    store.initialize()
    app = BrowserApp(store)
    async with app.run_test():
        table = app.query_one('#table_all', FactTable)
        assert table.cursor_type == 'row'
    store.close()


@pytest.mark.asyncio
async def test_enter_key_opens_detail_screen(tmp_path):
    """Regression test: pressing Enter on a focused FactTable row must
    open the DetailScreen modal."""
    from email_memory_store.store import EmailMemoryStore
    from email_memory_store.tui.app import BrowserApp
    from email_memory_store.tui.widgets import FactTable
    from email_memory_store.tui.screens import DetailScreen

    store = EmailMemoryStore(tmp_path / 'em')
    store.initialize()
    app = BrowserApp(store)
    async with app.run_test() as pilot:
        table = app.query_one('#table_all', FactTable)
        fake_rows = [{
            'fact_type': 'action',
            'text': 'Some action text',
            'date': '2025-01-01',
            'status': 'open',
            'confidence': 0.9,
            'thread_subject': 'Some thread',
            'action_item_id': 1,
            'thread_id': 1,
        }]
        table.load_rows(fake_rows)
        table.focus()
        await pilot.pause()
        await pilot.press('enter')
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tab_id", ["people", "calendar_events", "pipeline_health"])
async def test_parity_tabs_mount(tmp_path, tab_id):
    """Each parity tab mounts its own FactTable with columns configured."""
    from email_memory_store.store import EmailMemoryStore
    from email_memory_store.tui.app import BrowserApp
    from email_memory_store.tui.widgets import FactTable

    store = EmailMemoryStore(tmp_path / 'em')
    store.initialize()
    app = BrowserApp(store)
    async with app.run_test():
        table = app.query_one(f'#table_{tab_id}', FactTable)
        assert len(table.columns) == len(FactTable.COLUMN_SPECS[tab_id])
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tab_id", ["people", "calendar_events", "pipeline_health"])
async def test_parity_tabs_fetch_rows_without_error(tmp_path, tab_id):
    """_fetch_rows routes each parity tab to a query that executes.

    The People tab reads the companion entity DB rather than the email DB,
    so this also covers that routing.
    """
    from email_memory_store.store import EmailMemoryStore
    from email_memory_store.tui.app import BrowserApp

    store = EmailMemoryStore(tmp_path / 'em')
    store.initialize()
    app = BrowserApp(store)
    async with app.run_test():
        assert app._fetch_rows(tab_id) == []
    store.close()


@pytest.mark.asyncio
async def test_people_tab_reads_entity_db(tmp_path):
    """Regression test: the People tab must query the entity connection.

    Passing the email connection would raise, since `people` lives only in
    the companion entity database.
    """
    from email_memory_store.store import EmailMemoryStore
    from email_memory_store.tui.app import BrowserApp

    store = EmailMemoryStore(tmp_path / 'em')
    store.initialize()
    store.entity_store.conn.execute(
        """
        INSERT INTO people (canonical_name, normalized_name, organization_hint,
            disambiguation_status, email_count, message_count)
        VALUES ('Example Person', 'example person', 'example.test', 'unique', 0, 7)
        """
    )
    app = BrowserApp(store)
    async with app.run_test():
        rows = app._fetch_rows("people")
        assert [r["canonical_name"] for r in rows] == ["Example Person"]
    store.close()


async def test_browser_app_passes_vector_store_to_modal_screens(tmp_path):
    from email_memory_store.store import EmailMemoryStore
    from email_memory_store.tui.app import BrowserApp
    from email_memory_store.tui.screens import AskScreen, SemanticSearchScreen

    store = EmailMemoryStore(tmp_path / 'em')
    store.initialize()
    sentinel = object()
    app = BrowserApp(store, vector_store=sentinel)
    pushed = []
    app.push_screen = pushed.append

    app.action_semantic_search()
    app.action_ask()

    assert isinstance(pushed[0], SemanticSearchScreen)
    assert pushed[0]._vector_store is sentinel
    assert isinstance(pushed[1], AskScreen)
    assert pushed[1]._vector_store is sentinel
    store.close()


async def test_semantic_search_screen_uses_injected_vector_store(monkeypatch):
    from email_memory_store.tui.screens import SemanticSearchScreen

    sentinel = object()
    captured = {}

    class FakeMarkdown:
        def update(self, value):
            captured.setdefault('updates', []).append(value)

    class FakeEngine:
        def __init__(self, *, vector_store, embedder=None):
            captured['vector_store'] = vector_store

        def search(self, query, *, effort, limit, filters):
            return []

    monkeypatch.setattr('email_memory_store.retrieval.engine.RetrievalEngine', FakeEngine)
    screen = SemanticSearchScreen(vector_store=sentinel)
    screen.query_one = lambda *_args, **_kwargs: FakeMarkdown()

    screen.on_input_submitted(SimpleNamespace(value='project'))

    assert captured['vector_store'] is sentinel


async def test_ask_screen_uses_injected_vector_store(monkeypatch):
    from email_memory_store.tui.screens import AskScreen
    from email_memory_store.promotion.llm import LLMProviderSpec

    sentinel = object()
    captured = {}

    class FakeMarkdown:
        def update(self, value):
            captured.setdefault('updates', []).append(value)

    class FakeEngine:
        def __init__(self, *, vector_store, embedder=None):
            captured['vector_store'] = vector_store

    class FakeAnswerer:
        def __init__(self, *, engine, provider_spec=None):
            captured['engine'] = engine
            captured['provider_spec'] = provider_spec

        def answer(self, query, *, effort, limit, filters):
            return SimpleNamespace(answer='done', used_handles=[], citations=[])

    monkeypatch.setattr('email_memory_store.retrieval.engine.RetrievalEngine', FakeEngine)
    monkeypatch.setattr('email_memory_store.retrieval.answerer.Answerer', FakeAnswerer)
    provider_spec = LLMProviderSpec(executable='/opt/bin/hermes-current')
    screen = AskScreen(vector_store=sentinel, provider_spec=provider_spec)
    screen.query_one = lambda *_args, **_kwargs: FakeMarkdown()

    screen.on_input_submitted(SimpleNamespace(value='question'))

    assert captured['vector_store'] is sentinel
    assert captured['provider_spec'] is provider_spec


async def test_ask_screen_reports_missing_runtime_provider_configuration():
    from email_memory_store.tui.screens import AskScreen

    captured: dict[str, list[str]] = {}

    class FakeMarkdown:
        def update(self, value):
            captured.setdefault('updates', []).append(value)

    screen = AskScreen(
        vector_store=object(),
        provider_error='LLM provider executable is not configured.',
    )
    screen.query_one = lambda *_args, **_kwargs: FakeMarkdown()

    screen.on_input_submitted(SimpleNamespace(value='question'))

    assert captured['updates'][-1] == (
        '**Configuration error:** LLM provider executable is not configured.'
    )

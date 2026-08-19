from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from email_memory_store import cli


class FakeVectorStore:
    def __init__(self) -> None:
        self._path = '/tmp/custom-root/chroma'
        self.deleted: list[tuple[str, list[str]]] = []

    def count(self, collection_name: str) -> int:
        return {'action_items': 2}.get(collection_name, 0)

    def delete(self, collection_name: str, ids: list[str]) -> None:
        self.deleted.append((collection_name, ids))


def test_cmd_embed_status_uses_root_scoped_vector_store(monkeypatch, capsys):
    seen: list[str] = []
    fake_store = FakeVectorStore()
    monkeypatch.setattr(cli, '_open_vector_store', lambda root: seen.append(root) or fake_store)

    cli.cmd_embed_status(SimpleNamespace(root='/tmp/custom-root'))

    payload = json.loads(capsys.readouterr().out)
    assert seen == ['/tmp/custom-root']
    assert payload['persist_path'] == '/tmp/custom-root/chroma'
    assert payload['collections']['action_items'] == 2


def test_cmd_search_uses_root_scoped_vector_store(monkeypatch, capsys):
    sentinel = object()
    captured: dict[str, object] = {}

    class FakeEngine:
        def __init__(self, *, vector_store, embedder=None):
            captured['vector_store'] = vector_store

        def search(self, query, *, effort, limit, filters):
            return [
                SimpleNamespace(
                    collection='deadlines',
                    id='7',
                    score=1.0,
                    document='deadline doc',
                    metadata={},
                    semantic_rank=0,
                    lexical_rank=None,
                    semantic_distance=0.1,
                )
            ]

    monkeypatch.setattr(cli, '_open_vector_store', lambda root: sentinel)
    monkeypatch.setattr(cli, 'RetrievalEngine', FakeEngine)

    cli.cmd_search(SimpleNamespace(
        root='/tmp/custom-root',
        legacy=False,
        query='deadline',
        effort='medium',
        limit=5,
        date_from=None,
        date_to=None,
        thread_id=None,
        thread_key=None,
    ))

    payload = json.loads(capsys.readouterr().out)
    assert captured['vector_store'] is sentinel
    assert payload['results'][0]['collection'] == 'deadlines'


def test_cmd_ask_uses_root_scoped_vector_store(monkeypatch, capsys):
    sentinel = object()
    captured: dict[str, object] = {}

    class FakeEngine:
        def __init__(self, *, vector_store, embedder=None):
            captured['vector_store'] = vector_store

    class FakeAnswerer:
        def __init__(self, *, engine, provider_spec=None):
            captured['engine'] = engine
            captured['provider_spec'] = provider_spec

        def answer(self, query, *, effort, limit, filters):
            return SimpleNamespace(query=query, answer='ok', used_handles=['D:7'], citations=[])

    monkeypatch.setattr(cli, '_open_vector_store', lambda root: sentinel)
    monkeypatch.setattr(cli, 'RetrievalEngine', FakeEngine)
    monkeypatch.setattr(cli, 'Answerer', FakeAnswerer)

    cli.cmd_ask(SimpleNamespace(
        root='/tmp/custom-root',
        query='when',
        effort='medium',
        limit=3,
        provider=None,
        model=None,
        date_from=None,
        date_to=None,
        thread_id=None,
        thread_key=None,
    ))

    payload = json.loads(capsys.readouterr().out)
    assert captured['vector_store'] is sentinel
    assert payload['answer'] == 'ok'


def test_cmd_embed_backfill_uses_root_scoped_vector_store(monkeypatch, capsys):
    sentinel = object()
    fake_store = SimpleNamespace(close=lambda: None, get_promotion_llm_config=lambda: {})
    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, '_open_store', lambda args: fake_store)
    monkeypatch.setattr(cli, '_open_vector_store', lambda root: sentinel)

    def fake_backfill_all(*, store, batch_size, vector_store, fact_store_db_path):
        captured['store'] = store
        captured['batch_size'] = batch_size
        captured['vector_store'] = vector_store
        captured['fact_store_db_path'] = fact_store_db_path
        return {'message_chunks': 9}

    monkeypatch.setattr(cli, 'backfill_all', fake_backfill_all)

    cli.cmd_embed_backfill(
        SimpleNamespace(
            root='/tmp/custom-root',
            batch_size=32,
            fact_store_db='/tmp/local-facts.db',
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert captured == {
        'store': fake_store,
        'batch_size': 32,
        'vector_store': sentinel,
        'fact_store_db_path': Path('/tmp/local-facts.db'),
    }
    assert payload['embedded']['message_chunks'] == 9


def test_cmd_cleanup_expired_prunes_root_scoped_vector_store(monkeypatch, capsys):
    fake_vector_store = FakeVectorStore()

    class FakeStore:
        def cleanup_expired_time_anchors(self, *, grace_days, dry_run):
            return {
                'deleted_deadline_ids': [4],
                'deleted_action_item_ids': [9],
                'deleted_calendar_event_ids': [12],
            }

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli, '_open_store', lambda args: FakeStore())
    monkeypatch.setattr(cli, '_open_vector_store', lambda root: fake_vector_store)

    cli.cmd_cleanup_expired(SimpleNamespace(root='/tmp/custom-root', grace_days=0, apply=False))

    payload = json.loads(capsys.readouterr().out)
    assert fake_vector_store.deleted == [('deadlines', ['4']), ('action_items', ['9']), ('calendar_events', ['12'])]
    assert payload['vectors_pruned'] == 3


import pytest


@pytest.mark.parametrize(
    ("handler_name", "run_attr", "args"),
    [
        (
            'cmd_initial_ingest',
            'run_initial_ingestion',
            SimpleNamespace(
                root='/tmp/custom-root',
                account='primary-account',
                email='user@example.test',
                include_folders=None,
                exclude_folders=None,
                page_size=100,
                max_pages_per_folder=5,
                embed=True,
                use_work_db=False,
            ),
        ),
        (
            'cmd_nightly_update',
            'run_nightly_update',
            SimpleNamespace(
                root='/tmp/custom-root',
                account='primary-account',
                email='user@example.test',
                include_folders=None,
                exclude_folders=None,
                page_size=100,
                pages_per_folder=2,
                embed=True,
                use_work_db=False,
            ),
        ),
        (
            'cmd_extract_threads',
            'extract_service',
            SimpleNamespace(
                root='/tmp/custom-root',
                limit=20,
                embed=True,
                use_work_db=False,
            ),
        ),
        (
            'cmd_run_llm_promotions',
            'promotion_service',
            SimpleNamespace(
                root='/tmp/custom-root',
                limit=10,
                embed=True,
                use_work_db=False,
                fact_store_db='/tmp/local-facts.db',
            ),
        ),
    ],
)
def test_embed_pipeline_handlers_use_root_scoped_vector_store(monkeypatch, capsys, handler_name, run_attr, args):
    fake_store = SimpleNamespace(close=lambda: None, get_promotion_llm_config=lambda: {})
    sentinel = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, '_open_store', lambda _args: fake_store)
    monkeypatch.setattr(cli, '_open_vector_store', lambda root: sentinel)
    monkeypatch.setattr(cli, '_maybe_checkpoint', lambda store, _args: None)

    def fake_embed_for_pipeline_event(store, *, event, vector_store=None, embedder=None, batch_size=64):
        captured['embed_event'] = event
        captured['vector_store'] = vector_store
        return {'ok': True}

    monkeypatch.setattr(cli, 'embed_for_pipeline_event', fake_embed_for_pipeline_event)

    if handler_name in {'cmd_initial_ingest', 'cmd_nightly_update'}:
        monkeypatch.setattr(cli, '_pre_flight_index_repair', lambda store: None)
        monkeypatch.setattr(cli, 'HimalayaClient', lambda: object())
        monkeypatch.setattr(cli, '_record_ingestion_report', lambda *a, **k: {})
        monkeypatch.setattr(cli, run_attr, lambda **kwargs: {'messages_added': 1})
    elif handler_name == 'cmd_extract_threads':
        class FakeExtractionService:
            def __init__(self, store):
                captured['service_store'] = store

            def run_extraction(self, *, limit, spec):
                captured['limit'] = limit
                return {'threads_processed': 1}

        monkeypatch.setattr(cli, 'ExtractionService', FakeExtractionService)
    elif handler_name == 'cmd_run_llm_promotions':
        class FakePromotionService:
            def __init__(self, store):
                captured['service_store'] = store

            def execute_and_commit_llm_promotions(self, *, limit, holographic_db_path):
                captured['limit'] = limit
                captured['holographic_db_path'] = holographic_db_path
                return {'promoted': 1}

        monkeypatch.setattr(cli, 'EmailPromotionService', FakePromotionService)

    getattr(cli, handler_name)(args)

    payload = json.loads(capsys.readouterr().out)
    assert captured['vector_store'] is sentinel
    if handler_name == 'cmd_run_llm_promotions':
        assert captured['holographic_db_path'] == '/tmp/local-facts.db'
    assert payload['embedded'] == {'ok': True}

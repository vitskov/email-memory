from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

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

    cli.cmd_embed_status(SimpleNamespace(root='/tmp/custom-root', vector_store='/tmp/exact-vectors'))

    payload = json.loads(capsys.readouterr().out)
    assert seen == [Path('/tmp/exact-vectors')]
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
        vector_store='/tmp/exact-vectors',
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
        vector_store='/tmp/exact-vectors',
        hermes_executable='/opt/bin/hermes',
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


def test_browse_snapshot_copies_exact_configured_databases(monkeypatch, tmp_path):
    main_db = tmp_path / 'durable' / 'selected-main.db'
    entity_db = tmp_path / 'identity' / 'selected-entity.db'
    work_db = tmp_path / 'scratch' / 'selected-work.db'
    for path, content in (
        (main_db, b'main'),
        (entity_db, b'entity'),
        (work_db, b'work'),
        (Path(f'{main_db}.wal'), b'main-wal'),
        (Path(f'{work_db}.wal'), b'work-wal'),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    captured: dict[str, object] = {}

    class FakeStore:
        def __init__(
            self, root, work_root=None, use_work_db=False, read_only=False, *,
            db_path=None, entity_db_path=None, work_db_path=None,
        ):
            captured['root'] = Path(root)
            captured['db_path'] = Path(db_path)
            captured['entity_db_path'] = Path(entity_db_path)
            captured['work_db_path'] = Path(work_db_path)
            captured['use_work_db'] = use_work_db
            captured['read_only'] = read_only
            captured['main_content'] = Path(db_path).read_bytes()
            captured['entity_content'] = Path(entity_db_path).read_bytes()
            captured['work_content'] = Path(work_db_path).read_bytes()
            captured['main_wal'] = Path(f'{db_path}.wal').read_bytes()
            captured['work_wal'] = Path(f'{work_db_path}.wal').read_bytes()

        def get_promotion_llm_config(self):
            return {'provider': {'name': 'hermes-default'}}

        def close(self):
            pass

    monkeypatch.setattr(cli, 'EmailMemoryStore', FakeStore)
    monkeypatch.setattr(cli, '_open_vector_store', lambda _path: object())
    monkeypatch.setattr(
        'email_memory_store.tui.launch_browser',
        lambda store, **kwargs: captured.update(browser_kwargs=kwargs),
    )

    cli.cmd_browse(SimpleNamespace(
        root=str(tmp_path / 'runtime-root'),
        work_root=None,
        main_db=str(main_db),
        entity_db=str(entity_db),
        work_db=str(work_db),
        vector_store=str(tmp_path / 'vectors'),
        use_work_db=True,
        read_only=False,
        snapshot=True,
        hermes_executable='/opt/bin/hermes-current',
    ))

    assert captured['db_path'].name == 'main.duckdb'
    assert captured['entity_db_path'].name == 'entity.duckdb'
    assert captured['work_db_path'].name == 'work.duckdb'
    assert captured['main_content'] == b'main'
    assert captured['entity_content'] == b'entity'
    assert captured['work_content'] == b'work'
    assert captured['main_wal'] == b'main-wal'
    assert captured['work_wal'] == b'work-wal'
    assert captured['use_work_db'] is True
    assert captured['read_only'] is True
    assert captured['browser_kwargs']['provider_spec'].executable == '/opt/bin/hermes-current'
    assert not captured['root'].exists()


def test_browse_snapshot_cleans_up_when_copy_fails(monkeypatch, tmp_path):
    snapshot_paths: list[Path] = []

    def fail_during_copy(_source: Path, destination: Path) -> None:
        destination.write_bytes(b'private-copy')
        snapshot_paths.append(destination.parent)
        if len(snapshot_paths) == 2:
            raise OSError('copy failed')

    monkeypatch.setattr(cli, '_copy_database_snapshot', fail_during_copy)
    with pytest.raises(OSError, match='copy failed'):
        cli.cmd_browse(SimpleNamespace(
            root=str(tmp_path / 'runtime-root'),
            main_db=str(tmp_path / 'main.db'),
            entity_db=str(tmp_path / 'entity.db'),
            work_db=None,
            use_work_db=False,
            snapshot=True,
        ))

    assert snapshot_paths
    assert not snapshot_paths[0].exists()


def test_browse_snapshot_ignores_missing_unused_work_database(monkeypatch, tmp_path):
    main_db = tmp_path / 'main.db'
    entity_db = tmp_path / 'entity.db'
    main_db.write_bytes(b'main')
    entity_db.write_bytes(b'entity')
    captured: dict[str, object] = {}

    def capture_browser(_args, **paths) -> None:
        captured.update(paths)
        captured['main_content'] = paths['main_db'].read_bytes()
        captured['entity_content'] = paths['entity_db'].read_bytes()

    monkeypatch.setattr(cli, '_run_browser', capture_browser)
    cli.cmd_browse(SimpleNamespace(
        root=str(tmp_path / 'runtime-root'),
        main_db=str(main_db),
        entity_db=str(entity_db),
        work_db=str(tmp_path / 'missing-work.db'),
        use_work_db=False,
        snapshot=True,
    ))

    assert captured['work_db'] is None
    assert captured['main_content'] == b'main'
    assert captured['entity_content'] == b'entity'
    assert not Path(captured['root']).exists()


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

    args.vector_store = '/tmp/exact-vectors'
    args.mail_client_executable = '/opt/bin/himalaya'
    args.hermes_executable = '/opt/bin/hermes'

    def fake_embed_for_pipeline_event(
        store, *, event, vector_store=None, embedder=None, batch_size=64,
        fact_store_db_path=None,
    ):
        captured['embed_event'] = event
        captured['vector_store'] = vector_store
        captured['fact_store_db_path'] = fact_store_db_path
        return {'ok': True}

    monkeypatch.setattr(cli, 'embed_for_pipeline_event', fake_embed_for_pipeline_event)

    if handler_name in {'cmd_initial_ingest', 'cmd_nightly_update'}:
        monkeypatch.setattr(cli, '_pre_flight_index_repair', lambda store: None)
        monkeypatch.setattr(cli, 'HimalayaClient', lambda **_kwargs: object())
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

            def execute_and_commit_llm_promotions(self, *, limit, config, holographic_db_path):
                captured['limit'] = limit
                captured['holographic_db_path'] = holographic_db_path
                return {'promoted': 1}

        monkeypatch.setattr(cli, 'EmailPromotionService', FakePromotionService)

    getattr(cli, handler_name)(args)

    payload = json.loads(capsys.readouterr().out)
    assert captured['vector_store'] is sentinel
    if handler_name == 'cmd_run_llm_promotions':
        assert captured['holographic_db_path'] == '/tmp/local-facts.db'
        assert captured['fact_store_db_path'] == '/tmp/local-facts.db'
    assert payload['embedded'] == {'ok': True}

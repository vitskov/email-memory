import sys
from types import ModuleType, SimpleNamespace

import email_memory_store.cli as cli
from email_memory_store.cli import build_parser


def test_cli_supports_ingest_bodies_command():
    parser = build_parser()
    args = parser.parse_args([
        'ingest-bodies',
        '--account', 'primary-account',
        '--folder', 'INBOX',
    ])
    backfill_args = parser.parse_args([
        'backfill-rfc-metadata',
        '--account', 'primary-account',
        '--email', 'user@example.test',
    ])
    retry_failed_args = parser.parse_args([
        'retry-failed-bodies',
        '--account', 'primary-account',
        '--limit', '25',
        '--folder', 'INBOX',
    ])
    repair_args = parser.parse_args([
        'repair-ingestion-state',
        '--account', 'primary-account',
        '--email', 'user@example.test',
        '--folder', 'INBOX',
        '--max-pages-per-folder', '5',
    ])
    reconcile_args = parser.parse_args(['reconcile-ingestion-cursors', '--apply'])
    lineage_args = parser.parse_args([
        'thread-lineage',
        '--thread-key', 'rfc822-thread:root@example.test',
        '--thread-key', 'rfc822-thread:other@example.test',
    ])
    cross_folder_args = parser.parse_args([
        'cross-folder-threads',
        '--limit', '15',
        '--query', 'project',
    ])
    assert args.command == 'ingest-bodies'
    assert args.folder == 'INBOX'
    assert backfill_args.command == 'backfill-rfc-metadata'
    assert backfill_args.account == 'primary-account'
    assert backfill_args.email == 'user@example.test'
    assert backfill_args.page_size == 100
    assert backfill_args.max_pages_per_folder == 10
    assert retry_failed_args.command == 'retry-failed-bodies'
    assert retry_failed_args.account == 'primary-account'
    assert retry_failed_args.limit == 25
    assert retry_failed_args.folders == ['INBOX']
    assert repair_args.command == 'repair-ingestion-state'
    assert repair_args.account == 'primary-account'
    assert repair_args.email == 'user@example.test'
    assert repair_args.include_folders == ['INBOX']
    assert repair_args.max_pages_per_folder == 5
    assert reconcile_args.command == 'reconcile-ingestion-cursors'
    assert reconcile_args.apply is True
    assert lineage_args.command == 'thread-lineage'
    assert lineage_args.thread_keys == ['rfc822-thread:root@example.test', 'rfc822-thread:other@example.test']
    assert cross_folder_args.command == 'cross-folder-threads'
    assert cross_folder_args.limit == 15
    assert cross_folder_args.query == 'project'

    initial_args = parser.parse_args([
        'initial-ingest',
        '--account', 'primary-account',
        '--email', 'user@example.test',
    ])
    nightly_args = parser.parse_args([
        'nightly-update',
        '--account', 'primary-account',
        '--email', 'user@example.test',
    ])
    assert initial_args.command == 'initial-ingest'
    assert nightly_args.command == 'nightly-update'


def test_cli_supports_runtime_config_option():
    parser = build_parser()
    args = parser.parse_args([
        '--root', '/runtime',
        '--work-root', '/work',
        '--fact-store-db', '/private/facts.db',
        '--runtime-config', '/private/runtime.toml',
        'status',
    ])

    assert args.root == '/runtime'
    assert args.work_root == '/work'
    assert args.fact_store_db == '/private/facts.db'
    assert args.runtime_config == '/private/runtime.toml'


def test_setup_private_imports_the_bootstrap_ui_only_when_invoked(monkeypatch):
    calls: list[str] = []
    private_setup = ModuleType("email_memory_store.tui.private_setup")
    private_setup.main = lambda: calls.append("started")
    monkeypatch.setitem(sys.modules, private_setup.__name__, private_setup)

    cli.cmd_setup_private(SimpleNamespace())

    assert calls == ["started"]


def test_cli_continuation_commands_use_parser_supported_folder_flags():
    parser = build_parser()

    initial_args = parser.parse_args([
        'initial-ingest',
        '--account', 'primary-account',
        '--email', 'user@example.test',
        '--include-folder', 'Archive',
        '--include-folder', 'Sent Items',
    ])
    backfill_args = parser.parse_args([
        'backfill-rfc-metadata',
        '--account', 'primary-account',
        '--email', 'user@example.test',
        '--include-folder', 'Archive',
    ])
    repair_args = parser.parse_args([
        'repair-ingestion-state',
        '--account', 'primary-account',
        '--email', 'user@example.test',
        '--folder', 'INBOX',
    ])

    assert initial_args.include_folders == ['Archive', 'Sent Items']
    assert backfill_args.include_folders == ['Archive']
    assert repair_args.include_folders == ['INBOX']


def test_cli_supports_search_and_promotions_commands():
    parser = build_parser()
    search_args = parser.parse_args(['search', '--query', 'parking'])
    people_args = parser.parse_args(['search-people', '--query', 'alice'])
    promo_args = parser.parse_args(['select-promotions'])
    fact_store_args = parser.parse_args(['promote-to-fact-store'])
    export_args = parser.parse_args(['export-fact-store-batch', '--output', '/tmp/fact-store.json'])
    mark_args = parser.parse_args([
        'mark-fact-store-written',
        '--batch-id', 'batch-123',
        '--fact', 'person:1:key-1=42',
    ])
    demote_args = parser.parse_args([
        'mark-fact-store-demoted',
        '--fact', 'person:1:key-1=contradicted by later emails',
    ])
    edit_args = parser.parse_args([
        'mark-fact-store-edited',
        '--entry', 'person:1:key-1|replacement durable memory|new evidence corrected the summary',
    ])
    status_args = parser.parse_args(['status'])
    merge_args = parser.parse_args([
        'merge-person',
        '--primary-person-id', '1',
        '--secondary-person-id', '2',
        '--reason', 'duplicate identity',
    ])
    split_args = parser.parse_args([
        'split-person',
        '--source-person-id', '1',
        '--new-canonical-name', 'Alice Example',
        '--email-address', 'contact@example.test',
        '--reason', 'separate person',
    ])
    purge_args = parser.parse_args([
        'purge-folder',
        '--folder', 'Trash',
        '--folder', 'Archive/Legacy',
        '--dry-run',
    ])
    excluded_args = parser.parse_args([
        'set-excluded-folders',
        '--folder', 'Trash',
        '--folder', 'Junk Email',
    ])
    promotion_status_args = parser.parse_args(['promotion-status'])
    reseed_promotion_assets_args = parser.parse_args(['reseed-promotion-assets', '--force'])
    set_promotion_llm_args = parser.parse_args([
        'set-promotion-llm-config',
        '--provider', 'codex-cli',
        '--model', 'gpt-5-codex',
        '--max-candidates-per-batch', '8',
        '--max-input-chars', '4000',
        '--soul-file', '/tmp/soul.md',
        '--rulebook-file', '/tmp/rulebook.md',
    ])
    plan_llm_args = parser.parse_args([
        'plan-llm-promotions',
        '--limit', '7',
    ])
    run_llm_args = parser.parse_args([
        'run-llm-promotions',
        '--limit', '5',
    ])
    pipeline_status_args = parser.parse_args(['pipeline-status'])
    setup_private_args = parser.parse_args(['setup-private'])

    assert search_args.command == 'search'
    assert search_args.query == 'parking'
    assert people_args.command == 'search-people'
    assert people_args.query == 'alice'
    assert promo_args.command == 'select-promotions'
    assert fact_store_args.command == 'promote-to-fact-store'
    assert export_args.command == 'export-fact-store-batch'
    assert export_args.output == '/tmp/fact-store.json'
    assert mark_args.command == 'mark-fact-store-written'
    assert mark_args.batch_id == 'batch-123'
    assert mark_args.facts == ['person:1:key-1=42']
    assert demote_args.command == 'mark-fact-store-demoted'
    assert demote_args.facts == ['person:1:key-1=contradicted by later emails']
    assert edit_args.command == 'mark-fact-store-edited'
    assert edit_args.entries == ['person:1:key-1|replacement durable memory|new evidence corrected the summary']
    assert status_args.command == 'status'
    assert merge_args.command == 'merge-person'
    assert merge_args.primary_person_id == 1
    assert merge_args.secondary_person_id == 2
    assert split_args.command == 'split-person'
    assert split_args.source_person_id == 1
    assert split_args.email_addresses == ['contact@example.test']
    assert purge_args.command == 'purge-folder'
    assert purge_args.folders == ['Trash', 'Archive/Legacy']
    assert purge_args.dry_run is True
    assert excluded_args.command == 'set-excluded-folders'
    assert excluded_args.folders == ['Trash', 'Junk Email']
    assert promotion_status_args.command == 'promotion-status'
    assert reseed_promotion_assets_args.command == 'reseed-promotion-assets'
    assert reseed_promotion_assets_args.force is True
    assert set_promotion_llm_args.command == 'set-promotion-llm-config'
    assert set_promotion_llm_args.provider == 'codex-cli'
    assert set_promotion_llm_args.model == 'gpt-5-codex'
    assert set_promotion_llm_args.max_candidates_per_batch == 8
    assert set_promotion_llm_args.max_input_chars == 4000
    assert set_promotion_llm_args.soul_file == '/tmp/soul.md'
    assert set_promotion_llm_args.rulebook_file == '/tmp/rulebook.md'
    assert plan_llm_args.command == 'plan-llm-promotions'
    assert plan_llm_args.limit == 7
    assert run_llm_args.command == 'run-llm-promotions'
    assert run_llm_args.limit == 5
    assert pipeline_status_args.command == 'pipeline-status'
    assert setup_private_args.command == 'setup-private'


def test_cli_supports_start_date_on_init_db():
    parser = build_parser()
    args = parser.parse_args(['init-db', '--start-date', '2026-01-15'])

    assert args.command == 'init-db'
    assert args.start_date == '2026-01-15'


def test_cli_defaults_start_date_to_2022_01_02_on_init_db():
    parser = build_parser()
    args = parser.parse_args(['init-db'])

    assert args.command == 'init-db'
    assert args.start_date == '2022-01-02'


def test_cli_supports_retrieval_search_flags():
    parser = build_parser()
    search_args = parser.parse_args([
        'search', '--query', 'qualifying exam',
        '--effort', 'heavy', '--limit', '5',
        '--thread-id', '7',
        '--date-from', '2026-01-01T00:00:00+00:00',
    ])
    assert search_args.command == 'search'
    assert search_args.effort == 'heavy'
    assert search_args.limit == 5
    assert search_args.thread_id == 7
    assert search_args.date_from == '2026-01-01T00:00:00+00:00'
    assert search_args.legacy is False

    legacy_args = parser.parse_args(['search', '--query', 'x', '--legacy'])
    assert legacy_args.legacy is True
    assert legacy_args.effort == 'medium'


def test_cli_supports_ask_command():
    parser = build_parser()
    ask_args = parser.parse_args([
        'ask', '--query', 'when is the deadline?',
        '--effort', 'medium', '--limit', '8',
        '--provider', 'codex-cli', '--model', 'gpt-5-codex',
    ])
    assert ask_args.command == 'ask'
    assert ask_args.query == 'when is the deadline?'
    assert ask_args.effort == 'medium'
    assert ask_args.limit == 8
    assert ask_args.provider == 'codex-cli'
    assert ask_args.model == 'gpt-5-codex'


def test_cli_supports_embed_flags_on_pipeline_commands():
    parser = build_parser()
    initial_embed = parser.parse_args([
        'initial-ingest', '--account', 'primary-account', '--email', 'user@example.test', '--embed',
    ])
    assert initial_embed.embed is True

    nightly_no_embed = parser.parse_args([
        'nightly-update', '--account', 'primary-account', '--email', 'user@example.test',
    ])
    assert nightly_no_embed.embed is False

    extract_embed = parser.parse_args(['extract-threads', '--embed'])
    assert extract_embed.embed is True

    run_llm_embed = parser.parse_args(['run-llm-promotions', '--embed'])
    assert run_llm_embed.embed is True


def test_cli_supports_embed_status_and_backfill():
    parser = build_parser()
    backfill_args = parser.parse_args(['embed-backfill', '--batch-size', '32'])
    status_args = parser.parse_args(['embed-status'])
    assert backfill_args.command == 'embed-backfill'
    assert backfill_args.batch_size == 32
    assert status_args.command == 'embed-status'

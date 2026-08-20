from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

from .db_rows import require_row, require_scalar
from .himalaya import HimalayaClient
from .holographic import default_holographic_db_path
from .ingestion.service import ingest_account_folders, ingest_envelopes, ingest_message_bodies, run_failed_body_backfill, run_ingestion_state_repair, run_initial_ingestion, run_nightly_update, run_rfc_metadata_backfill

from .extraction.service import ExtractionService
from .maintenance import rebuild_all_indexed_tables, rebuild_email_entity_index_table, rebuild_entity_message_index_table, rebuild_messages_table
from .promotion.llm import LLMProviderSpec, PromotionLLMConfig
from .promotion.service import EmailPromotionService
from .retrieval.answerer import Answerer
from .retrieval.embed_backfill import backfill_all, chunk_text
from .retrieval.engine import EFFORT_LEVELS, RetrievalEngine
from .retrieval.filters import RetrievalFilters, parse_natural_date_range
from .retrieval.incremental import embed_for_pipeline_event
from .retrieval.service import EmailRetrievalService
from .retrieval.vector_store import COLLECTION_NAMES, VectorStore
from .runtime import resolve_runtime_settings
from .store import EmailMemoryStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='email-memory-store')
    parser.add_argument('--root', default=None)
    parser.add_argument('--work-root', default=None)
    parser.add_argument('--fact-store-db', default=None)
    parser.add_argument('--runtime-config', default=None)
    parser.add_argument('--use-work-db', action='store_true')

    subparsers = parser.add_subparsers(dest='command', required=True)

    init_parser = subparsers.add_parser('init-db')
    init_parser.add_argument('--start-date', default='2022-01-02')
    init_parser.set_defaults(handler=cmd_init_db)

    ingest_folder_parser = subparsers.add_parser('ingest-folder')
    ingest_folder_parser.add_argument('--account', required=True)
    ingest_folder_parser.add_argument('--email', required=True)
    ingest_folder_parser.add_argument('--folder', required=True)
    ingest_folder_parser.add_argument('--page', type=int, default=1)
    ingest_folder_parser.add_argument('--page-size', type=int, default=100)
    ingest_folder_parser.set_defaults(handler=cmd_ingest_folder)

    ingest_account_parser = subparsers.add_parser('ingest-account')
    ingest_account_parser.add_argument('--account', required=True)
    ingest_account_parser.add_argument('--email', required=True)
    ingest_account_parser.add_argument('--include-folder', action='append', dest='include_folders')
    ingest_account_parser.add_argument('--exclude-folder', action='append', dest='exclude_folders')
    ingest_account_parser.add_argument('--page', type=int, default=1)
    ingest_account_parser.add_argument('--page-size', type=int, default=100)
    ingest_account_parser.set_defaults(handler=cmd_ingest_account)

    ingest_bodies_parser = subparsers.add_parser('ingest-bodies')
    ingest_bodies_parser.add_argument('--account', required=True)
    ingest_bodies_parser.add_argument('--folder', required=True)
    ingest_bodies_parser.add_argument('--page', type=int, default=1)
    ingest_bodies_parser.add_argument('--page-size', type=int, default=100)
    ingest_bodies_parser.set_defaults(handler=cmd_ingest_bodies)

    initial_ingest_parser = subparsers.add_parser('initial-ingest')
    initial_ingest_parser.add_argument('--account', required=True)
    initial_ingest_parser.add_argument('--email', required=True)
    initial_ingest_parser.add_argument('--include-folder', action='append', dest='include_folders')
    initial_ingest_parser.add_argument('--exclude-folder', action='append', dest='exclude_folders')
    initial_ingest_parser.add_argument('--page-size', type=int, default=100)
    initial_ingest_parser.add_argument('--max-pages-per-folder', type=int, default=10)
    initial_ingest_parser.add_argument('--embed', action='store_true', help='Embed newly ingested message bodies into the vector store')
    initial_ingest_parser.set_defaults(handler=cmd_initial_ingest)

    nightly_update_parser = subparsers.add_parser('nightly-update')
    nightly_update_parser.add_argument(
        '--account',
        help=(
            'mail connector account; scheduled deployments may provide '
            'EMAIL_MEMORY_ACCOUNT_NAME instead'
        ),
    )
    nightly_update_parser.add_argument(
        '--email',
        help=(
            'account address; scheduled deployments may provide '
            'EMAIL_MEMORY_ACCOUNT_EMAIL instead'
        ),
    )
    nightly_update_parser.add_argument('--include-folder', action='append', dest='include_folders')
    nightly_update_parser.add_argument('--exclude-folder', action='append', dest='exclude_folders')
    nightly_update_parser.add_argument('--page-size', type=int, default=100)
    nightly_update_parser.add_argument('--pages-per-folder', type=int, default=2)
    nightly_update_parser.add_argument('--embed', action='store_true', help='Embed newly ingested message bodies into the vector store')
    nightly_update_parser.set_defaults(handler=cmd_nightly_update)

    backfill_rfc_parser = subparsers.add_parser('backfill-rfc-metadata')
    backfill_rfc_parser.add_argument('--account', required=True)
    backfill_rfc_parser.add_argument('--email', required=True)
    backfill_rfc_parser.add_argument('--include-folder', action='append', dest='include_folders')
    backfill_rfc_parser.add_argument('--exclude-folder', action='append', dest='exclude_folders')
    backfill_rfc_parser.add_argument('--page-size', type=int, default=100)
    backfill_rfc_parser.add_argument('--max-pages-per-folder', type=int, default=10)
    backfill_rfc_parser.set_defaults(handler=cmd_backfill_rfc_metadata)

    retry_failed_bodies_parser = subparsers.add_parser('retry-failed-bodies')
    retry_failed_bodies_parser.add_argument('--account', required=True)
    retry_failed_bodies_parser.add_argument('--folder', action='append', dest='folders')
    retry_failed_bodies_parser.add_argument('--limit', type=int, default=100)
    retry_failed_bodies_parser.set_defaults(handler=cmd_retry_failed_bodies)

    repair_parser = subparsers.add_parser('repair-ingestion-state')
    repair_parser.add_argument('--account', required=True)
    repair_parser.add_argument('--email', required=True)
    repair_parser.add_argument('--folder', action='append', dest='include_folders')
    repair_parser.add_argument('--exclude-folder', action='append', dest='exclude_folders')
    repair_parser.add_argument('--page-size', type=int, default=100)
    repair_parser.add_argument('--max-pages-per-folder', type=int, default=10)
    repair_parser.set_defaults(handler=cmd_repair_ingestion_state)

    reconcile_cursors_parser = subparsers.add_parser('reconcile-ingestion-cursors')
    reconcile_cursors_parser.add_argument('--apply', action='store_true')
    reconcile_cursors_parser.set_defaults(handler=cmd_reconcile_ingestion_cursors)

    thread_lineage_parser = subparsers.add_parser('thread-lineage')
    thread_lineage_parser.add_argument('--thread-key', action='append', dest='thread_keys', required=True)
    thread_lineage_parser.set_defaults(handler=cmd_thread_lineage)

    cross_folder_threads_parser = subparsers.add_parser('cross-folder-threads')
    cross_folder_threads_parser.add_argument('--limit', type=int, default=20)
    cross_folder_threads_parser.add_argument('--query')
    cross_folder_threads_parser.set_defaults(handler=cmd_cross_folder_threads)

    search_parser = subparsers.add_parser('search')
    search_parser.add_argument('--query', required=True)
    search_parser.add_argument('--limit', type=int, default=10)
    search_parser.add_argument('--effort', choices=EFFORT_LEVELS, default='medium')
    search_parser.add_argument('--thread-id', type=int)
    search_parser.add_argument('--thread-key')
    search_parser.add_argument('--date-from')
    search_parser.add_argument('--date-to')
    search_parser.add_argument('--legacy', action='store_true', help='Use legacy keyword SQL search instead of hybrid vector engine')
    search_parser.set_defaults(handler=cmd_search)

    ask_parser = subparsers.add_parser('ask')
    ask_parser.add_argument('--query', required=True)
    ask_parser.add_argument('--limit', type=int, default=10)
    ask_parser.add_argument('--effort', choices=EFFORT_LEVELS, default='medium')
    ask_parser.add_argument('--thread-id', type=int)
    ask_parser.add_argument('--thread-key')
    ask_parser.add_argument('--date-from')
    ask_parser.add_argument('--date-to')
    ask_parser.add_argument('--provider')
    ask_parser.add_argument('--model')
    ask_parser.set_defaults(handler=cmd_ask)

    search_people_parser = subparsers.add_parser('search-people')
    search_people_parser.add_argument('--query', required=True)
    search_people_parser.add_argument('--limit', type=int, default=10)
    search_people_parser.set_defaults(handler=cmd_search_people)

    promotions_parser = subparsers.add_parser('select-promotions')
    promotions_parser.add_argument('--limit', type=int, default=20)
    promotions_parser.add_argument('--record', action='store_true')
    promotions_parser.set_defaults(handler=cmd_select_promotions)

    fact_store_parser = subparsers.add_parser('promote-to-fact-store')
    fact_store_parser.add_argument('--limit', type=int, default=20)
    fact_store_parser.add_argument('--record', action='store_true')
    fact_store_parser.set_defaults(handler=cmd_promote_to_fact_store)

    export_fact_store_parser = subparsers.add_parser('export-fact-store-batch')
    export_fact_store_parser.add_argument('--limit', type=int, default=20)
    export_fact_store_parser.add_argument('--output', required=True)
    export_fact_store_parser.add_argument('--record', action='store_true')
    export_fact_store_parser.set_defaults(handler=cmd_export_fact_store_batch)

    promotion_status_parser = subparsers.add_parser('promotion-status')
    promotion_status_parser.set_defaults(handler=cmd_promotion_status)

    reseed_promotion_assets_parser = subparsers.add_parser('reseed-promotion-assets')
    reseed_promotion_assets_parser.add_argument('--force', action='store_true')
    reseed_promotion_assets_parser.set_defaults(handler=cmd_reseed_promotion_assets)

    set_promotion_llm_parser = subparsers.add_parser('set-promotion-llm-config')
    set_promotion_llm_parser.add_argument('--provider', required=True)
    set_promotion_llm_parser.add_argument('--model')
    set_promotion_llm_parser.add_argument('--max-candidates-per-batch', type=int, default=20)
    set_promotion_llm_parser.add_argument('--max-input-chars', type=int)
    set_promotion_llm_parser.add_argument('--soul-file')
    set_promotion_llm_parser.add_argument('--rulebook-file')
    set_promotion_llm_parser.set_defaults(handler=cmd_set_promotion_llm_config)

    plan_llm_parser = subparsers.add_parser('plan-llm-promotions')
    plan_llm_parser.add_argument('--limit', type=int, default=20)
    plan_llm_parser.set_defaults(handler=cmd_plan_llm_promotions)

    run_llm_parser = subparsers.add_parser('run-llm-promotions')
    run_llm_parser.add_argument('--limit', type=int, default=20)
    run_llm_parser.add_argument('--embed', action='store_true', help='Embed newly promoted holographic facts into the vector store')
    run_llm_parser.set_defaults(handler=cmd_run_llm_promotions)

    mark_fact_store_demoted_parser = subparsers.add_parser('mark-fact-store-demoted')
    mark_fact_store_demoted_parser.add_argument('--fact', action='append', dest='facts', required=True)
    mark_fact_store_demoted_parser.set_defaults(handler=cmd_mark_fact_store_demoted)

    mark_fact_store_edited_parser = subparsers.add_parser('mark-fact-store-edited')
    mark_fact_store_edited_parser.add_argument('--entry', action='append', dest='entries', required=True)
    mark_fact_store_edited_parser.set_defaults(handler=cmd_mark_fact_store_edited)

    mark_fact_store_parser = subparsers.add_parser('mark-fact-store-written')
    mark_fact_store_parser.add_argument('--batch-id', required=True)
    mark_fact_store_parser.add_argument('--fact', action='append', dest='facts', required=True)
    mark_fact_store_parser.set_defaults(handler=cmd_mark_fact_store_written)

    merge_person_parser = subparsers.add_parser('merge-person')
    merge_person_parser.add_argument('--primary-person-id', type=int, required=True)
    merge_person_parser.add_argument('--secondary-person-id', type=int, required=True)
    merge_person_parser.add_argument('--reason', required=True)
    merge_person_parser.set_defaults(handler=cmd_merge_person)

    split_person_parser = subparsers.add_parser('split-person')
    split_person_parser.add_argument('--source-person-id', type=int, required=True)
    split_person_parser.add_argument('--new-canonical-name', required=True)
    split_person_parser.add_argument('--email-address', action='append', dest='email_addresses', required=True)
    split_person_parser.add_argument('--reason', required=True)
    split_person_parser.set_defaults(handler=cmd_split_person)

    purge_parser = subparsers.add_parser('purge-folder')
    purge_parser.add_argument('--folder', action='append', dest='folders', required=True)
    purge_parser.add_argument('--dry-run', action='store_true')
    purge_parser.set_defaults(handler=cmd_purge_folder)

    set_excluded_parser = subparsers.add_parser('set-excluded-folders')
    set_excluded_parser.add_argument('--folder', action='append', dest='folders', required=True)
    set_excluded_parser.set_defaults(handler=cmd_set_excluded_folders)

    set_grace_parser = subparsers.add_parser('set-expiry-grace')
    set_grace_parser.add_argument('--days', type=int, required=True,
                                  help='Days a time-anchored memory is kept past its reference time before it is eligible for cleanup')
    set_grace_parser.set_defaults(handler=cmd_set_expiry_grace)

    cleanup_expired_parser = subparsers.add_parser('cleanup-expired')
    cleanup_expired_parser.add_argument('--grace-days', type=int, default=None,
                                        help='Override the persisted grace period for this run only')
    cleanup_expired_parser.add_argument('--apply', action='store_true',
                                        help='Actually delete; without it this is a dry run')
    cleanup_expired_parser.set_defaults(handler=cmd_cleanup_expired)

    status_parser = subparsers.add_parser('status')
    status_parser.set_defaults(handler=cmd_status)

    pipeline_status_parser = subparsers.add_parser('pipeline-status')
    pipeline_status_parser.set_defaults(handler=cmd_pipeline_status)

    repair_messages_index_parser = subparsers.add_parser('repair-messages-index')
    repair_messages_index_parser.set_defaults(handler=cmd_repair_messages_index)

    repair_entity_index_parser = subparsers.add_parser('repair-entity-index')
    repair_entity_index_parser.set_defaults(handler=cmd_repair_entity_index)

    repair_email_entity_index_parser = subparsers.add_parser('repair-email-entity-index')
    repair_email_entity_index_parser.set_defaults(handler=cmd_repair_email_entity_index)

    extract_threads_parser = subparsers.add_parser('extract-threads')
    extract_threads_parser.add_argument('--limit', type=int, default=20)
    extract_threads_parser.add_argument('--embed', action='store_true', help='Embed newly extracted entities into the vector store')
    extract_threads_parser.set_defaults(handler=cmd_extract_threads)

    extraction_status_parser = subparsers.add_parser('extraction-status')
    extraction_status_parser.set_defaults(handler=cmd_extraction_status)

    embed_backfill_parser = subparsers.add_parser('embed-backfill')
    embed_backfill_parser.add_argument('--batch-size', type=int, default=64)
    embed_backfill_parser.set_defaults(handler=cmd_embed_backfill)

    embed_status_parser = subparsers.add_parser('embed-status')
    embed_status_parser.set_defaults(handler=cmd_embed_status)

    setup_private_parser = subparsers.add_parser('setup-private')
    setup_private_parser.set_defaults(handler=cmd_setup_private)

    runtime_doctor_parser = subparsers.add_parser('runtime-doctor')
    runtime_doctor_parser.add_argument(
        '--require',
        action='append',
        choices=('mail', 'selected-llm'),
        default=[],
        dest='required_capabilities',
    )
    runtime_doctor_parser.set_defaults(handler=cmd_runtime_doctor)

    browse_parser = subparsers.add_parser('browse')
    browse_parser.add_argument('--read-only', action='store_true', default=False)
    browse_parser.add_argument('--snapshot', action='store_true', default=False,
                               help='Copy DB to a temp file before opening (allows browsing while a write process holds the lock)')
    browse_parser.set_defaults(handler=cmd_browse)

    return parser


def _open_store(args: argparse.Namespace) -> EmailMemoryStore:
    store = EmailMemoryStore(
        args.root,
        work_root=args.work_root,
        use_work_db=args.use_work_db,
        db_path=getattr(args, 'main_db', None),
        entity_db_path=getattr(args, 'entity_db', None),
        work_db_path=getattr(args, 'work_db', None),
    )
    store.initialize(start_date=getattr(args, 'start_date', None))
    return store


def _open_vector_store(path: str | Path) -> VectorStore:
    return VectorStore(Path(path).expanduser())


def _vector_store_path(args: argparse.Namespace) -> Path:
    configured = getattr(args, 'vector_store', None)
    return Path(configured).expanduser() if configured else Path(args.root).expanduser() / 'chroma'


def _mail_client(args: argparse.Namespace) -> HimalayaClient:
    executable = getattr(args, 'mail_client_executable', None)
    if executable is None:
        raise ValueError('the mail client executable is not configured')
    use_default_account = bool(
        getattr(args, '_use_verified_default_account', False)
    )
    client = HimalayaClient(
        binary=str(executable),
        use_default_account=use_default_account,
    )
    if use_default_account:
        client.require_unique_default_account(str(args.account))
    return client


def _scheduled_folder_policy(
    parser: argparse.ArgumentParser, variable: str
) -> list[str]:
    raw = os.environ.get(variable)
    try:
        value = json.loads(raw) if raw is not None else None
    except json.JSONDecodeError:
        value = None
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        parser.error(f'nightly-update requires valid {variable}')
    return [item.strip() for item in value]


def _runtime_llm_spec(args: argparse.Namespace, spec: LLMProviderSpec) -> LLMProviderSpec:
    executable_by_provider = {
        'hermes-default': getattr(args, 'hermes_executable', None),
        'codex-cli': getattr(args, 'codex_executable', None),
        'claude-code-cli': getattr(args, 'claude_executable', None),
    }
    executable = executable_by_provider.get(spec.name)
    if executable is None:
        raise ValueError('the selected LLM provider executable is not configured')
    return spec.bind_executable(str(executable))


def _runtime_llm_config(args: argparse.Namespace, store: EmailMemoryStore) -> PromotionLLMConfig:
    config = PromotionLLMConfig.from_dict(store.get_promotion_llm_config())
    spec = _runtime_llm_spec(args, config.provider)
    assert spec.executable is not None
    return config.bind_provider_executable(spec.executable)


def _maybe_checkpoint(store: EmailMemoryStore, args: argparse.Namespace) -> None:
    if getattr(args, 'use_work_db', False):
        store.checkpoint_to_durable()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_ingestion_report(
    store: EmailMemoryStore,
    *,
    command: str,
    started_at: str,
    result: dict[str, object] | None = None,
    error: str | None = None,
) -> dict[str, object]:
    report: dict[str, object] = {
        'command': command,
        'started_at': started_at,
        'finished_at': _utc_now_iso(),
        'ok': error is None,
        'error': error,
    }
    if result is not None:
        report.update(result)
    store.set_last_ingestion_report(report)
    return report


def _parse_fact_mappings(values: list[str]) -> dict[str, int]:
    fact_map: dict[str, int] = {}
    for value in values:
        dedup_key, fact_id = value.rsplit('=', 1)
        fact_map[dedup_key] = int(fact_id)
    return fact_map


def _parse_text_mappings(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        dedup_key, text = value.rsplit('=', 1)
        parsed[dedup_key] = text
    return parsed


def _parse_edit_entries(values: list[str]) -> dict[str, dict[str, str]]:
    parsed: dict[str, dict[str, str]] = {}
    for value in values:
        dedup_key, replacement_text, reason = value.split('|', 2)
        parsed[dedup_key] = {
            'replacement_text': replacement_text,
            'reason': reason,
        }
    return parsed


def cmd_init_db(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        print(json.dumps(store.stats(), indent=2, default=str))
    finally:
        store.close()


def cmd_ingest_folder(args: argparse.Namespace) -> None:
    store = _open_store(args)
    client = _mail_client(args)
    try:
        result = ingest_envelopes(
            store=store,
            client=client,
            account_name=args.account,
            email_address=args.email,
            folder_name=args.folder,
            page=args.page,
            page_size=args.page_size,
        )
        _maybe_checkpoint(store, args)
        print(json.dumps(result, indent=2, default=str))
    finally:
        store.close()


def cmd_ingest_account(args: argparse.Namespace) -> None:
    store = _open_store(args)
    client = _mail_client(args)
    try:
        result = ingest_account_folders(
            store=store,
            client=client,
            account_name=args.account,
            email_address=args.email,
            include_folders=args.include_folders,
            exclude_folders=args.exclude_folders,
            page=args.page,
            page_size=args.page_size,
        )
        _maybe_checkpoint(store, args)
        print(json.dumps(result, indent=2, default=str))
    finally:
        store.close()


def cmd_ingest_bodies(args: argparse.Namespace) -> None:
    store = _open_store(args)
    client = _mail_client(args)
    try:
        result = ingest_message_bodies(
            store=store,
            client=client,
            account_name=args.account,
            folder_name=args.folder,
            page=args.page,
            page_size=args.page_size,
        )
        _maybe_checkpoint(store, args)
        print(json.dumps(result, indent=2, default=str))
    finally:
        store.close()


def cmd_initial_ingest(args: argparse.Namespace) -> None:
    store = _open_store(args)
    _pre_flight_index_repair(store)
    client = _mail_client(args)
    started_at = _utc_now_iso()
    vector_store = _open_vector_store(_vector_store_path(args)) if getattr(args, 'embed', False) else None
    try:
        result = run_initial_ingestion(
            store=store,
            client=client,
            account_name=args.account,
            email_address=args.email,
            include_folders=args.include_folders,
            exclude_folders=args.exclude_folders,
            page_size=args.page_size,
            max_pages_per_folder=args.max_pages_per_folder,
        )
        _record_ingestion_report(store, command='initial-ingest', started_at=started_at, result=result)
        _maybe_checkpoint(store, args)
        if getattr(args, 'embed', False):
            result['embedded'] = embed_for_pipeline_event(store, event='ingestion', vector_store=vector_store)
        print(json.dumps(result, indent=2, default=str))
    except Exception as exc:
        _record_ingestion_report(store, command='initial-ingest', started_at=started_at, error=str(exc))
        _maybe_checkpoint(store, args)
        raise
    finally:
        store.close()


def cmd_nightly_update(args: argparse.Namespace) -> None:
    client = _mail_client(args)
    store = _open_store(args)
    _pre_flight_index_repair(store)
    started_at = _utc_now_iso()
    vector_store = _open_vector_store(_vector_store_path(args)) if getattr(args, 'embed', False) else None
    try:
        result = run_nightly_update(
            store=store,
            client=client,
            account_name=args.account,
            email_address=args.email,
            include_folders=args.include_folders,
            exclude_folders=args.exclude_folders,
            page_size=args.page_size,
            pages_per_folder=args.pages_per_folder,
        )
        _record_ingestion_report(store, command='nightly-update', started_at=started_at, result=result)
        _maybe_checkpoint(store, args)
        if getattr(args, 'embed', False):
            result['embedded'] = embed_for_pipeline_event(store, event='ingestion', vector_store=vector_store)
        print(json.dumps(result, indent=2, default=str))
    except Exception as exc:
        _record_ingestion_report(store, command='nightly-update', started_at=started_at, error=str(exc))
        _maybe_checkpoint(store, args)
        raise
    finally:
        store.close()


def cmd_backfill_rfc_metadata(args: argparse.Namespace) -> None:
    store = _open_store(args)
    client = _mail_client(args)
    started_at = _utc_now_iso()
    try:
        result = run_rfc_metadata_backfill(
            store=store,
            client=client,
            account_name=args.account,
            email_address=args.email,
            include_folders=args.include_folders,
            exclude_folders=args.exclude_folders,
            page_size=args.page_size,
            max_pages_per_folder=args.max_pages_per_folder,
        )
        _record_ingestion_report(store, command='backfill-rfc-metadata', started_at=started_at, result=result)
        _maybe_checkpoint(store, args)
        print(json.dumps(result, indent=2, default=str))
    except Exception as exc:
        _record_ingestion_report(store, command='backfill-rfc-metadata', started_at=started_at, error=str(exc))
        _maybe_checkpoint(store, args)
        raise
    finally:
        store.close()


def cmd_retry_failed_bodies(args: argparse.Namespace) -> None:
    store = _open_store(args)
    client = _mail_client(args)
    started_at = _utc_now_iso()
    try:
        result = run_failed_body_backfill(
            store=store,
            client=client,
            account_name=args.account,
            include_folders=args.folders,
            limit=args.limit,
        )
        _record_ingestion_report(store, command='retry-failed-bodies', started_at=started_at, result=result)
        _maybe_checkpoint(store, args)
        print(json.dumps(result, indent=2, default=str))
    except Exception as exc:
        _record_ingestion_report(store, command='retry-failed-bodies', started_at=started_at, error=str(exc))
        _maybe_checkpoint(store, args)
        raise
    finally:
        store.close()


def cmd_repair_messages_index(args: argparse.Namespace) -> None:
    store = _open_store(args)
    started_at = _utc_now_iso()
    try:
        result = rebuild_messages_table(store)
        _record_ingestion_report(store, command='repair-messages-index', started_at=started_at, result=result)
        _maybe_checkpoint(store, args)
        print(json.dumps(result, indent=2, default=str))
    except Exception as exc:
        _record_ingestion_report(store, command='repair-messages-index', started_at=started_at, error=str(exc))
        _maybe_checkpoint(store, args)
        raise
    finally:
        store.close()


def cmd_repair_entity_index(args: argparse.Namespace) -> None:
    store = _open_store(args)
    started_at = _utc_now_iso()
    try:
        result = rebuild_entity_message_index_table(store.entity_store)
        _record_ingestion_report(store, command='repair-entity-index', started_at=started_at, result=result)
        _maybe_checkpoint(store, args)
        print(json.dumps(result, indent=2, default=str))
    except Exception as exc:
        _record_ingestion_report(store, command='repair-entity-index', started_at=started_at, error=str(exc))
        _maybe_checkpoint(store, args)
        raise
    finally:
        store.close()


def cmd_repair_email_entity_index(args: argparse.Namespace) -> None:
    store = _open_store(args)
    started_at = _utc_now_iso()
    try:
        result = rebuild_email_entity_index_table(store)
        _record_ingestion_report(store, command='repair-email-entity-index', started_at=started_at, result=result)
        _maybe_checkpoint(store, args)
        print(json.dumps(result, indent=2, default=str))
    except Exception as exc:
        _record_ingestion_report(store, command='repair-email-entity-index', started_at=started_at, error=str(exc))
        _maybe_checkpoint(store, args)
        raise
    finally:
        store.close()


def _pre_flight_index_repair(store: EmailMemoryStore) -> None:
    """Rebuild both ART-indexed tables before ingestion starts.

    DuckDB 1.5.x can leave PRIMARY KEY + UNIQUE constraint indexes in a
    corrupt state after an internal assertion abort. The rebuild is
    idempotent and sub-second for typical mailbox sizes, so we run it
    unconditionally at the start of every ingestion command rather than
    trying to probe for corruption first (which requires complex
    connection-invalidation handling).
    """
    results = rebuild_all_indexed_tables(store)
    for r in results:
        print(f"[pre-flight] {r['table']} rebuilt: {r['rows_before']} rows, seq→{r['seq_advanced_to']}")


def cmd_repair_ingestion_state(args: argparse.Namespace) -> None:
    store = _open_store(args)
    client = _mail_client(args)
    started_at = _utc_now_iso()
    try:
        result = run_ingestion_state_repair(
            store=store,
            client=client,
            account_name=args.account,
            email_address=args.email,
            include_folders=args.include_folders,
            exclude_folders=args.exclude_folders,
            page_size=args.page_size,
            max_pages_per_folder=args.max_pages_per_folder,
        )
        _record_ingestion_report(store, command='repair-ingestion-state', started_at=started_at, result=result)
        _maybe_checkpoint(store, args)
        print(json.dumps(result, indent=2, default=str))
    except Exception as exc:
        _record_ingestion_report(store, command='repair-ingestion-state', started_at=started_at, error=str(exc))
        _maybe_checkpoint(store, args)
        raise
    finally:
        store.close()


def cmd_reconcile_ingestion_cursors(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        result = store.reconcile_ingest_sync_cursors(apply=args.apply)
        print(json.dumps(result, indent=2, default=str))
    finally:
        store.close()


def cmd_thread_lineage(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        print(json.dumps({'thread_lineages': store.get_thread_lineages(thread_keys=args.thread_keys)}, indent=2, default=str))
    finally:
        store.close()


def cmd_cross_folder_threads(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        print(json.dumps({'cross_folder_threads': store.list_cross_folder_threads(limit=args.limit, query=args.query)}, indent=2, default=str))
    finally:
        store.close()


def _build_retrieval_filters(args: argparse.Namespace) -> RetrievalFilters:
    date_from, date_to = parse_natural_date_range(args.query)
    if getattr(args, 'date_from', None):
        date_from = datetime.fromisoformat(args.date_from)
        if date_from.tzinfo is None:
            date_from = date_from.replace(tzinfo=timezone.utc)
    if getattr(args, 'date_to', None):
        date_to = datetime.fromisoformat(args.date_to)
        if date_to.tzinfo is None:
            date_to = date_to.replace(tzinfo=timezone.utc)
    return RetrievalFilters(
        date_from=date_from,
        date_to=date_to,
        thread_id=getattr(args, 'thread_id', None),
        thread_key=getattr(args, 'thread_key', None),
    )


def cmd_search(args: argparse.Namespace) -> None:
    if getattr(args, 'legacy', False):
        store = _open_store(args)
        try:
            service = EmailRetrievalService(store)
            print(json.dumps(service.search(query=args.query, limit=args.limit), indent=2, default=str))
        finally:
            store.close()
        return
    filters = _build_retrieval_filters(args)
    engine = RetrievalEngine(vector_store=_open_vector_store(_vector_store_path(args)))
    results = engine.search(args.query, effort=args.effort, limit=args.limit, filters=filters)
    payload = {
        'query': args.query,
        'effort': args.effort,
        'filters': {
            'date_from': filters.date_from.isoformat() if filters.date_from else None,
            'date_to': filters.date_to.isoformat() if filters.date_to else None,
            'thread_id': filters.thread_id,
            'thread_key': filters.thread_key,
        },
        'results': [
            {
                'collection': r.collection,
                'id': r.id,
                'score': r.score,
                'document': r.document,
                'metadata': r.metadata,
                'semantic_rank': r.semantic_rank,
                'lexical_rank': r.lexical_rank,
                'semantic_distance': r.semantic_distance,
            }
            for r in results
        ],
    }
    print(json.dumps(payload, indent=2, default=str))


def cmd_ask(args: argparse.Namespace) -> None:
    filters = _build_retrieval_filters(args)
    spec = None
    if args.provider:
        spec = _runtime_llm_spec(args, LLMProviderSpec(name=args.provider, model=args.model))
    else:
        spec = _runtime_llm_spec(args, LLMProviderSpec())
    answerer = Answerer(
        engine=RetrievalEngine(vector_store=_open_vector_store(_vector_store_path(args))),
        provider_spec=spec,
    )
    result = answerer.answer(
        args.query,
        effort=args.effort,
        limit=args.limit,
        filters=filters,
    )
    payload = {
        'query': result.query,
        'answer': result.answer,
        'used_handles': result.used_handles,
        'citations': [
            {
                'handle': c.handle,
                'collection': c.collection,
                'id': c.id,
                'metadata': c.metadata,
                'document': c.document,
            }
            for c in result.citations
        ],
    }
    print(json.dumps(payload, indent=2, default=str))


def cmd_search_people(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        print(json.dumps(store.search_people(query=args.query, limit=args.limit), indent=2, default=str))
    finally:
        store.close()


def cmd_select_promotions(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        service = EmailPromotionService(store)
        results = service.select_promotions(limit=args.limit)
        if args.record:
            service.record_promotions(results)
            _maybe_checkpoint(store, args)
        print(json.dumps(results, indent=2, default=str))
    finally:
        store.close()


def cmd_promote_to_fact_store(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        service = EmailPromotionService(store)
        results = service.select_fact_store_promotions(limit=args.limit)
        if args.record:
            service.record_fact_store_promotions(results)
            _maybe_checkpoint(store, args)
        print(json.dumps(results, indent=2, default=str))
    finally:
        store.close()


def cmd_export_fact_store_batch(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        service = EmailPromotionService(store)
        batch = service.export_fact_store_batch(limit=args.limit, output_path=Path(args.output))
        if args.record:
            service.record_fact_store_promotions(batch['items'])
            _maybe_checkpoint(store, args)
        print(json.dumps(batch, indent=2, default=str))
    finally:
        store.close()


def cmd_promotion_status(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        print(json.dumps({'promotion_llm_config': store.get_promotion_llm_config()}, indent=2, default=str))
    finally:
        store.close()


def cmd_reseed_promotion_assets(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        print(
            json.dumps(
                {
                    'promotion_assets': store.reseed_promotion_assets(force=args.force),
                    'force': bool(args.force),
                },
                indent=2,
                default=str,
            )
        )
    finally:
        store.close()


def cmd_set_promotion_llm_config(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        config = store.set_promotion_llm_config(
            {
                'provider': {'name': args.provider, 'model': args.model},
                'batching': {
                    'max_candidates_per_batch': args.max_candidates_per_batch,
                    'max_input_chars': args.max_input_chars,
                },
                'soul': {'path': args.soul_file},
                'rulebook': {'path': args.rulebook_file},
            }
        )
        _maybe_checkpoint(store, args)
        print(json.dumps({'promotion_llm_config': config}, indent=2, default=str))
    finally:
        store.close()


def cmd_plan_llm_promotions(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        service = EmailPromotionService(store)
        print(json.dumps(service.build_llm_promotion_plan(
            limit=args.limit,
            config=_runtime_llm_config(args, store),
        ), indent=2, default=str))
    finally:
        store.close()


def cmd_run_llm_promotions(args: argparse.Namespace) -> None:
    store = _open_store(args)
    vector_store = _open_vector_store(_vector_store_path(args)) if getattr(args, 'embed', False) else None
    try:
        service = EmailPromotionService(store)
        result = service.execute_and_commit_llm_promotions(
            limit=args.limit,
            config=_runtime_llm_config(args, store),
            holographic_db_path=getattr(args, 'fact_store_db', None),
        )
        _maybe_checkpoint(store, args)
        if getattr(args, 'embed', False):
            result['embedded'] = embed_for_pipeline_event(
                store,
                event='promotion',
                vector_store=vector_store,
                fact_store_db_path=getattr(args, 'fact_store_db', None),
            )
        print(json.dumps(result, indent=2, default=str))
    except Exception:
        _maybe_checkpoint(store, args)
        raise
    finally:
        store.close()


def cmd_mark_fact_store_demoted(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        service = EmailPromotionService(store)
        updated = service.mark_fact_store_demoted(_parse_text_mappings(args.facts))
        _maybe_checkpoint(store, args)
        print(json.dumps({'updated': updated}, indent=2, default=str))
    finally:
        store.close()


def cmd_mark_fact_store_edited(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        service = EmailPromotionService(store)
        updated = service.mark_fact_store_edited(_parse_edit_entries(args.entries))
        _maybe_checkpoint(store, args)
        print(json.dumps({'updated': updated}, indent=2, default=str))
    finally:
        store.close()


def cmd_mark_fact_store_written(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        service = EmailPromotionService(store)
        updated = service.mark_fact_store_written(
            batch_id=args.batch_id,
            fact_map=_parse_fact_mappings(args.facts),
        )
        _maybe_checkpoint(store, args)
        print(json.dumps({'batch_id': args.batch_id, 'updated': updated}, indent=2, default=str))
    finally:
        store.close()


def cmd_merge_person(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        person_id = store.merge_people(
            primary_person_id=args.primary_person_id,
            secondary_person_id=args.secondary_person_id,
            reason=args.reason,
        )
        _maybe_checkpoint(store, args)
        print(json.dumps({'person_id': person_id, 'status': 'merged'}, indent=2, default=str))
    finally:
        store.close()


def cmd_split_person(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        person_id = store.split_person(
            source_person_id=args.source_person_id,
            new_canonical_name=args.new_canonical_name,
            email_addresses=args.email_addresses,
            reason=args.reason,
        )
        _maybe_checkpoint(store, args)
        print(json.dumps({'person_id': person_id, 'status': 'split'}, indent=2, default=str))
    finally:
        store.close()


def cmd_purge_folder(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        result = store.purge_messages_by_folders(args.folders, dry_run=args.dry_run)
        if not args.dry_run:
            _maybe_checkpoint(store, args)
        print(json.dumps(result, indent=2, default=str))
    finally:
        store.close()


def cmd_set_excluded_folders(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        folders = store.set_excluded_folders(args.folders)
        _maybe_checkpoint(store, args)
        print(json.dumps({'excluded_folders': folders}, indent=2, default=str))
    finally:
        store.close()


def cmd_set_expiry_grace(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        days = store.set_expiry_grace_days(args.days)
        _maybe_checkpoint(store, args)
        print(json.dumps({'expiry_grace_days': days}, indent=2, default=str))
    finally:
        store.close()


def cmd_cleanup_expired(args: argparse.Namespace) -> None:
    store = _open_store(args)
    vector_store = _open_vector_store(_vector_store_path(args))
    try:
        result = store.cleanup_expired_time_anchors(grace_days=args.grace_days, dry_run=not args.apply)
        # Expired rows are embedded retrieval sources; prune their vectors so
        # search/ask stop surfacing rows that no longer exist.
        pruned = 0
        deleted_deadlines = result.get('deleted_deadline_ids') or []
        deleted_action_items = result.get('deleted_action_item_ids') or []
        deleted_calendar_events = result.get('deleted_calendar_event_ids') or []
        if deleted_deadlines:
            vector_store.delete('deadlines', [str(i) for i in deleted_deadlines])
            pruned += len(deleted_deadlines)
        if deleted_action_items:
            vector_store.delete('action_items', [str(i) for i in deleted_action_items])
            pruned += len(deleted_action_items)
        if deleted_calendar_events:
            vector_store.delete('calendar_events', [str(i) for i in deleted_calendar_events])
            pruned += len(deleted_calendar_events)
        if args.apply:
            _maybe_checkpoint(store, args)
        result['vectors_pruned'] = pruned
        print(json.dumps(result, indent=2, default=str))
    finally:
        store.close()


def cmd_status(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        print(json.dumps(store.stats(), indent=2, default=str))
    finally:
        store.close()


def _count_holographic_fact_sources(db_path: Path) -> int | None:
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(db_path) as conn:
            return int(require_scalar(conn.execute("SELECT COUNT(*) FROM facts").fetchone(), operation='count holographic facts'))
    except sqlite3.Error:
        return None


def _count_message_chunk_sources(store: EmailMemoryStore) -> int:
    total = 0
    for (cleaned_text,) in store.conn.execute(
        "SELECT cleaned_text FROM messages WHERE cleaned_text IS NOT NULL AND cleaned_text <> ''"
    ).fetchall():
        total += len(chunk_text(cleaned_text))
    return total


def _count_calendar_event_sources(store: EmailMemoryStore) -> int:
    return int(
        require_scalar(store.conn.execute(
            """
            SELECT COUNT(*)
            FROM calendar_events
            WHERE COALESCE(summary, '') <> ''
               OR COALESCE(description, '') <> ''
               OR COALESCE(organizer, '') <> ''
               OR COALESCE(organizer_email, '') <> ''
               OR COALESCE(location, '') <> ''
               OR COALESCE(status, '') <> ''
               OR COALESCE(method, '') <> ''
               OR starts_at IS NOT NULL
               OR ends_at IS NOT NULL
               OR COALESCE(attendees_json, '') <> ''
               OR COALESCE(uid, '') <> ''
            """
        ).fetchone(), operation='count calendar event sources')
    )


def _build_promotion_health(store: EmailMemoryStore) -> dict[str, object]:
    status_counts = {
        (status or 'pending'): int(count)
        for status, count in store.conn.execute(
            "SELECT status, COUNT(*) FROM promotion_log GROUP BY 1 ORDER BY 1"
        ).fetchall()
    }
    latest_row = require_row(store.conn.execute(
        """
        SELECT
            MAX(promoted_at),
            MAX(fact_store_written_at),
            MAX(demoted_at),
            MAX(revised_at)
        FROM promotion_log
        """
    ).fetchone(), operation='load latest promotion timestamps')
    llm_config = PromotionLLMConfig.from_dict(store.get_promotion_llm_config())
    written_without_fact_id = int(
        require_scalar(store.conn.execute(
            """
            SELECT COUNT(*)
            FROM promotion_log
            WHERE status = 'fact_store_written' AND holographic_fact_id IS NULL
            """
        ).fetchone(), operation='count written promotions without fact ids')
    )
    return {
        'configured_provider': llm_config.provider.name,
        'configured_model': llm_config.provider.model,
        'batching': {
            'max_candidates_per_batch': llm_config.batching.max_candidates_per_batch,
            'max_input_chars': llm_config.batching.max_input_chars,
        },
        'status_counts': status_counts,
        'ready_for_fact_store': status_counts.get('fact_store_ready', 0),
        'written_without_fact_id': written_without_fact_id,
        'latest_promoted_at': latest_row[0],
        'latest_fact_store_written_at': latest_row[1],
        'latest_demoted_at': latest_row[2],
        'latest_revised_at': latest_row[3],
    }


def _build_retrieval_health(
    store: EmailMemoryStore,
    root: str | Path,
    fact_store_db: str | Path | None = None,
) -> dict[str, object]:
    vector_store = _open_vector_store(root)
    expected_rows = {
        'holographic_facts': _count_holographic_fact_sources(
            Path(fact_store_db) if fact_store_db is not None else default_holographic_db_path()
        ),
        'action_items': int(require_scalar(store.conn.execute("SELECT COUNT(*) FROM action_items").fetchone(), operation='count action items')),
        'deadlines': int(require_scalar(store.conn.execute("SELECT COUNT(*) FROM deadlines").fetchone(), operation='count deadlines')),
        'decisions': int(require_scalar(store.conn.execute("SELECT COUNT(*) FROM decisions").fetchone(), operation='count decisions')),
        'thread_summaries': int(require_scalar(store.conn.execute("SELECT COUNT(*) FROM thread_summaries").fetchone(), operation='count thread summaries')),
        'message_chunks': _count_message_chunk_sources(store),
        'calendar_events': _count_calendar_event_sources(store),
    }
    collections: dict[str, dict[str, int]] = {}
    collections_with_drift: list[str] = []
    total_vectors = 0
    for name in COLLECTION_NAMES:
        vectors = int(vector_store.count(name))
        total_vectors += vectors
        summary: dict[str, int] = {'vectors': vectors}
        source_rows = expected_rows.get(name)
        if source_rows is not None:
            summary['source_rows'] = int(source_rows)
            summary['delta'] = vectors - int(source_rows)
            if summary['delta'] != 0:
                collections_with_drift.append(name)
        collections[name] = summary
    return {
        'persist_path': str(vector_store._path),
        'collections': collections,
        'collections_with_drift': collections_with_drift,
        'total_vectors': total_vectors,
    }


def _build_cleanup_expired_health(store: EmailMemoryStore) -> dict[str, object]:
    result = store.cleanup_expired_time_anchors(dry_run=True)
    result['total_matched'] = (
        int(result.get('deadlines_matched', 0))
        + int(result.get('action_items_matched', 0))
        + int(result.get('calendar_events_matched', 0))
    )
    return result


def cmd_pipeline_status(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        payload = store.pipeline_status()
        payload['promotion'] = _build_promotion_health(store)
        payload['retrieval'] = _build_retrieval_health(
            store,
            _vector_store_path(args),
            getattr(args, 'fact_store_db', None),
        )
        payload['cleanup_expired'] = _build_cleanup_expired_health(store)
        print(json.dumps(payload, indent=2, default=str))
    finally:
        store.close()

def cmd_extract_threads(args: argparse.Namespace) -> None:
    store = _open_store(args)
    vector_store = _open_vector_store(_vector_store_path(args)) if getattr(args, 'embed', False) else None
    try:
        raw_config = store.get_promotion_llm_config()
        llm_config = PromotionLLMConfig.from_dict(raw_config)
        spec = _runtime_llm_spec(args, llm_config.provider)
        service = ExtractionService(store)
        result = service.run_extraction(limit=args.limit, spec=spec)
        _maybe_checkpoint(store, args)
        if getattr(args, 'embed', False):
            result['embedded'] = embed_for_pipeline_event(store, event='extraction', vector_store=vector_store)
        print(json.dumps(result, indent=2, default=str))
    finally:
        store.close()


def cmd_embed_backfill(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        counts = backfill_all(
            store=store,
            batch_size=args.batch_size,
            vector_store=_open_vector_store(_vector_store_path(args)),
            fact_store_db_path=(
                Path(args.fact_store_db) if getattr(args, 'fact_store_db', None) else None
            ),
        )
        print(json.dumps({'embedded': counts}, indent=2, default=str))
    finally:
        store.close()


def cmd_embed_status(args: argparse.Namespace) -> None:
    vector_store = _open_vector_store(_vector_store_path(args))
    counts = {name: vector_store.count(name) for name in COLLECTION_NAMES}
    print(json.dumps({'collections': counts, 'persist_path': str(vector_store._path)}, indent=2, default=str))


def cmd_setup_private(_args: argparse.Namespace) -> None:
    """Launch the local runtime attachment bootstrap without importing it at startup."""
    from .tui.private_setup import main as private_setup_main

    private_setup_main()


def cmd_runtime_doctor(args: argparse.Namespace) -> None:
    """Report runtime attachment health without printing configured paths."""
    storage = {
        'runtime_root': Path(args.root).is_dir(),
        'main_db': Path(args.main_db).is_file(),
        'entity_db': Path(args.entity_db).is_file(),
        'vector_store': Path(args.vector_store).is_dir(),
    }
    if getattr(args, 'work_db', None):
        storage['work_db'] = Path(args.work_db).is_file()
    if getattr(args, 'fact_store_db', None):
        storage['fact_store_db'] = Path(args.fact_store_db).is_file()

    executables: dict[str, dict[str, bool]] = {}
    for name, field in (
        ('himalaya', 'mail_client_executable'),
        ('hermes', 'hermes_executable'),
        ('codex', 'codex_executable'),
        ('claude', 'claude_executable'),
    ):
        configured = getattr(args, field, None)
        path = Path(configured) if configured else None
        executables[name] = {
            'configured': path is not None,
            'usable': bool(path and path.is_file() and os.access(path, os.X_OK)),
        }
    required = set(getattr(args, 'required_capabilities', []) or [])
    selected_llm_config_readable = True
    selected_provider = 'hermes-default'
    if 'selected-llm' in required and Path(args.main_db).is_file():
        try:
            import duckdb

            connection = duckdb.connect(str(args.main_db), read_only=True)
            try:
                table_row = connection.execute(
                    "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'metadata'"
                ).fetchone()
                table_exists = bool(table_row and table_row[0])
                if table_exists:
                    row = connection.execute(
                        "SELECT value FROM metadata WHERE key = 'promotion_llm_config'"
                    ).fetchone()
                    if row:
                        raw_config = json.loads(row[0])
                        selected_provider = LLMProviderSpec.from_dict(
                            (raw_config or {}).get('provider')
                        ).name
            finally:
                connection.close()
        except Exception:
            selected_llm_config_readable = False
    executable_name_by_provider = {
        'hermes-default': 'hermes',
        'codex-cli': 'codex',
        'claude-code-cli': 'claude',
    }
    selected_executable_name = executable_name_by_provider.get(selected_provider)
    selected_llm_ready = bool(
        selected_llm_config_readable
        and selected_executable_name
        and executables[selected_executable_name]['usable']
    )
    capabilities = {
        'mail_required': 'mail' in required,
        'mail_ready': executables['himalaya']['usable'],
        'selected_llm_required': 'selected-llm' in required,
        'selected_llm_config_readable': selected_llm_config_readable,
        'selected_llm_ready': selected_llm_ready,
    }
    payload = {
        'schema': 'runtime-v2-or-compatible',
        'storage': storage,
        'executables': executables,
        'capabilities': capabilities,
        'paths_redacted': True,
    }
    payload['ok'] = all(storage.values())
    if capabilities['mail_required']:
        payload['ok'] = payload['ok'] and capabilities['mail_ready']
    if capabilities['selected_llm_required']:
        payload['ok'] = payload['ok'] and capabilities['selected_llm_ready']
    print(json.dumps(payload, indent=2))
    if not payload['ok']:
        raise SystemExit(1)


def _copy_database_snapshot(source: Path, destination: Path) -> None:
    """Copy one configured database and its optional WAL under a stable snapshot name."""
    import shutil

    shutil.copy2(source, destination)
    source_wal = Path(f"{source}.wal")
    if source_wal.is_file():
        shutil.copy2(source_wal, Path(f"{destination}.wal"))


def _run_browser(
    args: argparse.Namespace,
    *,
    root: str | Path,
    main_db: Path,
    entity_db: Path,
    work_db: Path | None,
    snapshot: bool,
) -> None:
    store = EmailMemoryStore(
        root,
        work_root=None if snapshot else args.work_root,
        use_work_db=args.use_work_db,
        read_only=True if snapshot else getattr(args, 'read_only', False),
        db_path=main_db,
        entity_db_path=entity_db,
        work_db_path=work_db,
    )
    try:
        from .tui import launch_browser
        persisted = PromotionLLMConfig.from_dict(store.get_promotion_llm_config())
        try:
            provider_spec = _runtime_llm_spec(args, persisted.provider)
            provider_error = None
        except ValueError:
            provider_spec = None
            provider_error = 'LLM provider executable is not configured.'
        launch_browser(
            store,
            vector_store=_open_vector_store(_vector_store_path(args)),
            provider_spec=provider_spec,
            provider_error=provider_error,
        )
    finally:
        store.close()


def cmd_browse(args: argparse.Namespace) -> None:
    import tempfile

    main_db = Path(getattr(args, 'main_db', None) or Path(args.root) / 'email_memory.duckdb')
    entity_db = Path(
        getattr(args, 'entity_db', None) or Path(args.root) / 'entity_memory.duckdb'
    )
    work_db_value = getattr(args, 'work_db', None)
    work_db = Path(work_db_value) if work_db_value else None
    if not getattr(args, 'snapshot', False):
        _run_browser(
            args,
            root=args.root,
            main_db=main_db,
            entity_db=entity_db,
            work_db=work_db,
            snapshot=False,
        )
        return

    with tempfile.TemporaryDirectory(prefix='ems_browse_') as snapshot_dir:
        destination = Path(snapshot_dir)
        snapshot_main_db = destination / 'main.duckdb'
        snapshot_entity_db = destination / 'entity.duckdb'
        _copy_database_snapshot(main_db, snapshot_main_db)
        _copy_database_snapshot(entity_db, snapshot_entity_db)
        snapshot_work_db = None
        if args.use_work_db and work_db is not None:
            snapshot_work_db = destination / 'work.duckdb'
            _copy_database_snapshot(work_db, snapshot_work_db)
        print(f'[browse] snapshot copied to {snapshot_dir}')
        _run_browser(
            args,
            root=snapshot_dir,
            main_db=snapshot_main_db,
            entity_db=snapshot_entity_db,
            work_db=snapshot_work_db,
            snapshot=True,
        )


def cmd_extraction_status(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        threads_total = require_scalar(store.conn.execute("SELECT COUNT(*) FROM threads").fetchone(), operation='count threads')
        threads_extracted = require_scalar(store.conn.execute("SELECT COUNT(DISTINCT thread_id) FROM thread_summaries").fetchone(), operation='count extracted threads')
        threads_pending = require_scalar(store.conn.execute(
            """
            SELECT COUNT(DISTINCT t.thread_id)
            FROM threads t
            JOIN messages m ON m.thread_key = t.thread_key
            LEFT JOIN thread_summaries ts ON ts.thread_id = t.thread_id
            WHERE ts.summary_id IS NULL
              AND m.cleaned_text IS NOT NULL
              AND length(trim(m.cleaned_text)) > 0
            """
        ).fetchone(), operation='count pending threads')
        action_items_count = require_scalar(store.conn.execute("SELECT COUNT(*) FROM action_items").fetchone(), operation='count action items')
        deadlines_count = require_scalar(store.conn.execute("SELECT COUNT(*) FROM deadlines").fetchone(), operation='count deadlines')
        decisions_count = require_scalar(store.conn.execute("SELECT COUNT(*) FROM decisions").fetchone(), operation='count decisions')
        thread_summaries_count = require_scalar(store.conn.execute("SELECT COUNT(*) FROM thread_summaries").fetchone(), operation='count thread summaries')
        status = {
            'threads_total': int(threads_total),
            'threads_extracted': int(threads_extracted),
            'threads_pending': int(threads_pending),
            'action_items': int(action_items_count),
            'deadlines': int(deadlines_count),
            'decisions': int(decisions_count),
            'thread_summaries': int(thread_summaries_count),
        }
        print(json.dumps(status, indent=2))
    finally:
        store.close()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == 'nightly-update':
        account_from_environment = not bool(args.account)
        if not args.account:
            args.account = os.environ.get('EMAIL_MEMORY_ACCOUNT_NAME')
        if not args.account or not args.account.strip():
            parser.error(
                'nightly-update requires --account or EMAIL_MEMORY_ACCOUNT_NAME'
            )
        args.account = args.account.strip()
        if not args.email:
            args.email = os.environ.get('EMAIL_MEMORY_ACCOUNT_EMAIL')
        if not args.email or not args.email.strip():
            parser.error(
                'nightly-update requires --email or EMAIL_MEMORY_ACCOUNT_EMAIL'
            )
        args.email = args.email.strip()
        args._use_verified_default_account = bool(
            account_from_environment
            and os.environ.get(
                'EMAIL_MEMORY_INTERNAL_VERIFIED_DEFAULT_ACCOUNT'
            ) == '1'
        )
        if args._use_verified_default_account:
            if args.include_folders is None:
                args.include_folders = _scheduled_folder_policy(
                    parser, 'EMAIL_MEMORY_INCLUDE_FOLDERS_JSON'
                )
            if args.exclude_folders is None:
                args.exclude_folders = _scheduled_folder_policy(
                    parser, 'EMAIL_MEMORY_EXCLUDE_FOLDERS_JSON'
                )
    try:
        runtime = resolve_runtime_settings(
            runtime_root=args.root,
            work_root=args.work_root,
            fact_store_db=args.fact_store_db,
            runtime_config=args.runtime_config,
        )
    except (OSError, RuntimeError, ValueError):
        if args.command != 'runtime-doctor':
            raise
        print(json.dumps({
            'ok': False,
            'error': 'runtime manifest is unavailable or invalid',
            'paths_redacted': True,
        }, indent=2))
        raise SystemExit(2) from None
    args.root = str(runtime.runtime_root)
    args.work_root = str(runtime.work_root) if runtime.work_root else None
    args.fact_store_db = str(runtime.fact_store_db) if runtime.fact_store_db else None
    args.main_db = str(runtime.main_db)
    args.entity_db = str(runtime.entity_db)
    args.vector_store = str(runtime.vector_store)
    args.work_db = str(runtime.work_db) if runtime.work_db else None
    args.mail_client_executable = runtime.mail_client_executable
    args.hermes_executable = runtime.hermes_executable
    args.codex_executable = runtime.codex_executable
    args.claude_executable = runtime.claude_executable
    args.handler(args)


if __name__ == '__main__':
    main()

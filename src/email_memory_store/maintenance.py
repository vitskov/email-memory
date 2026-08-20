from __future__ import annotations

from typing import Any

import duckdb

from .db_rows import require_scalar
from .entity_schema import ENTITY_SCHEMA_SQL
from .entity_store import EntityMemoryStore
from .schema import SCHEMA_SQL
from .store import EmailMemoryStore


# Tables in the main DB that have both a sequence-backed PRIMARY KEY and a
# UNIQUE constraint. DuckDB 1.5.x can leave both ART indexes in a corrupt
# state after an internal assertion abort, so every one of these tables must
# be rebuilt before ingestion begins.
#
# Each entry: (table_name, pk_column, sequence_name)
_MAIN_DB_INDEXED_TABLES: tuple[tuple[str, str, str], ...] = (
    ('accounts',                    'account_id',                    'seq_account_id'),
    ('folders',                     'folder_id',                     'seq_folder_id'),
    ('messages',                    'message_pk',                    'seq_message_pk'),
    ('message_labels',              'message_label_id',              'seq_message_label_id'),
    ('failed_message_ingestions',   'failed_message_ingestion_id',   'seq_failed_message_ingestion_id'),
    ('email_entity_index',          'email_entity_index_id',         'seq_email_entity_index_id'),
    ('threads',                     'thread_id',                     'seq_thread_id'),
    ('contacts',                    'contact_id',                    'seq_contact_id'),
)

_ENTITY_DB_INDEXED_TABLES: tuple[tuple[str, str, str], ...] = (
    ('people',                  'person_id',                    'seq_person_id'),
    ('person_emails',           'person_email_id',              'seq_person_email_id'),
    ('person_aliases',          'person_alias_id',              'seq_person_alias_id'),
    ('message_entity_index',    'message_entity_index_id',      'seq_message_entity_index_id'),
)


def _rebuild_indexed_table(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    pk_col: str,
    seq_name: str,
    schema_sql: str,
) -> dict[str, Any]:
    """Rebuild one table to clear ART index corruption.

    Pattern: copy rows to staging → drop original → re-run schema_sql
    (IF NOT EXISTS guards skip already-present tables) → insert back →
    drop staging → advance sequence past max PK seen.
    """
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()]
    cols_csv = ', '.join(cols)

    before_count = int(require_scalar(conn.execute(f'SELECT COUNT(*) FROM {table_name}').fetchone(), operation=f'count {table_name} before rebuild'))
    max_pk = int(require_scalar(conn.execute(f'SELECT COALESCE(MAX({pk_col}), 0) FROM {table_name}').fetchone(), operation=f'find maximum {pk_col}'))

    staging = f'{table_name}_rebuild_staging'
    conn.execute(f'CREATE TABLE {staging} AS SELECT {cols_csv} FROM {table_name}')
    staging_count = int(require_scalar(conn.execute(f'SELECT COUNT(*) FROM {staging}').fetchone(), operation=f'count staged {table_name} rows'))
    if staging_count != before_count:
        conn.execute(f'DROP TABLE IF EXISTS {staging}')
        raise RuntimeError(
            f'staging row count mismatch for {table_name}: {staging_count} vs {before_count}'
        )

    conn.execute(f'DROP TABLE {table_name}')
    conn.execute(schema_sql)
    conn.execute(f'INSERT INTO {table_name}({cols_csv}) SELECT {cols_csv} FROM {staging}')
    after_count = int(require_scalar(conn.execute(f'SELECT COUNT(*) FROM {table_name}').fetchone(), operation=f'count {table_name} after rebuild'))
    conn.execute(f'DROP TABLE {staging}')
    if after_count != before_count:
        raise RuntimeError(
            f'restored row count mismatch for {table_name}: {after_count} vs {before_count}'
        )

    advanced_to = 0
    if max_pk > 0:
        target = max_pk + 1
        while True:
            advanced_to = int(require_scalar(conn.execute(f"SELECT nextval('{seq_name}')").fetchone(), operation=f'advance {seq_name}'))
            if advanced_to >= target:
                break

    return {
        'table': table_name,
        'rows_before': before_count,
        'rows_after': after_count,
        f'max_{pk_col}': max_pk,
        'seq_advanced_to': advanced_to,
    }


def rebuild_all_indexed_tables(store: EmailMemoryStore) -> list[dict[str, Any]]:
    """Rebuild every table in both DBs that has PK + UNIQUE ART indexes.

    Idempotent. Run this before any ingestion to prevent DuckDB 1.5.x
    corruption crashes.
    """
    store.conn.execute('CHECKPOINT')
    store.entity_store.conn.execute('CHECKPOINT')

    results = []
    for table_name, pk_col, seq_name in _MAIN_DB_INDEXED_TABLES:
        results.append(_rebuild_indexed_table(store.conn, table_name, pk_col, seq_name, SCHEMA_SQL))
    for table_name, pk_col, seq_name in _ENTITY_DB_INDEXED_TABLES:
        results.append(_rebuild_indexed_table(store.entity_store.conn, table_name, pk_col, seq_name, ENTITY_SCHEMA_SQL))

    store.conn.execute('CHECKPOINT')
    store.entity_store.conn.execute('CHECKPOINT')
    return results


# ---------------------------------------------------------------------------
# Per-table convenience wrappers kept for backward compatibility with the
# repair-messages-index / repair-entity-index / repair-email-entity-index
# CLI commands and existing tests.
# ---------------------------------------------------------------------------

MESSAGES_COLUMNS: tuple[str, ...] = (
    'message_pk', 'account_id', 'folder_id', 'mailbox_message_id',
    'stable_message_id', 'identity_source', 'internet_message_id',
    'rfc_in_reply_to', 'rfc_references_json', 'thread_key', 'subject',
    'normalized_subject', 'from_name', 'from_addr', 'to_addrs', 'cc_addrs',
    'bcc_addrs', 'sent_at', 'received_at', 'cleaned_text', 'text_hash',
    'has_attachments', 'importance_score', 'direction', 'is_read',
    'raw_path', 'ingestion_run_id', 'created_at', 'updated_at',
)


def rebuild_messages_table(store: EmailMemoryStore) -> dict[str, Any]:
    return _rebuild_indexed_table(store.conn, 'messages', 'message_pk', 'seq_message_pk', SCHEMA_SQL)


EMAIL_ENTITY_INDEX_COLUMNS: tuple[str, ...] = (
    'email_entity_index_id', 'message_pk', 'stable_message_id', 'person_id',
    'canonical_name', 'normalized_name', 'role', 'email_address',
    'created_at', 'updated_at',
)


def rebuild_email_entity_index_table(store: EmailMemoryStore) -> dict[str, Any]:
    return _rebuild_indexed_table(store.conn, 'email_entity_index', 'email_entity_index_id', 'seq_email_entity_index_id', SCHEMA_SQL)


MESSAGE_ENTITY_INDEX_COLUMNS: tuple[str, ...] = (
    'message_entity_index_id', 'person_id', 'email_message_pk',
    'stable_message_id', 'canonical_name', 'normalized_name',
    'role', 'email_address', 'created_at', 'updated_at',
)


def rebuild_entity_message_index_table(entity_store: EntityMemoryStore) -> dict[str, Any]:
    return _rebuild_indexed_table(entity_store.conn, 'message_entity_index', 'message_entity_index_id', 'seq_message_entity_index_id', ENTITY_SCHEMA_SQL)

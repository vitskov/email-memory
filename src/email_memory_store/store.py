from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import shlex
from typing import Any

from .promotion.assets import seed_runtime_promotion_assets
from .timeutils import normalize_date_only
from .promotion.llm import normalize_promotion_llm_config, promotion_llm_config_from_json

import duckdb

from .config import EmailMemoryPaths
from .db_rows import require_row, require_scalar
from .entity_store import EntityMemoryStore
from .schema import SCHEMA_SQL


_CURSOR_CONTINUATION_COMMANDS = {
    'initial_envelopes': 'initial-ingest',
    'rfc_metadata_backfill': 'backfill-rfc-metadata',
    'repair_bodies': 'repair-ingestion-state',
}

_CONTINUATION_FOLDER_FLAGS = {
    'initial-ingest': '--include-folder',
    'backfill-rfc-metadata': '--include-folder',
    'repair-ingestion-state': '--folder',
}


def _classify_sync_continuation_state(*, status: str, next_page: int | None, last_completed_page: int | None) -> str:
    if status == 'partial':
        return 'needs_attention'
    if status != 'in_progress':
        return 'idle'
    if next_page is not None and last_completed_page is not None and next_page == last_completed_page + 1:
        return 'resume_ready'
    return 'inspect_cursor'


def _build_continuation_command(*, command: str, account_name: str, email_address: str, folders: list[str]) -> str:
    folder_flag = _CONTINUATION_FOLDER_FLAGS[command]
    parts = [
        'email-memory-store',
        command,
        '--account',
        account_name,
        '--email',
        email_address,
    ]
    for folder in folders:
        parts.extend([folder_flag, folder])
    return shlex.join(parts)


class EmailMemoryStore:
    DEFAULT_DUCKDB_MEMORY_LIMIT = '4GB'
    DEFAULT_DUCKDB_THREADS = 4
    BODY_PERSISTENCE_THREADS = 1

    def __init__(
        self,
        root: str | Path,
        work_root: str | Path | None = None,
        use_work_db: bool = False,
        read_only: bool = False,
    ):
        self.paths = EmailMemoryPaths.from_root(root, work_root=work_root)
        self.paths.ensure_dirs()
        chosen_db = self.paths.work_db_path if use_work_db and self.paths.work_db_path else self.paths.db_path
        self._conn = duckdb.connect(str(chosen_db), read_only=read_only)
        self._active_db_path = chosen_db
        self._body_persistence_temp_dir = self.paths.cache_dir / 'duckdb_body_persistence.tmp'
        self.entity_store = EntityMemoryStore(self.paths.entity_db_path, read_only=read_only)

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        return self._conn

    @property
    def active_db_path(self) -> Path:
        return self._active_db_path

    def initialize(self, start_date: str | None = '2022-01-02') -> None:
        self._conn.execute(f"SET memory_limit='{self.DEFAULT_DUCKDB_MEMORY_LIMIT}'")
        self._conn.execute(f"SET threads TO {self.DEFAULT_DUCKDB_THREADS}")
        self._conn.execute(SCHEMA_SQL)
        self._conn.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS mailbox_message_id VARCHAR")
        self._conn.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS identity_source VARCHAR DEFAULT 'provisional'")
        self._conn.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS rfc_in_reply_to VARCHAR")
        self._conn.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS rfc_references_json JSON")
        self._conn.execute("ALTER TABLE promotion_log ADD COLUMN IF NOT EXISTS fact_store_dedup_key VARCHAR")
        self._conn.execute("ALTER TABLE promotion_log ADD COLUMN IF NOT EXISTS fact_store_batch_id VARCHAR")
        self._conn.execute("ALTER TABLE promotion_log ADD COLUMN IF NOT EXISTS fact_store_written_at TIMESTAMP")
        self._conn.execute("ALTER TABLE promotion_log ADD COLUMN IF NOT EXISTS demoted_at TIMESTAMP")
        self._conn.execute("ALTER TABLE promotion_log ADD COLUMN IF NOT EXISTS demotion_reason VARCHAR")
        self._conn.execute("ALTER TABLE promotion_log ADD COLUMN IF NOT EXISTS demotion_evidence_json TEXT")
        self._conn.execute("ALTER TABLE promotion_log ADD COLUMN IF NOT EXISTS revised_at TIMESTAMP")
        self._conn.execute("ALTER TABLE promotion_log ADD COLUMN IF NOT EXISTS revision_reason VARCHAR")
        self._conn.execute("ALTER TABLE promotion_log ADD COLUMN IF NOT EXISTS revised_text TEXT")
        self._conn.execute("CREATE TABLE IF NOT EXISTS message_labels (message_label_id BIGINT PRIMARY KEY DEFAULT nextval('seq_message_label_id'), message_pk BIGINT NOT NULL, label VARCHAR NOT NULL, label_type VARCHAR NOT NULL DEFAULT 'folder', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(message_pk, label, label_type))")
        self._conn.execute("CREATE TABLE IF NOT EXISTS ingest_sync_state (account_name VARCHAR NOT NULL, folder_name VARCHAR NOT NULL, sync_kind VARCHAR NOT NULL, next_page BIGINT DEFAULT 1, last_completed_page BIGINT, status VARCHAR DEFAULT 'pending', last_run_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(account_name, folder_name, sync_kind))")
        self._conn.execute("CREATE SEQUENCE IF NOT EXISTS seq_failed_message_ingestion_id START 1")
        self._conn.execute("CREATE TABLE IF NOT EXISTS failed_message_ingestions (failed_message_ingestion_id BIGINT PRIMARY KEY DEFAULT nextval('seq_failed_message_ingestion_id'), account_name VARCHAR NOT NULL, folder_name VARCHAR NOT NULL, mailbox_message_id VARCHAR NOT NULL, stable_message_id VARCHAR, failure_kind VARCHAR NOT NULL, error TEXT, retry_count BIGINT DEFAULT 0, status VARCHAR DEFAULT 'pending', first_failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, resolved_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(account_name, folder_name, mailbox_message_id, failure_kind))")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_failed_message_ingestions_status ON failed_message_ingestions(status)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_failed_message_ingestions_account_folder ON failed_message_ingestions(account_name, folder_name)")
        self.entity_store.initialize()
        if start_date is not None:
            self.set_start_date_once(start_date)

    def prepare_for_body_persistence(self) -> None:
        self._body_persistence_temp_dir.mkdir(parents=True, exist_ok=True)
        temp_directory = str(self._body_persistence_temp_dir).replace("'", "''")
        self._conn.execute(f"SET temp_directory = '{temp_directory}'")
        self._conn.execute(f"SET threads TO {self.BODY_PERSISTENCE_THREADS}")
        self._conn.execute("SET preserve_insertion_order = false")

    def restore_default_write_settings(self) -> None:
        self._conn.execute(f"SET threads TO {self.DEFAULT_DUCKDB_THREADS}")
        self._conn.execute("SET preserve_insertion_order = true")

    def flush_body_persistence_writes(self) -> None:
        self._conn.execute("CHECKPOINT")

    def close(self) -> None:
        self._conn.close()
        self.entity_store.close()

    def list_tables(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall()
        return [row[0] for row in rows]

    def stats(self) -> dict[str, Any]:
        table_counts = {}
        for table in self.list_tables():
            table_counts[table] = require_scalar(
                self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone(),
                operation=f'count rows in {table}',
            )
        entity_table_counts = {}
        entity_tables = [row[0] for row in self.entity_store.conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY table_name").fetchall()]
        for table in entity_tables:
            entity_table_counts[table] = require_scalar(
                self.entity_store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone(),
                operation=f'count entity rows in {table}',
            )
        return {
            'root': str(self.paths.root),
            'db_path': str(self.paths.db_path),
            'entity_db_path': str(self.paths.entity_db_path),
            'active_db_path': str(self.active_db_path),
            'config_dir': str(self.paths.config_dir),
            'promotion_rulebook_path': str(self.paths.promotion_rulebook_path),
            'default_promotion_soul_path': str(self.paths.default_promotion_soul_path),
            'batch_review_template_path': str(self.paths.batch_review_template_path),
            'raw_dir': str(self.paths.raw_dir),
            'cache_dir': str(self.paths.cache_dir),
            'reports_dir': str(self.paths.reports_dir),
            'work_root': str(self.paths.work_root) if self.paths.work_root else None,
            'work_db_path': str(self.paths.work_db_path) if self.paths.work_db_path else None,
            'start_date': self.get_start_date(),
            'excluded_folders': self.get_excluded_folders(),
            'expiry_grace_days': self.get_expiry_grace_days(),
            'promotion_llm_config': self.get_promotion_llm_config(),
            'last_ingestion_report': self.get_last_ingestion_report(),
            'table_counts': table_counts,
            'entity_table_counts': entity_table_counts,
        }

    def pipeline_status(self) -> dict[str, Any]:
        identity_rows = self._conn.execute(
            "SELECT COALESCE(identity_source, 'provisional') AS source, COUNT(*) FROM messages GROUP BY 1"
        ).fetchall()
        identity_sources = {row[0]: int(row[1]) for row in identity_rows}
        processing_row = require_row(self._conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE cleaned_text IS NOT NULL AND length(trim(cleaned_text)) > 0) AS with_body,
                COUNT(*) FILTER (WHERE internet_message_id IS NOT NULL) AS with_rfc_message_id,
                COUNT(*) FILTER (WHERE rfc_in_reply_to IS NOT NULL OR (rfc_references_json IS NOT NULL AND rfc_references_json <> '[]')) AS with_rfc_threading,
                COUNT(*) FILTER (WHERE stable_message_id LIKE 'provisional:%') AS provisional_ids,
                COUNT(*) FILTER (WHERE stable_message_id LIKE 'content:%') AS content_ids,
                COUNT(*) FILTER (WHERE stable_message_id LIKE 'rfc822:%') AS rfc822_ids
            FROM messages
            """
        ).fetchone(), operation='summarize message processing state')
        sync_counts: dict[str, dict[str, int]] = {}
        for sync_kind, status, count in self._conn.execute(
            "SELECT sync_kind, status, COUNT(*) FROM ingest_sync_state GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall():
            sync_counts.setdefault(sync_kind, {})[status] = int(count)
        active_sync_rows = self._conn.execute(
            """
            SELECT
                s.account_name,
                a.email_address,
                s.folder_name,
                s.sync_kind,
                s.next_page,
                s.last_completed_page,
                s.status,
                s.last_run_at
            FROM ingest_sync_state s
            LEFT JOIN accounts a ON a.account_name = s.account_name
            WHERE s.status IN ('in_progress', 'partial')
            ORDER BY s.last_run_at DESC NULLS LAST, s.account_name, s.folder_name, s.sync_kind
            """
        ).fetchall()
        active_sync_states: list[dict[str, Any]] = []
        continuation_groups: dict[tuple[str, str, str], dict[str, Any]] = {}
        for account_name, email_address, folder_name, sync_kind, next_page, last_completed_page, status, last_run_at in active_sync_rows:
            next_page_int = int(next_page) if next_page is not None else None
            last_completed_page_int = int(last_completed_page) if last_completed_page is not None else None
            continuation_state = _classify_sync_continuation_state(
                status=status,
                next_page=next_page_int,
                last_completed_page=last_completed_page_int,
            )
            active_sync_states.append(
                {
                    'account_name': account_name,
                    'email_address': email_address,
                    'folder_name': folder_name,
                    'sync_kind': sync_kind,
                    'next_page': next_page_int,
                    'last_completed_page': last_completed_page_int,
                    'status': status,
                    'last_run_at': last_run_at,
                    'continuation_state': continuation_state,
                }
            )
            command = _CURSOR_CONTINUATION_COMMANDS.get(sync_kind)
            if continuation_state != 'resume_ready' or not command or not email_address:
                continue
            key = (command, account_name, email_address)
            group = continuation_groups.setdefault(
                key,
                {
                    'command': command,
                    'account_name': account_name,
                    'email_address': email_address,
                    'folders': set(),
                    'sync_kinds': set(),
                },
            )
            group['folders'].add(folder_name)
            group['sync_kinds'].add(sync_kind)
        continuation_commands = []
        for group in continuation_groups.values():
            folders = sorted(group['folders'])
            sync_kinds = sorted(group['sync_kinds'])
            continuation_commands.append(
                {
                    'command': group['command'],
                    'account_name': group['account_name'],
                    'email_address': group['email_address'],
                    'folders': folders,
                    'sync_kinds': sync_kinds,
                    'shell_command': _build_continuation_command(
                        command=group['command'],
                        account_name=group['account_name'],
                        email_address=group['email_address'],
                        folders=folders,
                    ),
                }
            )
        continuation_commands.sort(key=lambda row: (row['command'], row['account_name'], row['folders']))
        extraction_counts = {
            'action_items': int(require_scalar(self._conn.execute("SELECT COUNT(*) FROM action_items").fetchone(), operation='count action items')),
            'deadlines': int(require_scalar(self._conn.execute("SELECT COUNT(*) FROM deadlines").fetchone(), operation='count deadlines')),
            'calendar_events': int(require_scalar(self._conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone(), operation='count calendar events')),
            'thread_summaries': int(require_scalar(self._conn.execute("SELECT COUNT(*) FROM thread_summaries").fetchone(), operation='count thread summaries')),
            'promotion_log': int(require_scalar(self._conn.execute("SELECT COUNT(*) FROM promotion_log").fetchone(), operation='count promotion log rows')),
        }
        failed_ingestion_counts = {
            'open': int(require_scalar(self._conn.execute("SELECT COUNT(*) FROM failed_message_ingestions WHERE status <> 'resolved'").fetchone(), operation='count open failed message ingestions')),
            'resolved': int(require_scalar(self._conn.execute("SELECT COUNT(*) FROM failed_message_ingestions WHERE status = 'resolved'").fetchone(), operation='count resolved failed message ingestions')),
        }
        cross_folder_threads = int(
            require_scalar(self._conn.execute(
                """
                SELECT COUNT(*)
                FROM (
                    SELECT m.thread_key
                    FROM messages m
                    JOIN message_labels ml ON ml.message_pk = m.message_pk AND ml.label_type = 'folder'
                    GROUP BY m.thread_key
                    HAVING COUNT(DISTINCT ml.label) > 1
                ) cross_folder
                """
            ).fetchone(), operation='count cross-folder threads')
        )
        return {
            'runtime': {
                'root': str(self.paths.root),
                'db_path': str(self.paths.db_path),
                'entity_db_path': str(self.paths.entity_db_path),
                'reports_dir': str(self.paths.reports_dir),
                'start_date': self.get_start_date(),
            },
            'messages': {
                'total': int(processing_row[0]),
                'with_body': int(processing_row[1]),
                'with_rfc_message_id': int(processing_row[2]),
                'with_rfc_threading': int(processing_row[3]),
                'provisional_ids': int(processing_row[4]),
                'content_ids': int(processing_row[5]),
                'rfc822_ids': int(processing_row[6]),
                'identity_sources': identity_sources,
                'cross_folder_threads': cross_folder_threads,
            },
            'ingestion': {
                'folders': int(require_scalar(self._conn.execute("SELECT COUNT(*) FROM folders").fetchone(), operation='count folders')),
                'sync_state_counts': sync_counts,
                'active_sync_states': active_sync_states,
                'continuation_commands': continuation_commands,
                'last_report': self.get_last_ingestion_report(),
                'failed_body_ingestions': failed_ingestion_counts,
            },
            'extraction': extraction_counts,
            'entities': {
                'contacts': int(require_scalar(self._conn.execute("SELECT COUNT(*) FROM contacts").fetchone(), operation='count contacts')),
                'email_entity_links': int(require_scalar(self._conn.execute("SELECT COUNT(*) FROM email_entity_index").fetchone(), operation='count email entity links')),
                'people': int(require_scalar(self.entity_store.conn.execute("SELECT COUNT(*) FROM people").fetchone(), operation='count people')),
                'person_aliases': int(require_scalar(self.entity_store.conn.execute("SELECT COUNT(*) FROM person_aliases").fetchone(), operation='count person aliases')),
                'person_emails': int(require_scalar(self.entity_store.conn.execute("SELECT COUNT(*) FROM person_emails").fetchone(), operation='count person emails')),
                'entity_message_links': int(require_scalar(self.entity_store.conn.execute("SELECT COUNT(*) FROM message_entity_index").fetchone(), operation='count entity message links')),
            },
        }

    def get_metadata(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM metadata WHERE key = ?", [key]).fetchone()
        return row[0] if row else None

    def set_metadata(self, key: str, value: str) -> str:
        self._conn.execute("DELETE FROM metadata WHERE key = ?", [key])
        self._conn.execute(
            "INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            [key, value],
        )
        return value

    def get_last_ingestion_report(self) -> dict[str, Any] | None:
        raw = self.get_metadata('last_ingestion_report')
        if not raw:
            return None
        return json.loads(raw)

    def set_last_ingestion_report(self, report: dict[str, Any]) -> dict[str, Any]:
        self.set_metadata('last_ingestion_report', json.dumps(report, sort_keys=True, default=str))
        return report

    def record_failed_message_ingestion(
        self,
        *,
        account_name: str,
        folder_name: str,
        mailbox_message_id: str,
        stable_message_id: str | None,
        failure_kind: str,
        error: str | None,
    ) -> dict[str, Any]:
        row = self._conn.execute(
            """
            SELECT failed_message_ingestion_id, retry_count, status
            FROM failed_message_ingestions
            WHERE account_name = ? AND folder_name = ? AND mailbox_message_id = ? AND failure_kind = ?
            """,
            [account_name, folder_name, mailbox_message_id, failure_kind],
        ).fetchone()
        if row:
            failed_message_ingestion_id = int(row[0])
            retry_count = int(row[1] or 0) + 1
            current_status = row[2] or 'pending'
            if current_status == 'resolved':
                self._conn.execute(
                    """
                    UPDATE failed_message_ingestions
                    SET stable_message_id = COALESCE(?, stable_message_id),
                        error = ?,
                        retry_count = ?,
                        status = 'pending',
                        last_failed_at = CURRENT_TIMESTAMP,
                        resolved_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE failed_message_ingestion_id = ?
                    """,
                    [stable_message_id, error, retry_count, failed_message_ingestion_id],
                )
            else:
                self._conn.execute(
                    """
                    UPDATE failed_message_ingestions
                    SET stable_message_id = COALESCE(?, stable_message_id),
                        error = ?,
                        retry_count = ?,
                        last_failed_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE failed_message_ingestion_id = ?
                    """,
                    [stable_message_id, error, retry_count, failed_message_ingestion_id],
                )
        else:
            self._conn.execute(
                """
                INSERT INTO failed_message_ingestions(
                    account_name, folder_name, mailbox_message_id, stable_message_id, failure_kind,
                    error, retry_count, status, first_failed_at, last_failed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                [account_name, folder_name, mailbox_message_id, stable_message_id, failure_kind, error],
            )
            failed_message_ingestion_id = int(
                require_scalar(self._conn.execute(
                    """
                    SELECT failed_message_ingestion_id
                    FROM failed_message_ingestions
                    WHERE account_name = ? AND folder_name = ? AND mailbox_message_id = ? AND failure_kind = ?
                    """,
                    [account_name, folder_name, mailbox_message_id, failure_kind],
                ).fetchone(), operation='load failed message ingestion id')
            )
        row = require_row(self._conn.execute(
            """
            SELECT account_name, folder_name, mailbox_message_id, stable_message_id, failure_kind,
                   error, retry_count, status, first_failed_at, last_failed_at, resolved_at
            FROM failed_message_ingestions
            WHERE failed_message_ingestion_id = ?
            """,
            [failed_message_ingestion_id],
        ).fetchone(), operation='load failed message ingestion')
        return {
            'account_name': row[0],
            'folder_name': row[1],
            'mailbox_message_id': row[2],
            'stable_message_id': row[3],
            'failure_kind': row[4],
            'error': row[5],
            'retry_count': int(row[6] or 0),
            'status': row[7],
            'first_failed_at': row[8],
            'last_failed_at': row[9],
            'resolved_at': row[10],
        }

    def resolve_failed_message_ingestion(
        self,
        *,
        account_name: str,
        folder_name: str,
        mailbox_message_id: str,
    ) -> None:
        self._conn.execute(
            """
            UPDATE failed_message_ingestions
            SET status = 'resolved',
                resolved_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE account_name = ? AND folder_name = ? AND mailbox_message_id = ?
              AND status <> 'resolved'
            """,
            [account_name, folder_name, mailbox_message_id],
        )

    def list_failed_message_ingestions(
        self,
        *,
        account_name: str | None = None,
        statuses: list[str] | None = None,
        folders: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        effective_statuses = statuses or ['pending']
        if account_name:
            clauses.append('account_name = ?')
            params.append(account_name)
        if effective_statuses:
            clauses.append(f"status IN ({', '.join('?' for _ in effective_statuses)})")
            params.extend(effective_statuses)
        if folders:
            folder_clauses = []
            for folder in folders:
                folder_clauses.append('(folder_name = ? OR folder_name LIKE ?)')
                params.extend([folder, f'{folder}/%'])
            clauses.append(f"({' OR '.join(folder_clauses)})")
        query = """
            SELECT account_name, folder_name, mailbox_message_id, stable_message_id, failure_kind,
                   error, retry_count, status, first_failed_at, last_failed_at, resolved_at
            FROM failed_message_ingestions
        """
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY status ASC, last_failed_at ASC, failed_message_ingestion_id ASC'
        if limit is not None:
            query += f' LIMIT {int(limit)}'
        rows = self._conn.execute(query, params).fetchall()
        return [
            {
                'account_name': row[0],
                'folder_name': row[1],
                'mailbox_message_id': row[2],
                'stable_message_id': row[3],
                'failure_kind': row[4],
                'error': row[5],
                'retry_count': int(row[6] or 0),
                'status': row[7],
                'first_failed_at': row[8],
                'last_failed_at': row[9],
                'resolved_at': row[10],
            }
            for row in rows
        ]

    def get_excluded_folders(self) -> list[str]:
        raw = self.get_metadata('excluded_folders')
        if not raw:
            return []
        values = json.loads(raw)
        return [value for value in values if value]

    def set_excluded_folders(self, folders: list[str]) -> list[str]:
        normalized = [folder for folder in dict.fromkeys(folders) if folder]
        self.set_metadata('excluded_folders', json.dumps(normalized))
        return normalized

    DEFAULT_EXPIRY_GRACE_DAYS = 365

    def get_expiry_grace_days(self) -> int:
        """Days a time-anchored memory is retained past its reference time.

        Persistent, user-settable. Nothing older than ``today - grace`` is
        eligible for expiry cleanup, so a just-passed deadline is never removed
        out from under the user.
        """
        raw = self.get_metadata('expiry_grace_days')
        if raw is None:
            return self.DEFAULT_EXPIRY_GRACE_DAYS
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return self.DEFAULT_EXPIRY_GRACE_DAYS
        return value if value >= 0 else self.DEFAULT_EXPIRY_GRACE_DAYS

    def set_expiry_grace_days(self, days: int) -> int:
        if days < 0:
            raise ValueError('expiry grace days must be >= 0')
        self.set_metadata('expiry_grace_days', str(int(days)))
        return int(days)

    def select_expired_time_anchors(self, *, grace_days: int | None = None) -> dict[str, Any]:
        """Identify purely time-tracking memories whose reference time has passed.

        A memory qualifies only when its whole informational value was the date
        or time it marked. Durable memories are preserved:

        * a deadline promoted into holographic memory (the system's own
          "matters long-term" decision) is kept;
        * a deadline anchored to a broader topic (``related_project`` set) is
          kept — the issue outlives the date;
        * a recurring calendar event (``recurrence_rule`` set) is kept, since it
          still influences the future.

        Action items are time-anchored only when they carry a ``due_date``; an
        undated action item is a pure to-do with no expiry and is never
        selected. A dated action item is eligible on the same terms as a
        deadline (past grace, not promoted). It has no ``related_project``
        column, so its broader topic is preserved by the untouched thread
        summary rather than by a per-row guard.
        """
        grace = self.get_expiry_grace_days() if grace_days is None else max(0, int(grace_days))

        deadline_rows = self._conn.execute(
            """
            SELECT d.deadline_id, d.label, d.due_date
            FROM deadlines d
            WHERE d.due_date IS NOT NULL
              AND d.due_date < (CURRENT_DATE - CAST(? AS INTEGER))
              AND d.related_project IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM promotion_log p
                  WHERE p.source_object_type = 'deadline'
                    AND p.source_object_id = CAST(d.deadline_id AS VARCHAR)
                    AND p.status = 'fact_store_written'
              )
            ORDER BY d.due_date
            """,
            [grace],
        ).fetchall()

        action_item_rows = self._conn.execute(
            """
            SELECT a.action_item_id, a.action_text, a.due_date
            FROM action_items a
            WHERE a.due_date IS NOT NULL
              AND a.due_date < (CURRENT_DATE - CAST(? AS INTEGER))
              AND NOT EXISTS (
                  SELECT 1 FROM promotion_log p
                  WHERE p.source_object_type = 'action_item'
                    AND p.source_object_id = CAST(a.action_item_id AS VARCHAR)
                    AND p.status = 'fact_store_written'
              )
            ORDER BY a.due_date
            """,
            [grace],
        ).fetchall()

        event_rows = self._conn.execute(
            """
            SELECT c.calendar_event_id, c.summary, COALESCE(c.ends_at, c.starts_at) AS ref_at
            FROM calendar_events c
            WHERE COALESCE(c.ends_at, c.starts_at) IS NOT NULL
              AND COALESCE(c.ends_at, c.starts_at) < (CURRENT_TIMESTAMP - CAST(? AS INTEGER) * INTERVAL '1 day')
              AND (c.recurrence_rule IS NULL OR trim(c.recurrence_rule) = '')
            ORDER BY ref_at
            """,
            [grace],
        ).fetchall()

        return {
            'grace_days': grace,
            'deadline_ids': [int(r[0]) for r in deadline_rows],
            'action_item_ids': [int(r[0]) for r in action_item_rows],
            'calendar_event_ids': [int(r[0]) for r in event_rows],
            'deadline_samples': [{'deadline_id': int(r[0]), 'label': r[1], 'due_date': str(r[2])} for r in deadline_rows[:10]],
            'action_item_samples': [{'action_item_id': int(r[0]), 'action_text': r[1], 'due_date': str(r[2])} for r in action_item_rows[:10]],
            'calendar_event_samples': [{'calendar_event_id': int(r[0]), 'summary': r[1], 'ref_at': str(r[2])} for r in event_rows[:10]],
        }

    def cleanup_expired_time_anchors(self, *, grace_days: int | None = None, dry_run: bool = True) -> dict[str, Any]:
        """Delete expired, non-durable time anchors. Dry-run reports without deleting.

        Returns the affected ids so the caller can prune the corresponding
        retrieval vectors.
        """
        selection = self.select_expired_time_anchors(grace_days=grace_days)
        deadline_ids = selection['deadline_ids']
        action_item_ids = selection['action_item_ids']
        event_ids = selection['calendar_event_ids']
        result = {
            'grace_days': selection['grace_days'],
            'dry_run': dry_run,
            'deadlines_matched': len(deadline_ids),
            'action_items_matched': len(action_item_ids),
            'calendar_events_matched': len(event_ids),
            'deadlines_deleted': 0,
            'action_items_deleted': 0,
            'calendar_events_deleted': 0,
            'deleted_deadline_ids': [],
            'deleted_action_item_ids': [],
            'deleted_calendar_event_ids': [],
            'deadline_samples': selection['deadline_samples'],
            'action_item_samples': selection['action_item_samples'],
            'calendar_event_samples': selection['calendar_event_samples'],
        }
        if dry_run:
            return result
        if deadline_ids:
            placeholders = ','.join(['?'] * len(deadline_ids))
            self._conn.execute(f"DELETE FROM deadlines WHERE deadline_id IN ({placeholders})", deadline_ids)
            result['deadlines_deleted'] = len(deadline_ids)
            result['deleted_deadline_ids'] = list(deadline_ids)
        if action_item_ids:
            placeholders = ','.join(['?'] * len(action_item_ids))
            self._conn.execute(f"DELETE FROM action_items WHERE action_item_id IN ({placeholders})", action_item_ids)
            result['action_items_deleted'] = len(action_item_ids)
            result['deleted_action_item_ids'] = list(action_item_ids)
        if event_ids:
            placeholders = ','.join(['?'] * len(event_ids))
            self._conn.execute(f"DELETE FROM calendar_events WHERE calendar_event_id IN ({placeholders})", event_ids)
            result['calendar_events_deleted'] = len(event_ids)
            result['deleted_calendar_event_ids'] = list(event_ids)
        return result

    def get_promotion_llm_config(self) -> dict[str, Any]:
        return promotion_llm_config_from_json(
            self.get_metadata('promotion_llm_config'),
            default_soul_path=str(self.paths.default_promotion_soul_path),
            default_rulebook_path=str(self.paths.promotion_rulebook_path),
        )

    def set_promotion_llm_config(self, config: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_promotion_llm_config(
            config,
            default_soul_path=str(self.paths.default_promotion_soul_path),
            default_rulebook_path=str(self.paths.promotion_rulebook_path),
        )
        self.set_metadata('promotion_llm_config', json.dumps(normalized, sort_keys=True))
        return normalized

    def reseed_promotion_assets(self, *, force: bool = False) -> dict[str, str]:
        seeded = seed_runtime_promotion_assets(
            runtime_root=self.paths.promotion_config_dir,
            force=force,
        )
        result = {key: str(value) for key, value in seeded.items() if key != 'seeded_paths'}
        result['seeded_paths'] = json.dumps(seeded['seeded_paths'])
        return result

    def set_start_date_once(self, value: str) -> str:
        normalized = normalize_date_only(value)
        if normalized is None:
            raise ValueError('start_date must be a non-empty ISO date')
        row = self._conn.execute("SELECT value FROM metadata WHERE key = 'start_date'").fetchone()
        if row:
            return row[0]
        self._conn.execute(
            "INSERT INTO metadata(key, value, updated_at) VALUES ('start_date', ?, CURRENT_TIMESTAMP)",
            [normalized],
        )
        return normalized

    def get_start_date(self) -> str | None:
        return self.get_metadata('start_date')

    def get_start_datetime(self):
        start_date = self.get_start_date()
        return normalize_date_only(start_date, as_datetime=True) if start_date else None

    def ensure_account(self, account_name: str, email_address: str, provider: str | None = None) -> int:
        row = self._conn.execute(
            "SELECT account_id FROM accounts WHERE account_name = ?",
            [account_name],
        ).fetchone()
        if row:
            self._conn.execute(
                "UPDATE accounts SET email_address = ?, provider = COALESCE(?, provider), updated_at = CURRENT_TIMESTAMP WHERE account_id = ?",
                [email_address, provider, row[0]],
            )
            return int(row[0])
        inserted = self._conn.execute(
            "INSERT INTO accounts(account_name, email_address, provider) VALUES (?, ?, ?) RETURNING account_id",
            [account_name, email_address, provider],
        ).fetchone()
        return int(require_scalar(inserted, operation='create account'))

    def ensure_folder(self, account_id: int, folder_name: str, folder_type: str | None = None) -> int:
        row = self._conn.execute(
            "SELECT folder_id FROM folders WHERE account_id = ? AND folder_name = ?",
            [account_id, folder_name],
        ).fetchone()
        if row:
            self._conn.execute(
                "UPDATE folders SET folder_type = COALESCE(?, folder_type), updated_at = CURRENT_TIMESTAMP WHERE folder_id = ?",
                [folder_type, row[0]],
            )
            return int(row[0])
        inserted = self._conn.execute(
            "INSERT INTO folders(account_id, folder_name, folder_type) VALUES (?, ?, ?) RETURNING folder_id",
            [account_id, folder_name, folder_type],
        ).fetchone()
        return int(require_scalar(inserted, operation='create folder'))

    def ensure_contact(self, email_address: str, display_name: str | None = None) -> int:
        row = self._conn.execute(
            "SELECT contact_id FROM contacts WHERE primary_email = ?",
            [email_address.lower()],
        ).fetchone()
        if row:
            self._conn.execute(
                "UPDATE contacts SET display_name = COALESCE(?, display_name), last_seen_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE contact_id = ?",
                [display_name, row[0]],
            )
            return int(row[0])
        inserted = self._conn.execute(
            "INSERT INTO contacts(primary_email, display_name, first_seen_at, last_seen_at) VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) RETURNING contact_id",
            [email_address.lower(), display_name],
        ).fetchone()
        return int(require_scalar(inserted, operation='create contact'))

    def ensure_thread(self, account_id: int, thread_key: str, canonical_subject: str | None) -> int:
        row = self._conn.execute(
            "SELECT thread_id FROM threads WHERE thread_key = ?",
            [thread_key],
        ).fetchone()
        if row:
            self._conn.execute(
                "UPDATE threads SET canonical_subject = COALESCE(?, canonical_subject), updated_at = CURRENT_TIMESTAMP WHERE thread_id = ?",
                [canonical_subject, row[0]],
            )
            return int(row[0])
        inserted = self._conn.execute(
            "INSERT INTO threads(account_id, thread_key, canonical_subject) VALUES (?, ?, ?) RETURNING thread_id",
            [account_id, thread_key, canonical_subject],
        ).fetchone()
        return int(require_scalar(inserted, operation='create thread'))

    @staticmethod
    def _identity_precedence(identity_source: str | None) -> int:
        normalized = (identity_source or 'provisional').strip().lower()
        return {
            'provisional': 0,
            'content': 1,
            'rfc822': 2,
        }.get(normalized, 0)

    @staticmethod
    def _thread_precedence(thread_key: str | None) -> int:
        normalized = (thread_key or '').strip()
        if normalized.startswith('rfc822-thread:'):
            return 2
        if normalized.startswith('fallback-thread:'):
            return 1
        if normalized.startswith('thread:'):
            return 0
        return 0

    def _resolve_thread_key(self, *, existing_thread_key: str | None, incoming_thread_key: str | None) -> str | None:
        if not existing_thread_key:
            return incoming_thread_key
        if not incoming_thread_key:
            return existing_thread_key
        if self._thread_precedence(existing_thread_key) >= self._thread_precedence(incoming_thread_key):
            return existing_thread_key
        return incoming_thread_key

    def _refresh_thread_state(self, *, thread_key: str, canonical_subject: str | None = None) -> None:
        row = require_row(self._conn.execute(
            """
            SELECT
                MIN(COALESCE(sent_at, received_at)) AS first_message_at,
                MAX(COALESCE(sent_at, received_at)) AS last_message_at,
                COUNT(*) AS message_count,
                COUNT(DISTINCT from_addr) AS participant_count
            FROM messages
            WHERE thread_key = ?
            """,
            [thread_key],
        ).fetchone(), operation='summarize thread state')
        message_count = int(row[2]) if row[2] is not None else 0
        if message_count == 0:
            self._conn.execute("DELETE FROM threads WHERE thread_key = ?", [thread_key])
            return
        self._conn.execute(
            """
            UPDATE threads
            SET canonical_subject = COALESCE(?, canonical_subject),
                first_message_at = ?,
                last_message_at = ?,
                message_count = ?,
                participant_count = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE thread_key = ?
            """,
            [canonical_subject, row[0], row[1], message_count, int(row[3] or 0), thread_key],
        )

    def reassign_message_thread(self, *, message_pk: int, thread_key: str, canonical_subject: str | None = None) -> None:
        message_row = self._conn.execute(
            "SELECT account_id, thread_key FROM messages WHERE message_pk = ?",
            [message_pk],
        ).fetchone()
        if not message_row:
            raise KeyError(f'message_pk {message_pk} not found')
        account_id = int(message_row[0])
        previous_thread_key = message_row[1]
        resolved_thread_key = self._resolve_thread_key(existing_thread_key=previous_thread_key, incoming_thread_key=thread_key)
        if resolved_thread_key is None:
            return
        self.ensure_thread(account_id=account_id, thread_key=resolved_thread_key, canonical_subject=canonical_subject)
        if resolved_thread_key != previous_thread_key:
            self._conn.execute(
                "UPDATE messages SET thread_key = ?, updated_at = CURRENT_TIMESTAMP WHERE message_pk = ?",
                [resolved_thread_key, message_pk],
            )
        self._refresh_thread_state(thread_key=resolved_thread_key, canonical_subject=canonical_subject)
        if previous_thread_key and previous_thread_key != resolved_thread_key:
            self._refresh_thread_state(thread_key=previous_thread_key)

    def upsert_message_stub(
        self,
        *,
        account_id: int,
        folder_id: int,
        mailbox_message_id: str | None = None,
        stable_message_id: str = '',
        identity_source: str = 'provisional',
        internet_message_id: str | None = None,
        thread_key: str,
        subject: str,
        normalized_subject: str,
        from_name: str | None,
        from_addr: str,
        to_addrs: list[str],
        sent_at: str | datetime | None,
        received_at: str | datetime | None,
        has_attachments: bool,
        direction: str,
        is_read: bool,
    ) -> tuple[int, bool]:
        row = self._conn.execute(
            """
            SELECT message_pk, stable_message_id, identity_source, thread_key
            FROM messages
            WHERE stable_message_id = ?
               OR (account_id = ? AND mailbox_message_id = ?)
            ORDER BY CASE WHEN stable_message_id = ? THEN 0 ELSE 1 END, message_pk ASC
            LIMIT 1
            """,
            [stable_message_id, account_id, mailbox_message_id, stable_message_id],
        ).fetchone()
        if row:
            existing_message_pk = int(row[0])
            existing_stable_message_id = row[1]
            existing_identity_source = row[2]
            existing_thread_key = row[3]
            resolved_identity_source = existing_identity_source
            if self._identity_precedence(identity_source) > self._identity_precedence(existing_identity_source):
                resolved_identity_source = identity_source
            existing_has_canonical_identity = existing_stable_message_id.startswith(('rfc822:', 'content:'))
            incoming_is_provisional = stable_message_id.startswith('provisional:')
            if existing_has_canonical_identity and incoming_is_provisional:
                return existing_message_pk, False
            resolved_thread_key = self._resolve_thread_key(
                existing_thread_key=existing_thread_key,
                incoming_thread_key=thread_key,
            )
            existing_row = require_row(self._conn.execute(
                """
                SELECT folder_id, mailbox_message_id, subject, normalized_subject, from_name,
                       to_addrs, sent_at, has_attachments, direction, is_read, identity_source
                FROM messages
                WHERE message_pk = ?
                """,
                [existing_message_pk],
            ).fetchone(), operation='load existing message stub')
            update_clauses: list[str] = []
            update_params: list[Any] = []
            if folder_id != existing_row[0]:
                update_clauses.append('folder_id = ?')
                update_params.append(folder_id)
            if mailbox_message_id and mailbox_message_id != existing_row[1]:
                update_clauses.append('mailbox_message_id = ?')
                update_params.append(mailbox_message_id)
            if subject != existing_row[2]:
                update_clauses.append('subject = ?')
                update_params.append(subject)
            if normalized_subject != existing_row[3]:
                update_clauses.append('normalized_subject = ?')
                update_params.append(normalized_subject)
            if from_name != existing_row[4]:
                update_clauses.append('from_name = ?')
                update_params.append(from_name)
            if to_addrs != existing_row[5]:
                update_clauses.append('to_addrs = ?')
                update_params.append(to_addrs)
            if sent_at != existing_row[6]:
                update_clauses.append('sent_at = ?')
                update_params.append(sent_at)
            if has_attachments != existing_row[7]:
                update_clauses.append('has_attachments = ?')
                update_params.append(has_attachments)
            if direction != existing_row[8]:
                update_clauses.append('direction = ?')
                update_params.append(direction)
            if is_read != existing_row[9]:
                update_clauses.append('is_read = ?')
                update_params.append(is_read)
            if resolved_identity_source != existing_row[10]:
                update_clauses.append('identity_source = ?')
                update_params.append(resolved_identity_source)
            if update_clauses:
                update_clauses.append('updated_at = CURRENT_TIMESTAMP')
                self._conn.execute(
                    f"UPDATE messages SET {', '.join(update_clauses)} WHERE message_pk = ?",
                    update_params + [existing_message_pk],
                )
            if resolved_thread_key and resolved_thread_key != existing_thread_key:
                self.reassign_message_thread(
                    message_pk=existing_message_pk,
                    thread_key=resolved_thread_key,
                    canonical_subject=normalized_subject,
                )
            return existing_message_pk, False
        inserted = self._conn.execute(
            """
            INSERT INTO messages(
                account_id, folder_id, mailbox_message_id, stable_message_id, identity_source, internet_message_id, thread_key,
                subject, normalized_subject, from_name, from_addr, to_addrs,
                sent_at, received_at, has_attachments, direction, is_read
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING message_pk
            """,
            [account_id, folder_id, mailbox_message_id, stable_message_id, identity_source, internet_message_id, thread_key, subject, normalized_subject, from_name, from_addr, to_addrs, sent_at, received_at, has_attachments, direction, is_read],
        ).fetchone()
        return int(require_scalar(inserted, operation='create message stub')), True

    def update_message_content(self, *, message_pk: int, cleaned_text: str, raw_path: str | None = None, text_hash: str | None = None) -> None:
        self._conn.execute(
            """
            UPDATE messages
            SET cleaned_text = ?,
                text_hash = COALESCE(?, text_hash),
                raw_path = COALESCE(?, raw_path),
                updated_at = CURRENT_TIMESTAMP
            WHERE message_pk = ?
            """,
            [cleaned_text, text_hash, raw_path, message_pk],
        )

    def promote_message_identity(
        self,
        *,
        message_pk: int,
        stable_message_id: str,
        identity_source: str,
        internet_message_id: str | None = None,
    ) -> int:
        row = self._conn.execute(
            "SELECT stable_message_id, identity_source, internet_message_id FROM messages WHERE message_pk = ?",
            [message_pk],
        ).fetchone()
        if not row:
            raise KeyError(f'message_pk {message_pk} not found')
        existing_stable_message_id, existing_identity_source, existing_internet_message_id = row
        desired_internet_message_id = internet_message_id or existing_internet_message_id
        if (
            existing_stable_message_id == stable_message_id
            and existing_identity_source == identity_source
            and existing_internet_message_id == desired_internet_message_id
        ):
            return message_pk
        if existing_stable_message_id == stable_message_id:
            self._conn.execute(
                """
                UPDATE messages
                SET identity_source = ?,
                    internet_message_id = COALESCE(?, internet_message_id),
                    updated_at = CURRENT_TIMESTAMP
                WHERE message_pk = ?
                """,
                [identity_source, desired_internet_message_id, message_pk],
            )
            return message_pk
        self._conn.execute(
            """
            UPDATE messages
            SET stable_message_id = ?,
                identity_source = ?,
                internet_message_id = COALESCE(?, internet_message_id),
                updated_at = CURRENT_TIMESTAMP
            WHERE message_pk = ?
            """,
            [stable_message_id, identity_source, desired_internet_message_id, message_pk],
        )
        return message_pk

    def update_message_identity(self, *, message_pk: int, stable_message_id: str, identity_source: str) -> int:
        return self.promote_message_identity(
            message_pk=message_pk,
            stable_message_id=stable_message_id,
            identity_source=identity_source,
        )

    def get_message_stable_message_id(self, *, message_pk: int) -> str:
        row = self._conn.execute("SELECT stable_message_id FROM messages WHERE message_pk = ?", [message_pk]).fetchone()
        if not row:
            raise KeyError(f'message_pk {message_pk} not found')
        return row[0]

    def get_message_row_by_stable_message_id(self, *, stable_message_id: str) -> tuple[Any, ...] | None:
        return self._conn.execute(
            "SELECT message_pk, stable_message_id, folder_id, mailbox_message_id FROM messages WHERE stable_message_id = ? LIMIT 1",
            [stable_message_id],
        ).fetchone()

    def collapse_duplicate_message(self, *, canonical_message_pk: int, duplicate_message_pk: int) -> int:
        if canonical_message_pk == duplicate_message_pk:
            return canonical_message_pk
        duplicate_row = self._conn.execute(
            "SELECT stable_message_id, folder_id, mailbox_message_id, thread_key FROM messages WHERE message_pk = ?",
            [duplicate_message_pk],
        ).fetchone()
        if not duplicate_row:
            return canonical_message_pk
        duplicate_stable_message_id, duplicate_folder_id, duplicate_mailbox_message_id, duplicate_thread_key = duplicate_row
        canonical_row = self._conn.execute(
            "SELECT folder_id, mailbox_message_id FROM messages WHERE message_pk = ?",
            [canonical_message_pk],
        ).fetchone()
        if not canonical_row:
            raise KeyError(f'message_pk {canonical_message_pk} not found')
        canonical_folder_id, canonical_mailbox_message_id = canonical_row
        self._conn.execute(
            """
            UPDATE messages
            SET folder_id = COALESCE(folder_id, ?),
                mailbox_message_id = COALESCE(mailbox_message_id, ?),
                updated_at = CURRENT_TIMESTAMP
            WHERE message_pk = ?
            """,
            [duplicate_folder_id or canonical_folder_id, duplicate_mailbox_message_id or canonical_mailbox_message_id, canonical_message_pk],
        )
        self._conn.execute(
            """
            INSERT INTO message_labels(message_pk, label, label_type, updated_at)
            SELECT ?, label, label_type, CURRENT_TIMESTAMP
            FROM message_labels
            WHERE message_pk = ?
              AND NOT EXISTS (
                  SELECT 1 FROM message_labels existing
                  WHERE existing.message_pk = ?
                    AND existing.label = message_labels.label
                    AND existing.label_type = message_labels.label_type
              )
            """,
            [canonical_message_pk, duplicate_message_pk, canonical_message_pk],
        )
        self._conn.execute("DELETE FROM message_labels WHERE message_pk = ?", [duplicate_message_pk])
        self._conn.execute("DELETE FROM calendar_events WHERE message_pk = ?", [duplicate_message_pk])
        self._conn.execute("DELETE FROM action_items WHERE message_pk = ?", [duplicate_message_pk])
        self._conn.execute("DELETE FROM deadlines WHERE message_pk = ?", [duplicate_message_pk])
        self._conn.execute("DELETE FROM email_entity_index WHERE message_pk = ?", [duplicate_message_pk])
        self.entity_store.conn.execute("DELETE FROM message_entity_index WHERE email_message_pk = ?", [duplicate_message_pk])
        if duplicate_stable_message_id:
            self._conn.execute(
                "DELETE FROM promotion_log WHERE source_object_type = 'message' AND source_object_id = ?",
                [duplicate_stable_message_id],
            )
        self._conn.execute("DELETE FROM messages WHERE message_pk = ?", [duplicate_message_pk])
        if duplicate_thread_key:
            self._refresh_thread_state(thread_key=duplicate_thread_key)
        canonical_thread_row = self._conn.execute(
            "SELECT thread_key, normalized_subject FROM messages WHERE message_pk = ?",
            [canonical_message_pk],
        ).fetchone()
        if canonical_thread_row and canonical_thread_row[0]:
            self._refresh_thread_state(
                thread_key=str(canonical_thread_row[0]),
                canonical_subject=canonical_thread_row[1],
            )
        return canonical_message_pk

    def get_message_thread_key(self, *, message_pk: int) -> str | None:
        row = self._conn.execute("SELECT thread_key FROM messages WHERE message_pk = ?", [message_pk]).fetchone()
        if not row:
            raise KeyError(f'message_pk {message_pk} not found')
        return row[0]

    def update_message_rfc_threading(
        self,
        *,
        message_pk: int,
        internet_message_id: str | None,
        rfc_in_reply_to: str | None,
        rfc_references_json: str,
        thread_key: str,
    ) -> None:
        message_row = self._conn.execute(
            "SELECT normalized_subject FROM messages WHERE message_pk = ?",
            [message_pk],
        ).fetchone()
        if not message_row:
            raise KeyError(f'message_pk {message_pk} not found')
        self._conn.execute(
            """
            UPDATE messages
            SET internet_message_id = COALESCE(?, internet_message_id),
                rfc_in_reply_to = ?,
                rfc_references_json = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE message_pk = ?
            """,
            [internet_message_id, rfc_in_reply_to, rfc_references_json, message_pk],
        )
        self.reassign_message_thread(
            message_pk=message_pk,
            thread_key=thread_key,
            canonical_subject=message_row[0],
        )

    def replace_message_entities(self, *, message_pk: int, stable_message_id: str, people: list[dict[str, Any]]) -> None:
        desired_rows: dict[tuple[Any, ...], dict[str, Any]] = {}
        for person in people:
            dedup_key = (
                person['person_id'],
                stable_message_id,
                person['role'],
                person.get('email_address'),
            )
            desired_rows[dedup_key] = person

        existing_rows = self._conn.execute(
            """
            SELECT message_pk, person_id, stable_message_id, role, email_address, canonical_name, normalized_name
            FROM email_entity_index
            WHERE stable_message_id = ?
            """,
            [stable_message_id],
        ).fetchall()
        existing_by_key = {
            (row[1], row[2], row[3], row[4]): {
                'message_pk': row[0],
                'canonical_name': row[5],
                'normalized_name': row[6],
            }
            for row in existing_rows
        }

        self._conn.begin()
        try:
            for key, person in desired_rows.items():
                existing = existing_by_key.get(key)
                if existing is None:
                    self._conn.execute(
                        """
                        INSERT INTO email_entity_index(
                            message_pk, stable_message_id, person_id, canonical_name,
                            normalized_name, role, email_address, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        [
                            message_pk,
                            stable_message_id,
                            person['person_id'],
                            person['canonical_name'],
                            person['normalized_name'],
                            person['role'],
                            person.get('email_address'),
                        ],
                    )
                    continue
                if (
                    existing['message_pk'] != message_pk
                    or existing['canonical_name'] != person['canonical_name']
                    or existing['normalized_name'] != person['normalized_name']
                ):
                    self._conn.execute(
                        """
                        UPDATE email_entity_index
                        SET message_pk = ?,
                            canonical_name = ?,
                            normalized_name = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE stable_message_id = ?
                          AND person_id = ?
                          AND role = ?
                          AND email_address IS NOT DISTINCT FROM ?
                        """,
                        [
                            message_pk,
                            person['canonical_name'],
                            person['normalized_name'],
                            stable_message_id,
                            person['person_id'],
                            person['role'],
                            person.get('email_address'),
                        ],
                    )

            for key in existing_by_key:
                if key in desired_rows:
                    continue
                person_id, existing_stable_message_id, role, email_address = key
                self._conn.execute(
                    """
                    DELETE FROM email_entity_index
                    WHERE stable_message_id = ?
                      AND person_id = ?
                      AND role = ?
                      AND email_address IS NOT DISTINCT FROM ?
                    """,
                    [existing_stable_message_id, person_id, role, email_address],
                )

            self.entity_store.replace_message_entities(
                email_message_pk=message_pk,
                stable_message_id=stable_message_id,
                people=list(desired_rows.values()),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def replace_calendar_events(self, *, message_pk: int, events: list[dict[str, Any]]) -> None:
        self._conn.execute("DELETE FROM calendar_events WHERE message_pk = ?", [message_pk])
        for event in events:
            self._conn.execute(
                """
                INSERT INTO calendar_events(
                    message_pk, filename, mime_type, method, uid, summary,
                    description, status, sequence, recurrence_id, recurrence_rule,
                    starts_at, ends_at, starts_at_tzid, ends_at_tzid, recurrence_id_tzid,
                    organizer, organizer_email, location, attendees_json, raw_ics, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                [
                    message_pk,
                    event.get('filename'),
                    event.get('mime_type'),
                    event.get('method'),
                    event.get('uid'),
                    event.get('summary'),
                    event.get('description'),
                    event.get('status'),
                    event.get('sequence'),
                    event.get('recurrence_id'),
                    event.get('recurrence_rule'),
                    event.get('starts_at'),
                    event.get('ends_at'),
                    event.get('starts_at_tzid'),
                    event.get('ends_at_tzid'),
                    event.get('recurrence_id_tzid'),
                    event.get('organizer'),
                    event.get('organizer_email'),
                    event.get('location'),
                    event.get('attendees_json'),
                    event['raw_ics'],
                ],
            )

    def replace_message_action_items(self, *, message_pk: int, action_items: list[dict[str, Any]]) -> None:
        thread_id = self._resolve_message_thread_id(message_pk)
        self._conn.execute("DELETE FROM action_items WHERE message_pk = ?", [message_pk])
        for action_item in action_items:
            self._conn.execute(
                """
                INSERT INTO action_items(thread_id, message_pk, owner, action_text, due_date, status, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    thread_id,
                    message_pk,
                    action_item.get('owner'),
                    action_item['action_text'],
                    action_item.get('due_date'),
                    action_item.get('status', 'open'),
                    action_item.get('confidence', 1.0),
                ],
            )

    def replace_message_deadlines(self, *, message_pk: int, deadlines: list[dict[str, Any]]) -> None:
        thread_id = self._resolve_message_thread_id(message_pk)
        self._conn.execute("DELETE FROM deadlines WHERE message_pk = ?", [message_pk])
        for deadline in deadlines:
            self._conn.execute(
                """
                INSERT INTO deadlines(thread_id, message_pk, label, due_date, related_project, confidence, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    thread_id,
                    message_pk,
                    deadline['label'],
                    deadline['due_date'],
                    deadline.get('related_project'),
                    deadline.get('confidence', 1.0),
                    deadline.get('status', 'open'),
                ],
            )

    def _resolve_message_thread_id(self, message_pk: int) -> int | None:
        thread_row = self._conn.execute("SELECT thread_key FROM messages WHERE message_pk = ?", [message_pk]).fetchone()
        if not thread_row or not thread_row[0]:
            return None
        resolved_thread_row = self._conn.execute("SELECT thread_id FROM threads WHERE thread_key = ?", [thread_row[0]]).fetchone()
        return int(resolved_thread_row[0]) if resolved_thread_row else None

    def replace_message_labels(self, *, message_pk: int, labels: list[str], label_type: str = 'folder') -> None:
        desired_labels = sorted(set(label for label in labels if label))
        existing_rows = self._conn.execute(
            "SELECT label FROM message_labels WHERE message_pk = ? AND label_type = ?",
            [message_pk, label_type],
        ).fetchall()
        existing_labels = {row[0] for row in existing_rows}

        self._conn.begin()
        try:
            for label in desired_labels:
                if label in existing_labels:
                    self._conn.execute(
                        "UPDATE message_labels SET updated_at = CURRENT_TIMESTAMP WHERE message_pk = ? AND label = ? AND label_type = ?",
                        [message_pk, label, label_type],
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO message_labels(message_pk, label, label_type, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                        [message_pk, label, label_type],
                    )
            for label in existing_labels - set(desired_labels):
                self._conn.execute(
                    "DELETE FROM message_labels WHERE message_pk = ? AND label = ? AND label_type = ?",
                    [message_pk, label, label_type],
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def get_ingest_sync_state(self, *, account_name: str, folder_name: str, sync_kind: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT account_name, folder_name, sync_kind, next_page, last_completed_page, status, last_run_at FROM ingest_sync_state WHERE account_name = ? AND folder_name = ? AND sync_kind = ?",
            [account_name, folder_name, sync_kind],
        ).fetchone()
        if not row:
            return None
        return {
            'account_name': row[0],
            'folder_name': row[1],
            'sync_kind': row[2],
            'next_page': int(row[3]) if row[3] is not None else 1,
            'last_completed_page': int(row[4]) if row[4] is not None else None,
            'status': row[5],
            'last_run_at': row[6],
        }

    def upsert_ingest_sync_state(self, *, account_name: str, folder_name: str, sync_kind: str, next_page: int, last_completed_page: int | None, status: str) -> None:
        self._conn.execute(
            "DELETE FROM ingest_sync_state WHERE account_name = ? AND folder_name = ? AND sync_kind = ?",
            [account_name, folder_name, sync_kind],
        )
        self._conn.execute(
            """
            INSERT INTO ingest_sync_state(account_name, folder_name, sync_kind, next_page, last_completed_page, status, last_run_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            [account_name, folder_name, sync_kind, next_page, last_completed_page, status],
        )

    def reconcile_ingest_sync_cursors(self, *, apply: bool = False) -> dict[str, object]:
        """Normalize legacy cursor residue without advancing any mail scan.

        Only body cursors whose driving envelope cursor is already complete and
        whose folder has no pending body failure are eligible.  All resumable
        envelope scans and every unresolved partial state remain untouched.
        """
        rows = self._conn.execute(
            """
            SELECT
                body.account_name,
                body.folder_name,
                body.sync_kind,
                body.next_page,
                body.last_completed_page,
                body.status,
                envelope.status
            FROM ingest_sync_state body
            JOIN ingest_sync_state envelope
              ON envelope.account_name = body.account_name
             AND envelope.folder_name = body.folder_name
             AND envelope.sync_kind = CASE body.sync_kind
                 WHEN 'initial_bodies' THEN 'initial_envelopes'
                 WHEN 'nightly_bodies' THEN 'nightly_envelopes'
             END
            WHERE body.sync_kind IN ('initial_bodies', 'nightly_bodies')
              AND body.status IN ('in_progress', 'partial')
              AND envelope.status = 'complete'
            ORDER BY body.account_name, body.folder_name, body.sync_kind
            """
        ).fetchall()
        normalized: list[dict[str, object]] = []
        for account_name, folder_name, sync_kind, next_page, last_completed_page, status, _ in rows:
            if self.list_failed_message_ingestions(
                account_name=str(account_name), folders=[str(folder_name)], limit=1
            ):
                continue
            if sync_kind == 'initial_bodies' and int(next_page) != int(last_completed_page) + 1:
                continue
            if sync_kind == 'nightly_bodies' and int(next_page) != 1:
                continue
            normalized.append(
                {
                    'account_name': str(account_name),
                    'folder_name': str(folder_name),
                    'sync_kind': str(sync_kind),
                    'previous_status': str(status),
                }
            )
        if apply:
            for cursor in normalized:
                row = self.get_ingest_sync_state(
                    account_name=str(cursor['account_name']),
                    folder_name=str(cursor['folder_name']),
                    sync_kind=str(cursor['sync_kind']),
                )
                if row is None:
                    continue
                self.upsert_ingest_sync_state(
                    account_name=str(cursor['account_name']),
                    folder_name=str(cursor['folder_name']),
                    sync_kind=str(cursor['sync_kind']),
                    next_page=int(row['next_page']),
                    last_completed_page=(
                        int(row['last_completed_page'])
                        if row['last_completed_page'] is not None
                        else None
                    ),
                    status='complete',
                )
        return {'candidates': normalized, 'updated': len(normalized) if apply else 0}

    def _thread_lineage_rows(self, *, where_sql: str, params: list[Any], limit: int | None = None) -> list[dict[str, Any]]:
        query = f"""
            SELECT
                m.thread_key,
                COALESCE(t.canonical_subject, min(m.normalized_subject), ''),
                COUNT(DISTINCT m.message_pk) AS message_count,
                MIN(COALESCE(m.sent_at, m.received_at)) AS first_message_at,
                MAX(COALESCE(m.sent_at, m.received_at)) AS last_message_at,
                string_agg(DISTINCT ml.label, '||' ORDER BY ml.label) FILTER (WHERE ml.label_type = 'folder') AS folder_labels,
                string_agg(DISTINCT m.stable_message_id, '||' ORDER BY m.stable_message_id) AS stable_message_ids
            FROM messages m
            LEFT JOIN threads t ON t.thread_key = m.thread_key
            LEFT JOIN message_labels ml ON ml.message_pk = m.message_pk
            {where_sql}
            GROUP BY m.thread_key, t.canonical_subject
            ORDER BY COUNT(DISTINCT m.message_pk) DESC, MIN(COALESCE(m.sent_at, m.received_at)) ASC NULLS LAST, m.thread_key ASC
        """
        if limit is not None:
            query += f' LIMIT {int(limit)}'
        rows = self._conn.execute(query, params).fetchall()
        lineages: list[dict[str, Any]] = []
        for row in rows:
            thread_key = row[0]
            lineage_root = thread_key
            if str(thread_key).startswith('rfc822-thread:'):
                lineage_root = str(thread_key)[len('rfc822-thread:'):]
            elif str(thread_key).startswith('fallback-thread:'):
                lineage_root = str(thread_key)[len('fallback-thread:'):]
            folder_labels = [item for item in str(row[5] or '').split('||') if item]
            stable_message_ids = [item for item in str(row[6] or '').split('||') if item]
            lineages.append(
                {
                    'thread_key': thread_key,
                    'lineage_root': lineage_root,
                    'canonical_subject': row[1] or '',
                    'message_count': int(row[2] or 0),
                    'first_message_at': row[3],
                    'last_message_at': row[4],
                    'folder_labels': folder_labels,
                    'stable_message_ids': stable_message_ids,
                }
            )
        return lineages

    def get_thread_lineages(self, *, thread_keys: list[str]) -> list[dict[str, Any]]:
        normalized_keys = [key for key in dict.fromkeys(thread_keys) if key]
        if not normalized_keys:
            return []
        placeholders = ','.join(['?'] * len(normalized_keys))
        return self._thread_lineage_rows(
            where_sql=f"WHERE m.thread_key IN ({placeholders})",
            params=normalized_keys,
        )

    def list_cross_folder_threads(self, *, limit: int = 20, query: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        where_sql = ''
        if query:
            like = f'%{query.lower()}%'
            where_sql = """
            WHERE m.thread_key IN (
                SELECT DISTINCT m2.thread_key
                FROM messages m2
                LEFT JOIN message_labels ml2 ON ml2.message_pk = m2.message_pk AND ml2.label_type = 'folder'
                WHERE lower(COALESCE(m2.subject, '')) LIKE ?
                   OR lower(COALESCE(m2.cleaned_text, '')) LIKE ?
                   OR lower(COALESCE(ml2.label, '')) LIKE ?
            )
            """
            params.extend([like, like, like])
        rows = self._thread_lineage_rows(where_sql=where_sql, params=params)
        return [row for row in rows if len(row['folder_labels']) > 1][:limit]

    def search_people(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return self.entity_store.search_people(query=query, limit=limit)

    def merge_people(self, *, primary_person_id: int, secondary_person_id: int, reason: str) -> int:
        self._conn.execute(
            """
            DELETE FROM email_entity_index AS secondary
            USING email_entity_index AS primary_idx
            WHERE secondary.person_id = ?
              AND primary_idx.person_id = ?
              AND secondary.stable_message_id = primary_idx.stable_message_id
              AND secondary.role = primary_idx.role
              AND COALESCE(secondary.email_address, '') = COALESCE(primary_idx.email_address, '')
            """,
            [secondary_person_id, primary_person_id],
        )
        self._conn.execute(
            "UPDATE email_entity_index SET person_id = ?, updated_at = CURRENT_TIMESTAMP WHERE person_id = ?",
            [primary_person_id, secondary_person_id],
        )
        return self.entity_store.merge_people(
            primary_person_id=primary_person_id,
            secondary_person_id=secondary_person_id,
            reason=reason,
        )

    def split_person(self, *, source_person_id: int, new_canonical_name: str, email_addresses: list[str], reason: str) -> int:
        new_person_id = self.entity_store.split_person(
            source_person_id=source_person_id,
            new_canonical_name=new_canonical_name,
            email_addresses=email_addresses,
            reason=reason,
        )
        normalized_emails = [email.lower() for email in email_addresses]
        if normalized_emails:
            placeholders = ','.join(['?'] * len(normalized_emails))
            self._conn.execute(
                f"""
                UPDATE email_entity_index
                SET person_id = ?, canonical_name = ?, normalized_name = lower(?), updated_at = CURRENT_TIMESTAMP
                WHERE person_id = ? AND lower(email_address) IN ({placeholders})
                """,
                [new_person_id, new_canonical_name, new_canonical_name, source_person_id, *normalized_emails],
            )
        return new_person_id

    def purge_messages_by_folders(self, folders: list[str], dry_run: bool = False) -> dict[str, Any]:
        normalized_folders = [folder for folder in dict.fromkeys(folders) if folder]
        if not normalized_folders:
            return {
                'folders': [],
                'messages_matched': 0,
                'messages_deleted': 0,
                'labels_deleted': 0,
                'calendar_events_deleted': 0,
                'email_entity_links_deleted': 0,
                'entity_message_links_deleted': 0,
            }
        folder_conditions = ' OR '.join(['(ml.label = ? OR ml.label LIKE ?)' for _ in normalized_folders])
        folder_params: list[str] = []
        for folder in normalized_folders:
            folder_params.extend([folder, f'{folder}/%'])
        message_rows = self._conn.execute(
            f"""
            SELECT DISTINCT ml.message_pk, m.stable_message_id
            FROM message_labels ml
            JOIN messages m ON m.message_pk = ml.message_pk
            WHERE ml.label_type = 'folder' AND ({folder_conditions})
            ORDER BY ml.message_pk
            """,
            folder_params,
        ).fetchall()
        message_pks = [int(row[0]) for row in message_rows]
        stable_ids = [row[1] for row in message_rows]
        counts = {
            'folders': normalized_folders,
            'messages_matched': len(message_pks),
            'messages_deleted': 0,
            'labels_deleted': 0,
            'calendar_events_deleted': 0,
            'email_entity_links_deleted': 0,
            'entity_message_links_deleted': 0,
        }
        if dry_run or not message_pks:
            return counts
        msg_placeholders = ','.join(['?'] * len(message_pks))
        counts['labels_deleted'] = int(require_scalar(self._conn.execute(
            f"SELECT COUNT(*) FROM message_labels WHERE message_pk IN ({msg_placeholders})",
            message_pks,
        ).fetchone(), operation='count labels to delete'))
        counts['calendar_events_deleted'] = int(require_scalar(self._conn.execute(
            f"SELECT COUNT(*) FROM calendar_events WHERE message_pk IN ({msg_placeholders})",
            message_pks,
        ).fetchone(), operation='count calendar events to delete'))
        counts['email_entity_links_deleted'] = int(require_scalar(self._conn.execute(
            f"SELECT COUNT(*) FROM email_entity_index WHERE message_pk IN ({msg_placeholders})",
            message_pks,
        ).fetchone(), operation='count email entity links to delete'))
        counts['entity_message_links_deleted'] = int(require_scalar(self.entity_store.conn.execute(
            f"SELECT COUNT(*) FROM message_entity_index WHERE email_message_pk IN ({msg_placeholders})",
            message_pks,
        ).fetchone(), operation='count entity message links to delete'))
        self._conn.execute(f"DELETE FROM message_labels WHERE message_pk IN ({msg_placeholders})", message_pks)
        self._conn.execute(f"DELETE FROM calendar_events WHERE message_pk IN ({msg_placeholders})", message_pks)
        self._conn.execute(f"DELETE FROM email_entity_index WHERE message_pk IN ({msg_placeholders})", message_pks)
        self.entity_store.conn.execute(f"DELETE FROM message_entity_index WHERE email_message_pk IN ({msg_placeholders})", message_pks)
        self._conn.execute(f"DELETE FROM messages WHERE message_pk IN ({msg_placeholders})", message_pks)
        counts['messages_deleted'] = len(message_pks)
        if stable_ids:
            stable_placeholders = ','.join(['?'] * len(stable_ids))
            self._conn.execute(f"DELETE FROM promotion_log WHERE source_object_type = 'message' AND source_object_id IN ({stable_placeholders})", stable_ids)
        return counts

    def checkpoint_to_durable(self) -> None:
        if self.active_db_path == self.paths.db_path:
            return
        self._conn.execute("CHECKPOINT")
        self._conn.execute(f"ATTACH '{self.paths.db_path}' AS durable")
        try:
            self._conn.execute(SCHEMA_SQL.replace("CREATE ", "CREATE durable.", 1))
        except Exception:
            # Schema bootstrap is handled table-by-table below; sequence creation may differ across attached DBs.
            pass
        try:
            for table in self.list_tables():
                self._conn.execute(f"CREATE OR REPLACE TABLE durable.{table} AS SELECT * FROM {table}")
        finally:
            self._conn.execute("DETACH durable")

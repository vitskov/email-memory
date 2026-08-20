from pathlib import Path
import json

from email_memory_store import EmailMemoryStore
from email_memory_store.maintenance import rebuild_entity_message_index_table, rebuild_messages_table


EXPECTED_TABLES = {
    "accounts",
    "folders",
    "messages",
    "threads",
    "contacts",
    "thread_summaries",
    "decisions",
    "action_items",
    "deadlines",
    "calendar_events",
    "metadata",
    "promotion_log",
}


def test_store_uses_explicit_database_paths(tmp_path: Path):
    root = tmp_path / "runtime"
    main_db = tmp_path / "durable" / "main.duckdb"
    entity_db = tmp_path / "entities" / "entity.duckdb"
    work_db = tmp_path / "work" / "active.duckdb"

    store = EmailMemoryStore(
        root,
        use_work_db=True,
        db_path=main_db,
        entity_db_path=entity_db,
        work_db_path=work_db,
    )
    try:
        assert store.paths.db_path == main_db
        assert store.paths.entity_db_path == entity_db
        assert store.paths.work_db_path == work_db
        assert store.active_db_path == work_db
    finally:
        store.close()


def test_initialize_creates_expected_directories_and_tables(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    assert store.paths.db_path.exists()
    assert store.paths.config_dir.exists()
    assert store.paths.default_promotion_soul_path.exists()
    assert store.paths.promotion_rulebook_path.exists()
    assert store.paths.batch_review_template_path.exists()
    assert store.paths.raw_dir.exists()
    assert store.paths.cache_dir.exists()
    assert store.paths.reports_dir.exists()
    assert EXPECTED_TABLES.issubset(set(store.list_tables()))

    store.close()


def test_stats_reports_zero_counts_for_fresh_database(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    stats = store.stats()

    assert stats["db_path"].endswith("email_memory.duckdb")
    for table in EXPECTED_TABLES - {"metadata"}:
        assert stats["table_counts"][table] == 0
    assert stats["table_counts"]["metadata"] == 1

    store.close()


def test_initialize_persists_default_start_date_when_not_provided(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    stats = store.stats()
    row = store.conn.execute("SELECT value FROM metadata WHERE key = 'start_date'").fetchone()
    assert row[0] == '2022-01-02'
    assert stats["start_date"] == '2022-01-02'

    store.close()


def test_initialize_persists_start_date_metadata_once(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize(start_date="2026-01-15")
    store.close()

    reopened = EmailMemoryStore(tmp_path / "email_memory")
    reopened.initialize(start_date="2025-01-01")
    stats = reopened.stats()

    row = reopened.conn.execute("SELECT value FROM metadata WHERE key = 'start_date'").fetchone()
    assert row[0] == '2026-01-15'
    assert stats["start_date"] == '2026-01-15'

    reopened.close()


def test_status_payload_exposes_persisted_metaparameters(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize(start_date="2024-05-20")

    stats = store.stats()

    assert stats["start_date"] == '2024-05-20'
    assert stats["db_path"].endswith("email_memory.duckdb")
    assert stats["active_db_path"].endswith("email_memory.duckdb")
    assert stats["config_dir"].endswith("config")
    assert stats["default_promotion_soul_path"].endswith("config/promotion/souls/default.md")
    assert stats["promotion_rulebook_path"].endswith("config/promotion/rulebooks/MEMORY_PROMOTION_RULEBOOK.md")
    assert stats["batch_review_template_path"].endswith("config/promotion/templates/batch_review_prompt.md")
    assert stats["raw_dir"].endswith("raw")
    assert stats["cache_dir"].endswith("cache")
    assert stats["reports_dir"].endswith("reports")

    store.close()


def test_reconcile_ingest_sync_cursors_only_closes_proven_legacy_residue(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    for sync_kind, next_page, last_completed_page, status in (
        ('initial_envelopes', 3, 2, 'complete'),
        ('initial_bodies', 3, 2, 'partial'),
        ('nightly_envelopes', 1, 2, 'complete'),
        ('nightly_bodies', 1, 2, 'in_progress'),
    ):
        store.upsert_ingest_sync_state(
            account_name='primary-account',
            folder_name='INBOX',
            sync_kind=sync_kind,
            next_page=next_page,
            last_completed_page=last_completed_page,
            status=status,
        )
    store.upsert_ingest_sync_state(
        account_name='primary-account',
        folder_name='Archive',
        sync_kind='initial_envelopes',
        next_page=4,
        last_completed_page=3,
        status='in_progress',
    )
    store.upsert_ingest_sync_state(
        account_name='primary-account',
        folder_name='Archive',
        sync_kind='initial_bodies',
        next_page=4,
        last_completed_page=3,
        status='in_progress',
    )

    preview = store.reconcile_ingest_sync_cursors()
    assert preview['updated'] == 0
    assert {cursor['sync_kind'] for cursor in preview['candidates']} == {
        'initial_bodies',
        'nightly_bodies',
    }

    applied = store.reconcile_ingest_sync_cursors(apply=True)
    assert applied['updated'] == 2
    assert store.get_ingest_sync_state(
        account_name='primary-account', folder_name='INBOX', sync_kind='initial_bodies'
    )['status'] == 'complete'
    assert store.get_ingest_sync_state(
        account_name='primary-account', folder_name='INBOX', sync_kind='nightly_bodies'
    )['status'] == 'complete'
    assert store.get_ingest_sync_state(
        account_name='primary-account', folder_name='Archive', sync_kind='initial_bodies'
    )['status'] == 'in_progress'
    store.close()


def test_pipeline_status_reports_identity_processing_and_last_ingestion_failures(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()

    account_id = store.ensure_account('primary-account', 'user@example.test')
    folder_id = store.ensure_folder(account_id, 'INBOX')
    store.ensure_thread(account_id, 'thread-1', 'Subject')
    message_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id='provisional:msg-1',
        internet_message_id=None,
        thread_key='thread-1',
        subject='Subject',
        normalized_subject='subject',
        from_name='Sender',
        from_addr='sender@example.test',
        to_addrs=['user@example.test'],
        sent_at='2026-04-04 12:00:00+00:00',
        received_at='2026-04-04 12:00:00+00:00',
        has_attachments=False,
        direction='incoming',
        is_read=False,
    )
    store.update_message_content(message_pk=message_pk, cleaned_text='hello', raw_path=None, text_hash='body-hash-1')
    store.update_message_identity(message_pk=message_pk, stable_message_id='content:msg-1', identity_source='content')
    store.upsert_ingest_sync_state(account_name='primary-account', folder_name='Archive', sync_kind='initial_envelopes', next_page=125, last_completed_page=124, status='in_progress')
    store.upsert_ingest_sync_state(account_name='primary-account', folder_name='Archive', sync_kind='initial_bodies', next_page=125, last_completed_page=124, status='in_progress')
    store.upsert_ingest_sync_state(account_name='primary-account', folder_name='Sent Items', sync_kind='initial_envelopes', next_page=121, last_completed_page=120, status='in_progress')
    store.upsert_ingest_sync_state(account_name='primary-account', folder_name='Sent Items', sync_kind='initial_bodies', next_page=121, last_completed_page=120, status='in_progress')
    store.upsert_ingest_sync_state(account_name='primary-account', folder_name='INBOX', sync_kind='repair_bodies', next_page=3, last_completed_page=2, status='partial')
    store.set_last_ingestion_report(
        {
            'command': 'initial-ingest',
            'body_export_failures': ['provisional:failed-msg'],
            'missing_body_messages': [],
        }
    )

    report = store.pipeline_status()
    stats = store.stats()

    assert report['messages']['total'] == 1
    assert report['messages']['with_body'] == 1
    assert report['messages']['identity_sources']['content'] == 1
    assert report['ingestion']['sync_state_counts']['initial_envelopes']['in_progress'] == 2
    assert report['ingestion']['sync_state_counts']['initial_bodies']['in_progress'] == 2
    assert report['ingestion']['sync_state_counts']['repair_bodies']['partial'] == 1
    active_states = sorted(
        ({k: v for k, v in row.items() if k != 'last_run_at'} for row in report['ingestion']['active_sync_states']),
        key=lambda row: (row['folder_name'], row['sync_kind'], row['status']),
    )
    assert active_states == [
        {
            'account_name': 'primary-account',
            'email_address': 'user@example.test',
            'folder_name': 'Archive',
            'sync_kind': 'initial_bodies',
            'next_page': 125,
            'last_completed_page': 124,
            'status': 'in_progress',
            'continuation_state': 'resume_ready',
        },
        {
            'account_name': 'primary-account',
            'email_address': 'user@example.test',
            'folder_name': 'Archive',
            'sync_kind': 'initial_envelopes',
            'next_page': 125,
            'last_completed_page': 124,
            'status': 'in_progress',
            'continuation_state': 'resume_ready',
        },
        {
            'account_name': 'primary-account',
            'email_address': 'user@example.test',
            'folder_name': 'INBOX',
            'sync_kind': 'repair_bodies',
            'next_page': 3,
            'last_completed_page': 2,
            'status': 'partial',
            'continuation_state': 'needs_attention',
        },
        {
            'account_name': 'primary-account',
            'email_address': 'user@example.test',
            'folder_name': 'Sent Items',
            'sync_kind': 'initial_bodies',
            'next_page': 121,
            'last_completed_page': 120,
            'status': 'in_progress',
            'continuation_state': 'resume_ready',
        },
        {
            'account_name': 'primary-account',
            'email_address': 'user@example.test',
            'folder_name': 'Sent Items',
            'sync_kind': 'initial_envelopes',
            'next_page': 121,
            'last_completed_page': 120,
            'status': 'in_progress',
            'continuation_state': 'resume_ready',
        },
    ]
    assert report['ingestion']['continuation_commands'] == [
        {
            'command': 'initial-ingest',
            'account_name': 'primary-account',
            'email_address': 'user@example.test',
            'folders': ['Archive', 'Sent Items'],
            'sync_kinds': ['initial_envelopes'],
            'shell_command': "email-memory-store initial-ingest --account primary-account --email user@example.test --include-folder Archive --include-folder 'Sent Items'",
        }
    ]
    assert report['ingestion']['last_report']['body_export_failures'] == ['provisional:failed-msg']
    assert stats['last_ingestion_report']['command'] == 'initial-ingest'
    assert stats['last_ingestion_report']['body_export_failures'] == ['provisional:failed-msg']

    store.close()


def test_persisted_excluded_folders_round_trip_via_metadata(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    assert store.get_excluded_folders() == []
    store.set_excluded_folders(["Trash", "Junk Email", "Trash"])

    assert store.get_excluded_folders() == ["Trash", "Junk Email"]
    raw = store.get_metadata("excluded_folders")
    assert json.loads(raw) == ["Trash", "Junk Email"]

    store.close()


def test_persisted_promotion_llm_config_round_trip_via_metadata(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    config = store.get_promotion_llm_config()
    assert config["provider"]["name"] == "hermes-default"
    assert config["batching"]["max_candidates_per_batch"] > 0
    assert config["soul"]["path"].endswith("config/promotion/souls/default.md")
    assert config["rulebook"]["path"].endswith("config/promotion/rulebooks/MEMORY_PROMOTION_RULEBOOK.md")

    updated = store.set_promotion_llm_config(
        {
            "provider": {"name": "codex-cli", "model": "gpt-5-codex"},
            "batching": {"max_candidates_per_batch": 8, "max_input_chars": 4000},
            "soul": {"path": "/tmp/custom-soul.md"},
            "rulebook": {"path": "/tmp/custom-rulebook.md"},
        }
    )
    assert updated["provider"]["name"] == "codex-cli"
    assert updated["provider"]["model"] == "gpt-5-codex"
    assert updated["batching"]["max_candidates_per_batch"] == 8
    assert updated["batching"]["max_input_chars"] == 4000
    assert updated["soul"]["path"] == "/tmp/custom-soul.md"
    assert updated["rulebook"]["path"] == "/tmp/custom-rulebook.md"

    raw = store.get_metadata("promotion_llm_config")
    assert json.loads(raw)["provider"]["name"] == "codex-cli"
    assert store.stats()["promotion_llm_config"]["provider"]["model"] == "gpt-5-codex"

    store.close()


def test_reseed_promotion_assets_force_restores_packaged_defaults(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    store.paths.default_promotion_soul_path.write_text("custom soul\n", encoding="utf-8")
    store.paths.promotion_rulebook_path.write_text("custom rulebook\n", encoding="utf-8")

    seeded = store.reseed_promotion_assets(force=True)

    assert seeded["soul_path"].endswith("config/promotion/souls/default.md")
    assert seeded["rulebook_path"].endswith("config/promotion/rulebooks/MEMORY_PROMOTION_RULEBOOK.md")
    assert "batch_review_prompt.md" in seeded["seeded_paths"]
    assert "Default Promotion Soul" in store.paths.default_promotion_soul_path.read_text(encoding="utf-8")
    assert "Memory Promotion Rulebook" in store.paths.promotion_rulebook_path.read_text(encoding="utf-8")
    assert "Batch Review Prompt Template" in store.paths.batch_review_template_path.read_text(encoding="utf-8")

    store.close()


def test_replace_message_entities_can_be_replayed_without_delete_reinsert_crash(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    account_id = store.ensure_account("primary-account", "user@example.test")
    folder_id = store.ensure_folder(account_id, "administrivia")
    message_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id="rfc822:test-message@example.test",
        identity_source="rfc822",
        internet_message_id="<test-message@example.test>",
        thread_key="thread:test",
        subject="Subject",
        normalized_subject="subject",
        from_name="Sender",
        from_addr="sender@example.test",
        to_addrs=["recipient@example.test"],
        sent_at="2026-04-04 12:00:00+00:00",
        received_at="2026-04-04 12:00:00+00:00",
        has_attachments=False,
        direction="incoming",
        is_read=False,
    )

    sender_id, sender_name = store.entity_store.ensure_person("Sender", organization_hint="example.com")
    recipient_id, recipient_name = store.entity_store.ensure_person("Recipient", organization_hint="example.com")
    store.entity_store.ensure_person_email(sender_id, "sender@example.test")
    store.entity_store.ensure_person_email(recipient_id, "recipient@example.test")

    people = [
        {
            "person_id": sender_id,
            "canonical_name": sender_name,
            "normalized_name": "sender",
            "role": "from",
            "email_address": "sender@example.test",
        },
        {
            "person_id": recipient_id,
            "canonical_name": recipient_name,
            "normalized_name": "recipient",
            "role": "to",
            "email_address": "recipient@example.test",
        },
    ]

    store.replace_message_entities(
        message_pk=message_pk,
        stable_message_id="rfc822:test-message@example.test",
        people=people,
    )
    store.replace_message_entities(
        message_pk=message_pk,
        stable_message_id="rfc822:test-message@example.test",
        people=people,
    )

    email_rows = store.conn.execute(
        "SELECT person_id, stable_message_id, role, email_address FROM email_entity_index ORDER BY person_id, role"
    ).fetchall()
    entity_rows = store.entity_store.conn.execute(
        "SELECT person_id, stable_message_id, role, email_address FROM message_entity_index ORDER BY person_id, role"
    ).fetchall()

    assert email_rows == [
        (sender_id, "rfc822:test-message@example.test", "from", "sender@example.test"),
        (recipient_id, "rfc822:test-message@example.test", "to", "recipient@example.test"),
    ]
    assert entity_rows == [
        (sender_id, "rfc822:test-message@example.test", "from", "sender@example.test"),
        (recipient_id, "rfc822:test-message@example.test", "to", "recipient@example.test"),
    ]

    store.close()


def test_collapse_duplicate_message_merges_labels_and_removes_orphan_thread(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    account_id = store.ensure_account("primary-account", "user@example.test")
    inbox_folder_id = store.ensure_folder(account_id, "INBOX")
    archive_folder_id = store.ensure_folder(account_id, "Archive")
    store.ensure_thread(account_id, "rfc822-thread:canonical@example.test", "Replay Subject")
    store.ensure_thread(account_id, "thread:provisional-replay", "Replay Subject")
    canonical_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=inbox_folder_id,
        mailbox_message_id=None,
        stable_message_id="rfc822:canonical@example.test",
        identity_source="rfc822",
        internet_message_id="<canonical@example.test>",
        thread_key="rfc822-thread:canonical@example.test",
        subject="Replay Subject",
        normalized_subject="replay subject",
        from_name="Sender",
        from_addr="sender@example.test",
        to_addrs=["user@example.test"],
        sent_at="2026-04-04 12:00:00+00:00",
        received_at="2026-04-04 12:00:00+00:00",
        has_attachments=False,
        direction="incoming",
        is_read=False,
    )
    duplicate_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=archive_folder_id,
        mailbox_message_id="dup-1",
        stable_message_id="provisional:dup-1",
        identity_source="provisional",
        internet_message_id=None,
        thread_key="thread:provisional-replay",
        subject="Replay Subject",
        normalized_subject="replay subject",
        from_name="Sender",
        from_addr="sender@example.test",
        to_addrs=["user@example.test"],
        sent_at="2026-04-04 12:00:00+00:00",
        received_at="2026-04-04 12:00:00+00:00",
        has_attachments=False,
        direction="incoming",
        is_read=False,
    )
    store.replace_message_labels(message_pk=canonical_pk, labels=["INBOX"])
    store.replace_message_labels(message_pk=duplicate_pk, labels=["Archive"])

    result_pk = store.collapse_duplicate_message(canonical_message_pk=canonical_pk, duplicate_message_pk=duplicate_pk)

    assert result_pk == canonical_pk
    rows = store.conn.execute(
        "SELECT message_pk, stable_message_id, mailbox_message_id FROM messages ORDER BY message_pk"
    ).fetchall()
    assert rows == [(canonical_pk, "rfc822:canonical@example.test", "dup-1")]
    labels = store.conn.execute(
        "SELECT label FROM message_labels WHERE message_pk = ? ORDER BY label",
        [canonical_pk],
    ).fetchall()
    assert labels == [("Archive",), ("INBOX",)]
    thread_rows = store.conn.execute("SELECT thread_key FROM threads ORDER BY thread_key").fetchall()
    assert thread_rows == [("rfc822-thread:canonical@example.test",)]

    store.close()


def test_purge_messages_by_folder_deletes_message_and_dependent_rows(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    account_id = store.ensure_account("primary-account", "user@example.test")
    store.ensure_folder(account_id, "INBOX")
    junk_folder_id = store.ensure_folder(account_id, "Junk Email")
    thread_id = store.ensure_thread(account_id, "thread-1", "Subject")
    assert thread_id > 0

    message_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=junk_folder_id,
        stable_message_id="msg-1",
        internet_message_id="<msg-1@example.test>",
        thread_key="thread-1",
        subject="Subject",
        normalized_subject="subject",
        from_name="Sender",
        from_addr="sender@example.test",
        to_addrs=["user@example.test"],
        sent_at="2026-04-04 12:00:00+00:00",
        received_at="2026-04-04 12:00:00+00:00",
        has_attachments=False,
        direction="inbound",
        is_read=False,
    )
    store.replace_message_labels(message_pk=message_pk, labels=["Junk Email", "nested/list"])
    store.conn.execute(
        "INSERT INTO calendar_events(message_pk, raw_ics) VALUES (?, ?)",
        [message_pk, "BEGIN:VCALENDAR\nEND:VCALENDAR"],
    )
    store.conn.execute(
        "INSERT INTO email_entity_index(message_pk, stable_message_id, person_id, canonical_name, normalized_name, role, email_address) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [message_pk, "msg-1", 1, "Sender", "sender", "from", "sender@example.test"],
    )
    store.entity_store.conn.execute(
        "INSERT INTO people(person_id, canonical_name, normalized_name) VALUES (1, 'Sender', 'sender')"
    )
    store.entity_store.conn.execute(
        "INSERT INTO message_entity_index(message_entity_index_id, person_id, canonical_name, normalized_name, email_message_pk, stable_message_id, role, email_address) VALUES (1, 1, 'Sender', 'sender', ?, ?, 'from', ?) ",
        [message_pk, "msg-1", "sender@example.test"],
    )
    store.conn.execute(
        "INSERT INTO promotion_log(source_object_type, source_object_id, promoted_text, promoted_category, promoted_tags, status) VALUES ('message', 'msg-1', 'test', 'fact_candidate', '[]', 'selected')"
    )

    dry_run = store.purge_messages_by_folders(["Junk Email"], dry_run=True)
    assert dry_run["messages_matched"] == 1
    assert dry_run["messages_deleted"] == 0

    result = store.purge_messages_by_folders(["Junk Email"])
    assert result["messages_matched"] == 1
    assert result["messages_deleted"] == 1
    assert result["labels_deleted"] == 2
    assert result["calendar_events_deleted"] == 1
    assert result["email_entity_links_deleted"] == 1
    assert result["entity_message_links_deleted"] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM promotion_log").fetchone()[0] == 0
    assert store.entity_store.conn.execute("SELECT COUNT(*) FROM message_entity_index").fetchone()[0] == 0

    store.close()


def test_purge_messages_by_folder_matches_descendant_folder_labels(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    account_id = store.ensure_account("primary-account", "user@example.test")
    junk_folder_id = store.ensure_folder(account_id, "Junk Email/newsletters")
    message_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=junk_folder_id,
        stable_message_id="msg-desc",
        internet_message_id="<msg-desc@example.test>",
        thread_key="thread-desc",
        subject="Descendant",
        normalized_subject="descendant",
        from_name="Sender",
        from_addr="sender@example.test",
        to_addrs=["user@example.test"],
        sent_at="2026-04-04 12:00:00+00:00",
        received_at="2026-04-04 12:00:00+00:00",
        has_attachments=False,
        direction="inbound",
        is_read=False,
    )
    store.replace_message_labels(message_pk=message_pk, labels=["Junk Email/newsletters"])

    dry_run = store.purge_messages_by_folders(["Junk Email"], dry_run=True)
    assert dry_run["messages_matched"] == 1

    result = store.purge_messages_by_folders(["Junk Email"])
    assert result["messages_deleted"] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0

    store.close()


def test_record_failed_message_ingestion_reuses_pending_row_without_status_rewrite(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    first = store.record_failed_message_ingestion(
        account_name="primary-account",
        folder_name="INBOX",
        mailbox_message_id="123",
        stable_message_id="provisional:test-123",
        failure_kind="missing_message_stub",
        error="first error",
    )
    second = store.record_failed_message_ingestion(
        account_name="primary-account",
        folder_name="INBOX",
        mailbox_message_id="123",
        stable_message_id="provisional:test-123",
        failure_kind="missing_message_stub",
        error="second error",
    )

    rows = store.conn.execute(
        """
        SELECT failed_message_ingestion_id, retry_count, status, error
        FROM failed_message_ingestions
        WHERE account_name = 'primary-account' AND folder_name = 'INBOX' AND mailbox_message_id = '123' AND failure_kind = 'missing_message_stub'
        """
    ).fetchall()

    assert len(rows) == 1
    assert rows[0][0] == 1
    assert rows[0][1] == 1
    assert rows[0][2] == "pending"
    assert rows[0][3] == "second error"
    assert first["retry_count"] == 0
    assert second["retry_count"] == 1
    assert second["status"] == "pending"

    store.close()


def test_record_failed_message_ingestion_reopens_resolved_row(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    store.record_failed_message_ingestion(
        account_name="primary-account",
        folder_name="INBOX",
        mailbox_message_id="123",
        stable_message_id="provisional:test-123",
        failure_kind="missing_message_stub",
        error="first error",
    )
    store.resolve_failed_message_ingestion(
        account_name="primary-account",
        folder_name="INBOX",
        mailbox_message_id="123",
    )

    reopened = store.record_failed_message_ingestion(
        account_name="primary-account",
        folder_name="INBOX",
        mailbox_message_id="123",
        stable_message_id="provisional:test-123",
        failure_kind="missing_message_stub",
        error="reopened error",
    )

    row = store.conn.execute(
        """
        SELECT retry_count, status, error, resolved_at
        FROM failed_message_ingestions
        WHERE account_name = 'primary-account' AND folder_name = 'INBOX' AND mailbox_message_id = '123' AND failure_kind = 'missing_message_stub'
        """
    ).fetchone()

    assert row[0] == 1
    assert row[1] == "pending"
    assert row[2] == "reopened error"
    assert row[3] is None
    assert reopened["retry_count"] == 1
    assert reopened["status"] == "pending"

    store.close()


def test_upsert_message_stub_replays_same_internet_message_id_without_index_crash(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    account_id = store.ensure_account("primary-account", "user@example.test")
    folder_id = store.ensure_folder(account_id, "administrivia")
    message_pk, created = store.upsert_message_stub(
        account_id=account_id,
        folder_id=folder_id,
        mailbox_message_id="72",
        stable_message_id="rfc822:test@example.test",
        identity_source="rfc822",
        internet_message_id="<test@example.test>",
        thread_key="rfc822-thread:test@example.test",
        subject="Parking Permit Renewal Information",
        normalized_subject="parking permit renewal information",
        from_name="Cavell, Cassie J.",
        from_addr="sender@example.test",
        to_addrs=["team-list@lists.example.test"],
        sent_at="2025-06-24 11:46:00+00:00",
        received_at="2025-06-24 11:46:00+00:00",
        has_attachments=False,
        direction="incoming",
        is_read=True,
    )
    assert created is True

    replay_pk, replay_created = store.upsert_message_stub(
        account_id=account_id,
        folder_id=folder_id,
        mailbox_message_id="72",
        stable_message_id="rfc822:test@example.test",
        identity_source="rfc822",
        internet_message_id="<test@example.test>",
        thread_key="rfc822-thread:test@example.test",
        subject="Parking Permit Renewal Information",
        normalized_subject="parking permit renewal information",
        from_name="Cavell, Cassie J.",
        from_addr="sender@example.test",
        to_addrs=["team-list@lists.example.test"],
        sent_at="2025-06-24 11:46:00+00:00",
        received_at="2025-06-24 11:46:00+00:00",
        has_attachments=False,
        direction="incoming",
        is_read=True,
    )

    row = store.conn.execute(
        "SELECT message_pk, internet_message_id FROM messages WHERE message_pk = ?",
        [message_pk],
    ).fetchone()
    assert replay_created is False
    assert replay_pk == message_pk
    assert row == (message_pk, "<test@example.test>")

    store.close()


def test_list_cross_folder_threads_returns_aggregated_lineages(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    account_id = store.ensure_account("primary-account", "user@example.test")
    inbox_folder_id = store.ensure_folder(account_id, "INBOX")
    archive_folder_id = store.ensure_folder(account_id, "Archive")
    store.ensure_thread(account_id, "rfc822-thread:root@example.test", "Project Sync")
    first_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=inbox_folder_id,
        stable_message_id="rfc822:root@example.test",
        identity_source="rfc822",
        internet_message_id="<root@example.test>",
        thread_key="rfc822-thread:root@example.test",
        subject="Project Sync",
        normalized_subject="project sync",
        from_name="Alice",
        from_addr="alice@example.test",
        to_addrs=["user@example.test"],
        sent_at="2026-04-04 12:00:00+00:00",
        received_at="2026-04-04 12:00:00+00:00",
        has_attachments=False,
        direction="incoming",
        is_read=False,
    )
    second_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=archive_folder_id,
        stable_message_id="rfc822:reply@example.test",
        identity_source="rfc822",
        internet_message_id="<reply@example.test>",
        thread_key="rfc822-thread:root@example.test",
        subject="Re: Project Sync",
        normalized_subject="project sync",
        from_name="User",
        from_addr="user@example.test",
        to_addrs=["alice@example.test"],
        sent_at="2026-04-04 13:00:00+00:00",
        received_at="2026-04-04 13:00:00+00:00",
        has_attachments=False,
        direction="outgoing",
        is_read=False,
    )
    store.replace_message_labels(message_pk=first_pk, labels=["INBOX"])
    store.replace_message_labels(message_pk=second_pk, labels=["Archive"])

    rows = store.list_cross_folder_threads(limit=10, query="project")

    assert len(rows) == 1
    assert rows[0]["thread_key"] == "rfc822-thread:root@example.test"
    assert rows[0]["folder_labels"] == ["Archive", "INBOX"]
    assert rows[0]["message_count"] == 2

    store.close()


def test_pipeline_status_reports_cross_folder_threads(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    account_id = store.ensure_account("primary-account", "user@example.test")
    inbox_folder_id = store.ensure_folder(account_id, "INBOX")
    archive_folder_id = store.ensure_folder(account_id, "Archive")
    store.ensure_thread(account_id, "rfc822-thread:root@example.test", "Project Sync")
    first_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=inbox_folder_id,
        stable_message_id="rfc822:root@example.test",
        identity_source="rfc822",
        internet_message_id="<root@example.test>",
        thread_key="rfc822-thread:root@example.test",
        subject="Project Sync",
        normalized_subject="project sync",
        from_name="Alice",
        from_addr="alice@example.test",
        to_addrs=["user@example.test"],
        sent_at="2026-04-04 12:00:00+00:00",
        received_at="2026-04-04 12:00:00+00:00",
        has_attachments=False,
        direction="incoming",
        is_read=False,
    )
    second_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=archive_folder_id,
        stable_message_id="rfc822:reply@example.test",
        identity_source="rfc822",
        internet_message_id="<reply@example.test>",
        thread_key="rfc822-thread:root@example.test",
        subject="Re: Project Sync",
        normalized_subject="project sync",
        from_name="User",
        from_addr="user@example.test",
        to_addrs=["alice@example.test"],
        sent_at="2026-04-04 13:00:00+00:00",
        received_at="2026-04-04 13:00:00+00:00",
        has_attachments=False,
        direction="outgoing",
        is_read=False,
    )
    store.replace_message_labels(message_pk=first_pk, labels=["INBOX"])
    store.replace_message_labels(message_pk=second_pk, labels=["Archive"])

    report = store.pipeline_status()

    assert report["messages"]["cross_folder_threads"] == 1

    store.close()


def test_failed_message_ingestions_are_persisted_and_reported_in_pipeline_status(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    first = store.record_failed_message_ingestion(
        account_name="primary-account",
        folder_name="INBOX",
        mailbox_message_id="inbox-1",
        stable_message_id="rfc822:inbox-1@example.test",
        failure_kind="body_export",
        error="transient export failure",
    )
    second = store.record_failed_message_ingestion(
        account_name="primary-account",
        folder_name="projects",
        mailbox_message_id="projects-1",
        stable_message_id="rfc822:projects-1@example.test",
        failure_kind="body_persist",
        error="parser exploded",
    )
    store.resolve_failed_message_ingestion(account_name="primary-account", folder_name="INBOX", mailbox_message_id="inbox-1")

    open_failures = store.list_failed_message_ingestions(account_name="primary-account")
    all_failures = store.list_failed_message_ingestions(account_name="primary-account", statuses=["pending", "resolved"])
    pipeline = store.pipeline_status()

    assert first["status"] == "pending"
    assert second["failure_kind"] == "body_persist"
    assert len(open_failures) == 1
    assert open_failures[0]["mailbox_message_id"] == "projects-1"
    assert len(all_failures) == 2
    assert pipeline["ingestion"]["failed_body_ingestions"] == {
        "open": 1,
        "resolved": 1,
    }

    store.close()


def test_update_message_rfc_threading_reassigns_thread_and_removes_orphan_thread(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    account_id = store.ensure_account("primary-account", "user@example.test")
    folder_id = store.ensure_folder(account_id, "INBOX")
    heuristic_thread_id = store.ensure_thread(account_id, "thread:seed-thread", "Replay Subject")
    message_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id="provisional:thread-reassign",
        identity_source="provisional",
        internet_message_id=None,
        thread_key="thread:seed-thread",
        subject="Replay Subject",
        normalized_subject="replay subject",
        from_name="Sender",
        from_addr="sender@example.test",
        to_addrs=["user@example.test"],
        sent_at="2026-04-04 12:00:00+00:00",
        received_at="2026-04-04 12:00:00+00:00",
        has_attachments=False,
        direction="incoming",
        is_read=False,
    )
    store.conn.execute(
        """
        UPDATE threads
        SET message_count = 1,
            participant_count = 1,
            first_message_at = '2026-04-04 12:00:00+00:00',
            last_message_at = '2026-04-04 12:00:00+00:00'
        WHERE thread_id = ?
        """,
        [heuristic_thread_id],
    )

    store.update_message_rfc_threading(
        message_pk=message_pk,
        internet_message_id="<canonical@example.test>",
        rfc_in_reply_to=None,
        rfc_references_json='[]',
        thread_key="rfc822-thread:canonical@example.test",
    )

    message_row = store.conn.execute(
        "SELECT internet_message_id, thread_key FROM messages WHERE message_pk = ?",
        [message_pk],
    ).fetchone()
    assert message_row == ("<canonical@example.test>", "rfc822-thread:canonical@example.test")
    thread_rows = store.conn.execute(
        "SELECT thread_key, message_count, participant_count FROM threads ORDER BY thread_key"
    ).fetchall()
    assert thread_rows == [("rfc822-thread:canonical@example.test", 1, 1)]

    store.close()


def test_upsert_message_stub_preserves_existing_canonical_thread_stats_on_heuristic_replay(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    account_id = store.ensure_account("primary-account", "user@example.test")
    inbox_folder_id = store.ensure_folder(account_id, "INBOX")
    archive_folder_id = store.ensure_folder(account_id, "Archive")
    canonical_thread_id = store.ensure_thread(account_id, "rfc822-thread:canonical@example.test", "Replay Subject")
    message_pk, created = store.upsert_message_stub(
        account_id=account_id,
        folder_id=inbox_folder_id,
        mailbox_message_id=None,
        stable_message_id="rfc822:canonical@example.test",
        identity_source="rfc822",
        internet_message_id="<canonical@example.test>",
        thread_key="rfc822-thread:canonical@example.test",
        subject="Replay Subject",
        normalized_subject="replay subject",
        from_name="Sender",
        from_addr="sender@example.test",
        to_addrs=["user@example.test"],
        sent_at="2026-04-04 12:00:00+00:00",
        received_at="2026-04-04 12:00:00+00:00",
        has_attachments=False,
        direction="incoming",
        is_read=False,
    )
    assert created is True
    store.conn.execute(
        """
        UPDATE threads
        SET message_count = 1,
            participant_count = 1,
            first_message_at = '2026-04-04 12:00:00+00:00',
            last_message_at = '2026-04-04 12:00:00+00:00'
        WHERE thread_id = ?
        """,
        [canonical_thread_id],
    )

    replay_message_pk, replay_created = store.upsert_message_stub(
        account_id=account_id,
        folder_id=archive_folder_id,
        mailbox_message_id="dup-1",
        stable_message_id="provisional:replay-dup",
        identity_source="provisional",
        internet_message_id=None,
        thread_key="thread:provisional-replay",
        subject="Replay Subject",
        normalized_subject="replay subject",
        from_name="Sender",
        from_addr="sender@example.test",
        to_addrs=["user@example.test"],
        sent_at="2026-04-04 12:00:00+00:00",
        received_at="2026-04-04 12:00:00+00:00",
        has_attachments=False,
        direction="incoming",
        is_read=False,
    )

    assert replay_message_pk != message_pk
    assert replay_created is True
    canonical_message_row = store.conn.execute(
        "SELECT stable_message_id, thread_key FROM messages WHERE message_pk = ?",
        [message_pk],
    ).fetchone()
    assert canonical_message_row == ("rfc822:canonical@example.test", "rfc822-thread:canonical@example.test")
    canonical_thread_row = store.conn.execute(
        "SELECT message_count, participant_count FROM threads WHERE thread_key = ?",
        ["rfc822-thread:canonical@example.test"],
    ).fetchone()
    assert canonical_thread_row == (1, 1)

    store.close()


def test_rebuild_messages_table_preserves_rows_and_allows_identity_updates(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    account_id = store.ensure_account("primary-account", "user@example.test")
    folder_id = store.ensure_folder(account_id, "INBOX")
    store.ensure_thread(account_id, "rfc822-thread:one@example.test", "One")
    first_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=folder_id,
        mailbox_message_id="101",
        stable_message_id="rfc822:one@example.test",
        identity_source="rfc822",
        internet_message_id="<one@example.test>",
        thread_key="rfc822-thread:one@example.test",
        subject="One",
        normalized_subject="one",
        from_name="Alice",
        from_addr="alice@example.test",
        to_addrs=["user@example.test"],
        sent_at="2026-04-04 12:00:00+00:00",
        received_at="2026-04-04 12:00:00+00:00",
        has_attachments=False,
        direction="incoming",
        is_read=False,
    )
    second_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=folder_id,
        mailbox_message_id="102",
        stable_message_id="provisional:two",
        identity_source="provisional",
        internet_message_id=None,
        thread_key="thread:two",
        subject="Two",
        normalized_subject="two",
        from_name="Bob",
        from_addr="bob@example.test",
        to_addrs=["user@example.test"],
        sent_at="2026-04-05 12:00:00+00:00",
        received_at="2026-04-05 12:00:00+00:00",
        has_attachments=False,
        direction="incoming",
        is_read=False,
    )
    store.replace_message_action_items(
        message_pk=first_pk,
        action_items=[{'action_text': 'review draft', 'owner': 'alice', 'due_date': None, 'status': 'open', 'confidence': 0.6}],
    )

    result = rebuild_messages_table(store)

    assert result['rows_before'] == 2
    assert result['rows_after'] == 2
    assert result['max_message_pk'] == second_pk
    assert result['seq_advanced_to'] >= second_pk + 1

    preserved_first = store.conn.execute(
        "SELECT message_pk, stable_message_id, identity_source, subject FROM messages WHERE message_pk = ?",
        [first_pk],
    ).fetchone()
    assert preserved_first == (first_pk, "rfc822:one@example.test", "rfc822", "One")

    action_item_rows = store.conn.execute(
        "SELECT action_text FROM action_items WHERE message_pk = ?",
        [first_pk],
    ).fetchall()
    assert action_item_rows == [("review draft",)]

    store.promote_message_identity(
        message_pk=second_pk,
        stable_message_id="content:two",
        identity_source="content",
        internet_message_id="<two@example.test>",
    )
    promoted = store.conn.execute(
        "SELECT stable_message_id, identity_source, internet_message_id FROM messages WHERE message_pk = ?",
        [second_pk],
    ).fetchone()
    assert promoted == ("content:two", "content", "<two@example.test>")

    store.close()


def test_rebuild_entity_message_index_table_preserves_rows(tmp_path: Path):
    from email_memory_store.entity_store import EntityMemoryStore

    es = EntityMemoryStore(tmp_path / "entity_memory.duckdb")
    es.initialize()

    es.conn.execute(
        "INSERT INTO people(person_id, canonical_name, normalized_name) VALUES (1, 'Alice', 'alice')"
    )
    es.conn.execute(
        """INSERT INTO message_entity_index(
            person_id, email_message_pk, stable_message_id,
            canonical_name, normalized_name, role, email_address
        ) VALUES
            (1, 10, 'rfc822:msg1@example.test', 'Alice', 'alice', 'from', 'alice@example.test'),
            (1, 11, 'rfc822:msg2@example.test', 'Alice', 'alice', 'to',   'alice@example.test')
        """
    )

    result = rebuild_entity_message_index_table(es)

    assert result['rows_before'] == 2
    assert result['rows_after'] == 2
    assert result['max_message_entity_index_id'] == 2
    assert result['seq_advanced_to'] >= 3

    rows = es.conn.execute(
        "SELECT stable_message_id, role FROM message_entity_index ORDER BY message_entity_index_id"
    ).fetchall()
    assert rows == [('rfc822:msg1@example.test', 'from'), ('rfc822:msg2@example.test', 'to')]

    es.conn.close()

import json
from pathlib import Path
from types import SimpleNamespace

import email_memory_store.cli as cli
from email_memory_store.store import EmailMemoryStore


class FakeVectorStore:
    def __init__(self, path: Path, counts: dict[str, int]) -> None:
        self._path = path
        self._counts = counts

    def count(self, name: str) -> int:
        return self._counts[name]


def _seed_store(store: EmailMemoryStore) -> None:
    account_id = store.ensure_account("primary-account", "user@example.test", "office365")
    folder_id = store.ensure_folder(account_id, "INBOX", "inbox")
    thread_id = store.ensure_thread(account_id, "thread:status", "status thread")
    message_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id="rfc822:status-1@example.test",
        identity_source="rfc822",
        internet_message_id="<status-1@example.test>",
        thread_key="thread:status",
        subject="Status thread",
        normalized_subject="status thread",
        from_name="Alice",
        from_addr="alice@example.test",
        to_addrs=["user@example.test"],
        sent_at="2026-04-01 09:00:00+00:00",
        received_at="2026-04-01 09:00:00+00:00",
        has_attachments=False,
        direction="incoming",
        is_read=False,
    )
    store.update_message_content(
        message_pk=message_pk,
        cleaned_text="x" * 1701,
        raw_path=None,
        text_hash="status-body",
    )
    store.conn.execute(
        "INSERT INTO thread_summaries(thread_id, summary_type, summary_text, source_message_count) VALUES (?, 'rolling', 'summary', 1)",
        [thread_id],
    )
    store.conn.execute(
        "INSERT INTO decisions(thread_id, title, decision_text, status, confidence, source_message_pk) VALUES (?, 'Decision', 'Do the thing', 'confirmed', 0.9, ?)",
        [thread_id, message_pk],
    )
    store.conn.execute(
        "INSERT INTO deadlines(thread_id, message_pk, label, due_date, related_project, confidence, status) VALUES (?, ?, 'Deadline', TIMESTAMP '2026-04-10 12:00:00', 'proj', 0.8, 'open')",
        [thread_id, message_pk],
    )
    store.conn.execute(
        "INSERT INTO action_items(thread_id, message_pk, owner, action_text, due_date, status, confidence) VALUES (?, ?, 'user@example.test', 'Follow up', NULL, 'open', 0.95)",
        [thread_id, message_pk],
    )
    store.set_promotion_llm_config(
        {
            'provider': {'name': 'codex-cli', 'model': 'gpt-5.4-mini'},
            'batching': {'max_candidates_per_batch': 8, 'max_input_chars': 4000},
        }
    )
    store.conn.execute(
        """
        INSERT INTO promotion_log(
            source_object_type, source_object_id, promoted_text, promoted_category, promoted_tags,
            fact_store_dedup_key, status, promoted_at
        ) VALUES ('decision', '1', 'decision ready', 'decision', '[]', 'decision:1', 'fact_store_ready', TIMESTAMP '2026-04-02 10:00:00')
        """
    )
    store.conn.execute(
        """
        INSERT INTO promotion_log(
            source_object_type, source_object_id, promoted_text, promoted_category, promoted_tags,
            fact_store_dedup_key, status, fact_store_written_at, promoted_at, holographic_fact_id
        ) VALUES ('action_item', '1', 'written fact', 'task', '[]', 'task:1', 'fact_store_written', TIMESTAMP '2026-04-03 11:00:00', TIMESTAMP '2026-04-03 10:30:00', NULL)
        """
    )


def test_cmd_pipeline_status_reports_retrieval_and_promotion_health(tmp_path: Path, monkeypatch, capsys) -> None:
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    _seed_store(store)
    store.upsert_ingest_sync_state(
        account_name='primary-account',
        folder_name='Archive',
        sync_kind='initial_envelopes',
        next_page=125,
        last_completed_page=124,
        status='in_progress',
    )
    store.upsert_ingest_sync_state(
        account_name='primary-account',
        folder_name='Sent Items',
        sync_kind='initial_bodies',
        next_page=121,
        last_completed_page=120,
        status='in_progress',
    )
    store.set_expiry_grace_days(30)
    store.conn.execute(
        """
        INSERT INTO deadlines(thread_id, message_pk, label, due_date, related_project, confidence, status)
        VALUES (NULL, NULL, 'Expired deadline', CURRENT_DATE - CAST(45 AS INTEGER), NULL, 0.8, 'open')
        """
    )
    store.conn.execute(
        """
        INSERT INTO action_items(thread_id, message_pk, owner, action_text, due_date, status, confidence)
        VALUES (NULL, NULL, 'user@example.test', 'Expired task', CURRENT_DATE - CAST(45 AS INTEGER), 'open', 0.95)
        """
    )
    store.conn.execute(
        """
        INSERT INTO deadlines(thread_id, message_pk, label, due_date, related_project, confidence, status)
        VALUES (NULL, NULL, 'Recent deadline', CURRENT_DATE - CAST(5 AS INTEGER), NULL, 0.8, 'open')
        """
    )

    fake_vector_store = FakeVectorStore(
        tmp_path / 'email_memory' / 'chroma',
        {
            'holographic_facts': 2,
            'action_items': 2,
            'deadlines': 3,
            'decisions': 0,
            'thread_summaries': 1,
            'message_chunks': 1,
            'calendar_events': 0,
        },
    )

    fact_store_db = tmp_path / 'fact-store.db'
    seen_fact_store_paths: list[Path] = []

    monkeypatch.setattr(cli, '_open_store', lambda args: store)
    monkeypatch.setattr(cli, '_open_vector_store', lambda root: fake_vector_store)
    monkeypatch.setattr(
        cli,
        '_count_holographic_fact_sources',
        lambda path: seen_fact_store_paths.append(path) or 2,
    )

    cli.cmd_pipeline_status(
        SimpleNamespace(root=str(tmp_path / 'email_memory'), fact_store_db=str(fact_store_db))
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload['promotion']['configured_provider'] == 'codex-cli'
    assert seen_fact_store_paths == [fact_store_db]
    assert payload['promotion']['configured_model'] == 'gpt-5.4-mini'
    assert payload['promotion']['batching'] == {
        'max_candidates_per_batch': 8,
        'max_input_chars': 4000,
    }
    assert payload['promotion']['status_counts'] == {
        'fact_store_ready': 1,
        'fact_store_written': 1,
    }
    assert payload['promotion']['ready_for_fact_store'] == 1
    assert payload['promotion']['written_without_fact_id'] == 1
    assert payload['promotion']['latest_fact_store_written_at'] == '2026-04-03 11:00:00'

    assert payload['retrieval']['persist_path'] == str(tmp_path / 'email_memory' / 'chroma')
    assert payload['retrieval']['collections']['action_items'] == {
        'vectors': 2,
        'source_rows': 2,
        'delta': 0,
    }
    assert payload['retrieval']['collections']['deadlines'] == {
        'vectors': 3,
        'source_rows': 3,
        'delta': 0,
    }
    assert payload['retrieval']['collections']['message_chunks'] == {
        'vectors': 1,
        'source_rows': 2,
        'delta': -1,
    }
    assert payload['retrieval']['collections']['holographic_facts'] == {
        'vectors': 2,
        'source_rows': 2,
        'delta': 0,
    }
    assert payload['retrieval']['collections']['calendar_events'] == {
        'vectors': 0,
        'source_rows': 0,
        'delta': 0,
    }
    assert sorted(payload['retrieval']['collections_with_drift']) == ['decisions', 'message_chunks']
    assert payload['retrieval']['total_vectors'] == 9
    assert payload['cleanup_expired']['dry_run'] is True
    assert payload['cleanup_expired']['grace_days'] == 30
    assert payload['cleanup_expired']['deadlines_matched'] == 1
    assert payload['cleanup_expired']['action_items_matched'] == 1
    assert payload['cleanup_expired']['calendar_events_matched'] == 0
    assert payload['cleanup_expired']['total_matched'] == 2
    assert payload['cleanup_expired']['deadline_samples'][0]['deadline_id'] == 2
    assert payload['cleanup_expired']['deadline_samples'][0]['label'] == 'Expired deadline'
    assert payload['cleanup_expired']['action_item_samples'][0]['action_item_id'] == 2
    assert payload['cleanup_expired']['action_item_samples'][0]['action_text'] == 'Expired task'
    assert payload['ingestion']['continuation_commands'] == [
        {
            'command': 'initial-ingest',
            'account_name': 'primary-account',
            'email_address': 'user@example.test',
            'folders': ['Archive', 'Sent Items'],
            'sync_kinds': ['initial_bodies', 'initial_envelopes'],
            'shell_command': "email-memory-store initial-ingest --account primary-account --email user@example.test --include-folder Archive --include-folder 'Sent Items'",
        }
    ]

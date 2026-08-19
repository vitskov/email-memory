from pathlib import Path
import subprocess

import pytest

from email_memory_store.himalaya import HimalayaEnvelope
from email_memory_store.ingestion import ingest_account_folders, ingest_envelopes, run_initial_ingestion, run_nightly_update, run_rfc_metadata_backfill, run_failed_body_backfill, run_ingestion_state_repair
from email_memory_store.ingestion import service as ingestion_service
from email_memory_store.store import EmailMemoryStore


class FakeHimalayaClient:
    def __init__(self):
        self.calls = []
        self.remove_flag_calls = []

    def list_folders(self, account: str):
        return ["INBOX", "projects", "Trash"]

    def list_envelopes(self, account: str, folder: str = "INBOX", page: int = 1, page_size: int = 100):
        self.calls.append((account, folder, page, page_size))
        return [
            HimalayaEnvelope(
                message_id=f"{folder}-1",
                subject=f"Subject for {folder}",
                from_addr="sender@example.test",
                from_name="Sender",
                to_addrs=["user@example.test"],
                date="2026-04-03 19:09+00:00",
                has_attachment=False,
                flags=["Seen"],
                internet_message_id=f"<{folder}-1@example.test>",
            )
        ]

    def export_message(self, account: str, message_id: str, folder: str = 'INBOX', full: bool = True) -> str:
        return (
            f"Message-ID: <{folder}-1@example.test>\n"
            f"Subject: Subject for {folder}\n\n"
            f"Body for {folder}."
        )

    def remove_flags(self, *, account: str, folder: str, message_ids: list[str], flags: list[str]) -> str:
        self.remove_flag_calls.append((account, folder, message_ids, flags))
        return ''


class PagedHimalayaClient:
    def __init__(self):
        self.calls = []
        self.export_calls = []
        self.remove_flag_calls = []
        self.pages = {
            ('INBOX', 1): [
                HimalayaEnvelope(
                    message_id='inbox-1', subject='Inbox p1', from_addr='sender@example.test', from_name='Sender',
                    to_addrs=['user@example.test'], date='2026-04-03 19:09+00:00', has_attachment=False, flags=[], internet_message_id='<inbox-1@example.test>'
                )
            ],
            ('INBOX', 2): [
                HimalayaEnvelope(
                    message_id='inbox-2', subject='Inbox p2', from_addr='sender@example.test', from_name='Sender',
                    to_addrs=['user@example.test'], date='2026-04-02 19:09+00:00', has_attachment=False, flags=[], internet_message_id='<inbox-2@example.test>'
                )
            ],
            ('INBOX', 3): [],
            ('projects', 1): [
                HimalayaEnvelope(
                    message_id='projects-1', subject='Project p1', from_addr='project@example.test', from_name='Project',
                    to_addrs=['user@example.test'], date='2026-04-01 19:09+00:00', has_attachment=False, flags=[], internet_message_id='<projects-1@example.test>'
                )
            ],
            ('projects', 2): [],
            ('Trash', 1): [
                HimalayaEnvelope(
                    message_id='trash-1', subject='Trash p1', from_addr='trash@example.test', from_name='Trash',
                    to_addrs=['user@example.test'], date='2026-04-01 19:09+00:00', has_attachment=False, flags=[], internet_message_id='<trash-1@example.test>'
                )
            ],
        }

    def list_folders(self, account: str):
        return ['INBOX', 'projects', 'Trash']

    def list_envelopes(self, account: str, folder: str = 'INBOX', page: int = 1, page_size: int = 100):
        self.calls.append((account, folder, page, page_size))
        return list(self.pages.get((folder, page), []))

    def export_message(self, account: str, message_id: str, folder: str = 'INBOX', full: bool = True) -> str:
        self.export_calls.append((account, folder, message_id, full))
        payloads = {
            'inbox-1': 'Message-ID: <inbox-1@example.test>\nSubject: Inbox p1\n\nInitial inbox page 1 body.',
            'inbox-2': 'Message-ID: <inbox-2@example.test>\nSubject: Inbox p2\n\nInitial inbox page 2 body.',
            'projects-1': 'Message-ID: <projects-1@example.test>\nSubject: Project p1\n\nAction: Send the project packet.',
            'trash-1': 'Message-ID: <trash-1@example.test>\nSubject: Trash p1\n\nTrash body.',
        }
        return payloads[message_id]

    def remove_flags(self, *, account: str, folder: str, message_ids: list[str], flags: list[str]) -> str:
        self.remove_flag_calls.append((account, folder, message_ids, flags))
        return ''


class PagedClientWithPageError(PagedHimalayaClient):
    def list_envelopes(self, account: str, folder: str = 'INBOX', page: int = 1, page_size: int = 100):
        self.calls.append((account, folder, page, page_size))
        if folder == 'INBOX' and page == 2:
            raise subprocess.CalledProcessError(1, ['himalaya', 'envelope', 'list'], stderr='page out of range')
        return list(self.pages.get((folder, page), []))


class PagedClientWithExportError(PagedHimalayaClient):
    def export_message(self, account: str, message_id: str, folder: str = 'INBOX', full: bool = True) -> str:
        self.export_calls.append((account, folder, message_id, full))
        if message_id == 'projects-1':
            raise subprocess.CalledProcessError(1, ['himalaya', 'message', 'export'], stderr='transient export failure')
        return super().export_message(account=account, message_id=message_id, folder=folder, full=full)


class NestedFoldersClient(PagedHimalayaClient):
    def list_folders(self, account: str):
        return ['INBOX', 'projects', 'Trash', 'Trash/2019', 'Junk Email', 'Junk Email/newsletters']


class BackfillPagedClient:
    def __init__(self):
        self.calls = []
        self.export_calls = []
        self.pages = {
            ('INBOX', 1): [
                HimalayaEnvelope(
                    message_id='inbox-1', subject='Project sync', from_addr='sender@example.test', from_name='Sender',
                    to_addrs=['user@example.test'], date='2026-03-15 10:00+00:00', has_attachment=False, flags=[], internet_message_id='<root@example.test>'
                )
            ],
            ('INBOX', 2): [
                HimalayaEnvelope(
                    message_id='inbox-2', subject='Re: Project sync', from_addr='sender@example.test', from_name='Sender',
                    to_addrs=['user@example.test'], date='2026-03-16 10:00+00:00', has_attachment=False, flags=[], internet_message_id='<reply@example.test>'
                ),
                HimalayaEnvelope(
                    message_id='inbox-3', subject='No local stub', from_addr='sender@example.test', from_name='Sender',
                    to_addrs=['user@example.test'], date='2026-03-17 10:00+00:00', has_attachment=False, flags=[], internet_message_id='<missing@example.test>'
                ),
            ],
            ('INBOX', 3): [],
        }

    def list_folders(self, account: str):
        return ['INBOX']

    def list_envelopes(self, account: str, folder: str = 'INBOX', page: int = 1, page_size: int = 100):
        self.calls.append((account, folder, page, page_size))
        return list(self.pages.get((folder, page), []))

    def export_message(self, account: str, message_id: str, folder: str = 'INBOX', full: bool = True) -> str:
        self.export_calls.append((account, folder, message_id, full))
        payloads = {
            'inbox-1': 'Message-ID: <root@example.test>\nSubject: Project sync\n\nInitial message.',
            'inbox-2': 'Message-ID: <reply@example.test>\nIn-Reply-To: <root@example.test>\nReferences: <root@example.test>\nSubject: Re: Project sync\n\nReply message.',
        }
        return payloads[message_id]


def _seed_backfill_message(
    store: EmailMemoryStore,
    *,
    stable_message_id: str,
    thread_key: str,
    subject: str,
    normalized_subject: str,
    sent_at: str,
    cleaned_text: str | None,
    rfc_references_json: str | None,
    internet_message_id: str | None,
) -> None:
    account_id = store.ensure_account('primary-account', 'user@example.test', 'office365')
    folder_id = store.ensure_folder(account_id, 'INBOX', 'inbox')
    store.ensure_thread(account_id, thread_key, normalized_subject)
    message_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id=stable_message_id,
        internet_message_id=internet_message_id,
        thread_key=thread_key,
        subject=subject,
        normalized_subject=normalized_subject,
        from_name='Sender',
        from_addr='sender@example.test',
        to_addrs=['user@example.test'],
        sent_at=sent_at,
        received_at=sent_at,
        has_attachments=False,
        direction='incoming',
        is_read=False,
    )
    store.conn.execute(
        'UPDATE messages SET cleaned_text = ?, rfc_references_json = ? WHERE message_pk = ?',
        [cleaned_text, rfc_references_json, message_pk],
    )


class ReplayDuplicateClient:
    def __init__(self):
        self.remove_flag_calls = []

    def list_folders(self, account: str):
        return ['Archive']

    def list_envelopes(self, account: str, folder: str = 'Archive', page: int = 1, page_size: int = 100):
        if page > 1:
            return []
        return [
            HimalayaEnvelope(
                message_id='dup-1',
                subject='Replay Subject',
                from_addr='sender@example.test',
                from_name='Sender',
                to_addrs=['user@example.test'],
                date='2026-04-03 19:09+00:00',
                has_attachment=False,
                flags=[],
                internet_message_id=None,
            )
        ]

    def export_message(self, account: str, message_id: str, folder: str = 'Archive', full: bool = True) -> str:
        assert message_id == 'dup-1'
        return (
            'Message-ID: <canonical@example.test>\n'
            'Subject: Replay Subject\n'
            'From: Sender <sender@example.test>\n'
            'To: user@example.test\n\n'
            'Replay body.'
        )

    def remove_flags(self, *, account: str, folder: str, message_ids: list[str], flags: list[str]) -> str:
        self.remove_flag_calls.append((account, folder, message_ids, flags))
        return ''


class RecoveryClient(PagedHimalayaClient):
    def __init__(self):
        super().__init__()
        self.export_calls = []

    def list_folders(self, account: str):
        return ['projects']

    def export_message(self, account: str, message_id: str, folder: str = 'INBOX', full: bool = True) -> str:
        self.export_calls.append((account, folder, message_id, full))
        payloads = {
            'projects-1': 'Message-ID: <projects-1@example.test>\nSubject: Project p1\n\nAction: Send the project packet.',
        }
        return payloads[message_id]


class MultiRecoveryClient(PagedHimalayaClient):
    def __init__(self):
        super().__init__()
        self.export_calls = []

    def list_folders(self, account: str):
        return ['projects']

    def export_message(self, account: str, message_id: str, folder: str = 'INBOX', full: bool = True) -> str:
        self.export_calls.append((account, folder, message_id, full))
        payloads = {
            'retry-1': 'Message-ID: <retry-1@example.test>\nSubject: Retry one\n\nAction: First retry action.',
            'retry-2': 'Message-ID: <retry-2@example.test>\nSubject: Retry two\n\nAction: Second retry action.',
        }
        return payloads[message_id]


def _seed_failed_body_retry_rows(store: EmailMemoryStore, *, folder_name: str = 'projects', count: int = 2) -> None:
    account_id = store.ensure_account('primary-account', 'user@example.test', 'office365')
    folder_id = store.ensure_folder(account_id, folder_name, 'custom')
    store.ensure_thread(account_id, 'thread:retry-body', 'retry body')
    for index in range(1, count + 1):
        stable_message_id = f'rfc822:retry-{index}@example.test'
        mailbox_message_id = f'retry-{index}'
        message_pk, _ = store.upsert_message_stub(
            account_id=account_id,
            folder_id=folder_id,
            mailbox_message_id=mailbox_message_id,
            stable_message_id=stable_message_id,
            identity_source='rfc822',
            internet_message_id=f'<retry-{index}@example.test>',
            thread_key='thread:retry-body',
            subject=f'Retry {index}',
            normalized_subject=f'retry {index}',
            from_name='Retry Sender',
            from_addr='sender@example.test',
            to_addrs=['user@example.test'],
            sent_at='2026-04-03 19:09:00+00:00',
            received_at='2026-04-03 19:09:00+00:00',
            has_attachments=False,
            direction='incoming',
            is_read=False,
        )
        store.replace_message_labels(message_pk=message_pk, labels=[folder_name])
        store.record_failed_message_ingestion(
            account_name='primary-account',
            folder_name=folder_name,
            mailbox_message_id=mailbox_message_id,
            stable_message_id=stable_message_id,
            failure_kind='body_persist',
            error='synthetic pending retry',
        )


def _seed_replay_duplicate_rows(store: EmailMemoryStore) -> dict[str, object]:
    account_id = store.ensure_account('primary-account', 'user@example.test', 'office365')
    inbox_folder_id = store.ensure_folder(account_id, 'INBOX', 'inbox')
    archive_folder_id = store.ensure_folder(account_id, 'Archive', 'custom')
    store.ensure_thread(account_id, 'rfc822-thread:canonical@example.test', 'replay subject')
    store.ensure_thread(account_id, 'thread:provisional-replay', 'replay subject')
    canonical_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=inbox_folder_id,
        mailbox_message_id=None,
        stable_message_id='rfc822:canonical@example.test',
        identity_source='rfc822',
        internet_message_id='<canonical@example.test>',
        thread_key='rfc822-thread:canonical@example.test',
        subject='Replay Subject',
        normalized_subject='replay subject',
        from_name='Sender',
        from_addr='sender@example.test',
        to_addrs=['user@example.test'],
        sent_at='2026-04-03 19:09:00+00:00',
        received_at='2026-04-03 19:09:00+00:00',
        has_attachments=False,
        direction='incoming',
        is_read=False,
    )
    store.replace_message_labels(message_pk=canonical_pk, labels=['INBOX'])
    provisional_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=archive_folder_id,
        mailbox_message_id='dup-1',
        stable_message_id='provisional:replay-dup',
        identity_source='provisional',
        internet_message_id=None,
        thread_key='thread:provisional-replay',
        subject='Replay Subject',
        normalized_subject='replay subject',
        from_name='Sender',
        from_addr='sender@example.test',
        to_addrs=['user@example.test'],
        sent_at='2026-04-03 19:09:00+00:00',
        received_at='2026-04-03 19:09:00+00:00',
        has_attachments=False,
        direction='incoming',
        is_read=False,
    )
    store.replace_message_labels(message_pk=provisional_pk, labels=['Archive'])
    raw_text = (
        'Message-ID: <canonical@example.test>\n'
        'Subject: Replay Subject\n'
        'From: Sender <sender@example.test>\n'
        'To: user@example.test\n\n'
        'Replay body.'
    )
    envelope = HimalayaEnvelope(
        message_id='dup-1',
        subject='Replay Subject',
        from_addr='sender@example.test',
        from_name='Sender',
        to_addrs=['user@example.test'],
        date='2026-04-03 19:09+00:00',
        has_attachment=False,
        flags=[],
        internet_message_id=None,
    )
    return {
        'account_id': account_id,
        'canonical_pk': canonical_pk,
        'provisional_pk': provisional_pk,
        'raw_text': raw_text,
        'envelope': envelope,
    }


def test_ingest_account_folders_filters_requested_folders_and_accumulates_counts(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()
    client = FakeHimalayaClient()

    summary = ingest_account_folders(
        store=store,
        client=client,
        account_name="primary-account",
        email_address="user@example.test",
        include_folders=["INBOX", "projects"],
        exclude_folders=["Trash"],
        page_size=50,
    )

    assert summary["folders_processed"] == ["INBOX", "projects"]
    assert summary["messages_added"] == 2
    assert len(client.calls) == 2
    assert store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2

    store.close()


def test_ingest_envelopes_requires_persistent_start_date(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize(start_date=None)
    store.conn.execute("DELETE FROM metadata WHERE key = 'start_date'")
    client = FakeHimalayaClient()

    with pytest.raises(ValueError, match='start_date'):
        ingest_envelopes(
            store=store,
            client=client,
            account_name="primary-account",
            email_address="user@example.test",
            folder_name="INBOX",
            page_size=50,
        )

    store.close()


def test_ingest_envelopes_skips_messages_before_persistent_start_date(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize(start_date="2026-04-01")
    client = FakeHimalayaClient()
    client.list_envelopes = lambda account, folder="INBOX", page=1, page_size=100: [
        HimalayaEnvelope(
            message_id="old-1",
            subject="Too old",
            from_addr="sender@example.test",
            from_name="Sender",
            to_addrs=["user@example.test"],
            date="2026-03-15 10:00+00:00",
            has_attachment=False,
            flags=[],
            internet_message_id="<old-1@example.test>",
        ),
        HimalayaEnvelope(
            message_id="new-1",
            subject="Recent enough",
            from_addr="sender@example.test",
            from_name="Sender",
            to_addrs=["user@example.test"],
            date="2026-04-03 10:00+00:00",
            has_attachment=False,
            flags=[],
            internet_message_id="<new-1@example.test>",
        ),
    ]

    result = ingest_envelopes(
        store=store,
        client=client,
        account_name="primary-account",
        email_address="user@example.test",
        folder_name="INBOX",
        page_size=50,
    )

    assert result["messages_seen"] == 2
    assert result["messages_skipped_before_start_date"] == 1
    assert result["messages_added"] == 1
    rows = store.conn.execute("SELECT subject FROM messages ORDER BY message_pk").fetchall()
    assert rows == [("Recent enough",)]

    store.close()


def test_initial_ingestion_defaults_to_all_folders_except_exclusion_list_and_stores_folder_labels(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    client = PagedHimalayaClient()

    result = run_initial_ingestion(
        store=store,
        client=client,
        account_name='primary-account',
        email_address='user@example.test',
        exclude_folders=['Trash'],
        max_pages_per_folder=1,
        page_size=1,
    )

    assert result['folders_processed'] == ['INBOX', 'projects']
    assert result['messages_added'] == 2
    assert result['bodies_persisted'] == 2
    assert client.remove_flag_calls == [
        ('primary-account', 'INBOX', ['inbox-1'], ['seen']),
        ('primary-account', 'projects', ['projects-1'], ['seen']),
    ]
    labels = store.conn.execute("SELECT label FROM message_labels ORDER BY label").fetchall()
    assert labels == [('INBOX',), ('projects',)]
    body_rows = store.conn.execute(
        "SELECT stable_message_id, cleaned_text, rfc_references_json, thread_key FROM messages ORDER BY stable_message_id"
    ).fetchall()
    assert body_rows == [
        ('rfc822:inbox-1@example.test', 'Initial inbox page 1 body.', '[]', 'rfc822-thread:inbox-1@example.test'),
        ('rfc822:projects-1@example.test', 'Action: Send the project packet.', '[]', 'rfc822-thread:projects-1@example.test'),
    ]
    state_rows = store.conn.execute(
        "SELECT folder_name, sync_kind, next_page, status FROM ingest_sync_state ORDER BY folder_name, sync_kind"
    ).fetchall()
    assert ('INBOX', 'initial_envelopes', 2, 'in_progress') in state_rows
    assert ('projects', 'initial_envelopes', 2, 'in_progress') in state_rows

    store.close()


def test_repeated_initial_ingestion_preserves_existing_canonical_thread_keys(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    client = PagedHimalayaClient()

    first = run_initial_ingestion(
        store=store,
        client=client,
        account_name='primary-account',
        email_address='user@example.test',
        include_folders=['INBOX'],
        max_pages_per_folder=1,
        page_size=100,
    )
    assert first['messages_added'] == 1
    thread_count_after_first = store.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]

    second = run_initial_ingestion(
        store=store,
        client=client,
        account_name='primary-account',
        email_address='user@example.test',
        include_folders=['INBOX'],
        max_pages_per_folder=1,
        page_size=100,
    )

    assert second['messages_added'] == 0
    assert second['messages_updated'] == 1
    row = store.conn.execute(
        "SELECT thread_key FROM messages WHERE stable_message_id = 'rfc822:inbox-1@example.test'"
    ).fetchone()
    assert row == ('rfc822-thread:inbox-1@example.test',)
    assert store.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == thread_count_after_first

    store.close()


def test_resolve_message_row_for_raw_message_prefers_canonical_row_and_returns_provisional_duplicate(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    seeded = _seed_replay_duplicate_rows(store)

    row, parsed, duplicate_row = ingestion_service._resolve_message_row_for_raw_message(
        store=store,
        account_name='primary-account',
        folder_name='Archive',
        envelope=seeded['envelope'],
        raw_text=seeded['raw_text'],
    )

    assert row == (seeded['canonical_pk'], 'rfc822:canonical@example.test')
    assert duplicate_row == (seeded['provisional_pk'], 'provisional:replay-dup')
    assert parsed.internet_message_id == 'canonical@example.test'

    store.close()


def test_persist_message_body_collapses_provisional_duplicate_into_existing_canonical_row(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    seeded = _seed_replay_duplicate_rows(store)

    result = ingestion_service.persist_message_body(
        store=store,
        message_pk=seeded['provisional_pk'],
        stable_message_id='provisional:replay-dup',
        raw_text=seeded['raw_text'],
    )

    assert result['message_pk'] == seeded['canonical_pk']
    rows = store.conn.execute(
        "SELECT message_pk, stable_message_id, mailbox_message_id, cleaned_text FROM messages ORDER BY message_pk"
    ).fetchall()
    assert rows == [
        (seeded['canonical_pk'], 'rfc822:canonical@example.test', 'dup-1', 'Replay body.'),
    ]
    labels = store.conn.execute(
        "SELECT label FROM message_labels WHERE message_pk = ? ORDER BY label",
        [seeded['canonical_pk']],
    ).fetchall()
    assert labels == [('Archive',), ('INBOX',)]
    thread_keys = store.conn.execute("SELECT thread_key FROM threads ORDER BY thread_key").fetchall()
    assert thread_keys == [('rfc822-thread:canonical@example.test',)]

    store.close()


def test_initial_ingestion_replay_duplicate_merges_without_body_persist_failure(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    account_id = store.ensure_account('primary-account', 'user@example.test', 'office365')
    inbox_folder_id = store.ensure_folder(account_id, 'INBOX', 'inbox')
    store.ensure_thread(account_id, 'rfc822-thread:canonical@example.test', 'replay subject')
    canonical_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=inbox_folder_id,
        mailbox_message_id=None,
        stable_message_id='rfc822:canonical@example.test',
        identity_source='rfc822',
        internet_message_id='<canonical@example.test>',
        thread_key='rfc822-thread:canonical@example.test',
        subject='Replay Subject',
        normalized_subject='replay subject',
        from_name='Sender',
        from_addr='sender@example.test',
        to_addrs=['user@example.test'],
        sent_at='2026-04-03 19:09:00+00:00',
        received_at='2026-04-03 19:09:00+00:00',
        has_attachments=False,
        direction='incoming',
        is_read=False,
    )
    store.replace_message_labels(message_pk=canonical_pk, labels=['INBOX'])

    result = run_initial_ingestion(
        store=store,
        client=ReplayDuplicateClient(),
        account_name='primary-account',
        email_address='user@example.test',
        include_folders=['Archive'],
        max_pages_per_folder=1,
        page_size=100,
    )

    assert result['body_persist_failures'] == []
    rows = store.conn.execute(
        "SELECT stable_message_id, mailbox_message_id, cleaned_text FROM messages ORDER BY message_pk"
    ).fetchall()
    assert rows == [('rfc822:canonical@example.test', 'dup-1', 'Replay body.')]
    labels = store.conn.execute("SELECT label FROM message_labels ORDER BY label").fetchall()
    assert labels == [('Archive',), ('INBOX',)]
    assert store.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 1

    store.close()


def test_failed_body_ingestions_are_persisted_and_can_be_retried(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')

    first = run_initial_ingestion(
        store=store,
        client=PagedClientWithExportError(),
        account_name='primary-account',
        email_address='user@example.test',
        include_folders=['projects'],
        max_pages_per_folder=1,
        page_size=100,
    )

    assert first['body_export_failures'] == ['rfc822:projects-1@example.test']
    recorded = store.list_failed_message_ingestions(account_name='primary-account')
    assert len(recorded) == 1
    assert recorded[0]['folder_name'] == 'projects'
    assert recorded[0]['mailbox_message_id'] == 'projects-1'
    assert recorded[0]['failure_kind'] == 'body_export'

    prepare_settings: list[tuple[str, str, str]] = []
    flush_settings: list[tuple[str, str]] = []
    original_prepare = store.prepare_for_body_persistence
    original_flush = store.flush_body_persistence_writes

    def wrapped_prepare() -> None:
        original_prepare()
        prepare_settings.append(
            store.conn.execute(
                """
                SELECT
                    current_setting('threads')::VARCHAR,
                    current_setting('preserve_insertion_order')::VARCHAR,
                    current_setting('temp_directory')
                """
            ).fetchone()
        )

    def wrapped_flush() -> None:
        flush_settings.append(
            store.conn.execute(
                """
                SELECT
                    current_setting('threads')::VARCHAR,
                    current_setting('preserve_insertion_order')::VARCHAR
                """
            ).fetchone()
        )
        original_flush()

    store.prepare_for_body_persistence = wrapped_prepare
    store.flush_body_persistence_writes = wrapped_flush

    second = run_failed_body_backfill(
        store=store,
        client=RecoveryClient(),
        account_name='primary-account',
        include_folders=['projects'],
    )

    assert second['mode'] == 'failed_body_backfill'
    assert second['attempted'] == 1
    assert second['resolved'] == 1
    assert second['remaining_open'] == 0
    row = store.conn.execute(
        "SELECT cleaned_text FROM messages WHERE stable_message_id = 'rfc822:projects-1@example.test'"
    ).fetchone()
    assert row == ('Action: Send the project packet.',)
    resolved_rows = store.list_failed_message_ingestions(account_name='primary-account', statuses=['resolved'])
    assert len(resolved_rows) == 1
    assert resolved_rows[0]['resolved_at'] is not None
    assert prepare_settings == [('1', 'false', str(store.paths.cache_dir / 'duckdb_body_persistence.tmp'))]
    assert flush_settings == [('1', 'false')]
    restored = store.conn.execute(
        """
        SELECT
            current_setting('threads')::VARCHAR,
            current_setting('preserve_insertion_order')::VARCHAR
        """
    ).fetchone()
    assert restored == ('4', 'true')

    store.close()


def test_failed_body_backfill_uses_one_checkpoint_for_multi_row_batch(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    _seed_failed_body_retry_rows(store, count=2)

    prepare_settings: list[tuple[str, str, str]] = []
    flush_settings: list[tuple[str, str]] = []
    original_prepare = store.prepare_for_body_persistence
    original_flush = store.flush_body_persistence_writes

    def wrapped_prepare() -> None:
        original_prepare()
        prepare_settings.append(
            store.conn.execute(
                """
                SELECT
                    current_setting('threads')::VARCHAR,
                    current_setting('preserve_insertion_order')::VARCHAR,
                    current_setting('temp_directory')
                """
            ).fetchone()
        )

    def wrapped_flush() -> None:
        flush_settings.append(
            store.conn.execute(
                """
                SELECT
                    current_setting('threads')::VARCHAR,
                    current_setting('preserve_insertion_order')::VARCHAR
                """
            ).fetchone()
        )
        original_flush()

    store.prepare_for_body_persistence = wrapped_prepare
    store.flush_body_persistence_writes = wrapped_flush

    result = run_failed_body_backfill(
        store=store,
        client=MultiRecoveryClient(),
        account_name='primary-account',
        include_folders=['projects'],
    )

    assert result == {
        'mode': 'failed_body_backfill',
        'attempted': 2,
        'resolved': 2,
        'still_failing': 0,
        'missing_rows': 0,
        'remaining_open': 0,
    }
    assert prepare_settings == [('1', 'false', str(store.paths.cache_dir / 'duckdb_body_persistence.tmp'))]
    assert flush_settings == [('1', 'false')]
    rows = store.conn.execute(
        """
        SELECT stable_message_id, cleaned_text
        FROM messages
        WHERE stable_message_id LIKE 'rfc822:retry-%'
        ORDER BY stable_message_id
        """
    ).fetchall()
    assert rows == [
        ('rfc822:retry-1@example.test', 'Action: First retry action.'),
        ('rfc822:retry-2@example.test', 'Action: Second retry action.'),
    ]
    restored = store.conn.execute(
        """
        SELECT
            current_setting('threads')::VARCHAR,
            current_setting('preserve_insertion_order')::VARCHAR
        """
    ).fetchone()
    assert restored == ('4', 'true')

    store.close()


def test_failed_body_backfill_continues_after_row_failure_and_checkpoints_once_if_any_succeed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    _seed_failed_body_retry_rows(store, count=2)

    prepare_settings: list[tuple[str, str, str]] = []
    flush_settings: list[tuple[str, str]] = []
    original_prepare = store.prepare_for_body_persistence
    original_flush = store.flush_body_persistence_writes
    original_persist = ingestion_service.persist_message_body
    seen_stable_ids: list[str] = []

    def wrapped_prepare() -> None:
        original_prepare()
        prepare_settings.append(
            store.conn.execute(
                """
                SELECT
                    current_setting('threads')::VARCHAR,
                    current_setting('preserve_insertion_order')::VARCHAR,
                    current_setting('temp_directory')
                """
            ).fetchone()
        )

    def wrapped_flush() -> None:
        flush_settings.append(
            store.conn.execute(
                """
                SELECT
                    current_setting('threads')::VARCHAR,
                    current_setting('preserve_insertion_order')::VARCHAR
                """
            ).fetchone()
        )
        original_flush()

    def flaky_persist(**kwargs):
        stable_message_id = kwargs['stable_message_id']
        seen_stable_ids.append(stable_message_id)
        if stable_message_id == 'rfc822:retry-1@example.test':
            raise RuntimeError('synthetic persist failure')
        return original_persist(**kwargs)

    store.prepare_for_body_persistence = wrapped_prepare
    store.flush_body_persistence_writes = wrapped_flush
    monkeypatch.setattr(ingestion_service, 'persist_message_body', flaky_persist)

    result = run_failed_body_backfill(
        store=store,
        client=MultiRecoveryClient(),
        account_name='primary-account',
        include_folders=['projects'],
    )

    assert result == {
        'mode': 'failed_body_backfill',
        'attempted': 2,
        'resolved': 1,
        'still_failing': 1,
        'missing_rows': 0,
        'remaining_open': 1,
    }
    assert seen_stable_ids == ['rfc822:retry-1@example.test', 'rfc822:retry-2@example.test']
    assert prepare_settings == [('1', 'false', str(store.paths.cache_dir / 'duckdb_body_persistence.tmp'))]
    assert flush_settings == [('1', 'false')]
    remaining = store.list_failed_message_ingestions(account_name='primary-account', statuses=['pending'])
    assert len(remaining) == 1
    assert remaining[0]['stable_message_id'] == 'rfc822:retry-1@example.test'
    resolved_rows = store.list_failed_message_ingestions(account_name='primary-account', statuses=['resolved'])
    assert len(resolved_rows) == 1
    assert resolved_rows[0]['stable_message_id'] == 'rfc822:retry-2@example.test'
    row = store.conn.execute(
        "SELECT cleaned_text FROM messages WHERE stable_message_id = 'rfc822:retry-2@example.test'"
    ).fetchone()
    assert row == ('Action: Second retry action.',)
    restored = store.conn.execute(
        """
        SELECT
            current_setting('threads')::VARCHAR,
            current_setting('preserve_insertion_order')::VARCHAR
        """
    ).fetchone()
    assert restored == ('4', 'true')

    store.close()


def test_initial_ingestion_persists_separate_body_sync_state(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')

    result = run_initial_ingestion(
        store=store,
        client=PagedHimalayaClient(),
        account_name='primary-account',
        email_address='user@example.test',
        include_folders=['projects'],
        max_pages_per_folder=1,
        page_size=100,
    )

    assert result['bodies_persisted'] == 1
    body_state = store.get_ingest_sync_state(account_name='primary-account', folder_name='projects', sync_kind='initial_bodies')
    envelope_state = store.get_ingest_sync_state(account_name='primary-account', folder_name='projects', sync_kind='initial_envelopes')
    assert body_state is not None
    assert body_state['last_completed_page'] == 1
    assert body_state['status'] == 'complete'
    assert envelope_state is not None
    assert envelope_state['status'] == 'complete'

    store.close()


def test_ingestion_state_repair_reprocesses_legacy_heuristic_thread_rows(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2026-04-01')
    client = BackfillPagedClient()

    _seed_backfill_message(
        store,
        stable_message_id='rfc822:root@example.test',
        thread_key='thread:legacy-root',
        subject='Project sync',
        normalized_subject='project sync',
        sent_at='2026-03-15 10:00:00+00:00',
        cleaned_text='Legacy body already present.',
        rfc_references_json='[]',
        internet_message_id='<root@example.test>',
    )

    result = run_ingestion_state_repair(
        store=store,
        client=client,
        account_name='primary-account',
        email_address='user@example.test',
        include_folders=['INBOX'],
        max_pages_per_folder=1,
        page_size=1,
    )

    assert result['mode'] == 'ingestion_state_repair'
    assert result['messages_repaired'] == 1
    repaired_row = store.conn.execute(
        "SELECT thread_key, rfc_references_json FROM messages WHERE stable_message_id = 'rfc822:root@example.test'"
    ).fetchone()
    assert repaired_row == ('rfc822-thread:root@example.test', '[]')
    repair_state = store.get_ingest_sync_state(account_name='primary-account', folder_name='INBOX', sync_kind='repair_bodies')
    assert repair_state is not None
    assert repair_state['last_completed_page'] == 1

    store.close()


def test_nightly_update_scans_recent_pages_only_and_updates_sync_state(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    client = PagedHimalayaClient()

    result = run_nightly_update(
        store=store,
        client=client,
        account_name='primary-account',
        email_address='user@example.test',
        exclude_folders=['Trash'],
        pages_per_folder=1,
        page_size=50,
    )

    assert result['folders_processed'] == ['INBOX', 'projects']
    assert result['bodies_persisted'] == 2
    assert ('primary-account', 'INBOX', 1, 50) in client.calls
    assert ('primary-account', 'INBOX', 2, 50) not in client.calls
    nightly_message_rows = store.conn.execute(
        "SELECT stable_message_id, cleaned_text, rfc_references_json FROM messages ORDER BY stable_message_id"
    ).fetchall()
    assert nightly_message_rows == [
        ('rfc822:inbox-1@example.test', 'Initial inbox page 1 body.', '[]'),
        ('rfc822:projects-1@example.test', 'Action: Send the project packet.', '[]'),
    ]
    nightly_rows = store.conn.execute(
        "SELECT folder_name, sync_kind, last_completed_page, status FROM ingest_sync_state WHERE sync_kind = 'nightly_envelopes' ORDER BY folder_name"
    ).fetchall()
    assert nightly_rows == [
        ('INBOX', 'nightly_envelopes', 1, 'complete'),
        ('projects', 'nightly_envelopes', 1, 'complete'),
    ]

    store.close()


def test_initial_ingestion_treats_page_out_of_range_error_as_end_of_folder(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    client = PagedClientWithPageError()

    result = run_initial_ingestion(
        store=store,
        client=client,
        account_name='primary-account',
        email_address='user@example.test',
        exclude_folders=['Trash'],
        max_pages_per_folder=3,
        page_size=1,
    )

    assert result['messages_added'] == 2
    state = store.get_ingest_sync_state(account_name='primary-account', folder_name='INBOX', sync_kind='initial_envelopes')
    assert state['status'] == 'complete'
    assert state['last_completed_page'] == 1

    store.close()


def test_initial_ingestion_continues_when_body_export_fails_for_one_message(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    client = PagedClientWithExportError()

    result = run_initial_ingestion(
        store=store,
        client=client,
        account_name='primary-account',
        email_address='user@example.test',
        exclude_folders=['Trash'],
        max_pages_per_folder=1,
        page_size=1,
    )

    assert result['folders_processed'] == ['INBOX', 'projects']
    assert result['messages_added'] == 2
    assert result['bodies_persisted'] == 1
    assert result['body_export_failures'] == ['rfc822:projects-1@example.test']
    rows = store.conn.execute(
        "SELECT stable_message_id, cleaned_text FROM messages ORDER BY stable_message_id"
    ).fetchall()
    assert rows == [
        ('rfc822:inbox-1@example.test', 'Initial inbox page 1 body.'),
        ('rfc822:projects-1@example.test', None),
    ]

    store.close()


def test_initial_ingestion_uses_persisted_excluded_folders_and_excludes_descendants(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    store.set_excluded_folders(['Trash', 'Junk Email'])
    client = NestedFoldersClient()

    result = run_initial_ingestion(
        store=store,
        client=client,
        account_name='primary-account',
        email_address='user@example.test',
        max_pages_per_folder=1,
        page_size=1,
    )

    assert result['folders_processed'] == ['INBOX', 'projects']
    assert ('primary-account', 'Trash', 1, 1) not in client.calls
    assert ('primary-account', 'Trash/2019', 1, 1) not in client.calls
    assert ('primary-account', 'Junk Email', 1, 1) not in client.calls
    assert ('primary-account', 'Junk Email/newsletters', 1, 1) not in client.calls

    store.close()


def test_rfc_metadata_backfill_resumes_and_only_reprocesses_messages_missing_metadata(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2026-04-01')
    client = BackfillPagedClient()

    _seed_backfill_message(
        store,
        stable_message_id='rfc822:root@example.test',
        thread_key='thread:legacy-root',
        subject='Project sync',
        normalized_subject='project sync',
        sent_at='2026-03-15 10:00:00+00:00',
        cleaned_text='Legacy body already present.',
        rfc_references_json=None,
        internet_message_id='<root@example.test>',
    )
    _seed_backfill_message(
        store,
        stable_message_id='rfc822:reply@example.test',
        thread_key='rfc822-thread:root@example.test',
        subject='Re: Project sync',
        normalized_subject='project sync',
        sent_at='2026-03-16 10:00:00+00:00',
        cleaned_text='Already reprocessed body.',
        rfc_references_json='[]',
        internet_message_id='<reply@example.test>',
    )

    first = run_rfc_metadata_backfill(
        store=store,
        client=client,
        account_name='primary-account',
        email_address='user@example.test',
        max_pages_per_folder=1,
        page_size=1,
    )

    assert first['mode'] == 'rfc_metadata_backfill'
    assert first['folders_processed'] == ['INBOX']
    assert first['pages_processed'] == 1
    assert first['messages_seen'] == 1
    assert first['messages_reprocessed'] == 1
    assert first['messages_already_complete'] == 0
    assert first['messages_missing_local_stub'] == 0
    assert first['messages_without_existing_body'] == 0
    assert client.calls == [('primary-account', 'INBOX', 1, 1)]
    assert client.export_calls == [('primary-account', 'INBOX', 'inbox-1', True)]
    first_state = store.get_ingest_sync_state(account_name='primary-account', folder_name='INBOX', sync_kind='rfc_metadata_backfill')
    assert first_state['next_page'] == 2
    assert first_state['last_completed_page'] == 1
    assert first_state['status'] == 'in_progress'
    first_row = store.conn.execute(
        "SELECT thread_key, rfc_in_reply_to, rfc_references_json FROM messages WHERE stable_message_id = 'rfc822:root@example.test'"
    ).fetchone()
    assert first_row == ('rfc822-thread:root@example.test', None, '[]')

    second = run_rfc_metadata_backfill(
        store=store,
        client=client,
        account_name='primary-account',
        email_address='user@example.test',
        max_pages_per_folder=3,
        page_size=1,
    )

    assert second['pages_processed'] == 1
    assert second['messages_seen'] == 2
    assert second['messages_reprocessed'] == 0
    assert second['messages_already_complete'] == 1
    assert second['messages_missing_local_stub'] == 1
    assert second['messages_without_existing_body'] == 0
    assert client.calls[-2:] == [('primary-account', 'INBOX', 2, 1), ('primary-account', 'INBOX', 3, 1)]
    assert client.export_calls == [('primary-account', 'INBOX', 'inbox-1', True)]
    second_state = store.get_ingest_sync_state(account_name='primary-account', folder_name='INBOX', sync_kind='rfc_metadata_backfill')
    assert second_state['next_page'] == 3
    assert second_state['last_completed_page'] == 2
    assert second_state['status'] == 'complete'

    store.close()


def test_rfc_metadata_backfill_skips_messages_without_existing_body(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2026-04-01')
    client = BackfillPagedClient()

    _seed_backfill_message(
        store,
        stable_message_id='rfc822:root@example.test',
        thread_key='thread:legacy-root',
        subject='Project sync',
        normalized_subject='project sync',
        sent_at='2026-03-15 10:00:00+00:00',
        cleaned_text=None,
        rfc_references_json=None,
        internet_message_id='<root@example.test>',
    )

    result = run_rfc_metadata_backfill(
        store=store,
        client=client,
        account_name='primary-account',
        email_address='user@example.test',
        max_pages_per_folder=1,
        page_size=100,
    )

    assert result['messages_reprocessed'] == 0
    assert result['messages_without_existing_body'] == 1
    assert client.export_calls == []
    row = store.conn.execute(
        "SELECT rfc_references_json FROM messages WHERE stable_message_id = 'rfc822:root@example.test'"
    ).fetchone()
    assert row == (None,)

    store.close()


# ---------------------------------------------------------------------------
# nightly-update resilience to himalaya page-fetch failures
# ---------------------------------------------------------------------------

_OOB_STDERR = b"Error: \n   0: cannot list imap envelopes: page 2 out of bounds\n"
_TRANSIENT_STDERR = b"Error: \n   0: cannot select IMAP mailbox\n   1: cannot resolve IMAP task\n"


def _full_page(folder: str, page: int, size: int) -> list[HimalayaEnvelope]:
    """A page filled to page_size, which is what forces the next page request."""
    return [
        HimalayaEnvelope(
            message_id=f'{folder}-{page}-{i}',
            subject=f'{folder} p{page} m{i}',
            from_addr='sender@example.test',
            from_name='Sender',
            to_addrs=['user@example.test'],
            date='2026-04-03 19:09+00:00',
            has_attachment=False,
            flags=['Seen'],
            internet_message_id=f'<{folder}-{page}-{i}@example.test>',
        )
        for i in range(size)
    ]


class PageFailureClient:
    """Client whose second page raises, the way himalaya does at end-of-folder."""

    def __init__(self, stderr: bytes, page_size: int = 2):
        self.stderr = stderr
        self.page_size = page_size
        self.calls = []
        self.remove_flag_calls = []

    def list_folders(self, account: str):
        return ['alpha', 'beta']

    def list_envelopes(self, account: str, folder: str = 'INBOX', page: int = 1, page_size: int = 100):
        self.calls.append((folder, page))
        if page == 1:
            return _full_page(folder, 1, self.page_size)
        raise subprocess.CalledProcessError(1, ['himalaya', 'envelope', 'list'], output=b'', stderr=self.stderr)

    def export_message(self, account: str, message_id: str, folder: str = 'INBOX', full: bool = True) -> str:
        return f'Message-ID: <{message_id}@example.test>\nSubject: {message_id}\n\nBody for {message_id}.'

    def remove_flags(self, *, account: str, folder: str, message_ids: list[str], flags: list[str]) -> str:
        self.remove_flag_calls.append((account, folder, message_ids, flags))
        return ''


def test_nightly_update_treats_page_out_of_bounds_as_end_of_folder(tmp_path: Path):
    """A folder sized an exact multiple of page_size returns a full final page.

    The only way to discover it has ended is to request one more page and get
    "out of bounds" back. That must not abort the run.
    """
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    client = PageFailureClient(_OOB_STDERR, page_size=2)

    result = run_nightly_update(
        store=store,
        client=client,
        account_name='primary-account',
        email_address='user@example.test',
        pages_per_folder=2,
        page_size=2,
    )

    # Both folders walked, neither reported as a failure.
    assert result['folders_processed'] == ['alpha', 'beta']
    assert result['folder_fetch_failures'] == []
    assert ('alpha', 2) in client.calls and ('beta', 2) in client.calls
    for folder in ('alpha', 'beta'):
        state = store.get_ingest_sync_state(
            account_name='primary-account', folder_name=folder, sync_kind='nightly_envelopes')
        assert state['status'] == 'complete'
        assert state['last_completed_page'] == 1
    store.close()


def test_nightly_update_survives_transient_page_failure_and_continues(tmp_path: Path):
    """One transient blip must not cost the rest of the mailbox.

    Regression test for the outage where a single failed envelope list aborted
    the whole 50-folder pass.
    """
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    client = PageFailureClient(_TRANSIENT_STDERR, page_size=2)

    result = run_nightly_update(
        store=store,
        client=client,
        account_name='primary-account',
        email_address='user@example.test',
        pages_per_folder=2,
        page_size=2,
    )

    # The folder after the failing one still got scanned.
    assert result['folders_processed'] == ['alpha', 'beta']
    assert ('beta', 1) in client.calls

    failures = result['folder_fetch_failures']
    assert [f['folder_name'] for f in failures] == ['alpha', 'beta']
    assert failures[0]['page'] == 2
    # The reason is recorded, not swallowed the way CalledProcessError does.
    assert 'cannot select IMAP mailbox' in failures[0]['error']

    # Page 1 content still persisted despite the page-2 failure.
    assert result['bodies_persisted'] == 4

    state = store.get_ingest_sync_state(
        account_name='primary-account', folder_name='alpha', sync_kind='nightly_envelopes')
    assert state['status'] == 'partial'
    assert state['last_completed_page'] == 1
    store.close()

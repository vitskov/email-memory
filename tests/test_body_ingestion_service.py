import subprocess

from email_memory_store.himalaya import HimalayaEnvelope
from email_memory_store.ingestion.service import ingest_message_bodies, persist_message_body
from email_memory_store.store import EmailMemoryStore


class FakeHimalayaBodyClient:
    def __init__(self):
        self.remove_flag_calls = []

    def list_envelopes(self, account: str, folder: str = "INBOX", page: int = 1, page_size: int = 100):
        return [
            HimalayaEnvelope(
                message_id="266899",
                subject="Body ingest test",
                from_addr="sender@example.test",
                from_name="Sender",
                to_addrs=["user@example.test"],
                date="2026-04-03 19:09+00:00",
                has_attachment=False,
                flags=[],
                internet_message_id="<body-ingest-1@example.test>",
            )
        ]

    def export_message(self, account: str, message_id: str, folder: str = "INBOX", full: bool = True) -> str:
        return "From: Sender <sender@example.test>\nSubject: Body ingest test\n\nThis is the full body text."

    def remove_flags(self, *, account: str, folder: str, message_ids: list[str], flags: list[str]) -> str:
        self.remove_flag_calls.append((account, folder, message_ids, flags))
        return ''


def test_ingest_message_bodies_persists_clean_text_without_raw_eml(tmp_path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    account_id = store.ensure_account('primary-account', 'user@example.test', 'office365')
    folder_id = store.ensure_folder(account_id, 'INBOX', 'inbox')
    store.ensure_thread(account_id, 'thread:seed', 'body ingest test')
    store.upsert_message_stub(
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id='rfc822:body-ingest-1@example.test',
        internet_message_id='<body-ingest-1@example.test>',
        thread_key='thread:seed',
        subject='Body ingest test',
        normalized_subject='body ingest test',
        from_name='Sender',
        from_addr='sender@example.test',
        to_addrs=['user@example.test'],
        sent_at=None,
        received_at=None,
        has_attachments=False,
        direction='incoming',
        is_read=False,
    )

    client = FakeHimalayaBodyClient()
    summary = ingest_message_bodies(
        store=store,
        client=client,
        account_name='primary-account',
        folder_name='INBOX',
    )

    assert summary['bodies_persisted'] == 1
    assert client.remove_flag_calls == [('primary-account', 'INBOX', ['266899'], ['seen'])]
    row = store.conn.execute("SELECT cleaned_text, raw_path FROM messages WHERE stable_message_id = 'rfc822:body-ingest-1@example.test'").fetchone()
    assert row[0] == 'This is the full body text.'
    assert row[1] is None
    assert summary['calendar_events_saved'] == 0
    store.close()


def test_ingest_message_bodies_matches_existing_content_identity_after_provisional_stub_upgrade(tmp_path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    account_id = store.ensure_account('primary-account', 'user@example.test', 'office365')
    folder_id = store.ensure_folder(account_id, 'INBOX', 'inbox')
    store.ensure_thread(account_id, 'thread:seed', 'body ingest test')
    message_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id='provisional:body-ingest-1',
        internet_message_id=None,
        thread_key='thread:seed',
        subject='Body ingest test',
        normalized_subject='body ingest test',
        from_name='Sender',
        from_addr='sender@example.test',
        to_addrs=['user@example.test'],
        sent_at=None,
        received_at=None,
        has_attachments=False,
        direction='incoming',
        is_read=False,
    )
    raw_text = 'From: Sender <sender@example.test>\nTo: user@example.test\nSubject: Body ingest test\n\nThis is the full body text.'
    persist_message_body(store=store, message_pk=message_pk, stable_message_id='provisional:body-ingest-1', raw_text=raw_text)

    client = FakeHimalayaBodyClient()
    summary = ingest_message_bodies(
        store=store,
        client=client,
        account_name='primary-account',
        folder_name='INBOX',
    )

    assert summary['bodies_persisted'] == 1
    assert client.remove_flag_calls == [('primary-account', 'INBOX', ['266899'], ['seen'])]
    assert summary['missing_messages'] == []
    row = store.conn.execute('SELECT stable_message_id, identity_source FROM messages WHERE message_pk = ?', [message_pk]).fetchone()
    assert row[0].startswith('content:')
    assert row[1] == 'content'
    store.close()


class UnreadRestoreFailureClient(FakeHimalayaBodyClient):
    def remove_flags(self, *, account: str, folder: str, message_ids: list[str], flags: list[str]) -> str:
        self.remove_flag_calls.append((account, folder, message_ids, flags))
        raise subprocess.CalledProcessError(1, ['himalaya', 'flag', 'remove'])


def test_ingest_message_bodies_reports_unread_restore_failure(tmp_path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    account_id = store.ensure_account('primary-account', 'user@example.test', 'office365')
    folder_id = store.ensure_folder(account_id, 'INBOX', 'inbox')
    store.ensure_thread(account_id, 'thread:seed', 'body ingest test')
    store.upsert_message_stub(
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id='rfc822:body-ingest-1@example.test',
        internet_message_id='<body-ingest-1@example.test>',
        thread_key='thread:seed',
        subject='Body ingest test',
        normalized_subject='body ingest test',
        from_name='Sender',
        from_addr='sender@example.test',
        to_addrs=['user@example.test'],
        sent_at=None,
        received_at=None,
        has_attachments=False,
        direction='incoming',
        is_read=False,
    )

    client = UnreadRestoreFailureClient()
    summary = ingest_message_bodies(
        store=store,
        client=client,
        account_name='primary-account',
        folder_name='INBOX',
    )

    # The body still persists; a failed unread restore is not fatal.
    assert summary['bodies_persisted'] == 1
    assert summary['unread_restore_failures'] == ['rfc822:body-ingest-1@example.test']

    # The failure must survive the post-persist resolve sweep.
    open_failures = store.list_failed_message_ingestions(account_name='primary-account', statuses=['pending'])
    assert [failure['failure_kind'] for failure in open_failures] == ['body_persist']
    assert 'failed to restore unread state' in open_failures[0]['error']
    store.close()

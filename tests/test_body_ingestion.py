from datetime import datetime
from pathlib import Path

from email_memory_store.identity import build_content_stable_message_id
from email_memory_store.ingestion.service import persist_message_body
from email_memory_store.store import EmailMemoryStore


def _make_store_with_message(
    tmp_path: Path,
    *,
    sent_at: str | None = None,
    received_at: str | None = None,
) -> tuple[EmailMemoryStore, int]:
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    account_id = store.ensure_account('primary-account', 'user@example.test', 'office365')
    folder_id = store.ensure_folder(account_id, 'INBOX', 'inbox')
    store.ensure_thread(account_id, 'thread:abc', 'subject')
    message_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id='rfc822:body-test@example.test',
        internet_message_id='<body-test@example.test>',
        thread_key='thread:abc',
        subject='Body test',
        normalized_subject='body test',
        from_name='Sender',
        from_addr='sender@example.test',
        to_addrs=['user@example.test'],
        sent_at=sent_at,
        received_at=received_at,
        has_attachments=False,
        direction='incoming',
        is_read=False,
    )
    return store, message_pk


def _make_thread_message(
    store: EmailMemoryStore,
    *,
    account_id: int,
    folder_id: int,
    stable_message_id: str,
    thread_key: str,
    subject: str,
    normalized_subject: str,
    from_name: str,
    from_addr: str,
    to_addrs: list[str],
    sent_at: str,
    received_at: str,
    direction: str,
) -> int:
    message_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id=stable_message_id,
        internet_message_id=None,
        thread_key=thread_key,
        subject=subject,
        normalized_subject=normalized_subject,
        from_name=from_name,
        from_addr=from_addr,
        to_addrs=to_addrs,
        sent_at=sent_at,
        received_at=received_at,
        has_attachments=False,
        direction=direction,
        is_read=False,
    )
    return message_pk


def test_persist_message_body_stores_clean_text_without_raw_eml_by_default(tmp_path: Path):
    store, message_pk = _make_store_with_message(tmp_path)

    raw_text = 'From: Sender <sender@example.test>\nSubject: Body test\n\nHello from the message body.'
    result = persist_message_body(store=store, message_pk=message_pk, stable_message_id='rfc822:body-test@example.test', raw_text=raw_text)

    row = store.conn.execute("SELECT cleaned_text, raw_path FROM messages WHERE message_pk = ?", [message_pk]).fetchone()
    assert row[0] == 'Hello from the message body.'
    assert row[1] is None
    assert result['cleaned_text_length'] > 0
    assert result['calendar_events_saved'] == 0
    assert not list((tmp_path / 'email_memory' / 'raw').glob('*.eml'))

    store.close()


def test_persist_message_body_uses_memory_safe_duckdb_settings_and_restores_defaults(tmp_path: Path):
    store, message_pk = _make_store_with_message(tmp_path)
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

    raw_text = 'From: Sender <sender@example.test>\nSubject: Body test\n\nHello from the message body.'
    persist_message_body(store=store, message_pk=message_pk, stable_message_id='rfc822:body-test@example.test', raw_text=raw_text)

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
    assert (store.paths.cache_dir / 'duckdb_body_persistence.tmp').is_dir()

    store.close()


def test_persist_message_body_rewrites_provisional_identity_to_content_identity(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    account_id = store.ensure_account('primary-account', 'user@example.test', 'office365')
    folder_id = store.ensure_folder(account_id, 'INBOX', 'inbox')
    store.ensure_thread(account_id, 'thread:abc', 'body test')
    message_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id='provisional:seed-body-test',
        internet_message_id=None,
        thread_key='thread:abc',
        subject='Body test',
        normalized_subject='body test',
        from_name='Sender',
        from_addr='sender@example.test',
        to_addrs=['user@example.test'],
        sent_at=None,
        received_at=None,
        has_attachments=False,
        direction='incoming',
        is_read=False,
    )

    raw_text = 'From: Sender <sender@example.test>\nTo: user@example.test\nSubject: Body test\n\nHello from the message body.'
    persist_message_body(store=store, message_pk=message_pk, stable_message_id='provisional:seed-body-test', raw_text=raw_text)

    body_fingerprint, stable_message_id, identity_source = store.conn.execute(
        'SELECT text_hash, stable_message_id, identity_source FROM messages WHERE message_pk = ?',
        [message_pk],
    ).fetchone()
    assert body_fingerprint
    assert stable_message_id == build_content_stable_message_id(
        normalized_subject='body test',
        from_addr='sender@example.test',
        to_addrs=['user@example.test'],
        body_fingerprint=body_fingerprint,
    )
    assert identity_source == 'content'

    store.close()


def test_persist_message_body_extracts_action_items_from_clear_action_lines(tmp_path: Path):
    store, message_pk = _make_store_with_message(tmp_path)

    raw_text = (
        'From: Sender <sender@example.test>\n'
        'Subject: Body test\n\n'
        'Thanks for the update.\n'
        'Action: Send the revised budget to Adrienne.\n'
        'Action: Draft the follow-up note for the team team.\n'
    )

    persist_message_body(store=store, message_pk=message_pk, stable_message_id='rfc822:body-test@example.test', raw_text=raw_text)

    rows = store.conn.execute(
        "SELECT message_pk, owner, action_text, due_date, status, confidence FROM action_items ORDER BY action_item_id"
    ).fetchall()
    assert rows == [
        (message_pk, None, 'Send the revised budget to Adrienne.', None, 'open', 1.0),
        (message_pk, None, 'Draft the follow-up note for the team team.', None, 'open', 1.0),
    ]

    store.close()


def test_persist_message_body_rerun_replaces_action_items_instead_of_duplicating(tmp_path: Path):
    store, message_pk = _make_store_with_message(tmp_path)

    first_raw_text = 'From: Sender <sender@example.test>\nSubject: Body test\n\nAction: Send the revised budget to Adrienne.\n'
    second_raw_text = 'From: Sender <sender@example.test>\nSubject: Body test\n\nAction: Confirm the final room booking with events.\n'

    persist_message_body(store=store, message_pk=message_pk, stable_message_id='rfc822:body-test@example.test', raw_text=first_raw_text)
    persist_message_body(store=store, message_pk=message_pk, stable_message_id='rfc822:body-test@example.test', raw_text=second_raw_text)

    rows = store.conn.execute(
        "SELECT action_text FROM action_items WHERE message_pk = ? ORDER BY action_item_id",
        [message_pk],
    ).fetchall()
    assert rows == [('Confirm the final room booking with events.',)]

    store.close()


def test_persist_message_body_extracts_deadlines_from_explicit_due_phrases(tmp_path: Path):
    store, message_pk = _make_store_with_message(
        tmp_path,
        sent_at='2026-04-01 09:00:00+00:00',
        received_at='2026-04-01 09:00:00+00:00',
    )

    raw_text = (
        'From: Sender <sender@example.test>\n'
        'Subject: Body test\n\n'
        'Please note the deadline is April 10 for the final budget packet.\n'
        'The site walk-through is due by April 12.\n'
    )

    persist_message_body(store=store, message_pk=message_pk, stable_message_id='rfc822:body-test@example.test', raw_text=raw_text)

    thread_id = store.conn.execute(
        "SELECT t.thread_id FROM messages m JOIN threads t ON t.thread_key = m.thread_key WHERE m.message_pk = ?",
        [message_pk],
    ).fetchone()[0]
    rows = store.conn.execute(
        "SELECT thread_id, message_pk, label, due_date, related_project, confidence, status FROM deadlines ORDER BY deadline_id"
    ).fetchall()
    assert rows == [
        (thread_id, message_pk, 'final budget packet', datetime(2026, 4, 10, 12, 0), None, 1.0, 'open'),
        (thread_id, message_pk, 'site walk-through', datetime(2026, 4, 12, 12, 0), None, 1.0, 'open'),
    ]

    store.close()


def test_persist_message_body_rerun_replaces_deadlines_instead_of_duplicating(tmp_path: Path):
    store, message_pk = _make_store_with_message(
        tmp_path,
        sent_at='2026-04-01 09:00:00+00:00',
        received_at='2026-04-01 09:00:00+00:00',
    )

    first_raw_text = 'From: Sender <sender@example.test>\nSubject: Body test\n\nThe deadline is April 10 for the final budget packet.\n'
    second_raw_text = 'From: Sender <sender@example.test>\nSubject: Body test\n\nThe site walk-through is due April 15.\n'

    persist_message_body(store=store, message_pk=message_pk, stable_message_id='rfc822:body-test@example.test', raw_text=first_raw_text)
    persist_message_body(store=store, message_pk=message_pk, stable_message_id='rfc822:body-test@example.test', raw_text=second_raw_text)

    rows = store.conn.execute(
        "SELECT label, due_date FROM deadlines WHERE message_pk = ? ORDER BY deadline_id",
        [message_pk],
    ).fetchall()
    assert rows == [('site walk-through', datetime(2026, 4, 15, 12, 0))]

    store.close()


def test_persist_message_body_reconstructs_reply_chain_from_rfc_headers(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    account_id = store.ensure_account('primary-account', 'owner@example.test', 'office365')
    folder_id = store.ensure_folder(account_id, 'INBOX', 'inbox')
    store.ensure_thread(account_id, 'thread:seed-a', 'project sync')
    store.ensure_thread(account_id, 'thread:seed-b', 'project sync')
    store.ensure_thread(account_id, 'thread:seed-c', 'project sync')

    root_pk = _make_thread_message(
        store,
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id='seed-root',
        thread_key='thread:seed-a',
        subject='Project sync',
        normalized_subject='project sync',
        from_name='Alice',
        from_addr='alice@example.test',
        to_addrs=['owner@example.test'],
        sent_at='2026-04-01 09:00:00+00:00',
        received_at='2026-04-01 09:00:00+00:00',
        direction='incoming',
    )
    reply_pk = _make_thread_message(
        store,
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id='seed-reply',
        thread_key='thread:seed-b',
        subject='Re: Project sync',
        normalized_subject='project sync',
        from_name='Owner',
        from_addr='owner@example.test',
        to_addrs=['alice@example.test'],
        sent_at='2026-04-01 09:05:00+00:00',
        received_at='2026-04-01 09:05:00+00:00',
        direction='outgoing',
    )
    followup_pk = _make_thread_message(
        store,
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id='seed-followup',
        thread_key='thread:seed-c',
        subject='Re: Project sync',
        normalized_subject='project sync',
        from_name='Alice',
        from_addr='alice@example.test',
        to_addrs=['owner@example.test'],
        sent_at='2026-04-01 09:15:00+00:00',
        received_at='2026-04-01 09:15:00+00:00',
        direction='incoming',
    )

    persist_message_body(
        store=store,
        message_pk=root_pk,
        stable_message_id='seed-root',
        raw_text='Message-ID: <root@example.test>\nSubject: Project sync\n\nInitial message.',
    )
    persist_message_body(
        store=store,
        message_pk=reply_pk,
        stable_message_id='seed-reply',
        raw_text='Message-ID: <reply@example.test>\nIn-Reply-To: <root@example.test>\nReferences: <root@example.test>\nSubject: Re: Project sync\n\nReply message.',
    )
    persist_message_body(
        store=store,
        message_pk=followup_pk,
        stable_message_id='seed-followup',
        raw_text='Message-ID: <followup@example.test>\nIn-Reply-To: <reply@example.test>\nReferences: <root@example.test> <reply@example.test>\nSubject: Re: Project sync\n\nFollow-up message.',
    )

    rows = store.conn.execute(
        "SELECT stable_message_id, internet_message_id, rfc_in_reply_to, rfc_references_json, thread_key FROM messages ORDER BY sent_at"
    ).fetchall()
    assert rows == [
        ('rfc822:root@example.test', '<root@example.test>', None, '[]', 'rfc822-thread:root@example.test'),
        ('rfc822:reply@example.test', '<reply@example.test>', 'root@example.test', '["root@example.test"]', 'rfc822-thread:root@example.test'),
        ('rfc822:followup@example.test', '<followup@example.test>', 'reply@example.test', '["root@example.test", "reply@example.test"]', 'rfc822-thread:root@example.test'),
    ]

    store.close()


def test_persist_message_body_tolerates_malformed_message_id_header(tmp_path: Path):
    store, message_pk = _make_store_with_message(tmp_path)

    malformed_id = '[a44f9b49034a47ebb26ab5e224c519d2@example.test]'
    persist_message_body(
        store=store,
        message_pk=message_pk,
        stable_message_id='rfc822:body-test@example.test',
        raw_text=(
            f'Message-ID: <{malformed_id}>\n'
            'References: <root@example.test>\n'
            'Subject: Malformed id\n'
            '\n'
            'Body text.'
        ),
    )

    row = store.conn.execute(
        'SELECT stable_message_id, internet_message_id, rfc_references_json, thread_key FROM messages'
    ).fetchone()
    assert row == (
        f'rfc822:{malformed_id}',
        f'<{malformed_id}>',
        '["root@example.test"]',
        'rfc822-thread:root@example.test',
    )

    store.close()


def test_persist_message_body_uses_references_root_when_parent_message_is_missing(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    account_id = store.ensure_account('primary-account', 'owner@example.test', 'office365')
    folder_id = store.ensure_folder(account_id, 'INBOX', 'inbox')
    store.ensure_thread(account_id, 'thread:seed-a', 'grant update')
    store.ensure_thread(account_id, 'thread:seed-b', 'grant update')

    descendant_one_pk = _make_thread_message(
        store,
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id='desc-1',
        thread_key='thread:seed-a',
        subject='Re: Project update',
        normalized_subject='grant update',
        from_name='Alice',
        from_addr='alice@example.test',
        to_addrs=['owner@example.test'],
        sent_at='2026-04-10 09:00:00+00:00',
        received_at='2026-04-10 09:00:00+00:00',
        direction='incoming',
    )
    descendant_two_pk = _make_thread_message(
        store,
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id='desc-2',
        thread_key='thread:seed-b',
        subject='Re: Project update',
        normalized_subject='grant update',
        from_name='Owner',
        from_addr='owner@example.test',
        to_addrs=['alice@example.test'],
        sent_at='2026-04-10 10:00:00+00:00',
        received_at='2026-04-10 10:00:00+00:00',
        direction='outgoing',
    )

    persist_message_body(
        store=store,
        message_pk=descendant_one_pk,
        stable_message_id='desc-1',
        raw_text='Message-ID: <desc-1@example.test>\nReferences: <root@example.test> <missing-parent@example.test>\nSubject: Re: Project update\n\nFirst descendant.',
    )
    persist_message_body(
        store=store,
        message_pk=descendant_two_pk,
        stable_message_id='desc-2',
        raw_text='Message-ID: <desc-2@example.test>\nIn-Reply-To: <missing-parent@example.test>\nReferences: <root@example.test> <missing-parent@example.test>\nSubject: Re: Project update\n\nSecond descendant.',
    )

    rows = store.conn.execute(
        "SELECT stable_message_id, rfc_in_reply_to, rfc_references_json, thread_key FROM messages ORDER BY sent_at"
    ).fetchall()
    assert rows == [
        ('rfc822:desc-1@example.test', None, '["root@example.test", "missing-parent@example.test"]', 'rfc822-thread:root@example.test'),
        ('rfc822:desc-2@example.test', 'missing-parent@example.test', '["root@example.test", "missing-parent@example.test"]', 'rfc822-thread:root@example.test'),
    ]

    store.close()


def test_persist_message_body_uses_fallback_only_for_orphans_and_does_not_merge_far_apart_same_subject(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    account_id = store.ensure_account('primary-account', 'owner@example.test', 'office365')
    folder_id = store.ensure_folder(account_id, 'INBOX', 'inbox')
    for key in ('thread:seed-a', 'thread:seed-b', 'thread:seed-c', 'thread:seed-d'):
        store.ensure_thread(account_id, key, 'status update')

    first_pk = _make_thread_message(
        store,
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id='orphan-1',
        thread_key='thread:seed-a',
        subject='Status update',
        normalized_subject='status update',
        from_name='Alice',
        from_addr='alice@example.test',
        to_addrs=['owner@example.test'],
        sent_at='2026-01-01 09:00:00+00:00',
        received_at='2026-01-01 09:00:00+00:00',
        direction='incoming',
    )
    second_pk = _make_thread_message(
        store,
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id='orphan-2',
        thread_key='thread:seed-b',
        subject='Re: Status update',
        normalized_subject='status update',
        from_name='Owner',
        from_addr='owner@example.test',
        to_addrs=['alice@example.test'],
        sent_at='2026-01-03 09:00:00+00:00',
        received_at='2026-01-03 09:00:00+00:00',
        direction='outgoing',
    )
    third_pk = _make_thread_message(
        store,
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id='orphan-3',
        thread_key='thread:seed-c',
        subject='Status update',
        normalized_subject='status update',
        from_name='Alice',
        from_addr='alice@example.test',
        to_addrs=['owner@example.test'],
        sent_at='2026-05-15 09:00:00+00:00',
        received_at='2026-05-15 09:00:00+00:00',
        direction='incoming',
    )
    fourth_pk = _make_thread_message(
        store,
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id='non-orphan',
        thread_key='thread:seed-d',
        subject='Re: Status update',
        normalized_subject='status update',
        from_name='Owner',
        from_addr='owner@example.test',
        to_addrs=['alice@example.test'],
        sent_at='2026-05-16 09:00:00+00:00',
        received_at='2026-05-16 09:00:00+00:00',
        direction='outgoing',
    )

    persist_message_body(store=store, message_pk=first_pk, stable_message_id='orphan-1', raw_text='Subject: Status update\n\nFirst orphan.')
    persist_message_body(store=store, message_pk=second_pk, stable_message_id='orphan-2', raw_text='Subject: Re: Status update\n\nSecond orphan.')
    persist_message_body(store=store, message_pk=third_pk, stable_message_id='orphan-3', raw_text='Subject: Status update\n\nThird orphan months later.')
    persist_message_body(
        store=store,
        message_pk=fourth_pk,
        stable_message_id='non-orphan',
        raw_text='Message-ID: <child@example.test>\nIn-Reply-To: <unseen@example.test>\nSubject: Re: Status update\n\nThreaded orphan with headers.',
    )

    rows = store.conn.execute(
        "SELECT stable_message_id, thread_key FROM messages ORDER BY sent_at"
    ).fetchall()
    assert rows[0][1] == rows[1][1]
    assert rows[0][1].startswith('fallback-thread:')
    assert rows[2][1] != rows[0][1]
    assert rows[3][1] == 'rfc822-thread:unseen@example.test'

    store.close()


def test_persist_message_body_prefers_html_converted_to_clean_text_and_ignores_regular_attachment_payloads(tmp_path: Path):
    store, message_pk = _make_store_with_message(tmp_path)

    raw_text = "\n".join([
        'MIME-Version: 1.0',
        'Content-Type: multipart/mixed; boundary="mix"',
        '',
        '--mix',
        'Content-Type: text/html; charset="utf-8"',
        '',
        '<html><body><h1>Hello</h1><p>World</p><script>noise()</script></body></html>',
        '--mix',
        'Content-Type: application/pdf',
        'Content-Disposition: attachment; filename="paper.pdf"',
        'Content-Transfer-Encoding: base64',
        '',
        'UERGIEFUVEFDSE1FTlQgU0hPVUxEIE5PVCBBUFBFQVI=',
        '--mix--',
        '',
    ])

    persist_message_body(store=store, message_pk=message_pk, stable_message_id='rfc822:body-test@example.test', raw_text=raw_text)

    cleaned_text = store.conn.execute("SELECT cleaned_text FROM messages WHERE message_pk = ?", [message_pk]).fetchone()[0]
    assert cleaned_text == 'Hello\n\nWorld'
    assert 'PDF ATTACHMENT SHOULD NOT APPEAR' not in cleaned_text

    store.close()


def test_persist_message_body_saves_calendar_event_attachments(tmp_path: Path):
    store, message_pk = _make_store_with_message(tmp_path)

    raw_text = "\n".join([
        'MIME-Version: 1.0',
        'Content-Type: multipart/mixed; boundary="mix"',
        '',
        '--mix',
        'Content-Type: text/plain; charset="utf-8"',
        '',
        'Please see attached invite.',
        '--mix',
        'Content-Type: text/calendar; charset="utf-8"; method=REQUEST; name="invite.ics"',
        'Content-Disposition: attachment; filename="invite.ics"',
        '',
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'METHOD:REQUEST',
        'BEGIN:VEVENT',
        'UID:test-event-123',
        'SUMMARY:Project Meeting',
        'DTSTART:20260405T150000Z',
        'DTEND:20260405T153000Z',
        'ORGANIZER;CN=Prof Example:mailto:prof@example.test',
        'STATUS:CONFIRMED',
        'SEQUENCE:2',
        'ATTENDEE;CN=Alice Example;PARTSTAT=ACCEPTED;ROLE=REQ-PARTICIPANT:mailto:alice@example.test',
        'END:VEVENT',
        'END:VCALENDAR',
        '--mix--',
        '',
    ])

    result = persist_message_body(store=store, message_pk=message_pk, stable_message_id='rfc822:body-test@example.test', raw_text=raw_text)

    row = store.conn.execute(
        "SELECT filename, method, uid, summary, organizer, organizer_email, status, sequence, attendees_json, raw_ics FROM calendar_events WHERE message_pk = ?",
        [message_pk],
    ).fetchone()
    assert row[0] == 'invite.ics'
    assert row[1] == 'REQUEST'
    assert row[2] == 'test-event-123'
    assert row[3] == 'Project Meeting'
    assert row[4] == 'Prof Example'
    assert row[5] == 'prof@example.test'
    assert row[6] == 'CONFIRMED'
    assert row[7] == 2
    assert 'alice@example.test' in row[8]
    assert 'BEGIN:VCALENDAR' in row[9]
    assert result['calendar_events_saved'] == 1

    store.close()


def test_persist_message_body_hardens_calendar_cancellations_recurrence_and_timezones(tmp_path: Path):
    store, message_pk = _make_store_with_message(tmp_path)

    raw_text = "\n".join([
        'MIME-Version: 1.0',
        'Content-Type: multipart/mixed; boundary="mix"',
        '',
        '--mix',
        'Content-Type: text/calendar; charset="utf-8"; method=CANCEL; name="cancel.ics"',
        'Content-Disposition: attachment; filename="cancel.ics"',
        '',
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'METHOD:CANCEL',
        'BEGIN:VEVENT',
        'UID:series-42',
        'SUMMARY:Weekly Sync',
        'STATUS:CANCELLED',
        'SEQUENCE:9',
        'DTSTART;TZID=America/New_York:20260405T110000',
        'DTEND;TZID=America/New_York:20260405T113000',
        'RECURRENCE-ID;TZID=America/New_York:20260405T110000',
        'RRULE:FREQ=WEEKLY;BYDAY=MO',
        'ATTENDEE;CN=Bob Example;PARTSTAT=NEEDS-ACTION:mailto:bob@example.test',
        'END:VEVENT',
        'END:VCALENDAR',
        '--mix--',
        '',
    ])

    persist_message_body(store=store, message_pk=message_pk, stable_message_id='rfc822:body-test@example.test', raw_text=raw_text)

    row = store.conn.execute(
        "SELECT method, status, recurrence_id, recurrence_rule, starts_at_tzid, ends_at_tzid, recurrence_id_tzid, attendees_json, starts_at, ends_at FROM calendar_events WHERE message_pk = ?",
        [message_pk],
    ).fetchone()
    assert row[0] == 'CANCEL'
    assert row[1] == 'CANCELLED'
    assert row[2] == '20260405T110000'
    assert row[3] == 'FREQ=WEEKLY;BYDAY=MO'
    assert row[4] == 'America/New_York'
    assert row[5] == 'America/New_York'
    assert row[6] == 'America/New_York'
    assert 'bob@example.test' in row[7]
    assert str(row[8]) == '2026-04-05 15:00:00'
    assert str(row[9]) == '2026-04-05 15:30:00'

    store.close()

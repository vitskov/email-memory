from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from email import policy
from email.message import Message
from email.parser import Parser
from email.utils import getaddresses
from html import unescape
from html.parser import HTMLParser
import hashlib
import json
import re
import subprocess
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from ..entity_store import normalize_person_name
from ..himalaya import HimalayaClient, himalaya_stderr, is_page_out_of_bounds
from ..identity import (
    build_body_fingerprint,
    build_content_stable_message_id,
    build_stable_message_id,
    build_thread_key,
    normalize_subject,
)
from ..store import EmailMemoryStore
from ..timeutils import normalize_timestamp


WINDOWS_TO_IANA_TZIDS = {
    'Mountain Standard Time': 'America/Denver',
    'US Mountain Standard Time': 'America/Phoenix',
    'Mountain Time (US & Canada)': 'America/Denver',
    'Pacific Standard Time': 'America/Los_Angeles',
    'Central Standard Time': 'America/Chicago',
    'Eastern Standard Time': 'America/New_York',
    'UTC': 'UTC',
}


def ingest_envelopes(
    *,
    store: EmailMemoryStore,
    client: HimalayaClient,
    account_name: str,
    email_address: str,
    folder_name: str,
    folder_type: str | None = None,
    provider: str | None = None,
    page: int = 1,
    page_size: int = 100,
    envelopes: list[Any] | None = None,
) -> dict[str, Any]:
    account_id = store.ensure_account(account_name=account_name, email_address=email_address, provider=provider)
    folder_id = store.ensure_folder(account_id=account_id, folder_name=folder_name, folder_type=folder_type)

    envelopes = envelopes if envelopes is not None else client.list_envelopes(account=account_name, folder=folder_name, page=page, page_size=page_size)
    messages_added = 0
    messages_updated = 0
    messages_skipped_before_start_date = 0
    start_datetime = store.get_start_datetime()
    if start_datetime is None:
        raise ValueError('start_date is not configured; initialize the database first')

    for envelope in envelopes:
        envelope_ts = normalize_timestamp(envelope.date)
        if start_datetime is not None and envelope_ts is not None and envelope_ts.replace(tzinfo=None) < start_datetime:
            messages_skipped_before_start_date += 1
            continue
        stable_message_id = build_stable_message_id(account_name=account_name, folder_name=folder_name, envelope=envelope)
        normalized_subject = normalize_subject(envelope.subject)
        heuristic_thread_key = build_thread_key(account_name=account_name, envelope=envelope)
        if envelope.from_addr:
            store.ensure_contact(email_address=envelope.from_addr, display_name=envelope.from_name)

        direction = 'outgoing' if envelope.from_addr.lower() == email_address.lower() else 'incoming'
        message_pk, created = store.upsert_message_stub(
            account_id=account_id,
            folder_id=folder_id,
            mailbox_message_id=envelope.message_id,
            stable_message_id=stable_message_id,
            identity_source='rfc822' if envelope.internet_message_id else 'provisional',
            internet_message_id=envelope.internet_message_id,
            thread_key=heuristic_thread_key,
            subject=envelope.subject,
            normalized_subject=normalized_subject,
            from_name=envelope.from_name,
            from_addr=envelope.from_addr,
            to_addrs=envelope.to_addrs,
            sent_at=envelope_ts,
            received_at=envelope_ts,
            has_attachments=envelope.has_attachment,
            direction=direction,
            is_read='Seen' in envelope.flags,
        )
        thread_key = store.get_message_thread_key(message_pk=message_pk) or heuristic_thread_key
        thread_id = store.ensure_thread(account_id=account_id, thread_key=thread_key, canonical_subject=normalized_subject)
        if created:
            messages_added += 1
        else:
            messages_updated += 1

        store.replace_message_entities(
            message_pk=message_pk,
            stable_message_id=store.get_message_stable_message_id(message_pk=message_pk),
            people=_build_message_people(
                store=store,
                envelope=envelope,
                account_email=email_address,
            ),
        )
        store.replace_message_labels(message_pk=message_pk, labels=[folder_name], label_type='folder')

        store.conn.execute(
            """
            UPDATE threads
            SET message_count = (SELECT COUNT(*) FROM messages WHERE thread_key = ?),
                participant_count = (SELECT COUNT(DISTINCT from_addr) FROM messages WHERE thread_key = ?),
                first_message_at = COALESCE(first_message_at, ?),
                last_message_at = GREATEST(COALESCE(last_message_at, ?), ?),
                updated_at = CURRENT_TIMESTAMP
            WHERE thread_id = ?
            """,
            [thread_key, thread_key, envelope_ts, envelope_ts, envelope_ts, thread_id],
        )

    return {
        'account_id': account_id,
        'folder_id': folder_id,
        'folder_name': folder_name,
        'messages_seen': len(envelopes),
        'messages_skipped_before_start_date': messages_skipped_before_start_date,
        'messages_added': messages_added,
        'messages_updated': messages_updated,
    }


def _folder_matches_rule(folder_name: str, rule: str) -> bool:
    return folder_name == rule or folder_name.startswith(f'{rule}/')



def _resolve_excluded_folders(store: EmailMemoryStore, exclude_folders: Iterable[str] | None) -> list[str]:
    if exclude_folders is None:
        return store.get_excluded_folders()
    return store.set_excluded_folders(list(exclude_folders))



def _select_folders(
    *,
    available_folders: list[str],
    include_folders: Iterable[str] | None,
    exclude_folders: list[str],
) -> list[str]:
    include_set = set(include_folders) if include_folders else set(available_folders)
    return [
        folder for folder in available_folders
        if folder in include_set and not any(_folder_matches_rule(folder, rule) for rule in exclude_folders)
    ]



def ingest_account_folders(
    *,
    store: EmailMemoryStore,
    client: HimalayaClient,
    account_name: str,
    email_address: str,
    include_folders: Iterable[str] | None = None,
    exclude_folders: Iterable[str] | None = None,
    provider: str | None = None,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    available_folders = client.list_folders(account_name)
    resolved_exclude_folders = _resolve_excluded_folders(store, exclude_folders)
    folders = _select_folders(
        available_folders=available_folders,
        include_folders=include_folders,
        exclude_folders=resolved_exclude_folders,
    )

    summary: dict[str, Any] = {
        'folders_processed': [],
        'messages_seen': 0,
        'messages_skipped_before_start_date': 0,
        'messages_added': 0,
        'messages_updated': 0,
    }
    for folder in folders:
        result = ingest_envelopes(
            store=store,
            client=client,
            account_name=account_name,
            email_address=email_address,
            folder_name=folder,
            folder_type=_guess_folder_type(folder),
            provider=provider,
            page=page,
            page_size=page_size,
        )
        summary['folders_processed'].append(folder)
        summary['messages_seen'] += result['messages_seen']
        summary['messages_skipped_before_start_date'] += result['messages_skipped_before_start_date']
        summary['messages_added'] += result['messages_added']
        summary['messages_updated'] += result['messages_updated']
    return summary


def _resolve_message_row_for_raw_message(
    *,
    store: EmailMemoryStore,
    account_name: str,
    folder_name: str,
    envelope: Any,
    raw_text: str,
) -> tuple[tuple[Any, ...] | None, 'ParsedMessageContent', tuple[Any, ...] | None]:
    provisional_stable_message_id = build_stable_message_id(account_name=account_name, folder_name=folder_name, envelope=envelope)
    parsed = _parse_message_content(raw_text)
    body_fingerprint: str | None = None
    content_stable_message_id: str | None = None
    if parsed.cleaned_text.strip():
        body_fingerprint = build_body_fingerprint(parsed.cleaned_text)
        content_stable_message_id = build_content_stable_message_id(
            normalized_subject=parsed.normalized_subject,
            from_addr=parsed.from_addr,
            to_addrs=parsed.to_addrs,
            body_fingerprint=body_fingerprint,
        )

    preferred_candidate_ids: list[str] = []
    if parsed.internet_message_id:
        preferred_candidate_ids.append(f"rfc822:{parsed.internet_message_id}")
    if content_stable_message_id:
        preferred_candidate_ids.append(content_stable_message_id)

    preferred_row: tuple[Any, ...] | None = None
    for candidate_id in dict.fromkeys(preferred_candidate_ids):
        row = store.conn.execute(
            "SELECT message_pk, stable_message_id FROM messages WHERE stable_message_id = ?",
            [candidate_id],
        ).fetchone()
        if row:
            preferred_row = row
            break

    provisional_row = store.conn.execute(
        "SELECT message_pk, stable_message_id FROM messages WHERE stable_message_id = ?",
        [provisional_stable_message_id],
    ).fetchone()
    if preferred_row is None and provisional_row:
        preferred_row = provisional_row

    if preferred_row is None and body_fingerprint:
        preferred_row = store.conn.execute(
            """
            SELECT message_pk, stable_message_id
            FROM messages
            WHERE text_hash = ?
              AND COALESCE(normalized_subject, '') = ?
              AND COALESCE(lower(from_addr), '') = ?
            LIMIT 1
            """,
            [body_fingerprint, parsed.normalized_subject, (parsed.from_addr or '').lower()],
        ).fetchone()

    mailbox_row: tuple[Any, ...] | None = None
    if envelope.message_id:
        mailbox_row = store.conn.execute(
            "SELECT message_pk, stable_message_id FROM messages WHERE account_id = (SELECT account_id FROM accounts WHERE account_name = ?) AND mailbox_message_id = ? LIMIT 1",
            [account_name, envelope.message_id],
        ).fetchone()
        if preferred_row is None and mailbox_row:
            preferred_row = mailbox_row

    duplicate_row: tuple[Any, ...] | None = None
    for candidate_row in (provisional_row, mailbox_row):
        if not candidate_row or not preferred_row:
            continue
        if int(candidate_row[0]) == int(preferred_row[0]):
            continue
        if str(candidate_row[1]).startswith('provisional:'):
            duplicate_row = candidate_row
            break

    return preferred_row, parsed, duplicate_row


def _stable_message_id_for_envelope(*, account_name: str, folder_name: str, envelope: Any) -> str:
    return build_stable_message_id(account_name=account_name, folder_name=folder_name, envelope=envelope)


def _resolve_message_row_for_retry(
    *,
    store: EmailMemoryStore,
    account_name: str,
    folder_name: str,
    mailbox_message_id: str,
    stable_message_id: str | None,
    raw_text: str,
) -> tuple[tuple[Any, ...] | None, 'ParsedMessageContent', tuple[Any, ...] | None]:
    parsed = _parse_message_content(raw_text)
    body_fingerprint: str | None = None
    content_stable_message_id: str | None = None
    if parsed.cleaned_text.strip():
        body_fingerprint = build_body_fingerprint(parsed.cleaned_text)
        content_stable_message_id = build_content_stable_message_id(
            normalized_subject=parsed.normalized_subject,
            from_addr=parsed.from_addr,
            to_addrs=parsed.to_addrs,
            body_fingerprint=body_fingerprint,
        )

    preferred_candidate_ids: list[str] = []
    if parsed.internet_message_id:
        preferred_candidate_ids.append(f'rfc822:{parsed.internet_message_id}')
    if content_stable_message_id:
        preferred_candidate_ids.append(content_stable_message_id)
    if stable_message_id:
        preferred_candidate_ids.append(stable_message_id)

    preferred_row: tuple[Any, ...] | None = None
    for candidate_id in dict.fromkeys(preferred_candidate_ids):
        row = store.conn.execute(
            "SELECT message_pk, stable_message_id FROM messages WHERE stable_message_id = ?",
            [candidate_id],
        ).fetchone()
        if row:
            preferred_row = row
            break

    mailbox_row = store.conn.execute(
        "SELECT message_pk, stable_message_id FROM messages WHERE account_id = (SELECT account_id FROM accounts WHERE account_name = ?) AND mailbox_message_id = ? LIMIT 1",
        [account_name, mailbox_message_id],
    ).fetchone()
    if preferred_row is None and mailbox_row:
        preferred_row = mailbox_row

    duplicate_row: tuple[Any, ...] | None = None
    if mailbox_row and preferred_row and int(mailbox_row[0]) != int(preferred_row[0]) and str(mailbox_row[1]).startswith('provisional:'):
        duplicate_row = mailbox_row

    return preferred_row, parsed, duplicate_row


def _record_failed_body_ingestion(
    *,
    store: EmailMemoryStore,
    account_name: str,
    folder_name: str,
    mailbox_message_id: str,
    stable_message_id: str | None,
    failure_kind: str,
    error: str | None,
) -> None:
    store.record_failed_message_ingestion(
        account_name=account_name,
        folder_name=folder_name,
        mailbox_message_id=mailbox_message_id,
        stable_message_id=stable_message_id,
        failure_kind=failure_kind,
        error=error,
    )


def _resolve_failed_body_ingestion(
    *,
    store: EmailMemoryStore,
    account_name: str,
    folder_name: str,
    mailbox_message_id: str,
) -> None:
    store.resolve_failed_message_ingestion(
        account_name=account_name,
        folder_name=folder_name,
        mailbox_message_id=mailbox_message_id,
    )


def _try_export_message(
    *,
    client: HimalayaClient,
    account_name: str,
    folder_name: str,
    envelope: Any,
) -> str | None:
    try:
        return client.export_message(account=account_name, message_id=envelope.message_id, folder=folder_name, full=True)
    except subprocess.CalledProcessError:
        return None


def _envelope_was_unread(envelope: Any) -> bool:
    return not any(str(flag).lower() == 'seen' for flag in (getattr(envelope, 'flags', None) or []))


def _restore_unread_after_export(
    *,
    client: HimalayaClient,
    account_name: str,
    folder_name: str,
    envelope: Any,
) -> str | None:
    if not _envelope_was_unread(envelope):
        return None
    try:
        client.remove_flags(
            account=account_name,
            folder=folder_name,
            message_ids=[envelope.message_id],
            flags=['seen'],
        )
    except subprocess.CalledProcessError as exc:
        return str(exc)
    return None


def _persist_page_bodies(
    *,
    store: EmailMemoryStore,
    client: HimalayaClient,
    account_name: str,
    folder_name: str,
    envelopes: list[Any],
    start_datetime: datetime | None = None,
) -> dict[str, Any]:
    bodies_persisted = 0
    calendar_events_saved = 0
    missing_messages: list[str] = []
    body_export_failures: list[str] = []
    body_persist_failures: list[dict[str, str]] = []

    for envelope in envelopes:
        if start_datetime is not None:
            envelope_ts = normalize_timestamp(envelope.date)
            if envelope_ts is not None and envelope_ts.replace(tzinfo=None) < start_datetime:
                continue
        stable_message_id = _stable_message_id_for_envelope(account_name=account_name, folder_name=folder_name, envelope=envelope)
        raw_text = _try_export_message(
            client=client,
            account_name=account_name,
            folder_name=folder_name,
            envelope=envelope,
        )
        if raw_text is None:
            body_export_failures.append(stable_message_id)
            _record_failed_body_ingestion(
                store=store,
                account_name=account_name,
                folder_name=folder_name,
                mailbox_message_id=envelope.message_id,
                stable_message_id=stable_message_id,
                failure_kind='body_export',
                error='message export returned no body',
            )
            continue
        unread_restore_error = _restore_unread_after_export(
            client=client,
            account_name=account_name,
            folder_name=folder_name,
            envelope=envelope,
        )
        if unread_restore_error:
            body_persist_failures.append(
                {
                    'stable_message_id': stable_message_id,
                    'error': f'failed to restore unread state after body export: {unread_restore_error}',
                }
            )
        row, parsed, duplicate_row = _resolve_message_row_for_raw_message(
            store=store,
            account_name=account_name,
            folder_name=folder_name,
            envelope=envelope,
            raw_text=raw_text,
        )
        if not row:
            missing_messages.append(stable_message_id)
            _record_failed_body_ingestion(
                store=store,
                account_name=account_name,
                folder_name=folder_name,
                mailbox_message_id=envelope.message_id,
                stable_message_id=stable_message_id,
                failure_kind='missing_message_stub',
                error='no local message row matched the exported body',
            )
            continue
        try:
            result = persist_message_body(
                store=store,
                message_pk=int(row[0]),
                stable_message_id=row[1],
                raw_text=raw_text,
                parsed=parsed,
                duplicate_message_pk=int(duplicate_row[0]) if duplicate_row else None,
            )
        except Exception as exc:
            body_persist_failures.append(
                {
                    'stable_message_id': str(row[1]),
                    'error': str(exc),
                }
            )
            _record_failed_body_ingestion(
                store=store,
                account_name=account_name,
                folder_name=folder_name,
                mailbox_message_id=envelope.message_id,
                stable_message_id=str(row[1]),
                failure_kind='body_persist',
                error=str(exc),
            )
            continue
        _resolve_failed_body_ingestion(
            store=store,
            account_name=account_name,
            folder_name=folder_name,
            mailbox_message_id=envelope.message_id,
        )
        bodies_persisted += 1
        calendar_events_saved += result['calendar_events_saved']

    return {
        'bodies_persisted': bodies_persisted,
        'calendar_events_saved': calendar_events_saved,
        'missing_messages': missing_messages,
        'body_export_failures': body_export_failures,
        'body_persist_failures': body_persist_failures,
    }


def _body_sync_status(*, envelopes: list[Any], page_size: int, body_result: dict[str, Any]) -> str:
    had_failures = bool(body_result['missing_messages'] or body_result['body_export_failures'] or body_result['body_persist_failures'])
    if had_failures:
        return 'partial'
    if len(envelopes) < page_size:
        return 'complete'
    return 'in_progress'


def run_initial_ingestion(
    *,
    store: EmailMemoryStore,
    client: HimalayaClient,
    account_name: str,
    email_address: str,
    include_folders: Iterable[str] | None = None,
    exclude_folders: Iterable[str] | None = None,
    provider: str | None = None,
    page_size: int = 100,
    max_pages_per_folder: int = 10,
) -> dict[str, Any]:
    available_folders = client.list_folders(account_name)
    resolved_exclude_folders = _resolve_excluded_folders(store, exclude_folders)
    folders = _select_folders(
        available_folders=available_folders,
        include_folders=include_folders,
        exclude_folders=resolved_exclude_folders,
    )
    start_datetime = store.get_start_datetime()
    summary: dict[str, Any] = {
        'mode': 'initial_ingest',
        'folders_processed': folders,
        'pages_processed': 0,
        'messages_seen': 0,
        'messages_skipped_before_start_date': 0,
        'messages_added': 0,
        'messages_updated': 0,
        'bodies_persisted': 0,
        'calendar_events_saved': 0,
        'missing_body_messages': [],
        'body_export_failures': [],
        'body_persist_failures': [],
    }
    for folder in folders:
        state = store.get_ingest_sync_state(account_name=account_name, folder_name=folder, sync_kind='initial_envelopes') or {}
        next_page = int(state.get('next_page', 1))
        for offset in range(max_pages_per_folder):
            current_page = next_page + offset
            try:
                envelopes = client.list_envelopes(account=account_name, folder=folder, page=current_page, page_size=page_size)
            except subprocess.CalledProcessError as exc:
                if current_page > 1:
                    store.upsert_ingest_sync_state(
                        account_name=account_name,
                        folder_name=folder,
                        sync_kind='initial_envelopes',
                        next_page=current_page,
                        last_completed_page=current_page - 1,
                        status='complete',
                    )
                    break
                raise exc
            if not envelopes:
                store.upsert_ingest_sync_state(
                    account_name=account_name,
                    folder_name=folder,
                    sync_kind='initial_envelopes',
                    next_page=current_page,
                    last_completed_page=current_page - 1 if current_page > 1 else None,
                    status='complete',
                )
                break
            result = ingest_envelopes(
                store=store,
                client=client,
                account_name=account_name,
                email_address=email_address,
                folder_name=folder,
                folder_type=_guess_folder_type(folder),
                provider=provider,
                page=current_page,
                page_size=page_size,
                envelopes=envelopes,
            )
            summary['pages_processed'] += 1
            summary['messages_seen'] += result['messages_seen']
            summary['messages_skipped_before_start_date'] += result['messages_skipped_before_start_date']
            summary['messages_added'] += result['messages_added']
            summary['messages_updated'] += result['messages_updated']
            body_result = _persist_page_bodies(
                store=store,
                client=client,
                account_name=account_name,
                folder_name=folder,
                envelopes=envelopes,
                start_datetime=start_datetime,
            )
            summary['bodies_persisted'] += body_result['bodies_persisted']
            summary['calendar_events_saved'] += body_result['calendar_events_saved']
            summary['missing_body_messages'].extend(body_result['missing_messages'])
            summary['body_export_failures'].extend(body_result['body_export_failures'])
            summary['body_persist_failures'].extend(body_result['body_persist_failures'])
            status = 'complete' if len(envelopes) < page_size else 'in_progress'
            body_status = _body_sync_status(envelopes=envelopes, page_size=page_size, body_result=body_result)
            next_saved_page = current_page if status == 'complete' else current_page + 1
            body_next_page = current_page if body_status == 'complete' else current_page + 1
            store.upsert_ingest_sync_state(
                account_name=account_name,
                folder_name=folder,
                sync_kind='initial_envelopes',
                next_page=next_saved_page,
                last_completed_page=current_page,
                status=status,
            )
            store.upsert_ingest_sync_state(
                account_name=account_name,
                folder_name=folder,
                sync_kind='initial_bodies',
                next_page=body_next_page,
                last_completed_page=current_page,
                status=body_status,
            )
            if status == 'complete':
                break
    return summary


def run_nightly_update(
    *,
    store: EmailMemoryStore,
    client: HimalayaClient,
    account_name: str,
    email_address: str,
    include_folders: Iterable[str] | None = None,
    exclude_folders: Iterable[str] | None = None,
    provider: str | None = None,
    page_size: int = 100,
    pages_per_folder: int = 2,
) -> dict[str, Any]:
    available_folders = client.list_folders(account_name)
    resolved_exclude_folders = _resolve_excluded_folders(store, exclude_folders)
    folders = _select_folders(
        available_folders=available_folders,
        include_folders=include_folders,
        exclude_folders=resolved_exclude_folders,
    )
    start_datetime = store.get_start_datetime()
    summary: dict[str, Any] = {
        'mode': 'nightly_update',
        'folders_processed': folders,
        'pages_processed': 0,
        'messages_seen': 0,
        'messages_skipped_before_start_date': 0,
        'messages_added': 0,
        'messages_updated': 0,
        'bodies_persisted': 0,
        'calendar_events_saved': 0,
        'missing_body_messages': [],
        'body_export_failures': [],
        'body_persist_failures': [],
        'folder_fetch_failures': [],
    }
    for folder in folders:
        last_completed_page = None
        folder_status = 'complete'
        for current_page in range(1, pages_per_folder + 1):
            try:
                envelopes = client.list_envelopes(account=account_name, folder=folder, page=current_page, page_size=page_size)
            except subprocess.CalledProcessError as exc:
                if is_page_out_of_bounds(exc):
                    # A folder whose size is an exact multiple of page_size
                    # returns a full final page, so the only way to learn it is
                    # finished is to ask for one more page. Benign.
                    break
                # Anything else is a real fetch failure, and the mail provider
                # throttles in bursts. Losing one folder must not cost the rest
                # of the mailbox, so record it and carry on.
                folder_status = 'partial'
                summary['folder_fetch_failures'].append(
                    {
                        'folder_name': folder,
                        'page': current_page,
                        'error': himalaya_stderr(exc) or str(exc),
                    }
                )
                break
            if not envelopes:
                break
            result = ingest_envelopes(
                store=store,
                client=client,
                account_name=account_name,
                email_address=email_address,
                folder_name=folder,
                folder_type=_guess_folder_type(folder),
                provider=provider,
                page=current_page,
                page_size=page_size,
                envelopes=envelopes,
            )
            summary['pages_processed'] += 1
            summary['messages_seen'] += result['messages_seen']
            summary['messages_skipped_before_start_date'] += result['messages_skipped_before_start_date']
            summary['messages_added'] += result['messages_added']
            summary['messages_updated'] += result['messages_updated']
            body_result = _persist_page_bodies(
                store=store,
                client=client,
                account_name=account_name,
                folder_name=folder,
                envelopes=envelopes,
                start_datetime=start_datetime,
            )
            summary['bodies_persisted'] += body_result['bodies_persisted']
            summary['calendar_events_saved'] += body_result['calendar_events_saved']
            summary['missing_body_messages'].extend(body_result['missing_messages'])
            summary['body_export_failures'].extend(body_result['body_export_failures'])
            summary['body_persist_failures'].extend(body_result['body_persist_failures'])
            body_status = _body_sync_status(envelopes=envelopes, page_size=page_size, body_result=body_result)
            last_completed_page = current_page
            store.upsert_ingest_sync_state(
                account_name=account_name,
                folder_name=folder,
                sync_kind='nightly_bodies',
                next_page=1,
                last_completed_page=current_page,
                status=body_status if len(envelopes) >= page_size else ('complete' if body_status != 'partial' else 'partial'),
            )
            if len(envelopes) < page_size:
                break
        store.upsert_ingest_sync_state(
            account_name=account_name,
            folder_name=folder,
            sync_kind='nightly_envelopes',
            next_page=1,
            last_completed_page=last_completed_page,
            status=folder_status,
        )
    return summary


def _process_rfc_metadata_backfill_page(
    *,
    store: EmailMemoryStore,
    client: HimalayaClient,
    account_name: str,
    folder_name: str,
    envelopes: list[Any],
) -> dict[str, Any]:
    messages_seen = 0
    messages_reprocessed = 0
    messages_already_complete = 0
    messages_missing_local_stub = 0
    messages_without_existing_body = 0

    for envelope in envelopes:
        messages_seen += 1
        stable_message_id = build_stable_message_id(account_name=account_name, folder_name=folder_name, envelope=envelope)
        row = store.conn.execute(
            "SELECT message_pk, cleaned_text, rfc_references_json FROM messages WHERE stable_message_id = ?",
            [stable_message_id],
        ).fetchone()
        if not row:
            messages_missing_local_stub += 1
            continue
        message_pk = int(row[0])
        cleaned_text = row[1]
        rfc_references_json = row[2]
        if not cleaned_text:
            messages_without_existing_body += 1
            continue
        if rfc_references_json is not None:
            messages_already_complete += 1
            continue
        raw_text = _try_export_message(
            client=client,
            account_name=account_name,
            folder_name=folder_name,
            envelope=envelope,
        )
        if raw_text is None:
            continue
        persist_message_body(
            store=store,
            message_pk=message_pk,
            stable_message_id=stable_message_id,
            raw_text=raw_text,
        )
        messages_reprocessed += 1

    return {
        'messages_seen': messages_seen,
        'messages_reprocessed': messages_reprocessed,
        'messages_already_complete': messages_already_complete,
        'messages_missing_local_stub': messages_missing_local_stub,
        'messages_without_existing_body': messages_without_existing_body,
    }


def run_rfc_metadata_backfill(
    *,
    store: EmailMemoryStore,
    client: HimalayaClient,
    account_name: str,
    email_address: str,
    include_folders: Iterable[str] | None = None,
    exclude_folders: Iterable[str] | None = None,
    provider: str | None = None,
    page_size: int = 100,
    max_pages_per_folder: int = 10,
) -> dict[str, Any]:
    del email_address, provider
    available_folders = client.list_folders(account_name)
    resolved_exclude_folders = _resolve_excluded_folders(store, exclude_folders)
    folders = _select_folders(
        available_folders=available_folders,
        include_folders=include_folders,
        exclude_folders=resolved_exclude_folders,
    )
    summary: dict[str, Any] = {
        'mode': 'rfc_metadata_backfill',
        'folders_processed': folders,
        'pages_processed': 0,
        'messages_seen': 0,
        'messages_reprocessed': 0,
        'messages_already_complete': 0,
        'messages_missing_local_stub': 0,
        'messages_without_existing_body': 0,
    }
    for folder in folders:
        state = store.get_ingest_sync_state(account_name=account_name, folder_name=folder, sync_kind='rfc_metadata_backfill') or {}
        next_page = int(state.get('next_page', 1))
        for offset in range(max_pages_per_folder):
            current_page = next_page + offset
            try:
                envelopes = client.list_envelopes(account=account_name, folder=folder, page=current_page, page_size=page_size)
            except subprocess.CalledProcessError as exc:
                if current_page > 1:
                    store.upsert_ingest_sync_state(
                        account_name=account_name,
                        folder_name=folder,
                        sync_kind='rfc_metadata_backfill',
                        next_page=current_page,
                        last_completed_page=current_page - 1,
                        status='complete',
                    )
                    break
                raise exc
            if not envelopes:
                store.upsert_ingest_sync_state(
                    account_name=account_name,
                    folder_name=folder,
                    sync_kind='rfc_metadata_backfill',
                    next_page=current_page,
                    last_completed_page=current_page - 1 if current_page > 1 else None,
                    status='complete',
                )
                break
            result = _process_rfc_metadata_backfill_page(
                store=store,
                client=client,
                account_name=account_name,
                folder_name=folder,
                envelopes=envelopes,
            )
            summary['pages_processed'] += 1
            summary['messages_seen'] += result['messages_seen']
            summary['messages_reprocessed'] += result['messages_reprocessed']
            summary['messages_already_complete'] += result['messages_already_complete']
            summary['messages_missing_local_stub'] += result['messages_missing_local_stub']
            summary['messages_without_existing_body'] += result['messages_without_existing_body']
            status = 'complete' if len(envelopes) < page_size else 'in_progress'
            next_saved_page = current_page if status == 'complete' else current_page + 1
            store.upsert_ingest_sync_state(
                account_name=account_name,
                folder_name=folder,
                sync_kind='rfc_metadata_backfill',
                next_page=next_saved_page,
                last_completed_page=current_page,
                status=status,
            )
            if status == 'complete':
                break
    return summary


def _process_ingestion_state_repair_page(
    *,
    store: EmailMemoryStore,
    client: HimalayaClient,
    account_name: str,
    folder_name: str,
    envelopes: list[Any],
) -> dict[str, Any]:
    messages_seen = 0
    messages_repaired = 0
    messages_already_healthy = 0
    messages_missing_local_stub = 0
    messages_without_existing_body = 0

    for envelope in envelopes:
        messages_seen += 1
        stable_message_id = build_stable_message_id(account_name=account_name, folder_name=folder_name, envelope=envelope)
        row = store.conn.execute(
            "SELECT message_pk, cleaned_text, rfc_references_json, thread_key FROM messages WHERE stable_message_id = ?",
            [stable_message_id],
        ).fetchone()
        if not row:
            messages_missing_local_stub += 1
            continue
        message_pk = int(row[0])
        cleaned_text = row[1]
        rfc_references_json = row[2]
        thread_key = row[3] or ''
        if not cleaned_text:
            messages_without_existing_body += 1
            continue
        needs_repair = rfc_references_json is None or str(thread_key).startswith('thread:')
        if not needs_repair:
            messages_already_healthy += 1
            continue
        raw_text = _try_export_message(
            client=client,
            account_name=account_name,
            folder_name=folder_name,
            envelope=envelope,
        )
        if raw_text is None:
            continue
        persist_message_body(
            store=store,
            message_pk=message_pk,
            stable_message_id=stable_message_id,
            raw_text=raw_text,
        )
        messages_repaired += 1

    return {
        'messages_seen': messages_seen,
        'messages_repaired': messages_repaired,
        'messages_already_healthy': messages_already_healthy,
        'messages_missing_local_stub': messages_missing_local_stub,
        'messages_without_existing_body': messages_without_existing_body,
    }


def run_ingestion_state_repair(
    *,
    store: EmailMemoryStore,
    client: HimalayaClient,
    account_name: str,
    email_address: str,
    include_folders: Iterable[str] | None = None,
    exclude_folders: Iterable[str] | None = None,
    provider: str | None = None,
    page_size: int = 100,
    max_pages_per_folder: int = 10,
) -> dict[str, Any]:
    del email_address, provider
    available_folders = client.list_folders(account_name)
    resolved_exclude_folders = _resolve_excluded_folders(store, exclude_folders)
    folders = _select_folders(
        available_folders=available_folders,
        include_folders=include_folders,
        exclude_folders=resolved_exclude_folders,
    )
    summary: dict[str, Any] = {
        'mode': 'ingestion_state_repair',
        'folders_processed': folders,
        'pages_processed': 0,
        'messages_seen': 0,
        'messages_repaired': 0,
        'messages_already_healthy': 0,
        'messages_missing_local_stub': 0,
        'messages_without_existing_body': 0,
    }
    for folder in folders:
        state = store.get_ingest_sync_state(account_name=account_name, folder_name=folder, sync_kind='repair_bodies') or {}
        next_page = int(state.get('next_page', 1))
        for offset in range(max_pages_per_folder):
            current_page = next_page + offset
            try:
                envelopes = client.list_envelopes(account=account_name, folder=folder, page=current_page, page_size=page_size)
            except subprocess.CalledProcessError as exc:
                if current_page > 1:
                    store.upsert_ingest_sync_state(
                        account_name=account_name,
                        folder_name=folder,
                        sync_kind='repair_bodies',
                        next_page=current_page,
                        last_completed_page=current_page - 1,
                        status='complete',
                    )
                    break
                raise exc
            if not envelopes:
                store.upsert_ingest_sync_state(
                    account_name=account_name,
                    folder_name=folder,
                    sync_kind='repair_bodies',
                    next_page=current_page,
                    last_completed_page=current_page - 1 if current_page > 1 else None,
                    status='complete',
                )
                break
            result = _process_ingestion_state_repair_page(
                store=store,
                client=client,
                account_name=account_name,
                folder_name=folder,
                envelopes=envelopes,
            )
            summary['pages_processed'] += 1
            summary['messages_seen'] += result['messages_seen']
            summary['messages_repaired'] += result['messages_repaired']
            summary['messages_already_healthy'] += result['messages_already_healthy']
            summary['messages_missing_local_stub'] += result['messages_missing_local_stub']
            summary['messages_without_existing_body'] += result['messages_without_existing_body']
            status = 'complete' if len(envelopes) < page_size else 'in_progress'
            next_saved_page = current_page if status == 'complete' else current_page + 1
            store.upsert_ingest_sync_state(
                account_name=account_name,
                folder_name=folder,
                sync_kind='repair_bodies',
                next_page=next_saved_page,
                last_completed_page=current_page,
                status=status,
            )
            if status == 'complete':
                break
    return summary


def run_failed_body_backfill(
    *,
    store: EmailMemoryStore,
    client: HimalayaClient,
    account_name: str,
    include_folders: Iterable[str] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = store.list_failed_message_ingestions(
        account_name=account_name,
        statuses=['pending'],
        folders=list(include_folders) if include_folders is not None else None,
        limit=limit,
    )
    summary: dict[str, Any] = {
        'mode': 'failed_body_backfill',
        'attempted': 0,
        'resolved': 0,
        'still_failing': 0,
        'missing_rows': 0,
        'remaining_open': 0,
    }
    batch_safe_mode_enabled = bool(failures)
    persisted_in_batch = 0
    if batch_safe_mode_enabled:
        store.prepare_for_body_persistence()
    try:
        for failure in failures:
            summary['attempted'] += 1
            raw_text = None
            try:
                raw_text = client.export_message(
                    account=account_name,
                    message_id=failure['mailbox_message_id'],
                    folder=failure['folder_name'],
                    full=True,
                )
            except subprocess.CalledProcessError as exc:
                _record_failed_body_ingestion(
                    store=store,
                    account_name=account_name,
                    folder_name=failure['folder_name'],
                    mailbox_message_id=failure['mailbox_message_id'],
                    stable_message_id=failure.get('stable_message_id'),
                    failure_kind='body_export',
                    error=str(exc),
                )
                summary['still_failing'] += 1
                continue
            if raw_text is None:
                _record_failed_body_ingestion(
                    store=store,
                    account_name=account_name,
                    folder_name=failure['folder_name'],
                    mailbox_message_id=failure['mailbox_message_id'],
                    stable_message_id=failure.get('stable_message_id'),
                    failure_kind='body_export',
                    error='message export returned no body',
                )
                summary['still_failing'] += 1
                continue
            row, parsed, duplicate_row = _resolve_message_row_for_retry(
                store=store,
                account_name=account_name,
                folder_name=failure['folder_name'],
                mailbox_message_id=failure['mailbox_message_id'],
                stable_message_id=failure.get('stable_message_id'),
                raw_text=raw_text,
            )
            if not row:
                _record_failed_body_ingestion(
                    store=store,
                    account_name=account_name,
                    folder_name=failure['folder_name'],
                    mailbox_message_id=failure['mailbox_message_id'],
                    stable_message_id=failure.get('stable_message_id'),
                    failure_kind='missing_message_stub',
                    error='no local message row matched the exported body during retry',
                )
                summary['missing_rows'] += 1
                continue
            try:
                persist_message_body(
                    store=store,
                    message_pk=int(row[0]),
                    stable_message_id=row[1],
                    raw_text=raw_text,
                    parsed=parsed,
                    duplicate_message_pk=int(duplicate_row[0]) if duplicate_row else None,
                    manage_duckdb_write_settings=False,
                    checkpoint_writes=False,
                )
            except Exception as exc:
                _record_failed_body_ingestion(
                    store=store,
                    account_name=account_name,
                    folder_name=failure['folder_name'],
                    mailbox_message_id=failure['mailbox_message_id'],
                    stable_message_id=str(row[1]),
                    failure_kind='body_persist',
                    error=str(exc),
                )
                summary['still_failing'] += 1
                continue
            persisted_in_batch += 1
            _resolve_failed_body_ingestion(
                store=store,
                account_name=account_name,
                folder_name=failure['folder_name'],
                mailbox_message_id=failure['mailbox_message_id'],
            )
            summary['resolved'] += 1
        if batch_safe_mode_enabled and persisted_in_batch > 0:
            store.flush_body_persistence_writes()
    finally:
        if batch_safe_mode_enabled:
            store.restore_default_write_settings()
    summary['remaining_open'] = len(
        store.list_failed_message_ingestions(account_name=account_name, statuses=['pending'])
    )
    return summary


def ingest_message_bodies(
    *,
    store: EmailMemoryStore,
    client: HimalayaClient,
    account_name: str,
    folder_name: str,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    envelopes = client.list_envelopes(account=account_name, folder=folder_name, page=page, page_size=page_size)
    bodies_persisted = 0
    calendar_events_saved = 0
    messages_skipped_before_start_date = 0
    missing_messages: list[str] = []
    body_export_failures: list[str] = []
    unread_restore_failures: list[str] = []
    start_datetime = store.get_start_datetime()
    if start_datetime is None:
        raise ValueError('start_date is not configured; initialize the database first')

    for envelope in envelopes:
        envelope_ts = normalize_timestamp(envelope.date)
        if start_datetime is not None and envelope_ts is not None and envelope_ts.replace(tzinfo=None) < start_datetime:
            messages_skipped_before_start_date += 1
            continue
        stable_message_id = _stable_message_id_for_envelope(account_name=account_name, folder_name=folder_name, envelope=envelope)
        raw_text = _try_export_message(
            client=client,
            account_name=account_name,
            folder_name=folder_name,
            envelope=envelope,
        )
        if raw_text is None:
            body_export_failures.append(stable_message_id)
            _record_failed_body_ingestion(
                store=store,
                account_name=account_name,
                folder_name=folder_name,
                mailbox_message_id=envelope.message_id,
                stable_message_id=stable_message_id,
                failure_kind='body_export',
                error='message export returned no body',
            )
            continue
        unread_restore_error = _restore_unread_after_export(
            client=client,
            account_name=account_name,
            folder_name=folder_name,
            envelope=envelope,
        )
        if unread_restore_error:
            unread_restore_failures.append(stable_message_id)
        row, parsed, duplicate_row = _resolve_message_row_for_raw_message(
            store=store,
            account_name=account_name,
            folder_name=folder_name,
            envelope=envelope,
            raw_text=raw_text,
        )
        if not row:
            missing_messages.append(stable_message_id)
            _record_failed_body_ingestion(
                store=store,
                account_name=account_name,
                folder_name=folder_name,
                mailbox_message_id=envelope.message_id,
                stable_message_id=stable_message_id,
                failure_kind='missing_message_stub',
                error='no local message row matched the exported body',
            )
            continue
        result = persist_message_body(
            store=store,
            message_pk=int(row[0]),
            stable_message_id=row[1],
            raw_text=raw_text,
            parsed=parsed,
            duplicate_message_pk=int(duplicate_row[0]) if duplicate_row else None,
        )
        _resolve_failed_body_ingestion(
            store=store,
            account_name=account_name,
            folder_name=folder_name,
            mailbox_message_id=envelope.message_id,
        )
        if unread_restore_error:
            _record_failed_body_ingestion(
                store=store,
                account_name=account_name,
                folder_name=folder_name,
                mailbox_message_id=envelope.message_id,
                stable_message_id=stable_message_id,
                failure_kind='body_persist',
                error=f'failed to restore unread state after body export: {unread_restore_error}',
            )
        bodies_persisted += 1
        calendar_events_saved += result['calendar_events_saved']

    return {
        'folder_name': folder_name,
        'messages_seen': len(envelopes),
        'messages_skipped_before_start_date': messages_skipped_before_start_date,
        'bodies_persisted': bodies_persisted,
        'calendar_events_saved': calendar_events_saved,
        'missing_messages': missing_messages,
        'body_export_failures': body_export_failures,
        'unread_restore_failures': unread_restore_failures,
    }

def persist_message_body(
    *,
    store: EmailMemoryStore,
    message_pk: int,
    stable_message_id: str,
    raw_text: str,
    parsed: 'ParsedMessageContent | None' = None,
    duplicate_message_pk: int | None = None,
    manage_duckdb_write_settings: bool = True,
    checkpoint_writes: bool = True,
) -> dict[str, Any]:
    if manage_duckdb_write_settings:
        store.prepare_for_body_persistence()
    try:
        parsed = parsed or _parse_message_content(raw_text)
        body_fingerprint = build_body_fingerprint(parsed.cleaned_text)
        message_row = store.conn.execute(
            "SELECT account_id, normalized_subject, COALESCE(sent_at, received_at) FROM messages WHERE message_pk = ?",
            [message_pk],
        ).fetchone()
        canonical_subject = parsed.normalized_subject or (message_row[1] if message_row else None)
        if parsed.internet_message_id:
            canonical_stable_message_id = f'rfc822:{parsed.internet_message_id}'
            identity_source = 'rfc822'
        elif stable_message_id.startswith('rfc822:'):
            canonical_stable_message_id = stable_message_id
            identity_source = 'rfc822'
        else:
            canonical_stable_message_id = build_content_stable_message_id(
                normalized_subject=canonical_subject or '',
                from_addr=parsed.from_addr,
                to_addrs=parsed.to_addrs,
                body_fingerprint=body_fingerprint,
            )
            identity_source = 'content'

        canonical_row = store.get_message_row_by_stable_message_id(stable_message_id=canonical_stable_message_id)
        if canonical_row and int(canonical_row[0]) != message_pk:
            message_pk = store.collapse_duplicate_message(
                canonical_message_pk=int(canonical_row[0]),
                duplicate_message_pk=duplicate_message_pk or message_pk,
            )
            stable_message_id = canonical_stable_message_id
        elif duplicate_message_pk is not None and duplicate_message_pk != message_pk:
            store.collapse_duplicate_message(
                canonical_message_pk=message_pk,
                duplicate_message_pk=duplicate_message_pk,
            )

        thread_key = _reconstruct_message_thread(
            store=store,
            message_pk=message_pk,
            internet_message_id=parsed.internet_message_id,
            rfc_in_reply_to=parsed.rfc_in_reply_to,
            references=parsed.references,
        )
        message_row = store.conn.execute(
            "SELECT account_id, normalized_subject, COALESCE(sent_at, received_at) FROM messages WHERE message_pk = ?",
            [message_pk],
        ).fetchone()
        canonical_subject = parsed.normalized_subject or (message_row[1] if message_row else None)
        if message_row:
            store.ensure_thread(
                account_id=int(message_row[0]),
                thread_key=thread_key,
                canonical_subject=canonical_subject,
            )
        anchor_timestamp = message_row[2] if message_row else None
        store.update_message_content(
            message_pk=message_pk,
            cleaned_text=parsed.cleaned_text,
            raw_path=None,
            text_hash=body_fingerprint,
        )
        if canonical_stable_message_id != stable_message_id:
            message_pk = store.promote_message_identity(
                message_pk=message_pk,
                stable_message_id=canonical_stable_message_id,
                identity_source=identity_source,
                internet_message_id=f'<{parsed.internet_message_id}>' if parsed.internet_message_id else None,
            )
            stable_message_id = canonical_stable_message_id
        store.update_message_rfc_threading(
            message_pk=message_pk,
            internet_message_id=f'<{parsed.internet_message_id}>' if parsed.internet_message_id else None,
            rfc_in_reply_to=parsed.rfc_in_reply_to,
            rfc_references_json=json.dumps(parsed.references),
            thread_key=thread_key,
        )
        store.replace_message_action_items(message_pk=message_pk, action_items=parsed.action_items)
        store.replace_message_deadlines(
            message_pk=message_pk,
            deadlines=_extract_deadlines(parsed.cleaned_text, anchor_timestamp=anchor_timestamp),
        )
        store.replace_calendar_events(message_pk=message_pk, events=parsed.calendar_events)
        if checkpoint_writes:
            store.flush_body_persistence_writes()
        return {
            'message_pk': message_pk,
            'stable_message_id': stable_message_id,
            'identity_source': identity_source,
            'text_hash': body_fingerprint,
            'raw_path': None,
            'cleaned_text_length': len(parsed.cleaned_text),
            'calendar_events_saved': len(parsed.calendar_events),
        }
    finally:
        if manage_duckdb_write_settings:
            store.restore_default_write_settings()


@dataclass(slots=True)
class ParsedMessageContent:
    cleaned_text: str
    action_items: list[dict[str, Any]]
    deadlines: list[dict[str, Any]]
    calendar_events: list[dict[str, Any]]
    internet_message_id: str | None
    rfc_in_reply_to: str | None
    references: list[str]
    normalized_subject: str
    from_addr: str | None
    to_addrs: list[str]


class _HTMLToTextParser(HTMLParser):
    BLOCK_TAGS = {'p', 'div', 'br', 'li', 'ul', 'ol', 'section', 'article', 'tr', 'table', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {'script', 'style'}:
            self._ignored_depth += 1
            return
        if self._ignored_depth == 0 and tag in self.BLOCK_TAGS:
            self.parts.append('\n')

    def handle_endtag(self, tag: str) -> None:
        if tag in {'script', 'style'} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth == 0 and tag in self.BLOCK_TAGS:
            self.parts.append('\n')

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self.parts.append(data)

    def get_text(self) -> str:
        return ''.join(self.parts)


def _parse_message_content(raw_text: str) -> ParsedMessageContent:
    message = Parser(policy=policy.default).parsestr(raw_text)
    text_candidates: list[tuple[int, str]] = []
    calendar_events: list[dict[str, Any]] = []

    for part in _walk_message_parts(message):
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = (part.get_content_disposition() or '').lower()
        payload = _decode_part_payload(part)
        if not payload.strip():
            continue
        if content_type == 'text/calendar':
            calendar_events.append(_extract_calendar_event(part=part, payload=payload))
            continue
        if disposition == 'attachment':
            continue
        if content_type == 'text/plain':
            text_candidates.append((2, _normalize_plain_text(payload)))
        elif content_type == 'text/html':
            text_candidates.append((1, _html_to_text(payload)))

    if not text_candidates:
        text_candidates.append((0, _extract_fallback_body(raw_text)))

    best_text = max(text_candidates, key=lambda item: (item[0], len(item[1])))[1]
    from_addrs = [addr.lower() for _, addr in getaddresses(message.get_all('from', [])) if addr]
    recipient_addrs = [
        addr.lower()
        for _, addr in getaddresses(message.get_all('to', []) + message.get_all('cc', []))
        if addr
    ]
    return ParsedMessageContent(
        cleaned_text=best_text,
        action_items=_extract_action_items(best_text),
        deadlines=[],
        calendar_events=calendar_events,
        internet_message_id=_extract_single_message_id(message, 'message-id'),
        rfc_in_reply_to=_extract_single_message_id(message, 'in-reply-to'),
        references=_extract_references(message),
        normalized_subject=normalize_subject(message.get('subject', '')),
        from_addr=from_addrs[0] if from_addrs else None,
        to_addrs=sorted(dict.fromkeys(recipient_addrs)),
    )


def _extract_action_items(cleaned_text: str) -> list[dict[str, Any]]:
    action_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_line in cleaned_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(r'^(?:action|todo)\s*:\s*(.+)$', line, flags=re.IGNORECASE)
        if not match:
            continue
        action_text = match.group(1).strip()
        if not action_text:
            continue
        if action_text not in seen:
            seen.add(action_text)
            action_items.append(
                {
                    'owner': None,
                    'action_text': action_text,
                    'due_date': None,
                    'status': 'open',
                    'confidence': 1.0,
                }
            )
    return action_items


_MONTH_NAME_PATTERN = (
    r'(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
)


_MONTH_LOOKUP = {
    'jan': 1,
    'january': 1,
    'feb': 2,
    'february': 2,
    'mar': 3,
    'march': 3,
    'apr': 4,
    'april': 4,
    'may': 5,
    'jun': 6,
    'june': 6,
    'jul': 7,
    'july': 7,
    'aug': 8,
    'august': 8,
    'sep': 9,
    'sept': 9,
    'september': 9,
    'oct': 10,
    'october': 10,
    'nov': 11,
    'november': 11,
    'dec': 12,
    'december': 12,
}


def _extract_deadlines(cleaned_text: str, *, anchor_timestamp: datetime | None) -> list[dict[str, Any]]:
    deadlines: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    patterns = [
        re.compile(
            rf'\bdeadline\s+is\s+(?P<month>{_MONTH_NAME_PATTERN})\s+(?P<day>\d{{1,2}})(?:,\s*(?P<year>\d{{4}}))?\s+for\s+(?:the\s+)?(?P<label>[^.?!\n]+)',
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf'\b(?P<label>[^.?!\n]+?)\s+is\s+due(?:\s+by)?\s+(?P<month>{_MONTH_NAME_PATTERN})\s+(?P<day>\d{{1,2}})(?:,\s*(?P<year>\d{{4}}))?(?=$|[.?!\n])',
            flags=re.IGNORECASE,
        ),
    ]
    for raw_line in cleaned_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for pattern in patterns:
            match = pattern.search(line)
            if not match:
                continue
            due_date = _build_deadline_due_date(
                month_text=match.group('month'),
                day_text=match.group('day'),
                year_text=match.groupdict().get('year'),
                anchor_timestamp=anchor_timestamp,
            )
            if due_date is None:
                continue
            label = _normalize_deadline_label(match.group('label'))
            if not label:
                continue
            dedup_key = (label.lower(), due_date.isoformat(sep=' '))
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            deadlines.append(
                {
                    'label': label,
                    'due_date': due_date,
                    'related_project': None,
                    'confidence': 1.0,
                    'status': 'open',
                }
            )
            break
    return deadlines


def _build_deadline_due_date(
    *,
    month_text: str,
    day_text: str,
    year_text: str | None,
    anchor_timestamp: datetime | None,
) -> datetime | None:
    month = _MONTH_LOOKUP.get(month_text.lower().rstrip('.'))
    if month is None:
        return None
    year = int(year_text) if year_text else (anchor_timestamp.year if anchor_timestamp else None)
    if year is None:
        return None
    try:
        return datetime.combine(datetime(year, month, int(day_text)).date(), time(hour=12))
    except ValueError:
        return None


def _normalize_deadline_label(value: str) -> str:
    label = re.sub(r'\s+', ' ', value).strip().rstrip('.,;:')
    label = re.sub(r'^(?:the|a|an)\s+', '', label, flags=re.IGNORECASE)
    return label.strip()


def _extract_single_message_id(message: Message, header_name: str) -> str | None:
    for raw_value in _raw_header_values(message, header_name):
        match = re.search(r'<([^>]+)>', raw_value)
        if match:
            return _normalize_message_id(match.group(1))
        cleaned = _normalize_message_id(raw_value)
        if cleaned:
            return cleaned
    return None


def _extract_references(message: Message) -> list[str]:
    raw_values = _raw_header_values(message, 'references')
    if not raw_values:
        return []
    seen: set[str] = set()
    references: list[str] = []
    for raw_value in raw_values:
        for match in re.findall(r'<([^>]+)>', raw_value):
            normalized = _normalize_message_id(match)
            if normalized and normalized not in seen:
                seen.add(normalized)
                references.append(normalized)
    if references:
        return references
    normalized = _normalize_message_id(' '.join(raw_values))
    return [normalized] if normalized else []


def _raw_header_values(message: Message, header_name: str) -> list[str]:
    target = header_name.lower()
    values: list[str] = []
    for name, value in message.raw_items():
        if name.lower() == target and value:
            values.append(str(value))
    return values


def _normalize_message_id(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().strip('<>').strip()
    return normalized or None


def _reconstruct_message_thread(
    *,
    store: EmailMemoryStore,
    message_pk: int,
    internet_message_id: str | None,
    rfc_in_reply_to: str | None,
    references: list[str],
) -> str:
    if references:
        return f'rfc822-thread:{references[0]}'
    if rfc_in_reply_to:
        parent_row = store.conn.execute(
            "SELECT thread_key FROM messages WHERE internet_message_id = ?",
            [f'<{rfc_in_reply_to}>'],
        ).fetchone()
        if parent_row and parent_row[0]:
            return str(parent_row[0])
        return f'rfc822-thread:{rfc_in_reply_to}'
    if internet_message_id:
        return f'rfc822-thread:{internet_message_id}'
    return _build_fallback_thread_key(store=store, message_pk=message_pk)


def _build_fallback_thread_key(*, store: EmailMemoryStore, message_pk: int) -> str:
    row = store.conn.execute(
        """
        SELECT account_id, normalized_subject, COALESCE(sent_at, received_at)
        FROM messages
        WHERE message_pk = ?
        """,
        [message_pk],
    ).fetchone()
    if not row:
        return 'fallback-thread:unknown'
    account_id = int(row[0])
    normalized_subject = row[1] or ''
    anchor_ts = row[2]
    if normalized_subject and anchor_ts is not None:
        sibling = store.conn.execute(
            """
            SELECT thread_key
            FROM messages
            WHERE account_id = ?
              AND normalized_subject = ?
              AND message_pk <> ?
              AND thread_key LIKE 'fallback-thread:%'
              AND ABS(DATEDIFF('day', COALESCE(sent_at, received_at), ?)) <= 14
            ORDER BY ABS(DATEDIFF('second', COALESCE(sent_at, received_at), ?)), message_pk
            LIMIT 1
            """,
            [account_id, normalized_subject, message_pk, anchor_ts, anchor_ts],
        ).fetchone()
        if sibling and sibling[0]:
            return str(sibling[0])
    digest_material = f'{account_id}|{normalized_subject}|{anchor_ts or message_pk}'
    digest = hashlib.sha256(digest_material.encode('utf-8')).hexdigest()[:24]
    return f'fallback-thread:{digest}'


def _walk_message_parts(message: Message) -> Iterable[Message]:
    if message.is_multipart():
        for part in message.walk():
            yield part
    else:
        yield message


def _decode_part_payload(part: Any) -> str:
    try:
        content = part.get_content()
        return content if isinstance(content, str) else ''
    except Exception:
        payload = part.get_payload(decode=True)
        if payload is None:
            fallback = part.get_payload()
            return fallback if isinstance(fallback, str) else ''
        charset = part.get_content_charset() or 'utf-8'
        return bytes(payload).decode(charset, errors='replace')


def _normalize_plain_text(text: str) -> str:
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'\n{3,}', '\n\n', text)
    return '\n'.join(line.rstrip() for line in text.split('\n')).strip()


def _html_to_text(html: str) -> str:
    parser = _HTMLToTextParser()
    parser.feed(html)
    text = unescape(parser.get_text())
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _extract_calendar_event(*, part: Message, payload: str) -> dict[str, Any]:
    filename = part.get_filename() or part.get_param('name')
    method = part.get_param('method')
    lines = _unfold_ical_lines(payload)
    fields: dict[str, str] = {}
    field_params: dict[str, dict[str, str]] = {}
    attendees: list[dict[str, Any]] = []

    for line in lines:
        if ':' not in line:
            continue
        key_part, value = line.split(':', 1)
        base_key, params = _parse_ical_key_params(key_part)
        normalized_value = value.strip()
        if base_key == 'ATTENDEE':
            attendees.append(_parse_attendee(params, normalized_value))
            continue
        fields.setdefault(base_key, normalized_value)
        field_params.setdefault(base_key, params)

    organizer_name, organizer_email = _parse_organizer(field_params.get('ORGANIZER', {}), fields.get('ORGANIZER'))
    start_value = _parse_ics_timestamp(fields.get('DTSTART'), field_params.get('DTSTART', {}))
    end_value = _parse_ics_timestamp(fields.get('DTEND'), field_params.get('DTEND', {}))
    recurrence_value = _parse_ics_timestamp(fields.get('RECURRENCE-ID'), field_params.get('RECURRENCE-ID', {}), keep_text=True)
    return {
        'filename': filename,
        'mime_type': part.get_content_type(),
        'method': (method or fields.get('METHOD')),
        'uid': fields.get('UID'),
        'summary': fields.get('SUMMARY'),
        'description': fields.get('DESCRIPTION'),
        'status': fields.get('STATUS'),
        'sequence': _coerce_int(fields.get('SEQUENCE')),
        'recurrence_id': recurrence_value['raw'] if recurrence_value else None,
        'recurrence_rule': fields.get('RRULE'),
        'starts_at': start_value['utc'] if start_value else None,
        'ends_at': end_value['utc'] if end_value else None,
        'starts_at_tzid': start_value['tzid'] if start_value else None,
        'ends_at_tzid': end_value['tzid'] if end_value else None,
        'recurrence_id_tzid': recurrence_value['tzid'] if recurrence_value else None,
        'organizer': organizer_name,
        'organizer_email': organizer_email,
        'location': fields.get('LOCATION'),
        'attendees_json': json.dumps(attendees) if attendees else None,
        'raw_ics': payload.strip(),
    }


def _unfold_ical_lines(payload: str) -> list[str]:
    unfolded: list[str] = []
    for raw_line in payload.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if not raw_line:
            continue
        if raw_line.startswith((' ', '\t')) and unfolded:
            unfolded[-1] += raw_line[1:]
        else:
            unfolded.append(raw_line)
    return unfolded


def _parse_ical_key_params(key_part: str) -> tuple[str, dict[str, str]]:
    parts = key_part.split(';')
    base_key = parts[0].upper()
    params: dict[str, str] = {}
    for chunk in parts[1:]:
        if '=' not in chunk:
            continue
        name, value = chunk.split('=', 1)
        params[name.upper()] = value.strip('"')
    return base_key, params


def _parse_attendee(params: dict[str, str], value: str) -> dict[str, Any]:
    return {
        'email': _clean_mailto(value),
        'name': params.get('CN'),
        'partstat': params.get('PARTSTAT'),
        'role': params.get('ROLE'),
        'rsvp': params.get('RSVP'),
    }


def _parse_organizer(params: dict[str, str], value: str | None) -> tuple[str | None, str | None]:
    return params.get('CN'), _clean_mailto(value)


def _clean_mailto(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    if cleaned.lower().startswith('mailto:'):
        cleaned = cleaned[7:]
    return cleaned or None


def _coerce_int(value: str | None) -> int | None:
    if value is None or value == '':
        return None
    return int(value)


def _parse_ics_timestamp(value: str | None, params: dict[str, str] | None = None, keep_text: bool = False) -> dict[str, Any] | None:
    if not value:
        return None
    params = params or {}
    raw = value.strip()
    tzid = params.get('TZID')
    value_type = params.get('VALUE', '').upper()
    if value_type == 'DATE' or (len(raw) == 8 and raw.isdigit()):
        dt = datetime.strptime(raw, '%Y%m%d')
        return {'utc': dt, 'tzid': tzid, 'raw': raw}
    if raw.endswith('Z'):
        dt = datetime.strptime(raw, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        return {'utc': dt.astimezone(timezone.utc).replace(tzinfo=None), 'tzid': 'UTC', 'raw': raw if keep_text else raw}
    if tzid:
        dt = datetime.strptime(raw, '%Y%m%dT%H%M%S' if len(raw) == 15 else '%Y%m%dT%H%M')
        resolved_tz = _resolve_ics_tzid(tzid)
        if resolved_tz is not None:
            localized = dt.replace(tzinfo=resolved_tz)
            return {'utc': localized.astimezone(timezone.utc).replace(tzinfo=None), 'tzid': tzid, 'raw': raw}
        return {'utc': dt, 'tzid': tzid, 'raw': raw}
    for fmt in ('%Y%m%dT%H%M%S', '%Y%m%dT%H%M'):
        try:
            dt = datetime.strptime(raw, fmt)
            return {'utc': dt, 'tzid': tzid, 'raw': raw}
        except ValueError:
            continue
    return {'utc': None, 'tzid': tzid, 'raw': raw}


def _resolve_ics_tzid(tzid: str | None) -> ZoneInfo | None:
    if not tzid:
        return None
    candidates = [tzid.strip()]
    mapped = WINDOWS_TO_IANA_TZIDS.get(candidates[0])
    if mapped and mapped not in candidates:
        candidates.append(mapped)
    for candidate in candidates:
        try:
            return ZoneInfo(candidate)
        except Exception:
            continue
    return None


def _extract_fallback_body(raw_text: str) -> str:
    _head, sep, body = raw_text.partition("\n\n")
    candidate = body if sep else raw_text
    return _normalize_plain_text(candidate)


def _build_message_people(*, store: EmailMemoryStore, envelope, account_email: str) -> list[dict[str, Any]]:
    people: list[dict[str, Any]] = []
    if envelope.from_addr:
        people.append(
            _ensure_person_reference(
                store=store,
                display_name=envelope.from_name,
                email_address=envelope.from_addr,
                role='from',
            )
        )
    for address in envelope.to_addrs:
        if not address or address.lower() == account_email.lower():
            continue
        people.append(
            _ensure_person_reference(
                store=store,
                display_name=None,
                email_address=address,
                role='to',
            )
        )
    deduped: dict[tuple[int, str, str | None], dict[str, Any]] = {}
    for person in people:
        key = (person['person_id'], person['role'], person.get('email_address'))
        deduped[key] = person
    return list(deduped.values())


def _ensure_person_reference(*, store: EmailMemoryStore, display_name: str | None, email_address: str, role: str) -> dict[str, Any]:
    canonical_name = _choose_canonical_person_name(display_name=display_name, email_address=email_address)
    organization_hint = _organization_hint_from_email(email_address)
    person_id, stored_name = store.entity_store.ensure_person(canonical_name, organization_hint=organization_hint)
    store.entity_store.ensure_person_alias(person_id, canonical_name)
    if display_name and display_name != stored_name:
        store.entity_store.ensure_person_alias(person_id, display_name)
    store.entity_store.ensure_person_email(person_id, email_address)
    return {
        'person_id': person_id,
        'canonical_name': stored_name,
        'normalized_name': normalize_person_name(stored_name),
        'role': role,
        'email_address': email_address.lower(),
    }


def _choose_canonical_person_name(*, display_name: str | None, email_address: str) -> str:
    cleaned = ' '.join((display_name or '').strip().split())
    if cleaned:
        return cleaned
    return _guess_name_from_email(email_address)


def _guess_name_from_email(email_address: str) -> str:
    local_part = email_address.split('@', 1)[0]
    tokens = [token for token in re.split(r'[._+-]+', local_part) if token]
    if not tokens:
        return email_address
    return ' '.join(token.capitalize() for token in tokens)


def _organization_hint_from_email(email_address: str) -> str | None:
    parts = email_address.lower().split('@', 1)
    return parts[1] if len(parts) == 2 else None


def _guess_folder_type(folder_name: str) -> str:
    lowered = folder_name.lower()
    if lowered == 'inbox':
        return 'inbox'
    if 'sent' in lowered:
        return 'sent'
    if 'trash' in lowered or 'deleted' in lowered:
        return 'trash'
    if 'junk' in lowered or 'spam' in lowered:
        return 'junk'
    if 'draft' in lowered:
        return 'draft'
    return 'custom'

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from typing import Any

from ..promotion.llm import LLMProviderSpec, create_provider
from ..store import EmailMemoryStore
from .prompts import render_extraction_prompt

logger = logging.getLogger(__name__)

MAX_CHARS_PER_MESSAGE = 3000
MAX_MESSAGES_PER_THREAD = 10


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def run_llm_prompt(prompt: str, spec: LLMProviderSpec) -> str:
    """Run a prompt through the LLM provider and return raw text output."""
    provider = create_provider(spec)
    command = provider.build_command(prompt)
    completed = subprocess.run(command, text=True, capture_output=True, check=True)
    return completed.stdout


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences if present."""
    text = text.strip()
    # Strip ```json ... ``` or ``` ... ```
    fenced = re.match(r'^```(?:json)?\s*\n?(.*?)\n?```\s*$', text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    return text


def _parse_extraction_response(text: str) -> dict[str, Any]:
    """Parse LLM response as JSON, handling markdown fences and malformed output."""
    text = _strip_markdown_fences(text)
    # Try to find a JSON object in the text
    candidates = []
    if text:
        candidates.append(text)
    for match in re.finditer(r'\{', text):
        candidates.append(text[match.start():])
    for candidate in candidates:
        decoder = json.JSONDecoder()
        try:
            payload, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError(f'Could not parse extraction response as JSON: {text[:200]}')


def _parse_date_field(value: Any) -> str | None:
    """Parse a date field, returning None if null/empty/invalid."""
    if value is None or value == 'null' or value == '':
        return None
    s = str(value).strip()
    if not s or s.lower() == 'null':
        return None
    # Validate YYYY-MM-DD format
    try:
        datetime.strptime(s[:10], '%Y-%m-%d')
        return s[:10]
    except ValueError:
        return None


class ExtractionService:
    def __init__(self, store: EmailMemoryStore):
        self.store = store

    def get_unextracted_threads(self, limit: int = 50) -> list[dict]:
        """Return threads that have messages with bodies but no extraction yet."""
        rows = self.store.conn.execute(
            """
            SELECT DISTINCT
                t.thread_id,
                t.thread_key,
                t.canonical_subject
            FROM threads t
            JOIN messages m ON m.thread_key = t.thread_key
            LEFT JOIN thread_summaries ts ON ts.thread_id = t.thread_id
            WHERE ts.summary_id IS NULL
              AND m.cleaned_text IS NOT NULL
              AND length(trim(m.cleaned_text)) > 0
            ORDER BY t.thread_id ASC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        result = []
        for row in rows:
            thread_id = int(row[0])
            thread_key = row[1] or ''
            subject = row[2] or ''
            # Fetch up to MAX_MESSAGES_PER_THREAD messages for this thread
            msg_rows = self.store.conn.execute(
                """
                SELECT
                    m.message_pk,
                    m.from_addr,
                    m.from_name,
                    COALESCE(m.sent_at, m.received_at) AS msg_date,
                    m.normalized_subject,
                    m.cleaned_text
                FROM messages m
                WHERE m.thread_key = ?
                  AND m.cleaned_text IS NOT NULL
                  AND length(trim(m.cleaned_text)) > 0
                ORDER BY COALESCE(m.sent_at, m.received_at) ASC NULLS LAST,
                         m.message_pk ASC
                LIMIT ?
                """,
                [thread_key, MAX_MESSAGES_PER_THREAD],
            ).fetchall()
            messages = []
            for mr in msg_rows:
                messages.append({
                    'message_pk': int(mr[0]),
                    'from_addr': mr[1] or '',
                    'from_name': mr[2] or '',
                    'msg_date': str(mr[3]) if mr[3] else '',
                    'subject': mr[4] or subject,
                    'cleaned_text': mr[5] or '',
                })
            if messages:
                result.append({
                    'thread_id': thread_id,
                    'thread_key': thread_key,
                    'subject': subject,
                    'messages': messages,
                })
        return result

    def build_thread_prompt(self, thread_messages: list[dict]) -> str:
        """Render a readable block of messages for the thread."""
        parts = []
        for i, msg in enumerate(thread_messages, start=1):
            from_display = msg.get('from_name') or msg.get('from_addr') or 'Unknown'
            if msg.get('from_addr') and msg.get('from_name'):
                from_display = f"{msg['from_name']} <{msg['from_addr']}>"
            date_str = msg.get('msg_date') or 'Unknown date'
            subject_str = msg.get('subject') or 'No subject'
            body = (msg.get('cleaned_text') or '').strip()
            if len(body) > MAX_CHARS_PER_MESSAGE:
                body = body[:MAX_CHARS_PER_MESSAGE] + '\n[...truncated...]'
            parts.append(
                f"--- Message {i} ---\n"
                f"From: {from_display}\n"
                f"Date: {date_str}\n"
                f"Subject: {subject_str}\n\n"
                f"{body}"
            )
        thread_text = '\n\n'.join(parts)
        return render_extraction_prompt(thread_text)

    def extract_thread(
        self,
        thread_id: int,
        thread_key: str,
        messages: list[dict],
        spec: LLMProviderSpec,
    ) -> dict:
        """Call LLM, parse JSON response, returns parsed result."""
        prompt = self.build_thread_prompt(messages)
        raw_output = run_llm_prompt(prompt, spec)
        result = _parse_extraction_response(raw_output)
        # Normalize the result structure
        return {
            'action_items': result.get('action_items') or [],
            'deadlines': result.get('deadlines') or [],
            'decisions': result.get('decisions') or [],
            'summary': result.get('summary') or '',
            'key_points': result.get('key_points') or [],
        }

    def persist_extraction(
        self,
        thread_id: int,
        result: dict,
        message_pks: list[int],
    ) -> None:
        """Write extraction results to thread_summaries, action_items, deadlines, decisions."""
        now = _utc_now()
        action_items = result.get('action_items') or []
        deadlines = result.get('deadlines') or []
        decisions = result.get('decisions') or []
        summary_text = result.get('summary') or ''
        key_points = result.get('key_points') or []

        # Delete existing summary for this thread (upsert semantics)
        self.store.conn.execute(
            "DELETE FROM thread_summaries WHERE thread_id = ?",
            [thread_id],
        )

        # Insert new thread summary
        self.store.conn.execute(
            """
            INSERT INTO thread_summaries (
                thread_id, summary_type, summary_text,
                key_points_json, action_items_json, decisions_json,
                deadlines_json, participants_json,
                generated_at, source_message_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                thread_id,
                'extraction',
                summary_text,
                json.dumps(key_points),
                json.dumps(action_items),
                json.dumps(decisions),
                json.dumps(deadlines),
                json.dumps([]),
                now,
                len(message_pks),
            ],
        )

        # The first message_pk for thread-level records
        first_message_pk = message_pks[0] if message_pks else None

        # Insert action items (skip if confidence < 0.5)
        for item in action_items:
            confidence = float(item.get('confidence') or 0.0)
            if confidence < 0.5:
                continue
            action_text = str(item.get('action_text') or '').strip()
            if not action_text:
                continue
            due_date = _parse_date_field(item.get('due_date'))
            owner = item.get('owner') or None
            self.store.conn.execute(
                """
                INSERT INTO action_items (
                    thread_id, message_pk, owner, action_text,
                    due_date, status, confidence, extracted_at
                ) VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                [thread_id, first_message_pk, owner, action_text, due_date, confidence, now],
            )

        # Insert deadlines (skip if confidence < 0.5)
        for item in deadlines:
            confidence = float(item.get('confidence') or 0.0)
            if confidence < 0.5:
                continue
            label = str(item.get('label') or '').strip()
            if not label:
                continue
            due_date = _parse_date_field(item.get('due_date'))
            related_project = item.get('related_project') or None
            self.store.conn.execute(
                """
                INSERT INTO deadlines (
                    thread_id, message_pk, label, due_date,
                    related_project, confidence, status, extracted_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                [thread_id, first_message_pk, label, due_date, related_project, confidence, now],
            )

        # Insert decisions (skip if confidence < 0.5)
        for item in decisions:
            confidence = float(item.get('confidence') or 0.0)
            if confidence < 0.5:
                continue
            decision_text = str(item.get('decision_text') or '').strip()
            if not decision_text:
                continue
            title = str(item.get('title') or '').strip() or None
            decided_by = item.get('decided_by') or []
            decided_at = _parse_date_field(item.get('decided_at'))
            self.store.conn.execute(
                """
                INSERT INTO decisions (
                    thread_id, title, decision_text, decided_by_json,
                    decided_at, confidence, status, source_message_pk,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
                """,
                [
                    thread_id, title, decision_text,
                    json.dumps(decided_by), decided_at,
                    confidence, first_message_pk, now, now,
                ],
            )

        # Record extraction sync state for this thread
        self.store.conn.execute(
            """
            DELETE FROM ingest_sync_state
            WHERE account_name = '__extraction__'
              AND folder_name = ?
              AND sync_kind = 'extraction'
            """,
            [str(thread_id)],
        )
        self.store.conn.execute(
            """
            INSERT INTO ingest_sync_state (
                account_name, folder_name, sync_kind,
                next_page, last_completed_page, status,
                last_run_at, updated_at
            ) VALUES ('__extraction__', ?, 'extraction', 1, 1, 'complete', ?, ?)
            """,
            [str(thread_id), now, now],
        )

    def run_extraction(self, limit: int, spec: LLMProviderSpec) -> dict:
        """Orchestrate: get threads -> extract -> persist. Returns summary stats."""
        threads = self.get_unextracted_threads(limit=limit)
        stats = {
            'threads_processed': 0,
            'action_items_added': 0,
            'deadlines_added': 0,
            'decisions_added': 0,
            'errors': 0,
        }
        for thread in threads:
            thread_id = thread['thread_id']
            thread_key = thread['thread_key']
            messages = thread['messages']
            message_pks = [m['message_pk'] for m in messages]
            try:
                result = self.extract_thread(
                    thread_id=thread_id,
                    thread_key=thread_key,
                    messages=messages,
                    spec=spec,
                )
                self.persist_extraction(
                    thread_id=thread_id,
                    result=result,
                    message_pks=message_pks,
                )
                stats['threads_processed'] += 1
                # Count only items that passed confidence threshold
                for item in (result.get('action_items') or []):
                    if float(item.get('confidence') or 0.0) >= 0.5 and str(item.get('action_text') or '').strip():
                        stats['action_items_added'] += 1
                for item in (result.get('deadlines') or []):
                    if float(item.get('confidence') or 0.0) >= 0.5 and str(item.get('label') or '').strip():
                        stats['deadlines_added'] += 1
                for item in (result.get('decisions') or []):
                    if float(item.get('confidence') or 0.0) >= 0.5 and str(item.get('decision_text') or '').strip():
                        stats['decisions_added'] += 1
            except Exception as exc:
                logger.warning('Extraction failed for thread_id=%s: %s', thread_id, exc)
                stats['errors'] += 1
        return stats

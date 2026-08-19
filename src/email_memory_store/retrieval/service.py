from __future__ import annotations

from typing import Any

from ..store import EmailMemoryStore


class EmailRetrievalService:
    def __init__(self, store: EmailMemoryStore):
        self.store = store

    def search(self, query: str, limit: int = 10) -> dict[str, list[dict[str, Any]]]:
        like = f'%{query.lower()}%'
        messages = self._query_rows(
            """
            SELECT stable_message_id, subject, cleaned_text, from_addr, thread_key
            FROM messages
            WHERE lower(subject) LIKE ? OR lower(COALESCE(cleaned_text, '')) LIKE ? OR lower(from_addr) LIKE ?
            ORDER BY received_at DESC NULLS LAST, message_pk DESC
            LIMIT ?
            """,
            [like, like, like, limit],
        )
        thread_summaries = self._query_rows(
            """
            SELECT summary_id, thread_id, summary_type, summary_text
            FROM thread_summaries
            WHERE lower(summary_text) LIKE ?
            ORDER BY generated_at DESC, summary_id DESC
            LIMIT ?
            """,
            [like, limit],
        )
        decisions = self._query_rows(
            """
            SELECT decision_id, thread_id, title, decision_text, status, confidence
            FROM decisions
            WHERE lower(decision_text) LIKE ? OR lower(COALESCE(title, '')) LIKE ?
            ORDER BY confidence DESC, decision_id DESC
            LIMIT ?
            """,
            [like, like, limit],
        )
        action_items = self._query_rows(
            """
            SELECT action_item_id, thread_id, message_pk, owner, action_text, due_date, status, confidence
            FROM action_items
            WHERE lower(action_text) LIKE ? OR lower(COALESCE(owner, '')) LIKE ?
            ORDER BY confidence DESC, action_item_id DESC
            LIMIT ?
            """,
            [like, like, limit],
        )
        deadlines = self._query_rows(
            """
            SELECT deadline_id, thread_id, label, due_date, related_project, status
            FROM deadlines
            WHERE lower(COALESCE(label, '')) LIKE ? OR lower(COALESCE(related_project, '')) LIKE ?
            ORDER BY due_date ASC NULLS LAST, deadline_id DESC
            LIMIT ?
            """,
            [like, like, limit],
        )
        calendar_events = self._query_rows(
            """
            SELECT
                c.calendar_event_id,
                t.thread_id,
                c.message_pk,
                c.summary,
                c.description,
                c.organizer,
                c.organizer_email,
                c.location,
                c.status,
                c.method,
                c.starts_at,
                c.ends_at
            FROM calendar_events c
            LEFT JOIN messages m ON m.message_pk = c.message_pk
            LEFT JOIN threads t ON t.thread_key = m.thread_key AND t.account_id = m.account_id
            WHERE lower(COALESCE(c.summary, '')) LIKE ?
               OR lower(COALESCE(c.description, '')) LIKE ?
               OR lower(COALESCE(c.organizer, '')) LIKE ?
               OR lower(COALESCE(c.organizer_email, '')) LIKE ?
               OR lower(COALESCE(c.location, '')) LIKE ?
            ORDER BY c.starts_at DESC NULLS LAST, c.calendar_event_id DESC
            LIMIT ?
            """,
            [like, like, like, like, like, limit],
        )
        thread_keys: list[str] = []
        thread_keys.extend([row.get('thread_key') or '' for row in messages])
        if thread_summaries or decisions or action_items or deadlines or calendar_events:
            thread_ids = {
                int(row['thread_id'])
                for row in thread_summaries + decisions + action_items + deadlines + calendar_events
                if row.get('thread_id') is not None
            }
            if thread_ids:
                placeholders = ','.join(['?'] * len(thread_ids))
                thread_rows = self.store.conn.execute(
                    f"SELECT thread_key FROM threads WHERE thread_id IN ({placeholders})",
                    list(thread_ids),
                ).fetchall()
                thread_keys.extend([row[0] for row in thread_rows if row and row[0]])
        return {
            'messages': messages,
            'people': self.store.entity_store.search_people(query=query, limit=limit),
            'thread_summaries': thread_summaries,
            'thread_lineages': self.store.get_thread_lineages(thread_keys=thread_keys),
            'decisions': decisions,
            'action_items': action_items,
            'deadlines': deadlines,
            'calendar_events': calendar_events,
        }

    def _query_rows(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        cursor = self.store.conn.execute(sql, params)
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

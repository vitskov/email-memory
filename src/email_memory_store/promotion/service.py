from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..holographic import HolographicMemoryWriter
from ..operational_artifacts import write_private_text
from ..store import EmailMemoryStore
from .llm import BatchPlanner, PromotionLLMConfig, create_provider, load_soul_text, render_batch_prompt


class EmailPromotionService:
    LOGICAL_THREAD_MAX_GAP_DAYS = 45

    def __init__(self, store: EmailMemoryStore):
        self.store = store

    @staticmethod
    def _truncate_text(text: str, limit: int = 280) -> str:
        compact = text.strip().replace("\n", " ")
        if len(compact) > limit:
            return compact[:limit - 3] + '...'
        return compact

    @staticmethod
    def _normalize_address_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = [value]
        if not isinstance(value, list):
            return []
        return sorted({str(item).strip().lower() for item in value if str(item).strip()})

    def _participant_overlap_score(self, left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        return len(left & right) / max(1, min(len(left), len(right)))

    @staticmethod
    def _sort_at_str(value: Any) -> str:
        """Coerce a DuckDB timestamp (datetime or string) to an ISO string, or return ''."""
        if not value:
            return ''
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    @staticmethod
    def _parse_sort_at(value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError:
            return None

    def _load_thread_metadata(self, thread_ids: set[int]) -> dict[int, dict[str, Any]]:
        if not thread_ids:
            return {}
        placeholders = ','.join(['?'] * len(thread_ids))
        rows = self.store.conn.execute(
            f"""
            SELECT t.thread_id,
                   COALESCE(t.canonical_subject, min(m.normalized_subject), ''),
                   COALESCE(t.first_message_at, min(m.received_at), min(m.sent_at)),
                   COALESCE(t.last_message_at, max(m.received_at), max(m.sent_at)),
                   a.email_address,
                   m.from_addr,
                   m.to_addrs,
                   m.cc_addrs,
                   m.bcc_addrs
            FROM threads t
            JOIN accounts a ON a.account_id = t.account_id
            LEFT JOIN messages m ON m.thread_key = t.thread_key
            WHERE t.thread_id IN ({placeholders})
            GROUP BY t.thread_id, t.canonical_subject, t.first_message_at, t.last_message_at,
                     a.email_address, m.from_addr, m.to_addrs, m.cc_addrs, m.bcc_addrs
            ORDER BY t.thread_id
            """,
            list(thread_ids),
        ).fetchall()
        metadata: dict[int, dict[str, Any]] = {}
        for row in rows:
            thread_id = int(row[0])
            entry = metadata.setdefault(
                thread_id,
                {
                    'normalized_subject': row[1] or '',
                    'first_at': row[2] or row[3] or '',
                    'last_at': row[3] or row[2] or '',
                    'participants': set(),
                },
            )
            owner_email = (row[4] or '').strip().lower()
            candidates = set()
            if row[5]:
                candidates.add(str(row[5]).strip().lower())
            candidates.update(self._normalize_address_list(row[6]))
            candidates.update(self._normalize_address_list(row[7]))
            candidates.update(self._normalize_address_list(row[8]))
            if owner_email:
                candidates.discard(owner_email)
            entry['participants'].update(candidates)
        return metadata

    def _assign_logical_thread_groups(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        thread_ids = {int(item['thread_id']) for item in items if item.get('thread_id') is not None}
        metadata = self._load_thread_metadata(thread_ids)
        groups: list[dict[str, Any]] = []
        items_by_thread: dict[int, list[dict[str, Any]]] = {}
        for item in items:
            if item.get('thread_id') is None:
                continue
            items_by_thread.setdefault(int(item['thread_id']), []).append(item)

        for thread_id, thread_items in sorted(
            items_by_thread.items(),
            key=lambda pair: (
                metadata.get(pair[0], {}).get('first_at') or '',
                metadata.get(pair[0], {}).get('normalized_subject') or '',
                pair[0],
            ),
        ):
            meta = metadata.get(thread_id, {})
            subject = meta.get('normalized_subject') or ''
            participants = set(meta.get('participants') or set())
            matched_group = None
            thread_first_dt = self._parse_sort_at(meta.get('first_at'))
            for group in groups:
                if group['normalized_subject'] != subject:
                    continue
                group_first_dt = self._parse_sort_at(group['first_at'])
                if thread_first_dt and group_first_dt:
                    gap_days = abs((thread_first_dt - group_first_dt).days)
                    if gap_days > self.LOGICAL_THREAD_MAX_GAP_DAYS:
                        continue
                if self._participant_overlap_score(group['participants'], participants) > 0.5:
                    matched_group = group
                    break
            if matched_group is None:
                matched_group = {
                    'group_key': f'logical-thread:{len(groups) + 1}',
                    'normalized_subject': subject,
                    'participants': set(participants),
                    'first_at': meta.get('first_at') or '',
                }
                groups.append(matched_group)
            else:
                matched_group['participants'].update(participants)
                current_first = meta.get('first_at') or ''
                if current_first and (not matched_group['first_at'] or current_first < matched_group['first_at']):
                    matched_group['first_at'] = current_first
            for item in sorted(thread_items, key=lambda value: (value.get('sort_at') or '', int(value['source_object_id']))):
                item['batch_group_key'] = matched_group['group_key']
                item['batch_group_sort_at'] = matched_group['first_at'] or item.get('sort_at') or ''
                item['item_sort_at'] = item.get('sort_at') or matched_group['first_at'] or ''
        return items

    def _assign_decision_batch_groups(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        fallback_items: list[dict[str, Any]] = []
        for item in items:
            current_thread_key = (item.get('current_thread_key') or '').strip()
            if current_thread_key:
                item['batch_group_key'] = f'rfc822-thread:{current_thread_key}'
                item['batch_group_sort_at'] = item.get('current_thread_first_at') or item.get('sort_at') or ''
                item['item_sort_at'] = item.get('sort_at') or item.get('current_thread_first_at') or ''
                continue
            fallback_items.append(item)
        self._assign_logical_thread_groups(fallback_items)
        return items

    def _select_decision_promotions(self, limit: int) -> list[dict[str, Any]]:
        rows = self.store.conn.execute(
            """
            SELECT 'decision' AS source_object_type,
                   CAST(d.decision_id AS VARCHAR) AS source_object_id,
                   d.decision_text,
                   'project' AS promoted_category,
                   'email,decision' AS promoted_tags,
                   d.confidence,
                   d.thread_id,
                   COALESCE(m.received_at, m.sent_at, d.decided_at, current_t.first_message_at, t.first_message_at) AS sort_at,
                   m.thread_key AS current_thread_key,
                   COALESCE(current_t.first_message_at, m.received_at, m.sent_at, d.decided_at) AS current_thread_first_at
            FROM decisions d
            LEFT JOIN threads t ON t.thread_id = d.thread_id
            LEFT JOIN messages m ON m.message_pk = d.source_message_pk
            LEFT JOIN threads current_t ON current_t.thread_key = m.thread_key
            WHERE d.confidence >= 0.7
            ORDER BY COALESCE(current_t.first_message_at, t.first_message_at, m.received_at, m.sent_at, d.decided_at) ASC,
                     d.decision_id ASC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        results = []
        for row in rows:
            results.append(
                {
                    'source_object_type': row[0],
                    'source_object_id': row[1],
                    'promoted_text': self._truncate_text(row[2]),
                    'promoted_category': row[3],
                    'promoted_tags': row[4],
                    'thread_id': int(row[6]) if row[6] is not None else None,
                    'sort_at': row[7] or '',
                    'current_thread_key': row[8] or '',
                    'current_thread_first_at': row[9] or '',
                }
            )
        return self._assign_decision_batch_groups(results)

    def _select_action_item_promotions(self, limit: int) -> list[dict[str, Any]]:
        rows = self.store.conn.execute(
            """
            SELECT 'action_item' AS source_object_type,
                   CAST(ai.action_item_id AS VARCHAR) AS source_object_id,
                   ai.action_text,
                   ai.owner,
                   ai.thread_id,
                   COALESCE(m.received_at, m.sent_at, ai.due_date, ai.extracted_at) AS sort_at
            FROM action_items ai
            LEFT JOIN messages m ON m.message_pk = ai.message_pk
            LEFT JOIN threads t ON t.thread_id = ai.thread_id
            WHERE ai.confidence >= 0.6
              AND (ai.status IS NULL OR ai.status NOT IN ('done', 'cancelled', 'closed'))
            ORDER BY COALESCE(t.first_message_at, m.received_at, m.sent_at, ai.due_date, ai.extracted_at) ASC,
                     ai.action_item_id ASC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        results = []
        for row in rows:
            action_text = row[2] or ''
            owner = row[3]
            promoted_text = self._truncate_text(f"Action: {action_text}")
            promoted_tags = f'email,action_item,owner:{owner}' if owner else 'email,action_item'
            results.append(
                {
                    'source_object_type': row[0],
                    'source_object_id': row[1],
                    'promoted_text': promoted_text,
                    'promoted_category': 'action',
                    'promoted_tags': promoted_tags,
                    'thread_id': int(row[4]) if row[4] is not None else None,
                    'sort_at': self._sort_at_str(row[5]),
                }
            )
        return self._assign_logical_thread_groups(results)

    def _select_deadline_promotions(self, limit: int) -> list[dict[str, Any]]:
        rows = self.store.conn.execute(
            """
            SELECT 'deadline' AS source_object_type,
                   CAST(d.deadline_id AS VARCHAR) AS source_object_id,
                   d.label,
                   d.due_date,
                   d.related_project,
                   d.thread_id,
                   COALESCE(d.due_date, m.received_at, m.sent_at, d.extracted_at) AS sort_at
            FROM deadlines d
            LEFT JOIN messages m ON m.message_pk = d.message_pk
            LEFT JOIN threads t ON t.thread_id = d.thread_id
            WHERE d.confidence >= 0.6
              AND (d.status IS NULL OR d.status NOT IN ('met', 'cancelled', 'closed'))
            ORDER BY COALESCE(d.due_date, t.first_message_at, m.received_at, m.sent_at, d.extracted_at) ASC,
                     d.deadline_id ASC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        results = []
        for row in rows:
            label = row[2] or ''
            due_date = row[3]
            related_project = row[4]
            if due_date is not None:
                parsed = self._parse_sort_at(due_date)
                due_str = str(parsed.date()) if parsed else str(due_date)
            else:
                due_str = 'unspecified'
            if related_project:
                promoted_text = self._truncate_text(f"Deadline: {label} — due {due_str} [{related_project}]")
                promoted_tags = f'email,deadline,project:{related_project}'
            else:
                promoted_text = self._truncate_text(f"Deadline: {label} — due {due_str}")
                promoted_tags = 'email,deadline'
            results.append(
                {
                    'source_object_type': row[0],
                    'source_object_id': row[1],
                    'promoted_text': promoted_text,
                    'promoted_category': 'deadline',
                    'promoted_tags': promoted_tags,
                    'thread_id': int(row[5]) if row[5] is not None else None,
                    'sort_at': self._sort_at_str(row[6]),
                }
            )
        return self._assign_logical_thread_groups(results)

    def _select_person_promotions(self, limit: int) -> list[dict[str, Any]]:
        results = []
        for item in self.store.entity_store.select_people_for_promotion(limit=limit):
            text = self._truncate_text(item['promoted_text'])
            payload = dict(item.get('fact_store_payload') or {})
            if payload.get('content'):
                payload['content'] = self._truncate_text(payload['content'])
            enriched = {
                'source_object_type': item['source_object_type'],
                'source_object_id': item['source_object_id'],
                'promoted_text': text,
                'promoted_category': item['promoted_category'],
                'promoted_tags': item['promoted_tags'],
                'fact_store_payload': payload or None,
                'sort_at': item.get('sort_at') or '',
                'batch_group_key': f"person:{item['source_object_id']}",
                'batch_group_sort_at': item.get('sort_at') or '',
                'item_sort_at': item.get('sort_at') or '',
            }
            results.append(enriched)
        return results

    def select_promotions(self, limit: int = 20) -> list[dict[str, Any]]:
        # Distribute slots: decisions 40%, action_items 25%, deadlines 25%, people 10%
        # Each source gets at least 1 slot so small limits still surface all types.
        decision_limit = max(1, round(limit * 0.4))
        action_limit = max(1, round(limit * 0.25))
        deadline_limit = max(1, round(limit * 0.25))
        person_limit = max(1, limit - decision_limit - action_limit - deadline_limit)

        results: list[dict[str, Any]] = []
        results.extend(self._select_decision_promotions(limit=decision_limit))
        results.extend(self._select_action_item_promotions(limit=action_limit))
        results.extend(self._select_deadline_promotions(limit=deadline_limit))
        remaining = max(0, limit - len(results))
        if remaining:
            results.extend(self._select_person_promotions(limit=min(person_limit, remaining)))
        return results[:limit]

    def _build_fact_store_dedup_key(self, item: dict[str, Any]) -> str:
        payload = item.get('fact_store_payload') or {}
        fingerprint = json.dumps(
            {
                'source_object_type': item['source_object_type'],
                'source_object_id': item['source_object_id'],
                'content': payload.get('content') or item.get('promoted_text'),
                'category': payload.get('category') or item.get('promoted_category'),
                'tags': payload.get('tags') or item.get('promoted_tags'),
            },
            sort_keys=True,
            separators=(',', ':'),
        )
        digest = hashlib.sha1(fingerprint.encode('utf-8')).hexdigest()[:16]
        return f"{item['source_object_type']}:{item['source_object_id']}:{digest}"

    def select_fact_store_promotions(self, limit: int = 20) -> list[dict[str, Any]]:
        candidates = []
        for item in self.select_promotions(limit=limit * 3):
            payload = dict(item.get('fact_store_payload') or {})
            if not payload or not payload.get('content'):
                continue
            dedup_key = self._build_fact_store_dedup_key(item)
            existing = self.store.conn.execute(
                "SELECT 1 FROM promotion_log WHERE fact_store_dedup_key = ? AND status IN ('fact_store_ready', 'fact_store_written') LIMIT 1",
                [dedup_key],
            ).fetchone()
            if existing:
                continue
            enriched = dict(item)
            enriched['fact_store_payload'] = payload
            enriched['fact_store_dedup_key'] = dedup_key
            candidates.append(enriched)
            if len(candidates) >= limit:
                break
        return candidates

    def export_fact_store_batch(self, limit: int = 20, output_path: str | Path | None = None) -> dict[str, Any]:
        batch_id = f"fact-store-batch-{uuid4()}"
        items = []
        for item in self.select_fact_store_promotions(limit=limit):
            enriched = dict(item)
            enriched['batch_id'] = batch_id
            items.append(enriched)
        batch = {'batch_id': batch_id, 'items': items}
        if output_path is not None:
            path = Path(output_path).expanduser()
            write_private_text(
                path,
                json.dumps(batch, indent=2, sort_keys=True) + '\n',
                repair_parent_permissions=True,
            )
        return batch

    def record_promotions(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            self.store.conn.execute(
                """
                INSERT INTO promotion_log(source_object_type, source_object_id, promoted_text, promoted_category, promoted_tags, status)
                VALUES (?, ?, ?, ?, ?, 'selected')
                """,
                [
                    item['source_object_type'],
                    item['source_object_id'],
                    item['promoted_text'],
                    item.get('promoted_category'),
                    item.get('promoted_tags'),
                ],
            )

    def record_fact_store_promotions(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            payload = item.get('fact_store_payload') or {}
            dedup_key = item.get('fact_store_dedup_key') or self._build_fact_store_dedup_key(item)
            existing = self.store.conn.execute(
                "SELECT promotion_id FROM promotion_log WHERE fact_store_dedup_key = ? AND status IN ('fact_store_ready', 'fact_store_written') LIMIT 1",
                [dedup_key],
            ).fetchone()
            if existing:
                continue
            self.store.conn.execute(
                """
                INSERT INTO promotion_log(
                    source_object_type, source_object_id, promoted_text, promoted_category,
                    promoted_tags, fact_store_dedup_key, fact_store_batch_id, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'fact_store_ready')
                """,
                [
                    item['source_object_type'],
                    item['source_object_id'],
                    payload.get('content') or item['promoted_text'],
                    payload.get('category') or item.get('promoted_category'),
                    payload.get('tags') or item.get('promoted_tags'),
                    dedup_key,
                    item.get('batch_id'),
                ],
            )

    def mark_fact_store_written(self, batch_id: str, fact_map: dict[str, int]) -> int:
        updated = 0
        for dedup_key, fact_id in fact_map.items():
            before = self.store.conn.execute(
                "SELECT COUNT(*) FROM promotion_log WHERE fact_store_dedup_key = ? AND status = 'fact_store_ready'",
                [dedup_key],
            ).fetchone()[0]
            self.store.conn.execute(
                """
                UPDATE promotion_log
                SET status = 'fact_store_written',
                    holographic_fact_id = ?,
                    fact_store_batch_id = COALESCE(fact_store_batch_id, ?),
                    fact_store_written_at = CURRENT_TIMESTAMP
                WHERE fact_store_dedup_key = ? AND status = 'fact_store_ready'
                """,
                [fact_id, batch_id, dedup_key],
            )
            updated += int(before)
        return updated

    def mark_fact_store_demoted(self, reason_map: dict[str, str]) -> int:
        updated = 0
        for dedup_key, reason in reason_map.items():
            before = self.store.conn.execute(
                "SELECT COUNT(*) FROM promotion_log WHERE fact_store_dedup_key = ? AND status = 'fact_store_written'",
                [dedup_key],
            ).fetchone()[0]
            self.store.conn.execute(
                """
                UPDATE promotion_log
                SET status = 'fact_store_demoted',
                    demoted_at = CURRENT_TIMESTAMP,
                    demotion_reason = ?,
                    demotion_evidence_json = ?
                WHERE fact_store_dedup_key = ? AND status = 'fact_store_written'
                """,
                [reason, json.dumps({'summary': reason}), dedup_key],
            )
            updated += int(before)
        return updated

    def mark_fact_store_edited(self, edit_map: dict[str, dict[str, str]]) -> int:
        updated = 0
        for dedup_key, payload in edit_map.items():
            before = self.store.conn.execute(
                "SELECT COUNT(*) FROM promotion_log WHERE fact_store_dedup_key = ? AND status = 'fact_store_written'",
                [dedup_key],
            ).fetchone()[0]
            self.store.conn.execute(
                """
                UPDATE promotion_log
                SET status = 'fact_store_edited',
                    revised_at = CURRENT_TIMESTAMP,
                    revision_reason = ?,
                    revised_text = ?
                WHERE fact_store_dedup_key = ? AND status = 'fact_store_written'
                """,
                [payload['reason'], payload['replacement_text'], dedup_key],
            )
            updated += int(before)
        return updated

    def build_llm_promotion_plan(self, limit: int = 20, config: dict[str, Any] | None = None) -> dict[str, Any]:
        resolved = PromotionLLMConfig.from_dict(config or self.store.get_promotion_llm_config())
        provider = create_provider(resolved.provider)
        soul_text = load_soul_text(resolved.soul.path, default_soul_path=self.store.paths.default_promotion_soul_path)
        candidates = self.select_promotions(limit=limit)
        planner = BatchPlanner(
            max_candidates_per_batch=resolved.batching.max_candidates_per_batch,
            max_input_chars=resolved.batching.max_input_chars,
            soul_text=soul_text,
        )
        batches = planner.plan(candidates)
        rendered_batches = []
        for batch in batches:
            rendered = dict(batch)
            rendered['prompt'] = render_batch_prompt(soul_text=soul_text, batch=batch)
            rendered_batches.append(rendered)
        return {
            'provider': provider.spec.to_dict(),
            'batching': resolved.batching.to_dict(),
            'soul': {
                'path': resolved.soul.path or str(self.store.paths.default_promotion_soul_path),
                'text': soul_text,
            },
            'candidate_count': len(candidates),
            'candidates': candidates,
            'batches': rendered_batches,
        }

    def execute_llm_promotion_plan(self, limit: int = 20, config: dict[str, Any] | None = None) -> dict[str, Any]:
        plan = self.build_llm_promotion_plan(limit=limit, config=config)
        provider = create_provider(PromotionLLMConfig.from_dict({'provider': plan['provider'], 'batching': plan['batching'], 'soul': {'path': plan['soul']['path']}}).provider)
        results = [provider.evaluate_prompt(batch['prompt']) for batch in plan['batches']]
        return {
            'provider': plan['provider'],
            'executed_batches': len(results),
            'results': results,
            'candidate_count': plan['candidate_count'],
        }

    def execute_and_commit_llm_promotions(
        self,
        limit: int = 20,
        config: dict[str, Any] | None = None,
        holographic_db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Run LLM promotion batches and write accepted facts to Hermes holographic memory.

        For each result from the LLM:
        - action=promote  → write fact to holographic store, record in promotion_log as fact_store_written
        - action=reject   → log and skip
        - action=demote   → call mark_fact_store_demoted for any existing written fact with matching dedup key
        - action=edit     → call mark_fact_store_edited for any existing written fact with matching dedup key

        Returns a summary dict with counts for each action type.
        """
        plan = self.build_llm_promotion_plan(limit=limit, config=config)
        if not plan['batches']:
            return {'promoted': 0, 'rejected': 0, 'demoted': 0, 'edited': 0, 'errors': 0, 'candidate_count': 0}

        resolved = PromotionLLMConfig.from_dict(config or self.store.get_promotion_llm_config())
        provider = create_provider(resolved.provider)

        # Build a lookup from source_object_id → candidate item for dedup-key generation
        candidate_by_id: dict[str, dict[str, Any]] = {
            str(item['source_object_id']): item
            for item in plan['candidates']
        }

        promoted = rejected = demoted = edited = errors = 0
        batch_id = f'llm-batch-{uuid4()}'

        total_batches = len(plan['batches'])
        with HolographicMemoryWriter(holographic_db_path) as writer:
            for batch_num, batch in enumerate(plan['batches'], start=1):
                print(f'[promotion] batch {batch_num}/{total_batches} ({batch.get("candidate_count",0)} candidates)', flush=True)
                try:
                    raw = provider.evaluate_prompt(batch['prompt'])
                except Exception as exc:
                    errors += len(batch.get('items', []))
                    print(f'[promotion] batch {batch_num} error: {exc}', flush=True)
                    continue

                for result in raw.get('results', []):
                    source_id = str(result.get('source_object_id', ''))
                    action = str(result.get('action', '')).lower().strip()
                    memory_text = str(result.get('memory_text', '')).strip()
                    rationale = str(result.get('rationale', '')).strip()
                    candidate = candidate_by_id.get(source_id)

                    if action == 'promote':
                        if not memory_text:
                            errors += 1
                            continue
                        category = (candidate or {}).get('promoted_category', 'general')
                        tags = (candidate or {}).get('promoted_tags', '')
                        try:
                            fact_id = writer.write_fact(memory_text, category=category, tags=tags)
                        except Exception as exc:
                            print(f'[promotion] holographic write error for {source_id}: {exc}')
                            errors += 1
                            continue
                        # Stage and immediately mark written
                        if candidate:
                            dedup_key = self._build_fact_store_dedup_key(candidate)
                            # Upsert into promotion_log
                            existing = self.store.conn.execute(
                                "SELECT promotion_id FROM promotion_log WHERE fact_store_dedup_key = ? LIMIT 1",
                                [dedup_key],
                            ).fetchone()
                            if not existing:
                                self.store.conn.execute(
                                    """
                                    INSERT INTO promotion_log(
                                        source_object_type, source_object_id, promoted_text,
                                        promoted_category, promoted_tags,
                                        fact_store_dedup_key, fact_store_batch_id, status,
                                        holographic_fact_id, fact_store_written_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'fact_store_written', ?, CURRENT_TIMESTAMP)
                                    """,
                                    [
                                        candidate['source_object_type'],
                                        candidate['source_object_id'],
                                        memory_text,
                                        category,
                                        tags,
                                        dedup_key,
                                        batch_id,
                                        fact_id,
                                    ],
                                )
                            else:
                                self.store.conn.execute(
                                    """
                                    UPDATE promotion_log
                                    SET status = 'fact_store_written',
                                        holographic_fact_id = ?,
                                        fact_store_batch_id = COALESCE(fact_store_batch_id, ?),
                                        fact_store_written_at = CURRENT_TIMESTAMP,
                                        promoted_text = ?
                                    WHERE fact_store_dedup_key = ?
                                    """,
                                    [fact_id, batch_id, memory_text, dedup_key],
                                )
                        promoted += 1
                        print(f'[promotion] promoted {source_id} → fact_id={fact_id}: {memory_text[:80]}')

                    elif action == 'reject':
                        rejected += 1

                    elif action == 'demote':
                        if candidate:
                            dedup_key = self._build_fact_store_dedup_key(candidate)
                            demoted += self.mark_fact_store_demoted({dedup_key: rationale or 'demoted by LLM review'})

                    elif action == 'edit':
                        if candidate and memory_text:
                            dedup_key = self._build_fact_store_dedup_key(candidate)
                            edited += self.mark_fact_store_edited({
                                dedup_key: {'reason': rationale or 'edited by LLM review', 'replacement_text': memory_text}
                            })
                    else:
                        errors += 1

        return {
            'batch_id': batch_id,
            'candidate_count': plan['candidate_count'],
            'promoted': promoted,
            'rejected': rejected,
            'demoted': demoted,
            'edited': edited,
            'errors': errors,
        }

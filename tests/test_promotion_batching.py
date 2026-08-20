from pathlib import Path

import pytest

from email_memory_store.store import EmailMemoryStore
from email_memory_store.promotion.llm import load_soul_text, render_batch_prompt
from email_memory_store.promotion.service import EmailPromotionService


def _promotion_service_with_export_item() -> EmailPromotionService:
    service = EmailPromotionService.__new__(EmailPromotionService)
    service.select_fact_store_promotions = lambda limit=20: [  # type: ignore[method-assign]
        {'fact_store_payload': {'content': 'synthetic private export content'}}
    ][:limit]
    return service


def test_fact_store_export_is_owner_only(tmp_path: Path) -> None:
    output = tmp_path / 'private-export' / 'batch.json'

    _promotion_service_with_export_item().export_fact_store_batch(output_path=output)

    assert output.parent.stat().st_mode & 0o777 == 0o700
    assert output.stat().st_mode & 0o777 == 0o600
    assert 'synthetic private export content' in output.read_text(encoding='utf-8')


def test_fact_store_export_refuses_symlink_targets(tmp_path: Path) -> None:
    target = tmp_path / 'target.json'
    target.write_text('original\n', encoding='utf-8')
    output = tmp_path / 'batch.json'
    output.symlink_to(target)

    with pytest.raises(ValueError, match='symbolic links'):
        _promotion_service_with_export_item().export_fact_store_batch(output_path=output)

    assert target.read_text(encoding='utf-8') == 'original\n'


def _add_threaded_decision(
    store: EmailMemoryStore,
    *,
    account_id: int,
    folder_id: int,
    thread_key: str,
    subject: str,
    from_addr: str,
    to_addrs: list[str],
    sent_at: str,
    decision_text: str,
    confidence: float = 0.95,
) -> int:
    thread_id = store.ensure_thread(account_id, thread_key, subject.lower())
    message_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id=f'{thread_key}:{sent_at}',
        internet_message_id=f'<{thread_key}:{sent_at}@example.test>',
        thread_key=thread_key,
        subject=subject,
        normalized_subject=subject.lower(),
        from_name=from_addr.split('@', 1)[0],
        from_addr=from_addr,
        to_addrs=to_addrs,
        sent_at=sent_at,
        received_at=sent_at,
        has_attachments=False,
        direction='incoming',
        is_read=False,
    )
    store.conn.execute(
        """
        UPDATE threads
        SET first_message_at = COALESCE(first_message_at, ?),
            last_message_at = GREATEST(COALESCE(last_message_at, ?), ?),
            message_count = message_count + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE thread_id = ?
        """,
        [sent_at, sent_at, sent_at, thread_id],
    )
    decision_id = store.conn.execute(
        """
        INSERT INTO decisions(thread_id, title, decision_text, status, confidence, source_message_pk, decided_at)
        VALUES (?, ?, ?, 'confirmed', ?, ?, ?)
        RETURNING decision_id
        """,
        [thread_id, subject, decision_text, confidence, message_pk, sent_at],
    ).fetchone()[0]
    return int(decision_id)


def test_llm_batches_prefer_current_source_message_thread_membership_over_heuristic_merging(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    account_id = store.ensure_account('primary-account', 'owner@example.test', 'office365')
    folder_id = store.ensure_folder(account_id, 'INBOX', 'inbox')

    first = _add_threaded_decision(
        store,
        account_id=account_id,
        folder_id=folder_id,
        thread_key='thread:t1',
        subject='meeting tomorrow',
        from_addr='alice@example.test',
        to_addrs=['owner@example.test', 'bob@example.test'],
        sent_at='2026-01-01 09:00:00+00:00',
        decision_text='Alice and Bob agreed to meet tomorrow in the conference room.',
    )
    second = _add_threaded_decision(
        store,
        account_id=account_id,
        folder_id=folder_id,
        thread_key='thread:t2',
        subject='meeting tomorrow',
        from_addr='alice@example.test',
        to_addrs=['owner@example.test', 'bob@example.test', 'carol@example.test'],
        sent_at='2026-01-02 09:00:00+00:00',
        decision_text='Alice, Bob, and Carol confirmed the same meeting thread details.',
    )
    third = _add_threaded_decision(
        store,
        account_id=account_id,
        folder_id=folder_id,
        thread_key='thread:t3',
        subject='meeting tomorrow',
        from_addr='dave@example.test',
        to_addrs=['owner@example.test', 'erin@example.test'],
        sent_at='2026-01-03 09:00:00+00:00',
        decision_text='A separate meeting tomorrow thread exists for Dave and Erin only.',
    )
    store.set_promotion_llm_config({
        'provider': {'name': 'codex-cli', 'model': 'gpt-5-codex'},
        'batching': {'max_candidates_per_batch': 2, 'max_input_chars': 10000},
    })

    service = EmailPromotionService(store)
    promotions = service.select_promotions(limit=10)
    groups_by_decision = {item['source_object_id']: item['batch_group_key'] for item in promotions}

    assert groups_by_decision[str(first)] != groups_by_decision[str(second)]
    assert groups_by_decision[str(second)] != groups_by_decision[str(third)]

    plan = service.build_llm_promotion_plan(limit=10)
    flattened = [item['source_object_id'] for batch in plan['batches'] for item in batch['items']]
    assert flattened == [str(first), str(second), str(third)]

    store.close()


def test_llm_batches_follow_current_message_thread_key_even_when_decision_thread_id_is_stale(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    account_id = store.ensure_account('primary-account', 'owner@example.test', 'office365')
    folder_id = store.ensure_folder(account_id, 'INBOX', 'inbox')

    first = _add_threaded_decision(
        store,
        account_id=account_id,
        folder_id=folder_id,
        thread_key='thread:stale-a',
        subject='status update',
        from_addr='alice@example.test',
        to_addrs=['owner@example.test', 'bob@example.test'],
        sent_at='2026-01-01 09:00:00+00:00',
        decision_text='Alice confirmed the current status update thread.',
    )
    second = _add_threaded_decision(
        store,
        account_id=account_id,
        folder_id=folder_id,
        thread_key='thread:stale-b',
        subject='status update',
        from_addr='alice@example.test',
        to_addrs=['owner@example.test', 'bob@example.test'],
        sent_at='2026-01-01 10:00:00+00:00',
        decision_text='Bob replied in what is now the same RFC-reconstructed conversation.',
    )

    source_message_pk = store.conn.execute(
        'SELECT source_message_pk FROM decisions WHERE decision_id = ?',
        [second],
    ).fetchone()[0]
    current_thread_id = store.conn.execute(
        'SELECT thread_id FROM decisions WHERE decision_id = ?',
        [first],
    ).fetchone()[0]
    current_thread_key = store.conn.execute(
        'SELECT thread_key FROM threads WHERE thread_id = ?',
        [current_thread_id],
    ).fetchone()[0]
    store.conn.execute(
        'UPDATE messages SET thread_key = ? WHERE message_pk = ?',
        [current_thread_key, source_message_pk],
    )

    service = EmailPromotionService(store)
    promotions = service.select_promotions(limit=10)
    groups_by_decision = {item['source_object_id']: item['batch_group_key'] for item in promotions}

    assert groups_by_decision[str(first)] == groups_by_decision[str(second)]

    store.close()


def test_llm_batches_fall_back_to_heuristic_grouping_when_source_message_thread_key_is_unavailable(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    account_id = store.ensure_account('primary-account', 'owner@example.test', 'office365')
    folder_id = store.ensure_folder(account_id, 'INBOX', 'inbox')

    first = _add_threaded_decision(
        store,
        account_id=account_id,
        folder_id=folder_id,
        thread_key='thread:fallback-a',
        subject='budget review',
        from_addr='alice@example.test',
        to_addrs=['owner@example.test', 'bob@example.test'],
        sent_at='2026-01-05 09:00:00+00:00',
        decision_text='January budget review settled the Q1 travel allocation.',
    )
    second = _add_threaded_decision(
        store,
        account_id=account_id,
        folder_id=folder_id,
        thread_key='thread:fallback-b',
        subject='budget review',
        from_addr='alice@example.test',
        to_addrs=['owner@example.test', 'bob@example.test'],
        sent_at='2026-01-06 09:00:00+00:00',
        decision_text='January budget review settled the same allocation in a follow-up thread.',
    )
    store.conn.execute('UPDATE decisions SET source_message_pk = NULL WHERE decision_id IN (?, ?)', [first, second])

    service = EmailPromotionService(store)
    promotions = service.select_promotions(limit=10)
    groups_by_decision = {item['source_object_id']: item['batch_group_key'] for item in promotions}

    assert groups_by_decision[str(first)] == groups_by_decision[str(second)]

    store.close()


def test_llm_batches_split_same_subject_same_recipients_when_threads_are_far_apart(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    account_id = store.ensure_account('primary-account', 'owner@example.test', 'office365')
    folder_id = store.ensure_folder(account_id, 'INBOX', 'inbox')

    first = _add_threaded_decision(
        store,
        account_id=account_id,
        folder_id=folder_id,
        thread_key='thread:budget-q1',
        subject='budget review',
        from_addr='alice@example.test',
        to_addrs=['owner@example.test', 'bob@example.test'],
        sent_at='2026-01-05 09:00:00+00:00',
        decision_text='January budget review settled the Q1 travel allocation.',
    )
    second = _add_threaded_decision(
        store,
        account_id=account_id,
        folder_id=folder_id,
        thread_key='thread:budget-q2',
        subject='budget review',
        from_addr='alice@example.test',
        to_addrs=['owner@example.test', 'bob@example.test'],
        sent_at='2026-04-20 09:00:00+00:00',
        decision_text='April budget review settled the separate Q2 travel allocation.',
    )

    service = EmailPromotionService(store)
    promotions = service.select_promotions(limit=10)
    groups_by_decision = {item['source_object_id']: item['batch_group_key'] for item in promotions}

    assert groups_by_decision[str(first)] != groups_by_decision[str(second)]

    store.close()


def test_llm_batches_split_single_oversized_thread_only_when_needed(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    account_id = store.ensure_account('primary-account', 'owner@example.test', 'office365')
    folder_id = store.ensure_folder(account_id, 'INBOX', 'inbox')

    decision_ids = []
    for idx in range(3):
        decision_ids.append(
            _add_threaded_decision(
                store,
                account_id=account_id,
                folder_id=folder_id,
                thread_key='thread:oversized',
                subject='grant review',
                from_addr='alice@example.test',
                to_addrs=['owner@example.test', 'bob@example.test'],
                sent_at=f'2026-02-0{idx + 1} 09:00:00+00:00',
                decision_text=('Project review summary ' + str(idx) + ' ') * 20,
            )
        )
    service = EmailPromotionService(store)
    candidates = service.select_promotions(limit=10)
    soul_text = load_soul_text(None, default_soul_path=store.paths.default_promotion_soul_path)
    max_input_chars = len(render_batch_prompt(soul_text=soul_text, batch={'items': candidates[:2]}))
    assert len(render_batch_prompt(soul_text=soul_text, batch={'items': candidates})) > max_input_chars

    store.set_promotion_llm_config({
        'provider': {'name': 'codex-cli', 'model': 'gpt-5-codex'},
        'batching': {'max_candidates_per_batch': 10, 'max_input_chars': max_input_chars},
    })

    plan = service.build_llm_promotion_plan(limit=10)

    flattened = [item['source_object_id'] for batch in plan['batches'] for item in batch['items']]
    assert flattened == [str(value) for value in decision_ids]
    assert len(plan['batches']) == 2
    assert all(batch['items'] for batch in plan['batches'])

    store.close()

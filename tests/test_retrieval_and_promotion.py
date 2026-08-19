from pathlib import Path

from email_memory_store.store import EmailMemoryStore
from email_memory_store.retrieval.service import EmailRetrievalService
from email_memory_store.promotion.service import EmailPromotionService


def _seed_store(store: EmailMemoryStore) -> None:
    account_id = store.ensure_account("primary-account", "user@example.test", "office365")
    folder_id = store.ensure_folder(account_id, "INBOX", "inbox")
    store.ensure_contact("contact@example.test", "Example Contact")
    store.ensure_thread(account_id, "thread:abc", "parking closure")
    message_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id="rfc822:msg-1@example.test",
        internet_message_id="<msg-1@example.test>",
        thread_key="thread:abc",
        subject="Parking closure next week",
        normalized_subject="parking closure next week",
        from_name="Example Contact",
        from_addr="contact@example.test",
        to_addrs=["user@example.test"],
        sent_at=None,
        received_at=None,
        has_attachments=False,
        direction="incoming",
        is_read=False,
    )
    store.update_message_content(message_pk=message_pk, cleaned_text="The Sample visitor lot will be closed next week. This affects site parking.")
    store.conn.execute(
        "INSERT INTO thread_summaries(thread_id, summary_type, summary_text, source_message_count) VALUES (1, 'rolling', 'Campus parking thread about lot closure next week.', 1)"
    )
    store.conn.execute(
        "INSERT INTO decisions(thread_id, title, decision_text, status, confidence, source_message_pk) VALUES (1, 'Parking closure notice', 'Sample visitor lot is closed next week.', 'confirmed', 0.95, 1)"
    )
    store.conn.execute(
        "INSERT INTO deadlines(thread_id, message_pk, label, due_date, related_project, confidence, status) VALUES (1, 1, 'lot closure week', TIMESTAMP '2026-04-10 12:00:00', 'site parking', 0.9, 'open')"
    )
    store.conn.execute(
        """
        INSERT INTO calendar_events(
            message_pk, summary, description, organizer, organizer_email, location,
            starts_at, ends_at, raw_ics
        ) VALUES (
            1,
            'Parking coordination meeting',
            'Discuss the Sample visitor lot closure and alternatives.',
            'Example Contact',
            'contact@example.test',
            'Sample Hall',
            TIMESTAMP '2026-04-09 09:00:00',
            TIMESTAMP '2026-04-09 10:00:00',
            'BEGIN:VCALENDAR\nEND:VCALENDAR'
        )
        """
    )


def test_retrieval_service_returns_action_items(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    _seed_store(store)
    store.conn.execute(
        "INSERT INTO action_items(thread_id, message_pk, owner, action_text, due_date, status, confidence) VALUES (1, 1, NULL, 'Send the parking closure notice to site staff.', NULL, 'open', 1.0)"
    )

    service = EmailRetrievalService(store)
    results = service.search("parking")

    assert len(results["action_items"]) == 1
    assert results["action_items"][0]["message_pk"] == 1
    assert results["action_items"][0]["action_text"] == 'Send the parking closure notice to site staff.'

    store.close()


def test_retrieval_service_finds_messages_threads_and_decisions(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    _seed_store(store)

    service = EmailRetrievalService(store)
    results = service.search("parking")

    assert len(results["messages"]) == 1
    assert len(results["thread_summaries"]) == 1
    assert len(results["decisions"]) == 1
    assert results["messages"][0]["stable_message_id"] == "rfc822:msg-1@example.test"

    store.close()


def test_retrieval_service_returns_calendar_events(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    _seed_store(store)

    service = EmailRetrievalService(store)
    results = service.search("sample")

    assert len(results["calendar_events"]) == 1
    assert results["calendar_events"][0]["summary"] == 'Parking coordination meeting'
    assert results["calendar_events"][0]["thread_id"] == 1

    store.close()


def test_retrieval_service_reports_cross_folder_thread_lineage(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    account_id = store.ensure_account("primary-account", "user@example.test", "office365")
    inbox_folder_id = store.ensure_folder(account_id, "INBOX", "inbox")
    archive_folder_id = store.ensure_folder(account_id, "Archive", "custom")
    thread_id = store.ensure_thread(account_id, "rfc822-thread:root@example.test", "project sync")
    assert thread_id > 0
    first_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=inbox_folder_id,
        stable_message_id="rfc822:root@example.test",
        identity_source="rfc822",
        internet_message_id="<root@example.test>",
        thread_key="rfc822-thread:root@example.test",
        subject="Project sync",
        normalized_subject="project sync",
        from_name="Alice",
        from_addr="alice@example.test",
        to_addrs=["user@example.test"],
        sent_at="2026-04-01 09:00:00+00:00",
        received_at="2026-04-01 09:00:00+00:00",
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
        subject="Re: Project sync",
        normalized_subject="project sync",
        from_name="User",
        from_addr="user@example.test",
        to_addrs=["alice@example.test"],
        sent_at="2026-04-01 10:00:00+00:00",
        received_at="2026-04-01 10:00:00+00:00",
        has_attachments=False,
        direction="outgoing",
        is_read=False,
    )
    store.replace_message_labels(message_pk=first_pk, labels=["INBOX"])
    store.replace_message_labels(message_pk=second_pk, labels=["Archive"])
    store.update_message_content(message_pk=first_pk, cleaned_text="Project sync kickoff", text_hash="hash-1")
    store.update_message_content(message_pk=second_pk, cleaned_text="Project sync follow-up archived", text_hash="hash-2")

    service = EmailRetrievalService(store)
    results = service.search("project sync")

    assert len(results["thread_lineages"]) == 1
    lineage = results["thread_lineages"][0]
    assert lineage["thread_key"] == "rfc822-thread:root@example.test"
    assert lineage["lineage_root"] == "root@example.test"
    assert lineage["folder_labels"] == ["Archive", "INBOX"]
    assert lineage["message_count"] == 2

    store.close()


def test_promotion_service_selects_small_high_signal_email_facts(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    _seed_store(store)

    service = EmailPromotionService(store)
    promoted = service.select_promotions(limit=5)

    assert promoted
    assert any(item["source_object_type"] == "decision" for item in promoted)
    assert all(len(item["promoted_text"]) <= 280 for item in promoted)

    service.record_promotions(promoted)
    assert store.conn.execute("SELECT COUNT(*) FROM promotion_log").fetchone()[0] == len(promoted)

    store.close()


def test_promotion_service_can_stage_fact_store_payloads(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    _seed_store(store)

    person_id, canonical_name = store.entity_store.ensure_person('Example Contact', organization_hint='example.test')
    store.entity_store.ensure_person_alias(person_id, canonical_name)
    store.entity_store.ensure_person_email(person_id, 'contact@example.test')
    store.replace_message_entities(
        message_pk=1,
        stable_message_id='rfc822:msg-1@example.test',
        people=[
            {
                'person_id': person_id,
                'canonical_name': canonical_name,
                'normalized_name': 'example contact',
                'role': 'from',
                'email_address': 'contact@example.test',
            }
        ],
    )

    service = EmailPromotionService(store)
    payloads = service.select_fact_store_promotions(limit=5)

    assert payloads
    assert all(item['fact_store_payload']['content'] for item in payloads)
    assert all(item['fact_store_payload']['category'] for item in payloads)
    assert all(item['fact_store_dedup_key'] for item in payloads)

    export_path = tmp_path / 'fact-store-batch.json'
    batch = service.export_fact_store_batch(limit=5, output_path=export_path)
    assert batch['batch_id']
    assert export_path.exists()
    assert batch['items']
    assert all(item['batch_id'] == batch['batch_id'] for item in batch['items'])

    service.record_fact_store_promotions(batch['items'])
    service.record_fact_store_promotions(batch['items'])
    rows = store.conn.execute(
        "SELECT fact_store_dedup_key, status FROM promotion_log ORDER BY promotion_id"
    ).fetchall()
    assert len(rows) == len(batch['items'])
    assert all(status == 'fact_store_ready' for _, status in rows)

    dedup_key = batch['items'][0]['fact_store_dedup_key']
    service.mark_fact_store_written(batch_id=batch['batch_id'], fact_map={dedup_key: 501})
    written_row = store.conn.execute(
        "SELECT status, holographic_fact_id, fact_store_batch_id FROM promotion_log WHERE fact_store_dedup_key = ?",
        [dedup_key],
    ).fetchone()
    assert written_row == ('fact_store_written', 501, batch['batch_id'])

    demoted = service.mark_fact_store_demoted({dedup_key: 'Contradicted by newer thread summary'})
    assert demoted == 1
    demoted_row = store.conn.execute(
        "SELECT status, demotion_reason, holographic_fact_id FROM promotion_log WHERE fact_store_dedup_key = ?",
        [dedup_key],
    ).fetchone()
    assert demoted_row == ('fact_store_demoted', 'Contradicted by newer thread summary', 501)

    store.close()


def test_promotion_service_can_mark_written_fact_for_edit(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    _seed_store(store)

    person_id, canonical_name = store.entity_store.ensure_person('Example Contact', organization_hint='example.test')
    store.entity_store.ensure_person_alias(person_id, canonical_name)
    store.entity_store.ensure_person_email(person_id, 'contact@example.test')
    store.replace_message_entities(
        message_pk=1,
        stable_message_id='rfc822:msg-1@example.test',
        people=[{
            'person_id': person_id,
            'canonical_name': canonical_name,
            'normalized_name': 'example contact',
            'role': 'from',
            'email_address': 'contact@example.test',
        }],
    )

    service = EmailPromotionService(store)
    batch = service.export_fact_store_batch(limit=5, output_path=tmp_path / 'fact-store-batch.json')
    service.record_fact_store_promotions(batch['items'])
    dedup_key = batch['items'][0]['fact_store_dedup_key']
    service.mark_fact_store_written(batch_id=batch['batch_id'], fact_map={dedup_key: 501})

    edited = service.mark_fact_store_edited({
        dedup_key: {
            'replacement_text': 'Example Contact sent a durable parking-policy update affecting future site access.',
            'reason': 'Newer evidence shows the original memory was underspecified.',
        }
    })
    assert edited == 1
    edited_row = store.conn.execute(
        "SELECT status, revision_reason, revised_text, holographic_fact_id FROM promotion_log WHERE fact_store_dedup_key = ?",
        [dedup_key],
    ).fetchone()
    assert edited_row == (
        'fact_store_edited',
        'Newer evidence shows the original memory was underspecified.',
        'Example Contact sent a durable parking-policy update affecting future site access.',
        501,
    )

    store.close()


def test_select_action_item_promotions_returns_candidates(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    _seed_store(store)
    store.conn.execute(
        "INSERT INTO action_items(thread_id, message_pk, owner, action_text, due_date, status, confidence)"
        " VALUES (1, 1, 'alice@example.test', 'Schedule the parking re-opening with facilities.', NULL, 'open', 0.8)"
    )

    service = EmailPromotionService(store)
    promoted = service.select_promotions(limit=10)

    action_items = [item for item in promoted if item['source_object_type'] == 'action_item']
    assert len(action_items) == 1
    item = action_items[0]
    assert item['promoted_text'].startswith('Action:')
    assert 'parking re-opening' in item['promoted_text']
    assert item['promoted_category'] == 'action'
    assert 'action_item' in item['promoted_tags']
    assert 'owner:alice@example.test' in item['promoted_tags']
    assert len(item['promoted_text']) <= 280

    store.close()


def test_select_deadline_promotions_returns_candidates(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    _seed_store(store)
    # _seed_store already inserts a deadline with confidence 0.9 — verify it surfaces
    service = EmailPromotionService(store)
    promoted = service.select_promotions(limit=10)

    deadlines = [item for item in promoted if item['source_object_type'] == 'deadline']
    assert len(deadlines) == 1
    item = deadlines[0]
    assert item['promoted_text'].startswith('Deadline:')
    assert 'lot closure' in item['promoted_text']
    assert item['promoted_category'] == 'deadline'
    assert 'deadline' in item['promoted_tags']
    assert 'project:site parking' in item['promoted_tags']
    assert len(item['promoted_text']) <= 280

    store.close()


def test_low_confidence_candidates_excluded(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    _seed_store(store)
    store.conn.execute(
        "INSERT INTO action_items(thread_id, message_pk, owner, action_text, due_date, status, confidence)"
        " VALUES (1, 1, NULL, 'Low-confidence action item to be excluded.', NULL, 'open', 0.3)"
    )
    store.conn.execute(
        "INSERT INTO deadlines(thread_id, message_pk, label, due_date, related_project, confidence, status)"
        " VALUES (1, 1, 'low confidence deadline', NULL, NULL, 0.3, 'open')"
    )

    service = EmailPromotionService(store)
    promoted = service.select_promotions(limit=20)

    action_items = [item for item in promoted if item['source_object_type'] == 'action_item']
    assert len(action_items) == 0, "Low-confidence action items must not be promoted"

    # The seeded deadline from _seed_store has confidence 0.9 and should appear;
    # only the newly inserted low-confidence one should be absent.
    low_conf_deadlines = [
        item for item in promoted
        if item['source_object_type'] == 'deadline' and 'low confidence' in item['promoted_text']
    ]
    assert len(low_conf_deadlines) == 0, "Low-confidence deadlines must not be promoted"

    store.close()


def test_promotion_service_can_build_llm_promotion_plan(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    _seed_store(store)
    store.set_promotion_llm_config({
        'provider': {'name': 'codex-cli', 'model': 'gpt-5-codex'},
        'batching': {'max_candidates_per_batch': 1, 'max_input_chars': 1000},
    })

    service = EmailPromotionService(store)
    plan = service.build_llm_promotion_plan(limit=3)

    assert plan['provider']['name'] == 'codex-cli'
    assert plan['provider']['model'] == 'gpt-5-codex'
    assert plan['soul']['text']
    assert plan['candidates']
    assert plan['batches']
    assert all(batch['candidate_count'] == 1 for batch in plan['batches'])

    store.close()


def test_promotion_service_can_execute_llm_promotion_plan(tmp_path: Path, monkeypatch):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    _seed_store(store)
    store.set_promotion_llm_config({
        'provider': {'name': 'codex-cli', 'model': 'gpt-5-codex'},
        'batching': {'max_candidates_per_batch': 1, 'max_input_chars': 1000},
    })

    def fake_run(command, text, capture_output, check):
        class Result:
            stdout = '{"results":[{"source_object_id":"1","action":"promote","memory_text":"Durable parking memory","rationale":"Stable impact"}]}'
        return Result()

    monkeypatch.setattr('subprocess.run', fake_run)
    service = EmailPromotionService(store)
    executed = service.execute_llm_promotion_plan(limit=3)

    assert executed['provider']['name'] == 'codex-cli'
    assert executed['executed_batches'] >= 1
    assert executed['results'][0]['results'][0]['action'] == 'promote'

    store.close()


def test_execute_and_commit_llm_promotions_writes_to_holographic(tmp_path, monkeypatch):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    _seed_store(store)
    store.set_promotion_llm_config({
        'provider': {'name': 'codex-cli', 'model': 'gpt-5-codex'},
        'batching': {'max_candidates_per_batch': 5, 'max_input_chars': 50000},
    })

    def fake_run(command, text, capture_output, check):
        class Result:
            stdout = '{"results":[{"source_object_id":"1","action":"promote","memory_text":"Parking lot closure affects site next week","rationale":"Durable site fact"},{"source_object_id":"2","action":"reject","memory_text":"","rationale":"noise"}]}'
        return Result()

    monkeypatch.setattr('subprocess.run', fake_run)

    written_facts: list[dict] = []

    class FakeWriter:
        def __init__(self, db_path=None): pass
        def write_fact(self, content, category='general', tags=''):
            written_facts.append({'content': content, 'category': category, 'tags': tags})
            return len(written_facts)
        def remove_fact(self, fact_id): return True
        def update_fact(self, fact_id, **kwargs): return True
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass

    monkeypatch.setattr(
        'email_memory_store.promotion.service.HolographicMemoryWriter',
        FakeWriter,
    )

    service = EmailPromotionService(store)
    result = service.execute_and_commit_llm_promotions(limit=5)

    assert result['promoted'] == 1
    assert result['rejected'] == 1
    assert result['errors'] == 0
    assert len(written_facts) == 1
    assert 'Parking' in written_facts[0]['content']

    # Verify promotion_log entry written
    row = store.conn.execute(
        "SELECT status, holographic_fact_id FROM promotion_log WHERE status = 'fact_store_written' LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row[1] == 1  # fact_id returned by FakeWriter

    store.close()

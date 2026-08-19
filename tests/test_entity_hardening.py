from pathlib import Path

from email_memory_store.entity_store import EntityMemoryStore
from email_memory_store.himalaya import HimalayaEnvelope
from email_memory_store.ingestion import ingest_envelopes
from email_memory_store.promotion.service import EmailPromotionService
from email_memory_store.retrieval.service import EmailRetrievalService
from email_memory_store.store import EmailMemoryStore


class CollisionClient:
    def list_envelopes(self, account: str, folder: str = "INBOX", page: int = 1, page_size: int = 100):
        return [
            HimalayaEnvelope(
                message_id="c1",
                subject="Physics note",
                from_addr="contact@team-a.test",
                from_name="Sample Contact",
                to_addrs=["owner@example.test"],
                date="2026-04-03 10:00+00:00",
                has_attachment=False,
                flags=[],
                internet_message_id="<c1@example.test>",
            ),
            HimalayaEnvelope(
                message_id="c2",
                subject="Biology note",
                from_addr="contact@team-b.test",
                from_name="Sample Contact",
                to_addrs=["owner@example.test"],
                date="2026-04-04 10:00+00:00",
                has_attachment=False,
                flags=[],
                internet_message_id="<c2@example.test>",
            ),
        ]


def test_same_name_collisions_are_flagged_as_ambiguous_not_blindly_merged(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')

    ingest_envelopes(
        store=store,
        client=CollisionClient(),
        account_name='primary-account',
        email_address='owner@example.test',
        folder_name='INBOX',
    )

    rows = store.entity_store.conn.execute(
        "SELECT canonical_name, disambiguation_status, organization_hint, email_count FROM people ORDER BY person_id"
    ).fetchall()
    assert rows == [
        ('Sample Contact', 'ambiguous', 'team-a.test', 1),
        ('Sample Contact', 'ambiguous', 'team-b.test', 1),
    ]

    retrieval = EmailRetrievalService(store).search('sample')
    assert len(retrieval['people']) == 2
    assert all(item['disambiguation_status'] == 'ambiguous' for item in retrieval['people'])

    store.close()


def test_merge_and_split_person_controls_rewire_indices_and_keep_audit_trail(tmp_path: Path):
    entity_store = EntityMemoryStore(tmp_path / 'entity_memory.duckdb')
    entity_store.initialize()

    p1, _ = entity_store.ensure_person('Sample Contact', organization_hint='team-a.test')
    p2, _ = entity_store.ensure_person('Sample Contact', organization_hint='team-b.test')
    entity_store.ensure_person_email(p1, 'contact@team-a.test')
    entity_store.ensure_person_email(p2, 'contact@team-b.test')
    entity_store.replace_message_entities(
        email_message_pk=1,
        stable_message_id='msg-1',
        people=[{'person_id': p1, 'canonical_name': 'Sample Contact', 'normalized_name': 'sample contact', 'role': 'from', 'email_address': 'contact@team-a.test'}],
    )
    entity_store.replace_message_entities(
        email_message_pk=2,
        stable_message_id='msg-2',
        people=[{'person_id': p2, 'canonical_name': 'Sample Contact', 'normalized_name': 'sample contact', 'role': 'from', 'email_address': 'contact@team-b.test'}],
    )

    merged_person_id = entity_store.merge_people(primary_person_id=p1, secondary_person_id=p2, reason='manual merge test')
    merged_row = entity_store.conn.execute(
        "SELECT person_id, email_count, message_count, disambiguation_status FROM people WHERE person_id = ?",
        [merged_person_id],
    ).fetchone()
    assert merged_row == (p1, 2, 2, 'merged')

    split_person_id = entity_store.split_person(
        source_person_id=p1,
        new_canonical_name='Sample Contact',
        email_addresses=['contact@team-b.test'],
        reason='manual split test',
    )
    split_row = entity_store.conn.execute(
        "SELECT canonical_name, email_count, disambiguation_status FROM people WHERE person_id = ?",
        [split_person_id],
    ).fetchone()
    assert split_row == ('Sample Contact', 1, 'split')

    audit_rows = entity_store.conn.execute(
        "SELECT action, reason FROM entity_resolution_log ORDER BY event_id"
    ).fetchall()
    assert audit_rows == [
        ('merge', 'manual merge test'),
        ('split', 'manual split test'),
    ]

    entity_store.close()


def test_email_memory_store_merge_and_split_keep_email_side_indices_in_sync(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')

    ingest_envelopes(
        store=store,
        client=CollisionClient(),
        account_name='primary-account',
        email_address='owner@example.test',
        folder_name='INBOX',
    )

    people = store.entity_store.search_people('sample', limit=10)
    team_a_person = next(item for item in people if item['organization_hint'] == 'team-a.test')
    team_b_person = next(item for item in people if item['organization_hint'] == 'team-b.test')

    store.merge_people(
        primary_person_id=team_a_person['person_id'],
        secondary_person_id=team_b_person['person_id'],
        reason='manual CLI merge',
    )

    merged_email_rows = store.conn.execute(
        "SELECT DISTINCT person_id FROM email_entity_index WHERE canonical_name = 'Sample Contact' ORDER BY person_id"
    ).fetchall()
    assert merged_email_rows == [(team_a_person['person_id'],)]

    new_person_id = store.split_person(
        source_person_id=team_a_person['person_id'],
        new_canonical_name='Sample Contact',
        email_addresses=['contact@team-b.test'],
        reason='manual CLI split',
    )

    split_email_rows = store.conn.execute(
        "SELECT email_address, person_id FROM email_entity_index WHERE canonical_name = 'Sample Contact' ORDER BY email_address"
    ).fetchall()
    assert split_email_rows == [
        ('contact@team-a.test', team_a_person['person_id']),
        ('contact@team-b.test', new_person_id),
    ]

    store.close()


def test_person_promotions_include_fact_store_bridge_payloads(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')
    ingest_envelopes(
        store=store,
        client=CollisionClient(),
        account_name='primary-account',
        email_address='owner@example.test',
        folder_name='INBOX',
    )

    promotions = EmailPromotionService(store).select_promotions(limit=10)
    person_items = [item for item in promotions if item['source_object_type'] == 'person']
    assert person_items
    assert all('fact_store_payload' in item for item in person_items)
    assert all(item['fact_store_payload']['category'] == 'general' for item in person_items)
    assert any('Sample Contact' in item['fact_store_payload']['content'] for item in person_items)

    store.close()


def test_purge_folder_removes_messages_labels_and_cross_indices(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize(start_date='2022-01-02')

    ingest_envelopes(
        store=store,
        client=CollisionClient(),
        account_name='primary-account',
        email_address='owner@example.test',
        folder_name='Archive/Legacy',
    )

    preview = store.purge_messages_by_folders(['Archive/Legacy'], dry_run=True)
    assert preview['messages_matched'] == 2
    assert preview['messages_deleted'] == 0

    result = store.purge_messages_by_folders(['Archive/Legacy'])
    assert result['messages_deleted'] == 2
    assert store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM message_labels").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM email_entity_index").fetchone()[0] == 0
    assert store.entity_store.conn.execute("SELECT COUNT(*) FROM message_entity_index").fetchone()[0] == 0

    store.close()

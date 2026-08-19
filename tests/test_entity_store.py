from pathlib import Path

from email_memory_store.himalaya import HimalayaEnvelope
from email_memory_store.ingestion import ingest_envelopes
from email_memory_store.retrieval.service import EmailRetrievalService
from email_memory_store.promotion.service import EmailPromotionService
from email_memory_store.store import EmailMemoryStore


class FakeEntityClient:
    def list_envelopes(self, account: str, folder: str = "INBOX", page: int = 1, page_size: int = 100):
        return [
            HimalayaEnvelope(
                message_id="m1",
                subject="First message",
                from_addr="contact@unit-a.test",
                from_name="Example Contact",
                to_addrs=["user@example.test"],
                date="2026-04-03 10:00+00:00",
                has_attachment=False,
                flags=[],
                internet_message_id="<m1@example.test>",
            ),
            HimalayaEnvelope(
                message_id="m2",
                subject="Second message",
                from_addr="contact@example.test",
                from_name="Example Contact",
                to_addrs=["owner@example.test"],
                date="2026-04-04 10:00+00:00",
                has_attachment=False,
                flags=[],
                internet_message_id="<m2@example.test>",
            ),
        ]


def test_ingestion_creates_name_based_companion_entity_db_and_cross_indices(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize(start_date="2022-01-02")

    result = ingest_envelopes(
        store=store,
        client=FakeEntityClient(),
        account_name="primary-account",
        email_address="user@example.test",
        folder_name="INBOX",
    )

    assert result["messages_added"] == 2
    assert store.paths.entity_db_path.exists()

    person_rows = store.entity_store.conn.execute(
        "SELECT canonical_name, organization_hint, disambiguation_status, email_count, message_count FROM people WHERE normalized_name = 'example contact' ORDER BY organization_hint"
    ).fetchall()
    assert person_rows == [
        ("Example Contact", "example.test", "ambiguous", 1, 1),
        ("Example Contact", "unit-a.test", "ambiguous", 1, 1),
    ]

    email_rows = store.entity_store.conn.execute(
        "SELECT email_address FROM person_emails ORDER BY email_address"
    ).fetchall()
    assert email_rows == [("contact@example.test",), ("contact@unit-a.test",), ("owner@example.test",)]

    email_side_links = store.conn.execute(
        "SELECT canonical_name, role, email_address FROM email_entity_index WHERE canonical_name = 'Example Contact' ORDER BY email_address"
    ).fetchall()
    assert email_side_links == [
        ("Example Contact", "from", "contact@example.test"),
        ("Example Contact", "from", "contact@unit-a.test"),
    ]

    entity_side_links = store.entity_store.conn.execute(
        "SELECT canonical_name, role, email_address FROM message_entity_index WHERE canonical_name = 'Example Contact' ORDER BY email_address"
    ).fetchall()
    assert entity_side_links == [
        ("Example Contact", "from", "contact@example.test"),
        ("Example Contact", "from", "contact@unit-a.test"),
    ]

    store.close()


def test_retrieval_and_promotion_include_companion_entity_database(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize(start_date="2022-01-02")
    ingest_envelopes(
        store=store,
        client=FakeEntityClient(),
        account_name="primary-account",
        email_address="user@example.test",
        folder_name="INBOX",
    )

    retrieval = EmailRetrievalService(store).search("contact")
    assert retrieval["people"]
    assert len(retrieval["people"]) == 2
    assert all(item["canonical_name"] == "Example Contact" for item in retrieval["people"])
    emails = sorted(email for item in retrieval["people"] for email in item["emails"])
    assert emails == ["contact@example.test", "contact@unit-a.test"]

    promotions = EmailPromotionService(store).select_promotions(limit=10)
    assert any(item["source_object_type"] == "person" for item in promotions)
    person_item = next(item for item in promotions if item["source_object_type"] == "person")
    assert "Example Contact" in person_item["promoted_text"]
    assert person_item["fact_store_payload"]["category"] == "general"

    store.close()

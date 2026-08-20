from pathlib import Path
import subprocess

from email_memory_store.config import EmailMemoryPaths
from email_memory_store.himalaya import HimalayaClient, HimalayaEnvelope, parse_folder_list_output
from email_memory_store.identity import build_stable_message_id
from email_memory_store.ingestion import ingest_envelopes
from email_memory_store.store import EmailMemoryStore


class FakeHimalayaClient:
    def __init__(self, envelopes):
        self._envelopes = envelopes

    def list_envelopes(self, account: str, folder: str = "INBOX", page: int = 1, page_size: int = 100):
        return list(self._envelopes)


def test_parse_folder_list_output_reads_plain_himalaya_table():
    output = """| NAME   | DESC                    |
|--------|-------------------------|
| INBOX  | \\Marked, \\HasNoChildren |
| projects | \\HasNoChildren          |
"""
    folders = parse_folder_list_output(output)
    assert folders == ["INBOX", "projects"]


def test_parse_folder_list_output_preserves_nested_folder_names():
    output = """| NAME               | DESC           |
|--------------------|----------------|
| Archive            | \\HasChildren   |
| Archive/Legacy     | \\HasNoChildren |
| Schedules/Reminders | \\HasNoChildren |
"""
    folders = parse_folder_list_output(output)
    assert folders == ["Archive", "Archive/Legacy", "Schedules/Reminders"]


def test_himalaya_client_retries_transient_folder_list_failures(monkeypatch):
    calls = {'count': 0}

    def fake_run(args, text, capture_output, check, timeout):
        calls['count'] += 1
        if calls['count'] == 1:
            raise subprocess.CalledProcessError(1, args, stderr='transient failure')

        class Result:
            stdout = "| NAME | DESC |\n|------|------|\n| INBOX | \\HasNoChildren |\n| Archive/Legacy | \\HasNoChildren |\n"

        return Result()

    monkeypatch.setattr(subprocess, 'run', fake_run)
    client = HimalayaClient(retries=2, retry_delay=0)
    folders = client.list_folders('primary-account')
    assert folders == ['INBOX', 'Archive/Legacy']
    assert calls['count'] == 2


def test_build_stable_message_id_prefers_internet_message_id():
    envelope = HimalayaEnvelope(
        message_id="123",
        subject="Subject",
        from_addr="person@example.test",
        from_name="Person",
        to_addrs=["user@example.test"],
        date="2026-04-03 19:09+00:00",
        has_attachment=False,
        flags=[],
        internet_message_id="<abc@example.test>",
    )
    stable_id = build_stable_message_id(account_name="primary-account", folder_name="INBOX", envelope=envelope)
    assert stable_id == "rfc822:abc@example.test"


def test_build_stable_message_id_falls_back_to_deterministic_hash():
    envelope = HimalayaEnvelope(
        message_id="123",
        subject="Subject",
        from_addr="person@example.test",
        from_name="Person",
        to_addrs=["user@example.test"],
        date="2026-04-03 19:09+00:00",
        has_attachment=False,
        flags=[],
        internet_message_id=None,
    )
    stable_id = build_stable_message_id(account_name="primary-account", folder_name="INBOX", envelope=envelope)
    assert stable_id.startswith("provisional:")


def test_build_stable_message_id_fallback_is_folder_independent():
    envelope = HimalayaEnvelope(
        message_id="123",
        subject="Subject",
        from_addr="person@example.test",
        from_name="Person",
        to_addrs=["user@example.test"],
        date="2026-04-03 19:09+00:00",
        has_attachment=False,
        flags=[],
        internet_message_id=None,
    )
    inbox_id = build_stable_message_id(account_name="primary-account", folder_name="INBOX", envelope=envelope)
    archive_id = build_stable_message_id(account_name="primary-account", folder_name="Archive", envelope=envelope)
    assert inbox_id == archive_id
    assert inbox_id.startswith("provisional:")


def test_paths_support_optional_dev_shm_workhorse_root(tmp_path: Path):
    paths = EmailMemoryPaths.from_root(tmp_path / "durable", work_root=Path("/dev/shm/email-memory-store-work"))
    assert paths.db_path == tmp_path / "durable" / "email_memory.duckdb"
    assert paths.work_db_path == Path("/dev/shm/email-memory-store-work") / "email_memory.work.duckdb"


def test_ingest_envelopes_persists_accounts_folders_messages_threads_and_contacts(tmp_path: Path):
    store = EmailMemoryStore(tmp_path / "email_memory")
    store.initialize()

    client = FakeHimalayaClient(
        envelopes=[
            HimalayaEnvelope(
                message_id="266899",
                subject="Sample visitor lot closed next week",
                from_addr="sender@example.test",
                from_name="Example Contact",
                to_addrs=["team-list@example.test"],
                date="2026-04-03 19:09+00:00",
                has_attachment=False,
                flags=[],
                internet_message_id="<msg-1@example.test>",
            )
        ]
    )

    summary = ingest_envelopes(
        store=store,
        client=client,
        account_name="primary-account",
        email_address="user@example.test",
        folder_name="INBOX",
        folder_type="inbox",
    )

    assert summary["messages_added"] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 1

    row = store.conn.execute("SELECT stable_message_id, thread_key, direction FROM messages").fetchone()
    assert row[0] == "rfc822:msg-1@example.test"
    assert row[1].startswith("thread:")
    assert row[2] == "incoming"

    store.close()

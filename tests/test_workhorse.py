from pathlib import Path

from email_memory_store.store import EmailMemoryStore


def test_use_work_db_activates_workhorse_database_path(tmp_path: Path):
    durable = tmp_path / "durable"
    work = tmp_path / "workhorse"
    store = EmailMemoryStore(durable, work_root=work, use_work_db=True)
    store.initialize()

    assert store.active_db_path == work / "email_memory.work.duckdb"
    assert store.active_db_path.exists()
    assert store.paths.db_path == durable / "email_memory.duckdb"

    store.close()


def test_checkpoint_to_durable_copies_workhorse_database_contents(tmp_path: Path):
    durable = tmp_path / "durable"
    work = tmp_path / "workhorse"
    store = EmailMemoryStore(durable, work_root=work, use_work_db=True)
    store.initialize()

    account_id = store.ensure_account("primary-account", "user@example.test", "office365")
    assert account_id == 1

    store.checkpoint_to_durable()
    store.close()

    durable_store = EmailMemoryStore(durable)
    durable_store.initialize()
    assert durable_store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    durable_store.close()

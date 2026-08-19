from __future__ import annotations

import json
from pathlib import Path
import sys
import types
from unittest.mock import patch

import pytest

from email_memory_store.extraction.service import (
    ExtractionService,
    _parse_extraction_response,
    _strip_markdown_fences,
)
from email_memory_store.promotion.llm import LLMProviderSpec
from email_memory_store.store import EmailMemoryStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CANNED_LLM_JSON = {
    "action_items": [
        {
            "owner": "alice@example.test",
            "action_text": "Send the quarterly report to the board",
            "due_date": "2026-05-01",
            "confidence": 0.9,
        }
    ],
    "deadlines": [
        {
            "label": "Report submission",
            "due_date": "2026-05-01",
            "related_project": "Q1 Review",
            "confidence": 0.85,
        }
    ],
    "decisions": [
        {
            "title": "Adopt new format",
            "decision_text": "The team agreed to adopt the new reporting format for Q2.",
            "decided_by": ["alice@example.test", "bob@example.test"],
            "decided_at": "2026-04-15",
            "confidence": 0.95,
        }
    ],
    "summary": "The team discussed Q1 reporting requirements and agreed on a new format.",
    "key_points": ["New reporting format adopted", "Report due May 1st"],
}

SPEC = LLMProviderSpec(name='hermes-default', model=None)


def _make_store(tmp_path: Path) -> EmailMemoryStore:
    store = EmailMemoryStore(tmp_path / 'email_memory')
    store.initialize()
    return store


def _seed_thread(store: EmailMemoryStore, *, with_body: bool = True) -> tuple[int, str, int]:
    """Seed one account/folder/thread/message. Returns (thread_id, thread_key, message_pk)."""
    account_id = store.ensure_account('test', 'user@example.test', 'office365')
    folder_id = store.ensure_folder(account_id, 'INBOX', 'inbox')
    thread_key = 'thread:test-1'
    store.ensure_thread(account_id, thread_key, 'Test thread subject')
    message_pk, _ = store.upsert_message_stub(
        account_id=account_id,
        folder_id=folder_id,
        stable_message_id='rfc822:msg-extract-1@example.test',
        internet_message_id='<msg-extract-1@example.test>',
        thread_key=thread_key,
        subject='Test thread subject',
        normalized_subject='test thread subject',
        from_name='Alice',
        from_addr='alice@example.test',
        to_addrs=['user@example.test'],
        sent_at=None,
        received_at=None,
        has_attachments=False,
        direction='incoming',
        is_read=False,
    )
    if with_body:
        store.update_message_content(
            message_pk=message_pk,
            cleaned_text='Alice asked Bob to send the quarterly report by May 1st.',
        )
    # Fetch the thread_id from the DB
    row = store.conn.execute(
        "SELECT thread_id FROM threads WHERE thread_key = ?", [thread_key]
    ).fetchone()
    thread_id = int(row[0])
    return thread_id, thread_key, message_pk


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------

def test_strip_markdown_fences_removes_json_fences():
    raw = '```json\n{"foo": 1}\n```'
    assert _strip_markdown_fences(raw) == '{"foo": 1}'


def test_strip_markdown_fences_removes_plain_fences():
    raw = '```\n{"foo": 1}\n```'
    assert _strip_markdown_fences(raw) == '{"foo": 1}'


def test_strip_markdown_fences_leaves_plain_json_unchanged():
    raw = '{"foo": 1}'
    assert _strip_markdown_fences(raw) == '{"foo": 1}'


def test_parse_extraction_response_handles_plain_json():
    raw = json.dumps(CANNED_LLM_JSON)
    result = _parse_extraction_response(raw)
    assert result['summary'] == CANNED_LLM_JSON['summary']
    assert len(result['action_items']) == 1


def test_parse_extraction_response_handles_markdown_fenced_json():
    raw = '```json\n' + json.dumps(CANNED_LLM_JSON) + '\n```'
    result = _parse_extraction_response(raw)
    assert result['summary'] == CANNED_LLM_JSON['summary']


def test_parse_extraction_response_handles_json_with_leading_text():
    raw = 'Here is the extraction:\n' + json.dumps(CANNED_LLM_JSON)
    result = _parse_extraction_response(raw)
    assert 'action_items' in result


def test_parse_extraction_response_raises_on_garbage():
    with pytest.raises(ValueError):
        _parse_extraction_response('this is not json at all!!!')


# ---------------------------------------------------------------------------
# ExtractionService.get_unextracted_threads
# ---------------------------------------------------------------------------

def test_get_unextracted_threads_returns_threads_with_bodies(tmp_path):
    store = _make_store(tmp_path)
    thread_id, thread_key, message_pk = _seed_thread(store, with_body=True)

    service = ExtractionService(store)
    threads = service.get_unextracted_threads(limit=50)

    assert len(threads) == 1
    assert threads[0]['thread_id'] == thread_id
    assert threads[0]['thread_key'] == thread_key
    assert len(threads[0]['messages']) == 1
    assert 'quarterly report' in threads[0]['messages'][0]['cleaned_text']

    store.close()


def test_get_unextracted_threads_excludes_messages_without_body(tmp_path):
    store = _make_store(tmp_path)
    _seed_thread(store, with_body=False)

    service = ExtractionService(store)
    threads = service.get_unextracted_threads(limit=50)

    assert len(threads) == 0

    store.close()


def test_get_unextracted_threads_excludes_already_extracted(tmp_path):
    store = _make_store(tmp_path)
    thread_id, _, _ = _seed_thread(store, with_body=True)

    # Manually mark as extracted
    store.conn.execute(
        """
        INSERT INTO thread_summaries (thread_id, summary_type, summary_text, source_message_count)
        VALUES (?, 'extraction', 'Already done.', 1)
        """,
        [thread_id],
    )

    service = ExtractionService(store)
    threads = service.get_unextracted_threads(limit=50)

    assert len(threads) == 0

    store.close()


# ---------------------------------------------------------------------------
# ExtractionService.persist_extraction
# ---------------------------------------------------------------------------

def test_persist_extraction_writes_to_all_four_tables(tmp_path):
    store = _make_store(tmp_path)
    thread_id, _, message_pk = _seed_thread(store, with_body=True)

    service = ExtractionService(store)
    service.persist_extraction(
        thread_id=thread_id,
        result=CANNED_LLM_JSON,
        message_pks=[message_pk],
    )

    # thread_summaries
    row = store.conn.execute(
        "SELECT summary_text, source_message_count FROM thread_summaries WHERE thread_id = ?",
        [thread_id],
    ).fetchone()
    assert row is not None
    assert 'Q1 reporting' in row[0]
    assert row[1] == 1

    # action_items
    ai_rows = store.conn.execute(
        "SELECT owner, action_text, confidence FROM action_items WHERE thread_id = ?",
        [thread_id],
    ).fetchall()
    assert len(ai_rows) == 1
    assert ai_rows[0][0] == 'alice@example.test'
    assert 'quarterly report' in ai_rows[0][1]

    # deadlines
    dl_rows = store.conn.execute(
        "SELECT label, related_project FROM deadlines WHERE thread_id = ?",
        [thread_id],
    ).fetchall()
    assert len(dl_rows) == 1
    assert dl_rows[0][0] == 'Report submission'
    assert dl_rows[0][1] == 'Q1 Review'

    # decisions
    dec_rows = store.conn.execute(
        "SELECT title, decision_text FROM decisions WHERE thread_id = ?",
        [thread_id],
    ).fetchall()
    assert len(dec_rows) == 1
    assert dec_rows[0][0] == 'Adopt new format'
    assert 'new reporting format' in dec_rows[0][1]

    store.close()


def test_persist_extraction_skips_low_confidence_items(tmp_path):
    store = _make_store(tmp_path)
    thread_id, _, message_pk = _seed_thread(store, with_body=True)

    low_conf_result = {
        "action_items": [
            {"owner": None, "action_text": "Maybe do something", "due_date": None, "confidence": 0.3}
        ],
        "deadlines": [
            {"label": "Unclear deadline", "due_date": None, "related_project": None, "confidence": 0.4}
        ],
        "decisions": [
            {"title": "Maybe decided", "decision_text": "Possibly agreed.", "decided_by": [], "decided_at": None, "confidence": 0.2}
        ],
        "summary": "Not much happened.",
        "key_points": [],
    }

    service = ExtractionService(store)
    service.persist_extraction(
        thread_id=thread_id,
        result=low_conf_result,
        message_pks=[message_pk],
    )

    assert store.conn.execute("SELECT COUNT(*) FROM action_items WHERE thread_id = ?", [thread_id]).fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM deadlines WHERE thread_id = ?", [thread_id]).fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM decisions WHERE thread_id = ?", [thread_id]).fetchone()[0] == 0
    # But summary should still be written
    assert store.conn.execute("SELECT COUNT(*) FROM thread_summaries WHERE thread_id = ?", [thread_id]).fetchone()[0] == 1

    store.close()


def test_persist_extraction_is_idempotent(tmp_path):
    """Re-persisting replaces the previous extraction without duplicating rows."""
    store = _make_store(tmp_path)
    thread_id, _, message_pk = _seed_thread(store, with_body=True)

    service = ExtractionService(store)
    service.persist_extraction(
        thread_id=thread_id,
        result=CANNED_LLM_JSON,
        message_pks=[message_pk],
    )
    service.persist_extraction(
        thread_id=thread_id,
        result=CANNED_LLM_JSON,
        message_pks=[message_pk],
    )

    assert store.conn.execute("SELECT COUNT(*) FROM thread_summaries WHERE thread_id = ?", [thread_id]).fetchone()[0] == 1

    store.close()


# ---------------------------------------------------------------------------
# ExtractionService.run_extraction end-to-end with mocked LLM
# ---------------------------------------------------------------------------

def test_run_extraction_end_to_end_with_mocked_llm(tmp_path):
    store = _make_store(tmp_path)
    thread_id, _, message_pk = _seed_thread(store, with_body=True)

    canned_output = json.dumps(CANNED_LLM_JSON)

    service = ExtractionService(store)
    with patch('email_memory_store.extraction.service.run_llm_prompt', return_value=canned_output):
        stats = service.run_extraction(limit=20, spec=SPEC)

    assert stats['threads_processed'] == 1
    assert stats['action_items_added'] == 1
    assert stats['deadlines_added'] == 1
    assert stats['decisions_added'] == 1
    assert stats['errors'] == 0

    # Verify data in DB
    assert store.conn.execute("SELECT COUNT(*) FROM thread_summaries").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM action_items").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM deadlines").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1

    store.close()


def test_run_extraction_skips_already_extracted_threads(tmp_path):
    store = _make_store(tmp_path)
    thread_id, _, message_pk = _seed_thread(store, with_body=True)

    canned_output = json.dumps(CANNED_LLM_JSON)

    service = ExtractionService(store)
    with patch('email_memory_store.extraction.service.run_llm_prompt', return_value=canned_output):
        service.run_extraction(limit=20, spec=SPEC)

    # Run again — thread is now extracted
    with patch('email_memory_store.extraction.service.run_llm_prompt', return_value=canned_output) as mock_llm:
        stats2 = service.run_extraction(limit=20, spec=SPEC)
        mock_llm.assert_not_called()

    assert stats2['threads_processed'] == 0

    store.close()


def test_run_extraction_handles_llm_errors_gracefully(tmp_path):
    store = _make_store(tmp_path)
    _seed_thread(store, with_body=True)

    service = ExtractionService(store)
    with patch(
        'email_memory_store.extraction.service.run_llm_prompt',
        side_effect=RuntimeError('LLM unavailable'),
    ):
        stats = service.run_extraction(limit=20, spec=SPEC)

    assert stats['threads_processed'] == 0
    assert stats['errors'] == 1
    # Nothing should be written
    assert store.conn.execute("SELECT COUNT(*) FROM thread_summaries").fetchone()[0] == 0

    store.close()


def test_run_extraction_handles_malformed_llm_response(tmp_path):
    store = _make_store(tmp_path)
    _seed_thread(store, with_body=True)

    service = ExtractionService(store)
    with patch(
        'email_memory_store.extraction.service.run_llm_prompt',
        return_value='this is not valid json',
    ):
        stats = service.run_extraction(limit=20, spec=SPEC)

    assert stats['threads_processed'] == 0
    assert stats['errors'] == 1

    store.close()


def test_run_extraction_with_empty_result(tmp_path):
    """LLM returns empty lists — thread should still be marked as extracted."""
    store = _make_store(tmp_path)
    thread_id, _, message_pk = _seed_thread(store, with_body=True)

    empty_result = {
        "action_items": [],
        "deadlines": [],
        "decisions": [],
        "summary": "Nothing notable in this thread.",
        "key_points": [],
    }
    canned_output = json.dumps(empty_result)

    service = ExtractionService(store)
    with patch('email_memory_store.extraction.service.run_llm_prompt', return_value=canned_output):
        stats = service.run_extraction(limit=20, spec=SPEC)

    assert stats['threads_processed'] == 1
    assert stats['action_items_added'] == 0
    assert stats['deadlines_added'] == 0
    assert stats['decisions_added'] == 0
    # Summary should still be written
    assert store.conn.execute("SELECT COUNT(*) FROM thread_summaries WHERE thread_id = ?", [thread_id]).fetchone()[0] == 1

    store.close()


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


def _stub_cli_retrieval_dependencies() -> None:
    answerer_module = types.ModuleType("email_memory_store.retrieval.answerer")
    embedder_module = types.ModuleType("email_memory_store.retrieval.embedder")
    vector_store_module = types.ModuleType("email_memory_store.retrieval.vector_store")

    class _StubDefaultDependency:
        pass

    def _unexpected_default_dependency(*_args, **_kwargs):
        raise AssertionError("tests must inject retrieval dependencies")

    answerer_module.Answerer = _StubDefaultDependency
    embedder_module.Embedder = _StubDefaultDependency
    embedder_module.get_default_embedder = _unexpected_default_dependency
    vector_store_module.COLLECTION_NAMES = (
        "holographic_facts",
        "action_items",
        "deadlines",
        "decisions",
        "thread_summaries",
        "message_chunks",
    )
    vector_store_module.VectorStore = _StubDefaultDependency
    vector_store_module.get_default_store = _unexpected_default_dependency

    sys.modules.pop("email_memory_store.cli", None)
    sys.modules["email_memory_store.retrieval.answerer"] = answerer_module
    sys.modules["email_memory_store.retrieval.embedder"] = embedder_module
    sys.modules["email_memory_store.retrieval.vector_store"] = vector_store_module




def test_cli_extract_threads_uses_persisted_promotion_llm_config(tmp_path, monkeypatch):
    _stub_cli_retrieval_dependencies()
    from email_memory_store import cli

    root = tmp_path / 'email_memory'
    store = EmailMemoryStore(root)
    store.initialize()
    _seed_thread(store, with_body=True)
    store.set_promotion_llm_config({
        'provider': {'name': 'codex-cli', 'model': 'gpt-5-codex'},
    })
    legacy_config_path = root / 'config' / 'llm_config.json'
    legacy_config_path.write_text(json.dumps({
        'provider': {'name': 'hermes-default', 'model': 'gemma-4'}
    }), encoding='utf-8')
    store.close()

    captured: dict[str, object] = {}

    class FakeExtractionService:
        def __init__(self, store):
            pass

        def run_extraction(self, *, limit, spec):
            captured['limit'] = limit
            captured['spec'] = spec
            return {'threads_processed': 1}

    monkeypatch.setattr(cli, 'ExtractionService', FakeExtractionService)
    monkeypatch.setattr(cli, '_maybe_checkpoint', lambda store, args: None)

    parser = cli.build_parser()
    args = parser.parse_args(['--root', str(root), 'extract-threads', '--limit', '5'])
    cli.cmd_extract_threads(args)

    assert captured['limit'] == 5
    spec = captured['spec']
    assert isinstance(spec, LLMProviderSpec)
    assert spec.name == 'codex-cli'
    assert spec.model == 'gpt-5-codex'


def test_cli_extract_threads_command(tmp_path):
    _stub_cli_retrieval_dependencies()
    from email_memory_store.cli import build_parser

    store = _make_store(tmp_path)
    _seed_thread(store, with_body=True)
    store.close()

    parser = build_parser()
    args = parser.parse_args(['--root', str(tmp_path / 'email_memory'), 'extract-threads', '--limit', '5'])
    assert args.limit == 5
    assert args.handler.__name__ == 'cmd_extract_threads'


def test_cli_extraction_status_command(tmp_path):
    from email_memory_store.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(['--root', str(tmp_path / 'email_memory'), 'extraction-status'])
    assert args.handler.__name__ == 'cmd_extraction_status'


def test_cli_extraction_status_counts_only_extractable_pending_threads(tmp_path, capsys):
    from email_memory_store.cli import build_parser

    store = _make_store(tmp_path)
    _seed_thread(store, with_body=False)
    store.close()

    parser = build_parser()
    args = parser.parse_args(['--root', str(tmp_path / 'email_memory'), 'extraction-status'])
    args.handler(args)

    status = json.loads(capsys.readouterr().out)
    assert status['threads_total'] == 1
    assert status['threads_extracted'] == 0
    assert status['threads_pending'] == 0

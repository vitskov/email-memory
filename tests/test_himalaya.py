import subprocess

from email_memory_store.himalaya import HimalayaClient, _decode_command_output, parse_folder_list_output


def test_decode_command_output_accepts_utf8_and_latin1_bytes():
    assert _decode_command_output('plain text'.encode('utf-8')) == 'plain text'
    assert _decode_command_output(b'caf\xe9') == 'café'


def test_parse_folder_list_output_ignores_headers_and_returns_names():
    output = """
| NAME | FLAGS |
|------|-------|
| INBOX | inbox |
| Archive/Subfolder | custom |
"""
    assert parse_folder_list_output(output) == ['INBOX', 'Archive/Subfolder']


def test_remove_flags_uses_himalaya_flag_remove(monkeypatch):
    calls = []

    def fake_run(args, *, text, capture_output, check, timeout):
        calls.append(args)
        assert timeout == 60.0
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b'', stderr=b'')

    monkeypatch.setattr(subprocess, 'run', fake_run)

    client = HimalayaClient(binary='himalaya')
    client.remove_flags(
        account='primary-account',
        folder='INBOX',
        message_ids=['123'],
        flags=['seen'],
    )

    assert calls == [[
        'himalaya',
        'flag',
        'remove',
        '-a',
        'primary-account',
        '-f',
        'INBOX',
        '123',
        'seen',
    ]]


# ---------------------------------------------------------------------------
# error classification and retry policy
# ---------------------------------------------------------------------------

OUT_OF_BOUNDS_STDERR = (
    b"Error: \n   0: cannot list imap envelopes: page 2 out of bounds\n"
)
TRANSIENT_STDERR = (
    b"Error: \n   0: cannot select IMAP mailbox\n   1: cannot resolve IMAP task\n"
)


def _error(stderr: bytes) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, ['himalaya'], output=b'', stderr=stderr)


def test_himalaya_stderr_surfaces_text_calledprocesserror_omits():
    from email_memory_store.himalaya import himalaya_stderr

    exc = _error(OUT_OF_BOUNDS_STDERR)
    # The exception's own message carries the command but never the reason.
    assert 'out of bounds' not in str(exc)
    assert 'page 2 out of bounds' in himalaya_stderr(exc)


def test_himalaya_stderr_handles_missing_stderr():
    from email_memory_store.himalaya import himalaya_stderr

    assert himalaya_stderr(subprocess.CalledProcessError(1, ['himalaya'])) == ''


def test_error_classification_distinguishes_permanent_from_transient():
    from email_memory_store.himalaya import is_page_out_of_bounds, is_permanent_himalaya_error

    out_of_bounds = _error(OUT_OF_BOUNDS_STDERR)
    transient = _error(TRANSIENT_STDERR)

    assert is_permanent_himalaya_error(out_of_bounds) is True
    assert is_page_out_of_bounds(out_of_bounds) is True

    assert is_permanent_himalaya_error(transient) is False
    assert is_page_out_of_bounds(transient) is False


def test_run_does_not_retry_permanent_errors(monkeypatch):
    """Paging past the end of a folder is the answer, not a failure to retry."""
    attempts = []
    sleeps = []

    def fake_run(args, *, text, capture_output, check, timeout):
        attempts.append(args)
        raise _error(OUT_OF_BOUNDS_STDERR)

    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setattr('email_memory_store.himalaya.time.sleep', lambda s: sleeps.append(s))

    client = HimalayaClient(binary='himalaya', retries=4, retry_delay=2.0)
    try:
        client.list_envelopes(account='primary-account', folder='sample-folder', page=2)
    except subprocess.CalledProcessError:
        pass
    else:
        raise AssertionError('expected the permanent error to propagate')

    assert len(attempts) == 1
    assert sleeps == []


def test_run_retries_transient_errors_with_exponential_backoff(monkeypatch):
    attempts = []
    sleeps = []

    def fake_run(args, *, text, capture_output, check, timeout):
        attempts.append(args)
        if len(attempts) < 3:
            raise _error(TRANSIENT_STDERR)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b'[]', stderr=b'')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setattr('email_memory_store.himalaya.time.sleep', lambda s: sleeps.append(s))

    client = HimalayaClient(binary='himalaya', retries=4, retry_delay=2.0)
    assert client.list_envelopes(account='primary-account', folder='INBOX', page=1) == []

    assert len(attempts) == 3
    # Provider throttling lasts minutes; a flat short delay burns every attempt
    # inside one window.
    assert sleeps == [2.0, 4.0]


def test_run_backoff_is_capped(monkeypatch):
    sleeps = []

    def fake_run(args, *, text, capture_output, check, timeout):
        raise _error(TRANSIENT_STDERR)

    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setattr('email_memory_store.himalaya.time.sleep', lambda s: sleeps.append(s))

    client = HimalayaClient(binary='himalaya', retries=6, retry_delay=10.0, max_retry_delay=30.0)
    try:
        client.list_envelopes(account='primary-account', folder='INBOX', page=1)
    except subprocess.CalledProcessError:
        pass

    assert sleeps == [10.0, 20.0, 30.0, 30.0, 30.0]


def test_run_converts_timeouts_to_retryable_provider_failures(monkeypatch):
    attempts = []
    sleeps = []

    def fake_run(args, *, text, capture_output, check, timeout):
        attempts.append((args, timeout))
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setattr('email_memory_store.himalaya.time.sleep', lambda s: sleeps.append(s))

    client = HimalayaClient(binary='himalaya', retries=2, retry_delay=3.0, command_timeout_seconds=11.0)
    import pytest

    with pytest.raises(subprocess.CalledProcessError, match='returned non-zero exit status 124') as exc_info:
        client.list_envelopes(account='primary-account')

    assert 'timed out after 11 seconds' in str(exc_info.value.stderr)
    assert [timeout for _, timeout in attempts] == [11.0]
    assert sleeps == []


def test_himalaya_client_rejects_non_positive_command_timeout():
    import pytest

    with pytest.raises(ValueError, match='must be positive'):
        HimalayaClient(command_timeout_seconds=0)

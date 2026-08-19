from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class HimalayaEnvelope:
    message_id: str
    subject: str
    from_addr: str
    from_name: str | None
    to_addrs: list[str]
    date: str | None
    has_attachment: bool
    flags: list[str]
    internet_message_id: str | None = None

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> 'HimalayaEnvelope':
        return cls(
            message_id=str(payload.get('id', '')),
            subject=payload.get('subject', '') or '',
            from_addr=((payload.get('from') or {}).get('addr') or '').lower(),
            from_name=(payload.get('from') or {}).get('name'),
            to_addrs=[(item.get('addr') or '').lower() for item in _coerce_addr_list(payload.get('to'))],
            date=payload.get('date'),
            has_attachment=bool(payload.get('has_attachment', False)),
            flags=list(payload.get('flags') or []),
            internet_message_id=payload.get('message_id') or payload.get('internet_message_id'),
        )


def _coerce_addr_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def parse_folder_list_output(output: str) -> list[str]:
    folders: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith('|'):
            continue
        parts = [part.strip() for part in line.strip('|').split('|')]
        if len(parts) < 2:
            continue
        name = parts[0]
        if name in {'NAME', '------'} or set(name) <= {'-'}:
            continue
        if name:
            folders.append(name)
    return folders


def _decode_command_output(data: bytes | str) -> str:
    if isinstance(data, str):
        return data
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return data.decode('latin-1')


def himalaya_stderr(exc: subprocess.CalledProcessError) -> str:
    """Return the stderr text carried by a failed himalaya command.

    ``CalledProcessError``'s own message omits stderr entirely, which is why
    ingestion tracebacks historically recorded the failing command but never
    the reason for it.
    """
    return _decode_command_output(exc.stderr or b'').strip()


#: Substrings that mark a himalaya failure as permanent for the given
#: arguments. Retrying these only burns wall-clock: the answer will not change
#: until the request itself does.
_PERMANENT_ERROR_MARKERS: tuple[str, ...] = (
    'out of bounds',
    "doesn't exist",
    'does not exist',
    'cannot find maildir',
)


def is_permanent_himalaya_error(exc: subprocess.CalledProcessError) -> bool:
    """True when retrying the same himalaya command cannot succeed.

    Paging past the end of a mailbox is the common case: himalaya exits 1 with
    "page N out of bounds" rather than returning an empty list.
    """
    text = himalaya_stderr(exc).lower()
    return any(marker in text for marker in _PERMANENT_ERROR_MARKERS)


def is_page_out_of_bounds(exc: subprocess.CalledProcessError) -> bool:
    """True when the failure means "there is no such page" — i.e. end of folder.

    A folder whose message count is an exact multiple of the page size returns
    a full final page, so the caller cannot tell it has finished without asking
    for one more page and getting this back.
    """
    return 'out of bounds' in himalaya_stderr(exc).lower()


class HimalayaClient:
    def __init__(
        self,
        binary: str = 'himalaya',
        retries: int = 4,
        retry_delay: float = 2.0,
        max_retry_delay: float = 30.0,
    ):
        self.binary = binary
        self.retries = max(1, retries)
        self.retry_delay = max(0.0, retry_delay)
        self.max_retry_delay = max(self.retry_delay, max_retry_delay)

    def _sleep_for_attempt(self, attempt: int) -> None:
        """Exponential backoff, capped.

        Observed IMAP throttling from the mail provider persists for minutes,
        not milliseconds, so a fixed short delay exhausts every attempt inside
        a single throttle window.
        """
        delay = min(self.retry_delay * (2 ** (attempt - 1)), self.max_retry_delay)
        time.sleep(delay)

    def _run(self, args: list[str]) -> str:
        last_error: subprocess.CalledProcessError | None = None
        for attempt in range(1, self.retries + 1):
            try:
                result = subprocess.run(
                    [self.binary] + args,
                    text=False,
                    capture_output=True,
                    check=True,
                )
                return _decode_command_output(result.stdout)
            except subprocess.CalledProcessError as exc:
                last_error = exc
                if is_permanent_himalaya_error(exc) or attempt >= self.retries:
                    raise
                self._sleep_for_attempt(attempt)
        if last_error is not None:
            raise last_error
        raise RuntimeError('unreachable himalaya command runner state')

    def list_folders(self, account: str) -> list[str]:
        return parse_folder_list_output(self._run(['folder', 'list', '-a', account]))

    def list_envelopes(
        self,
        account: str,
        folder: str = 'INBOX',
        page: int = 1,
        page_size: int = 100,
    ) -> list[HimalayaEnvelope]:
        output = self._run([
            'envelope', 'list',
            '-a', account,
            '-f', folder,
            '-p', str(page),
            '-s', str(page_size),
            '-o', 'json',
        ])
        payload = json.loads(output)
        return [HimalayaEnvelope.from_json(item) for item in payload]

    def export_message(self, account: str, message_id: str, folder: str = 'INBOX', full: bool = True) -> str:
        args = ['message', 'export', '-a', account, '-f', folder, message_id]
        if full:
            args.append('--full')
        return self._run(args)

    def remove_flags(
        self,
        *,
        account: str,
        folder: str,
        message_ids: list[str],
        flags: list[str],
    ) -> str:
        if not message_ids or not flags:
            return ''
        return self._run(['flag', 'remove', '-a', account, '-f', folder, *message_ids, *flags])

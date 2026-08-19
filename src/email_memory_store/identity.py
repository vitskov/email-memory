from __future__ import annotations

import hashlib
import re

from .himalaya import HimalayaEnvelope

_RE_PREFIX = re.compile(r'^(?:re|fw|fwd)\s*:\s*', re.IGNORECASE)
_RE_SPACE = re.compile(r'\s+')


def normalize_subject(subject: str) -> str:
    normalized = subject.strip()
    while True:
        updated = _RE_PREFIX.sub('', normalized).strip()
        if updated == normalized:
            break
        normalized = updated
    return _RE_SPACE.sub(' ', normalized).lower()


def _clean_message_id(raw: str) -> str:
    return raw.strip().strip('<>').strip()


def _normalize_address(value: str | None) -> str:
    return (value or '').strip().lower()


def _normalize_address_list(values: list[str] | tuple[str, ...]) -> list[str]:
    return sorted({item for item in (_normalize_address(value) for value in values) if item})


def normalize_body_text(cleaned_text: str) -> str:
    lines = [' '.join(line.split()) for line in cleaned_text.splitlines()]
    non_empty = [line for line in lines if line]
    return '\n'.join(non_empty).strip()


def build_body_fingerprint(cleaned_text: str) -> str:
    normalized = normalize_body_text(cleaned_text)
    return hashlib.sha512(normalized.encode('utf-8')).hexdigest()


def build_content_stable_message_id(
    *,
    normalized_subject: str,
    from_addr: str | None,
    to_addrs: list[str] | tuple[str, ...],
    body_fingerprint: str,
) -> str:
    material = '|'.join([
        normalized_subject,
        _normalize_address(from_addr),
        ','.join(_normalize_address_list(list(to_addrs))),
        body_fingerprint,
    ])
    digest = hashlib.sha512(material.encode('utf-8')).hexdigest()
    return f'content:{digest}'


def build_stable_message_id(account_name: str, folder_name: str, envelope: HimalayaEnvelope) -> str:
    del folder_name
    if envelope.internet_message_id:
        return f'rfc822:{_clean_message_id(envelope.internet_message_id)}'

    material = '|'.join([
        account_name.lower(),
        envelope.message_id,
    ])
    digest = hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]
    return f'provisional:{digest}'


def build_thread_key(account_name: str, envelope: HimalayaEnvelope) -> str:
    material = '|'.join([
        account_name.lower(),
        normalize_subject(envelope.subject),
        ','.join(sorted(addr.lower() for addr in envelope.to_addrs[:5])),
    ])
    digest = hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]
    return f'thread:{digest}'

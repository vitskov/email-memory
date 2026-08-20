#!/usr/bin/env python3
"""Fail closed when publishable files contain common private-data signals."""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import zipfile


MAX_CONTENT_BYTES = 16 * 1024 * 1024

_IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
}
_PRIVATE_DIRECTORY_NAMES = {
    "credentials",
    "indexes",
    "private",
    "raw",
    "reports",
    "runtime",
    "secrets",
    "state",
    "vectors",
}
_PRIVATE_FILE_SUFFIXES = {
    ".db",
    ".duckdb",
    ".eml",
    ".key",
    ".log",
    ".mbox",
    ".parquet",
    ".pem",
    ".sqlite",
    ".sqlite3",
    ".wal",
}
_SYNTHETIC_EMAIL_DOMAINS = {
    "example.com",
    "example.net",
    "example.org",
}
_SYNTHETIC_HOME_USERS = {"example", "person", "runner", "user"}
_CREDENTIAL_PLACEHOLDERS = {
    "changeme",
    "dummy",
    "example",
    "hunter2",
    "password",
    "placeholder",
    "redacted",
    "test",
}

_EMAIL_RE = re.compile(
    r"(?<![\w.+-])([A-Z0-9._%+-]+)@([A-Z0-9.-]+\.[A-Z]{2,63})(?![\w.-])",
    re.IGNORECASE,
)
_HOME_PATH_RE = re.compile(
    r"(?<![\w.-])/(?:home|Users)/(?P<user>[A-Za-z0-9._-]+)(?:/|\b)"
    r"|(?<![\w.-])/data\d*/homes/(?P<data_user>[A-Za-z0-9._-]+)(?:/|\b)"
)
_TELEGRAM_TARGET_RE = re.compile(r"\btelegram\s*:\s*[^\s,;\]})>'\"]+", re.IGNORECASE)
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"\b(?:api[_-]?key|access[_-]?token|bot[_-]?token|client[_-]?secret|"
    r"password|passwd|refresh[_-]?token)\b\s*[:=]\s*[\"']?"
    r"(?P<value>[^\s\"',;})\]]{6,})",
    re.IGNORECASE,
)
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|"
    r"sk-[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{20,})\b"
)
_PRIVATE_KEY_RE = re.compile(r"-{5}BEGIN [A-Z ]*PRIVATE KEY-{5}")
_BEARER_TOKEN_RE = re.compile(
    r"\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._~+/-]{16,}", re.IGNORECASE
)


@dataclass(frozen=True, order=True)
class Finding:
    location: str
    rule: str
    line: int | None = None

    def render(self) -> str:
        line = f":{self.line}" if self.line is not None else ""
        return f"{self.location}{line}: {self.rule}"


def _path_findings(location: str) -> list[Finding]:
    normalized = location.replace("\\", "/")
    path = PurePosixPath(normalized)
    findings: list[Finding] = []
    lowered_parts = {part.lower() for part in path.parts}

    if lowered_parts & _PRIVATE_DIRECTORY_NAMES:
        findings.append(Finding(location, "private-runtime-directory"))

    name = path.name.lower()
    if name == ".env" or name.startswith(".env."):
        findings.append(Finding(location, "environment-file"))
    if any(name.endswith(suffix) for suffix in _PRIVATE_FILE_SUFFIXES):
        findings.append(Finding(location, "private-runtime-file"))

    return findings


def _is_synthetic_email_domain(domain: str) -> bool:
    normalized = domain.lower().rstrip(".")
    return (
        normalized in _SYNTHETIC_EMAIL_DOMAINS
        or normalized.endswith(".test")
        or normalized.endswith(".example")
        or normalized.endswith(".invalid")
    )


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def scan_text(text: str, location: str) -> list[Finding]:
    """Return privacy findings without echoing matched private values."""
    findings: list[Finding] = []

    for match in _EMAIL_RE.finditer(text):
        if not _is_synthetic_email_domain(match.group(2)):
            findings.append(
                Finding(location, "non-synthetic-email", _line_number(text, match.start()))
            )

    for match in _HOME_PATH_RE.finditer(text):
        user = (match.group("user") or match.group("data_user")).lower()
        if user not in _SYNTHETIC_HOME_USERS:
            findings.append(
                Finding(location, "absolute-local-home-path", _line_number(text, match.start()))
            )

    for match in _TELEGRAM_TARGET_RE.finditer(text):
        findings.append(
            Finding(location, "telegram-routing-target", _line_number(text, match.start()))
        )

    for match in _CREDENTIAL_ASSIGNMENT_RE.finditer(text):
        value = match.group("value").lower()
        if value not in _CREDENTIAL_PLACEHOLDERS and not value.startswith(("example", "test")):
            findings.append(
                Finding(location, "credential-assignment", _line_number(text, match.start()))
            )

    for rule, pattern in (
        ("known-secret-token", _KNOWN_TOKEN_RE),
        ("private-key-material", _PRIVATE_KEY_RE),
        ("bearer-credential", _BEARER_TOKEN_RE),
    ):
        for match in pattern.finditer(text):
            findings.append(Finding(location, rule, _line_number(text, match.start())))

    return findings


def scan_bytes(data: bytes, location: str) -> list[Finding]:
    findings = _path_findings(location)
    if len(data) > MAX_CONTENT_BYTES:
        findings.append(Finding(location, "oversized-content-not-inspected"))
        return findings
    if b"\x00" in data[:8192]:
        return findings
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return findings
    findings.extend(scan_text(text, location))
    return findings


def _iter_directory_files(root: Path) -> Iterator[Path]:
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_root)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in _IGNORED_DIRECTORY_NAMES and not (current / name).is_symlink()
        )
        for file_name in sorted(file_names):
            path = current / file_name
            if not path.is_symlink():
                yield path


def _scan_zip(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    with zipfile.ZipFile(path) as archive:
        for member in sorted(archive.infolist(), key=lambda item: item.filename):
            if member.is_dir():
                continue
            location = f"{path}!{member.filename}"
            if member.file_size > MAX_CONTENT_BYTES:
                findings.extend(_path_findings(location))
                findings.append(Finding(location, "oversized-content-not-inspected"))
                continue
            findings.extend(scan_bytes(archive.read(member), location))
    return findings


def _scan_tar(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    with tarfile.open(path, mode="r:*") as archive:
        for member in sorted(archive.getmembers(), key=lambda item: item.name):
            if not member.isfile():
                continue
            location = f"{path}!{member.name}"
            if member.size > MAX_CONTENT_BYTES:
                findings.extend(_path_findings(location))
                findings.append(Finding(location, "oversized-content-not-inspected"))
                continue
            extracted = archive.extractfile(member)
            if extracted is not None:
                findings.extend(scan_bytes(extracted.read(), location))
    return findings


def scan_path(path: Path) -> list[Finding]:
    if path.is_dir():
        findings: list[Finding] = _path_findings(path.name)
        for child in _iter_directory_files(path):
            relative = child.relative_to(path).as_posix()
            findings.extend(scan_bytes(child.read_bytes(), relative))
        return findings
    if not path.is_file():
        raise FileNotFoundError(path)
    if zipfile.is_zipfile(path):
        return [*_path_findings(str(path)), *_scan_zip(path)]
    if tarfile.is_tarfile(path):
        return [*_path_findings(str(path)), *_scan_tar(path)]
    return scan_bytes(path.read_bytes(), str(path))


def _run_git(arguments: Sequence[str], *, input_data: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", *arguments],
        input=input_data,
        check=True,
        capture_output=True,
    )
    return completed.stdout


def scan_tracked_files() -> list[Finding]:
    findings: list[Finding] = []
    for raw_path in _run_git(["ls-files", "-z"]).split(b"\0"):
        if not raw_path:
            continue
        path = Path(raw_path.decode("utf-8", errors="surrogateescape"))
        if path.is_file() and not path.is_symlink():
            findings.extend(scan_bytes(path.read_bytes(), path.as_posix()))
    return findings


def scan_git_history() -> list[Finding]:
    """Scan every unique blob reachable from any local Git ref."""
    findings: list[Finding] = []
    object_lines = _run_git(["rev-list", "--objects", "--all"]).splitlines()
    seen: set[bytes] = set()
    for line in object_lines:
        object_id, separator, raw_path = line.partition(b" ")
        if not separator or object_id in seen:
            continue
        seen.add(object_id)
        if _run_git(["cat-file", "-t", object_id.decode("ascii")]).strip() != b"blob":
            continue
        display_path = raw_path.decode("utf-8", errors="replace")
        data = _run_git(["cat-file", "-p", object_id.decode("ascii")])
        findings.extend(scan_bytes(data, f"git:{object_id.decode('ascii')[:12]}:{display_path}"))
    return findings


def _deduplicate(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(set(findings))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan publishable source and package artifacts for generic privacy violations."
    )
    parser.add_argument("paths", nargs="*", type=Path, help="Files, directories, wheels, or archives")
    parser.add_argument("--tracked", action="store_true", help="Scan files tracked by the current Git repository")
    parser.add_argument(
        "--git-history",
        action="store_true",
        help="Scan all blobs reachable from local Git refs (requires a full clone)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.paths and not args.tracked and not args.git_history:
        parser.error("provide at least one path, --tracked, or --git-history")

    findings: list[Finding] = []
    try:
        for path in args.paths:
            findings.extend(scan_path(path))
        if args.tracked:
            findings.extend(scan_tracked_files())
        if args.git_history:
            findings.extend(scan_git_history())
    except (FileNotFoundError, OSError, subprocess.CalledProcessError, tarfile.TarError) as error:
        print(f"privacy gate error: {error}", file=sys.stderr)
        return 2

    unique_findings = _deduplicate(findings)
    if unique_findings:
        print(f"privacy gate failed with {len(unique_findings)} finding(s):", file=sys.stderr)
        for finding in unique_findings:
            print(f"- {finding.render()}", file=sys.stderr)
        return 1

    print("privacy gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

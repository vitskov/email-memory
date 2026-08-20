#!/usr/bin/env python3
"""Fail closed when publishable files contain common private-data signals."""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import zipfile


MAX_CONTENT_BYTES = 16 * 1024 * 1024
_TRUSTED_GIT_PATH = Path("/usr/bin/git")
_TRUSTED_ASKPASS_PATH = "/bin/false"
_GIT_CONFIG_OVERRIDES = (
    "core.fsmonitor=false",
    "core.hooksPath=/dev/null",
    "credential.helper=",
)

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
_IDENTIFIER_CHARACTER_RE = re.compile(r"[A-Za-z0-9_]")

LocalDenylist = tuple[re.Pattern[str], ...]


class LocalDenylistError(ValueError):
    """Raised when owner-only local denylist configuration is unsafe or invalid."""


class GitTrustError(RuntimeError):
    """Raised when the fixed Git execution boundary cannot be trusted."""


@dataclass(frozen=True, order=True)
class Finding:
    location: str
    rule: str
    line: int | None = None

    def render(self) -> str:
        line = f":{self.line}" if self.line is not None else ""
        return f"{self.location}{line}: {self.rule}"


def _redact_local_identifiers(value: str, local_denylist: LocalDenylist) -> str:
    for pattern in local_denylist:
        value = pattern.sub("[local-identifier]", value)
    return value


def _path_findings(
    location: str, *, local_denylist: LocalDenylist = ()
) -> list[Finding]:
    location = _redact_local_identifiers(location, local_denylist)
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


def _compile_local_rule(value: str) -> re.Pattern[str]:
    prefix = (
        r"(?<![A-Za-z0-9_])" if _IDENTIFIER_CHARACTER_RE.fullmatch(value[0]) else ""
    )
    suffix = (
        r"(?![A-Za-z0-9_])" if _IDENTIFIER_CHARACTER_RE.fullmatch(value[-1]) else ""
    )
    return re.compile(f"{prefix}{re.escape(value)}{suffix}", re.IGNORECASE)


def load_local_denylist(path: Path) -> LocalDenylist:
    """Load literal private identifiers from validated owner-only local storage."""
    try:
        file_status = path.lstat()
        parent_status = path.parent.lstat()
    except OSError as error:
        raise LocalDenylistError("local denylist is unavailable") from error

    if stat.S_ISLNK(file_status.st_mode) or not stat.S_ISREG(file_status.st_mode):
        raise LocalDenylistError("local denylist must be a regular non-symlink file")
    if file_status.st_uid != os.getuid():
        raise LocalDenylistError("local denylist must be owned by the current user")
    if stat.S_IMODE(file_status.st_mode) != 0o600:
        raise LocalDenylistError("local denylist mode must be 0600")

    if stat.S_ISLNK(parent_status.st_mode) or not stat.S_ISDIR(parent_status.st_mode):
        raise LocalDenylistError(
            "local denylist parent must be a non-symlink directory"
        )
    if parent_status.st_uid != os.getuid():
        raise LocalDenylistError(
            "local denylist parent must be owned by the current user"
        )
    if stat.S_IMODE(parent_status.st_mode) != 0o700:
        raise LocalDenylistError("local denylist parent mode must be 0700")

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise LocalDenylistError("local denylist is not readable UTF-8 text") from error

    values = [line.strip() for line in text.splitlines()]
    rules = tuple(
        _compile_local_rule(value)
        for value in values
        if value and not value.startswith("#")
    )
    if not rules:
        raise LocalDenylistError("local denylist must contain at least one rule")
    return rules


def scan_text(
    text: str, location: str, *, local_denylist: LocalDenylist = ()
) -> list[Finding]:
    """Return privacy findings without echoing matched private values."""
    location = _redact_local_identifiers(location, local_denylist)
    findings: list[Finding] = []

    for match in _EMAIL_RE.finditer(text):
        if not _is_synthetic_email_domain(match.group(2)):
            findings.append(
                Finding(
                    location, "non-synthetic-email", _line_number(text, match.start())
                )
            )

    for match in _HOME_PATH_RE.finditer(text):
        user = (match.group("user") or match.group("data_user")).lower()
        if user not in _SYNTHETIC_HOME_USERS:
            findings.append(
                Finding(
                    location,
                    "absolute-local-home-path",
                    _line_number(text, match.start()),
                )
            )

    for match in _TELEGRAM_TARGET_RE.finditer(text):
        findings.append(
            Finding(
                location, "telegram-routing-target", _line_number(text, match.start())
            )
        )

    for match in _CREDENTIAL_ASSIGNMENT_RE.finditer(text):
        value = match.group("value").lower()
        if value not in _CREDENTIAL_PLACEHOLDERS and not value.startswith(
            ("example", "test")
        ):
            findings.append(
                Finding(
                    location, "credential-assignment", _line_number(text, match.start())
                )
            )

    for rule, pattern in (
        ("known-secret-token", _KNOWN_TOKEN_RE),
        ("private-key-material", _PRIVATE_KEY_RE),
        ("bearer-credential", _BEARER_TOKEN_RE),
    ):
        for match in pattern.finditer(text):
            findings.append(Finding(location, rule, _line_number(text, match.start())))

    for pattern in local_denylist:
        for match in pattern.finditer(text):
            findings.append(
                Finding(
                    location,
                    "local-denylist-identifier",
                    _line_number(text, match.start()),
                )
            )

    return findings


def scan_bytes(
    data: bytes, location: str, *, local_denylist: LocalDenylist = ()
) -> list[Finding]:
    location = _redact_local_identifiers(location, local_denylist)
    findings = _path_findings(location, local_denylist=local_denylist)
    if len(data) > MAX_CONTENT_BYTES:
        findings.append(Finding(location, "oversized-content-not-inspected"))
        return findings
    text = data.decode("utf-8", errors="replace")
    findings.extend(scan_text(text, location, local_denylist=local_denylist))
    return findings


def _iter_directory_files(root: Path) -> Iterator[Path]:
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_root)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in _IGNORED_DIRECTORY_NAMES
            and not (current / name).is_symlink()
        )
        for file_name in sorted(file_names):
            path = current / file_name
            if not path.is_symlink():
                yield path


def _scan_zip(path: Path, *, local_denylist: LocalDenylist = ()) -> list[Finding]:
    findings: list[Finding] = []
    with zipfile.ZipFile(path) as archive:
        for member in sorted(archive.infolist(), key=lambda item: item.filename):
            if member.is_dir():
                continue
            location = _redact_local_identifiers(
                f"{path}!{member.filename}", local_denylist
            )
            if member.file_size > MAX_CONTENT_BYTES:
                findings.extend(_path_findings(location, local_denylist=local_denylist))
                findings.append(Finding(location, "oversized-content-not-inspected"))
                continue
            findings.extend(
                scan_bytes(
                    archive.read(member), location, local_denylist=local_denylist
                )
            )
    return findings


def _scan_tar(path: Path, *, local_denylist: LocalDenylist = ()) -> list[Finding]:
    findings: list[Finding] = []
    with tarfile.open(path, mode="r:*") as archive:
        for member in sorted(archive.getmembers(), key=lambda item: item.name):
            if not member.isfile():
                continue
            location = _redact_local_identifiers(
                f"{path}!{member.name}", local_denylist
            )
            if member.size > MAX_CONTENT_BYTES:
                findings.extend(_path_findings(location, local_denylist=local_denylist))
                findings.append(Finding(location, "oversized-content-not-inspected"))
                continue
            extracted = archive.extractfile(member)
            if extracted is not None:
                findings.extend(
                    scan_bytes(
                        extracted.read(), location, local_denylist=local_denylist
                    )
                )
    return findings


def scan_path(path: Path, *, local_denylist: LocalDenylist = ()) -> list[Finding]:
    if path.is_dir():
        findings: list[Finding] = _path_findings(
            path.name, local_denylist=local_denylist
        )
        for child in _iter_directory_files(path):
            relative = child.relative_to(path).as_posix()
            findings.extend(
                scan_bytes(child.read_bytes(), relative, local_denylist=local_denylist)
            )
        return findings
    if not path.is_file():
        raise FileNotFoundError(path)
    if zipfile.is_zipfile(path):
        return [
            *_path_findings(str(path), local_denylist=local_denylist),
            *_scan_zip(path, local_denylist=local_denylist),
        ]
    if tarfile.is_tarfile(path):
        return [
            *_path_findings(str(path), local_denylist=local_denylist),
            *_scan_tar(path, local_denylist=local_denylist),
        ]
    return scan_bytes(path.read_bytes(), str(path), local_denylist=local_denylist)


def _trusted_git_executable() -> str:
    try:
        git_status = _TRUSTED_GIT_PATH.lstat()
    except OSError as error:
        raise GitTrustError("trusted Git executable is unavailable") from error
    if stat.S_ISLNK(git_status.st_mode) or not stat.S_ISREG(git_status.st_mode):
        raise GitTrustError("trusted Git executable is not a regular file")
    if git_status.st_uid != 0 or stat.S_IMODE(git_status.st_mode) & 0o022:
        raise GitTrustError("trusted Git executable has unsafe ownership or mode")
    if not os.access(_TRUSTED_GIT_PATH, os.X_OK):
        raise GitTrustError("trusted Git executable is not executable")
    return str(_TRUSTED_GIT_PATH)


def _git_environment() -> dict[str, str]:
    return {
        "GIT_ASKPASS": _TRUSTED_ASKPASS_PATH,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_GRAFT_FILE": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PAGER": "",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }


def _run_git(arguments: Sequence[str], *, input_data: bytes | None = None) -> bytes:
    command = [_trusted_git_executable(), "--no-optional-locks"]
    for override in _GIT_CONFIG_OVERRIDES:
        command.extend(("-c", override))
    command.extend(arguments)
    completed = subprocess.run(
        command,
        input=input_data,
        stdin=subprocess.DEVNULL if input_data is None else None,
        check=True,
        capture_output=True,
        env=_git_environment(),
    )
    return completed.stdout


def scan_tracked_files(*, local_denylist: LocalDenylist = ()) -> list[Finding]:
    findings: list[Finding] = []
    for raw_path in _run_git(["ls-files", "-z"]).split(b"\0"):
        if not raw_path:
            continue
        path = Path(raw_path.decode("utf-8", errors="surrogateescape"))
        if path.is_file() and not path.is_symlink():
            findings.extend(
                scan_bytes(
                    path.read_bytes(), path.as_posix(), local_denylist=local_denylist
                )
            )
    return findings


def _scan_git_path_metadata(
    raw_path: bytes, location: str, *, local_denylist: LocalDenylist
) -> list[Finding]:
    display_path = raw_path.decode("utf-8", errors="replace")
    findings = scan_text(display_path, location, local_denylist=local_denylist)
    findings.extend(
        Finding(location, finding.rule)
        for finding in _path_findings(display_path, local_denylist=local_denylist)
    )
    return findings


def _scan_git_object_metadata(
    data: bytes, location: str, *, local_denylist: LocalDenylist
) -> list[Finding]:
    if len(data) > MAX_CONTENT_BYTES:
        return [Finding(location, "oversized-content-not-inspected")]
    return scan_text(
        data.decode("utf-8", errors="replace"),
        location,
        local_denylist=local_denylist,
    )


def _scan_git_tree_paths(
    object_id: str,
    location: str,
    *,
    recursive: bool,
    local_denylist: LocalDenylist,
) -> list[Finding]:
    arguments = ["ls-tree", "-z", "--full-tree"]
    if recursive:
        arguments.append("-r")
    arguments.append(object_id)
    findings: list[Finding] = []
    for record in _run_git(arguments).split(b"\0"):
        if not record:
            continue
        _, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise GitTrustError("Git returned malformed tree metadata")
        findings.extend(
            _scan_git_path_metadata(raw_path, location, local_denylist=local_denylist)
        )
    return findings


def scan_git_history(*, local_denylist: LocalDenylist = ()) -> list[Finding]:
    """Scan all publishable content and metadata reachable from local Git refs."""
    shallow_state = _run_git(["rev-parse", "--is-shallow-repository"]).strip()
    if shallow_state != b"false":
        raise GitTrustError("Git history scan requires a complete non-shallow clone")
    findings: list[Finding] = []
    object_lines = _run_git(["rev-list", "--objects", "--all"]).splitlines()
    seen: set[bytes] = set()
    for line in object_lines:
        object_id = line.partition(b" ")[0]
        if not object_id or object_id in seen:
            continue
        seen.add(object_id)
        object_id_text = object_id.decode("ascii")
        object_type = (
            _run_git(["cat-file", "-t", object_id_text]).strip().decode("ascii")
        )
        if object_type not in {"blob", "commit", "tag", "tree"}:
            raise GitTrustError("Git returned an unsupported reachable object type")
        location = f"git:{object_type}:{object_id_text[:12]}"
        if object_type == "blob":
            data = _run_git(["cat-file", object_type, object_id_text])
            findings.extend(scan_bytes(data, location, local_denylist=local_denylist))
        elif object_type in {"commit", "tag"}:
            data = _run_git(["cat-file", object_type, object_id_text])
            findings.extend(
                _scan_git_object_metadata(data, location, local_denylist=local_denylist)
            )
        if object_type == "commit":
            findings.extend(
                _scan_git_tree_paths(
                    object_id_text,
                    location,
                    recursive=True,
                    local_denylist=local_denylist,
                )
            )
        elif object_type == "tree":
            findings.extend(
                _scan_git_tree_paths(
                    object_id_text,
                    location,
                    recursive=False,
                    local_denylist=local_denylist,
                )
            )
    return findings


def _deduplicate(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(set(findings))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan publishable source and package artifacts for generic privacy violations."
    )
    parser.add_argument(
        "paths", nargs="*", type=Path, help="Files, directories, wheels, or archives"
    )
    parser.add_argument(
        "--tracked",
        action="store_true",
        help="Scan files tracked by the current Git repository",
    )
    parser.add_argument(
        "--git-history",
        action="store_true",
        help="Scan all content and metadata reachable from local Git refs (requires a full clone)",
    )
    parser.add_argument(
        "--local-denylist",
        type=Path,
        metavar="PATH",
        help="Apply exact identifiers from validated owner-only local configuration",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.paths and not args.tracked and not args.git_history:
        parser.error("provide at least one path, --tracked, or --git-history")

    findings: list[Finding] = []
    try:
        local_denylist = (
            load_local_denylist(args.local_denylist) if args.local_denylist else ()
        )
        for path in args.paths:
            findings.extend(scan_path(path, local_denylist=local_denylist))
        if args.tracked:
            findings.extend(scan_tracked_files(local_denylist=local_denylist))
        if args.git_history:
            findings.extend(scan_git_history(local_denylist=local_denylist))
    except LocalDenylistError as error:
        print(f"privacy gate error: {error}", file=sys.stderr)
        return 2
    except GitTrustError as error:
        print(f"privacy gate error: {error}", file=sys.stderr)
        return 2
    except (
        FileNotFoundError,
        OSError,
        subprocess.CalledProcessError,
        tarfile.TarError,
    ) as error:
        message = "scan failed" if args.local_denylist else str(error)
        print(f"privacy gate error: {message}", file=sys.stderr)
        return 2

    unique_findings = _deduplicate(findings)
    if unique_findings:
        print(
            f"privacy gate failed with {len(unique_findings)} finding(s):",
            file=sys.stderr,
        )
        for finding in unique_findings:
            print(f"- {finding.render()}", file=sys.stderr)
        return 1

    print("privacy gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

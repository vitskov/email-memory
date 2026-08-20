"""Safe, generic utilities for small operational artifacts.

The event format deliberately accepts only bounded tokens and numeric/boolean
values.  It is not a logging API and must never be used to persist messages,
addresses, paths, exception text, or credentials.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any


_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_PRIVATE_TOKEN = re.compile(
    r"(?:^|[._-])(api[_-]?key|bearer|credential|passwd|password|secret|token)(?:$|[._-])",
    re.IGNORECASE,
)
_EVENT_KEYS = {
    "schema_version",
    "recorded_at",
    "event_code",
    "run_id",
    "severity",
    "stage",
    "exit_code",
    "count",
    "elapsed_seconds",
    "retryable",
}
_SEVERITIES = {"info", "warning", "error"}
_MAX_INTEGER = 2**31 - 1


def _reject_symlink_components(path: Path) -> None:
    """Reject existing symlinks in *path*, including parent components."""
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"symbolic links are not allowed: {current}")


def _require_directory(path: Path, *, create: bool, repair: bool = True) -> None:
    _reject_symlink_components(path)
    if create:
        missing: list[Path] = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent
        for component in reversed(missing):
            component.mkdir(mode=0o700)
            os.chmod(component, 0o700)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise FileNotFoundError(f"operational artifact directory does not exist: {path}") from None
    mode = metadata.st_mode
    if not stat.S_ISDIR(mode):
        raise NotADirectoryError(path)
    if metadata.st_uid != os.geteuid():
        raise PermissionError(f"operational artifact directory must be owned by the current user: {path}")
    if repair:
        os.chmod(path, 0o700)
    elif stat.S_IMODE(mode) != 0o700:
        raise PermissionError(f"directory must have mode 0700: {path}")


def _require_regular_file(path: Path) -> None:
    _reject_symlink_components(path)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"operational artifact is not a regular file: {path}")
    if metadata.st_uid != os.geteuid():
        raise PermissionError(f"operational artifact must be owned by the current user: {path}")
    if metadata.st_nlink != 1:
        raise ValueError(f"operational artifact must have exactly one hard link: {path}")


def _require_private_descriptor(descriptor: int, *, path: Path) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"operational artifact is not a regular file: {path}")
    if metadata.st_uid != os.geteuid():
        raise PermissionError(f"operational artifact must be owned by the current user: {path}")
    if metadata.st_nlink != 1:
        raise ValueError(f"operational artifact must have exactly one hard link: {path}")


def write_private_text(
    path: Path,
    text: str,
    *,
    repair_parent_permissions: bool = True,
) -> None:
    """Atomically write UTF-8 text with owner-only directory and file modes."""
    path = Path(path)
    if repair_parent_permissions:
        _require_directory(path.parent, create=True)
    else:
        _require_directory(path.parent, create=False, repair=False)
    if path.exists() or path.is_symlink():
        _require_regular_file(path)

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _safe_token(value: str, *, field: str) -> str:
    if not _TOKEN.fullmatch(value) or _PRIVATE_TOKEN.search(value):
        raise ValueError(f"{field} must be a bounded non-sensitive token")
    return value


def _bounded_integer(value: int, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if not minimum <= value <= _MAX_INTEGER:
        raise ValueError(f"{field} must be between {minimum} and {_MAX_INTEGER}")
    return value


def _event_record(
    *,
    event_code: str,
    run_id: str,
    severity: str = "info",
    stage: str | None = None,
    exit_code: int | None = None,
    count: int | None = None,
    elapsed_seconds: int | None = None,
    retryable: bool | None = None,
    recorded_at: datetime | None = None,
) -> dict[str, object]:
    if severity not in _SEVERITIES:
        raise ValueError(f"severity must be one of: {', '.join(sorted(_SEVERITIES))}")
    instant = recorded_at or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("recorded_at must be timezone-aware")
    record: dict[str, object] = {
        "schema_version": 1,
        "recorded_at": instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event_code": _safe_token(event_code, field="event_code"),
        "run_id": _safe_token(run_id, field="run_id"),
        "severity": severity,
    }
    if stage is not None:
        record["stage"] = _safe_token(stage, field="stage")
    if exit_code is not None:
        record["exit_code"] = _bounded_integer(exit_code, field="exit_code", minimum=-255)
    if count is not None:
        record["count"] = _bounded_integer(count, field="count")
    if elapsed_seconds is not None:
        record["elapsed_seconds"] = _bounded_integer(elapsed_seconds, field="elapsed_seconds")
    if retryable is not None:
        if not isinstance(retryable, bool):
            raise ValueError("retryable must be a boolean")
        record["retryable"] = retryable
    return record


def append_event(
    path: Path,
    *,
    event_code: str,
    run_id: str,
    severity: str = "info",
    stage: str | None = None,
    exit_code: int | None = None,
    count: int | None = None,
    elapsed_seconds: int | None = None,
    retryable: bool | None = None,
    recorded_at: datetime | None = None,
) -> None:
    """Append one validated structured event to an owner-only JSONL file."""
    record = _event_record(
        event_code=event_code,
        run_id=run_id,
        severity=severity,
        stage=stage,
        exit_code=exit_code,
        count=count,
        elapsed_seconds=elapsed_seconds,
        retryable=retryable,
        recorded_at=recorded_at,
    )
    path = Path(path)
    _require_directory(path.parent, create=True)
    if path.exists() or path.is_symlink():
        _require_regular_file(path)
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        _require_private_descriptor(descriptor, path=path)
        os.fchmod(descriptor, 0o600)
        payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_loaded_event(value: Any) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) - _EVENT_KEYS:
        raise ValueError("event contains unsupported fields")
    required = {"schema_version", "recorded_at", "event_code", "run_id", "severity"}
    if not required <= set(value) or value["schema_version"] != 1:
        raise ValueError("event does not match schema version 1")
    if not all(isinstance(value[key], str) for key in ("recorded_at", "event_code", "run_id", "severity")):
        raise ValueError("event string fields have invalid types")
    try:
        recorded_at = datetime.fromisoformat(str(value["recorded_at"]).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("event recorded_at is invalid") from None
    if recorded_at.tzinfo is None:
        raise ValueError("event recorded_at must be timezone-aware")
    optional = {key: value[key] for key in _EVENT_KEYS - required if key in value}
    expected = _event_record(
        event_code=str(value["event_code"]),
        run_id=str(value["run_id"]),
        severity=str(value["severity"]),
        stage=optional.get("stage") if isinstance(optional.get("stage"), str) else None,
        exit_code=optional.get("exit_code") if isinstance(optional.get("exit_code"), int) else None,
        count=optional.get("count") if isinstance(optional.get("count"), int) else None,
        elapsed_seconds=(
            optional.get("elapsed_seconds") if isinstance(optional.get("elapsed_seconds"), int) else None
        ),
        retryable=optional.get("retryable") if isinstance(optional.get("retryable"), bool) else None,
        recorded_at=recorded_at,
    )
    if expected != value:
        raise ValueError("event contains invalid or incorrectly typed fields")
    return expected


def render_event(event: dict[str, object]) -> str:
    """Render a validated event as bounded, generic human-readable text."""
    event = _validate_loaded_event(event)
    code = str(event["event_code"]).replace("_", " ").replace("-", " ")
    parts = [f"[{event['recorded_at']}]", str(event["severity"]).upper() + ":", code + ";"]
    parts.append(f"run={event['run_id']}")
    for key in ("stage", "exit_code", "count", "elapsed_seconds", "retryable"):
        if key in event:
            rendered_key = "elapsed" if key == "elapsed_seconds" else key
            rendered_value = f"{event[key]}s" if key == "elapsed_seconds" else str(event[key]).lower()
            parts.append(f"{rendered_key}={rendered_value}")
    return " ".join(parts)


def render_events(input_path: Path, output_path: Path) -> int:
    """Validate a JSONL event file and atomically render it as human text."""
    _require_regular_file(Path(input_path))
    rendered: list[str] = []
    with Path(input_path).open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                rendered.append(render_event(value))
            except (json.JSONDecodeError, ValueError) as error:
                raise ValueError(f"invalid event on line {line_number}: {error}") from None
    write_private_text(Path(output_path), "\n".join(rendered) + ("\n" if rendered else ""))
    return len(rendered)


def _validate_patterns(patterns: Iterable[str]) -> tuple[str, ...]:
    result = tuple(patterns)
    if not result:
        raise ValueError("at least one artifact pattern is required")
    for pattern in result:
        candidate = Path(pattern)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"artifact pattern must stay within its directory: {pattern}")
    return result


def _matched_files(directory: Path, patterns: Iterable[str]) -> list[Path]:
    matches = {path for pattern in _validate_patterns(patterns) for path in directory.glob(pattern)}
    for path in sorted(matches):
        _require_regular_file(path)
    return sorted(matches)


def secure_artifacts(directory: Path, files: Iterable[Path] = ()) -> None:
    """Create or harden an artifact directory and explicitly named files."""
    directory = Path(directory)
    _require_directory(directory, create=True)
    resolved_directory = directory.resolve()
    paths: list[Path] = []
    for item in files:
        path = Path(item)
        path = path if path.is_absolute() else directory / path
        if path.parent.resolve() != resolved_directory:
            raise ValueError(f"artifact file must be directly inside {directory}: {item}")
        if path.exists() or path.is_symlink():
            _require_regular_file(path)
        paths.append(path)
    for path in paths:
        if path.exists():
            os.chmod(path, 0o600)
        else:
            write_private_text(path, "")


def harden_artifacts(directory: Path, patterns: Iterable[str]) -> list[Path]:
    """Set known regular artifacts to 0600 after fail-closed preflight."""
    directory = Path(directory)
    _require_directory(directory, create=False)
    matches = _matched_files(directory, patterns)
    for path in matches:
        os.chmod(path, 0o600)
    return matches


def prune_files(
    directory: Path,
    patterns: Iterable[str],
    older_than_days: int | float,
    companion_suffix: str | None = None,
    now: datetime | None = None,
) -> list[Path]:
    """Delete old matched files, optionally only with and alongside a marker."""
    directory = Path(directory)
    _require_directory(directory, create=False)
    if isinstance(older_than_days, bool) or older_than_days < 0:
        raise ValueError("older_than_days must be non-negative")
    if companion_suffix is not None and (
        not companion_suffix or "/" in companion_suffix or "\\" in companion_suffix
    ):
        raise ValueError("companion_suffix must be a filename suffix")
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cutoff = instant - timedelta(days=older_than_days)
    matches = _matched_files(directory, patterns)
    candidates: list[tuple[Path, Path | None]] = []
    for path in matches:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if modified > cutoff:
            continue
        companion = Path(f"{path}{companion_suffix}") if companion_suffix else None
        if companion is not None:
            if not companion.exists() and not companion.is_symlink():
                continue
            _require_regular_file(companion)
        candidates.append((path, companion))
    removed: list[Path] = []
    for path, companion in candidates:
        path.unlink()
        removed.append(path)
        if companion is not None:
            companion.unlink()
            removed.append(companion)
    return removed


def _parse_boolean(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    secure = commands.add_parser("secure", help="create or harden private artifacts")
    secure.add_argument("--directory", required=True, type=Path)
    secure.add_argument("--file", action="append", default=[], type=Path)

    append = commands.add_parser("append", help="append one safe structured event")
    append.add_argument("--path", required=True, type=Path)
    append.add_argument("--event-code", required=True)
    append.add_argument("--run-id", required=True)
    append.add_argument("--severity", choices=sorted(_SEVERITIES), default="info")
    append.add_argument("--stage")
    append.add_argument("--exit-code", type=int)
    append.add_argument("--count", type=int)
    append.add_argument("--elapsed-seconds", type=int)
    append.add_argument("--retryable", type=_parse_boolean)

    render = commands.add_parser("render", help="render validated events as human text")
    render.add_argument("--input", required=True, type=Path)
    render.add_argument("--output", required=True, type=Path)

    harden = commands.add_parser("harden", help="harden matched regular files")
    harden.add_argument("--directory", required=True, type=Path)
    harden.add_argument("--pattern", required=True, action="append")

    prune = commands.add_parser("prune", help="prune old matched regular files")
    prune.add_argument("--directory", required=True, type=Path)
    prune.add_argument("--pattern", required=True, action="append")
    prune.add_argument("--days", required=True, type=float)
    prune.add_argument("--companion-suffix")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "secure":
        secure_artifacts(args.directory, args.file)
    elif args.command == "append":
        append_event(
            args.path,
            event_code=args.event_code,
            run_id=args.run_id,
            severity=args.severity,
            stage=args.stage,
            exit_code=args.exit_code,
            count=args.count,
            elapsed_seconds=args.elapsed_seconds,
            retryable=args.retryable,
        )
    elif args.command == "render":
        render_events(args.input, args.output)
    elif args.command == "harden":
        harden_artifacts(args.directory, args.pattern)
    elif args.command == "prune":
        prune_files(args.directory, args.pattern, args.days, args.companion_suffix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

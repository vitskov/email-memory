"""Durable job records and workers detached from the control MCP parent."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import stat
import subprocess
import sys
from typing import Any, Final


ACTIONS: Final[tuple[str, ...]] = (
    "maintenance",
    "retry_failed_bodies",
    "reconcile",
)
ACTIVE_STATES: Final[frozenset[str]] = frozenset({"queued", "running"})
JOB_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}")
MAX_RECORD_BYTES: Final[int] = 16 * 1024
MAX_ACTIVATION_BYTES: Final[int] = 1024
MAX_TERMINAL_RECORDS: Final[int] = 100
ACTIVATION_FILE: Final[str] = ".activation.json"


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _selected_xdg_path(
    variable: str, default: Path, environ: Mapping[str, str]
) -> Path:
    raw = environ.get(variable)
    selected = Path(raw) if raw else default
    if not selected.is_absolute() or ".." in selected.parts:
        raise RuntimeError("control state attachment is invalid")
    return selected


def _validate_directory(path: Path, *, owner_only: bool) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeError("control state attachment is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
        or (owner_only and metadata.st_mode & 0o077)
    ):
        raise RuntimeError("control state attachment is not secure")


def _ensure_directory(path: Path, *, owner_only: bool) -> None:
    if not path.exists():
        try:
            path.mkdir(mode=0o700)
        except OSError as error:
            raise RuntimeError(
                "control state attachment could not be created"
            ) from error
    elif owner_only:
        try:
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.geteuid():
                path.chmod(0o700)
        except OSError as error:
            raise RuntimeError(
                "control state attachment could not be secured"
            ) from error
    _validate_directory(path, owner_only=owner_only)


def state_directory(environ: Mapping[str, str] | None = None) -> Path:
    """Return the canonical owner-only XDG directory for durable job records."""
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME") or pwd.getpwuid(os.geteuid()).pw_dir)
    if not home.is_absolute():
        raise RuntimeError("control state attachment is invalid")
    state_home = _selected_xdg_path("XDG_STATE_HOME", home / ".local/state", env)
    if not state_home.exists():
        state_home.mkdir(mode=0o700, parents=True)
    _validate_directory(state_home, owner_only=False)
    application = state_home / "email-memory-store"
    _ensure_directory(application, owner_only=True)
    control = application / "control-jobs"
    _ensure_directory(control, owner_only=True)
    return control


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _record_path(directory: Path, job_id: str) -> Path:
    if JOB_ID_PATTERN.fullmatch(job_id) is None:
        raise ValueError("invalid job identifier")
    return directory / f"{job_id}.json"


def _atomic_write_json(
    directory: Path,
    destination: Path,
    value: Mapping[str, Any],
    *,
    maximum_bytes: int,
) -> None:
    temporary = directory / f".{destination.name}.{secrets.token_hex(8)}.tmp"
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if len(payload) > maximum_bytes:
        raise RuntimeError("control state record is too large")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise RuntimeError("control state record could not be written")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, destination)
        _fsync_directory(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_record(directory: Path, record: Mapping[str, Any]) -> None:
    destination = _record_path(directory, str(record["job_id"]))
    _atomic_write_json(
        directory,
        destination,
        record,
        maximum_bytes=MAX_RECORD_BYTES,
    )


def _single_open_json(path: Path, *, maximum_bytes: int) -> object:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise RuntimeError("control state record is invalid") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
            or metadata.st_size > maximum_bytes
        ):
            raise RuntimeError("control state record is invalid")
        payload = bytearray()
        while len(payload) <= maximum_bytes:
            chunk = os.read(descriptor, min(4096, maximum_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > maximum_bytes:
            raise RuntimeError("control state record is invalid")
        return json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("control state record is invalid") from error
    finally:
        os.close(descriptor)


def _read_record(path: Path) -> dict[str, Any]:
    try:
        payload = _single_open_json(path, maximum_bytes=MAX_RECORD_BYTES)
    except FileNotFoundError:
        raise
    except (OSError, RuntimeError) as error:
        raise RuntimeError("control job record is invalid") from error
    required = {
        "schema_version",
        "job_id",
        "action",
        "state",
        "created_at",
        "updated_at",
        "result",
        "worker_pid",
        "worker_identity",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != required
        or payload.get("schema_version") != 1
        or JOB_ID_PATTERN.fullmatch(str(payload.get("job_id", ""))) is None
        or payload.get("action") not in ACTIONS
        or payload.get("state") not in {"queued", "running", "succeeded", "failed"}
        or not isinstance(payload.get("created_at"), int | float)
        or not isinstance(payload.get("updated_at"), int | float)
        or not (payload.get("result") is None or isinstance(payload.get("result"), str))
        or not (
            payload.get("worker_pid") is None
            or isinstance(payload.get("worker_pid"), int)
        )
        or not (
            payload.get("worker_identity") is None
            or isinstance(payload.get("worker_identity"), str)
        )
    ):
        raise RuntimeError("control job record is invalid")
    return payload


@contextmanager
def _state_lock(directory: Path) -> Iterator[None]:
    path = directory / ".lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
        ):
            raise RuntimeError("control state lock is not secure")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _enabled_unlocked(directory: Path) -> bool:
    path = directory / ACTIVATION_FILE
    try:
        payload = _single_open_json(path, maximum_bytes=MAX_ACTIVATION_BYTES)
    except FileNotFoundError:
        return False
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "enabled"}
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("enabled"), bool)
    ):
        raise RuntimeError("control activation state is invalid")
    return bool(payload["enabled"])


def set_enabled(
    enabled: bool, *, environ: Mapping[str, str] | None = None
) -> dict[str, object]:
    """Atomically set the owner-only local add-on activation switch."""
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    directory = state_directory(environ)
    with _state_lock(directory):
        _atomic_write_json(
            directory,
            directory / ACTIVATION_FILE,
            {"schema_version": 1, "enabled": enabled},
            maximum_bytes=MAX_ACTIVATION_BYTES,
        )
    return {"enabled": enabled, "paths_redacted": True}


def is_enabled(*, environ: Mapping[str, str] | None = None) -> bool:
    directory = state_directory(environ)
    with _state_lock(directory):
        return _enabled_unlocked(directory)


def _parse_linux_process_identity(pid: int, raw: str) -> str | None:
    closing = raw.rfind(")")
    if closing < 0:
        return None
    tail = raw[closing + 1 :].split()
    if len(tail) < 20 or tail[0] == "Z":
        return None
    return f"linux:{pid}:{tail[19]}"


def _process_identity(pid: int) -> str | None:
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        identity = _parse_linux_process_identity(
            pid, proc_stat.read_text(encoding="utf-8")
        )
        if identity is not None:
            return identity
    except OSError:
        pass
    try:
        completed = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "stat=", "-o", "lstart="],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            check=False,
            timeout=1,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    output = completed.stdout.decode("ascii", errors="replace").strip()
    fields = output.split(maxsplit=1)
    if (
        completed.returncode != 0
        or len(fields) != 2
        or fields[0].startswith("Z")
        or not fields[1]
        or len(fields[1]) > 64
    ):
        return None
    return f"ps:{pid}:{fields[1]}"


def _is_worker_alive(record: Mapping[str, Any]) -> bool:
    pid = record.get("worker_pid")
    identity = record.get("worker_identity")
    return bool(
        isinstance(pid, int)
        and isinstance(identity, str)
        and _process_identity(pid) == identity
    )


def _records(directory: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in directory.glob("*.json"):
        if JOB_ID_PATTERN.fullmatch(path.stem) is None:
            continue
        record = _read_record(path)
        if record["job_id"] != path.stem:
            raise RuntimeError("control job record is invalid")
        records.append(record)
    return records


def _prune_terminal_records(directory: Path) -> None:
    terminal = sorted(
        (
            record
            for record in _records(directory)
            if record["state"] not in ACTIVE_STATES
        ),
        key=lambda record: (record["updated_at"], record["job_id"]),
        reverse=True,
    )
    removed = False
    for record in terminal[MAX_TERMINAL_RECORDS:]:
        _record_path(directory, str(record["job_id"])).unlink()
        removed = True
    if removed:
        _fsync_directory(directory)


def _active_record(directory: Path) -> dict[str, Any] | None:
    active: list[dict[str, Any]] = []
    for record in _records(directory):
        if record["state"] not in ACTIVE_STATES:
            continue
        if not _is_worker_alive(record):
            record.update(
                state="failed",
                result="worker_interrupted",
                updated_at=_now(),
                worker_pid=None,
                worker_identity=None,
            )
            _write_record(directory, record)
            continue
        active.append(record)
    if len(active) > 1:
        raise RuntimeError("control job state contains multiple active operations")
    return active[0] if active else None


def _public_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "job_id": record["job_id"],
        "action": record["action"],
        "state": record["state"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "result": record["result"],
        "paths_redacted": True,
    }


def _worker_environment(environ: Mapping[str, str]) -> dict[str, str]:
    home = Path(environ.get("HOME") or pwd.getpwuid(os.geteuid()).pw_dir)
    config = _selected_xdg_path("XDG_CONFIG_HOME", home / ".config", environ)
    data = _selected_xdg_path("XDG_DATA_HOME", home / ".local/share", environ)
    state = _selected_xdg_path("XDG_STATE_HOME", home / ".local/state", environ)
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config),
        "XDG_DATA_HOME": str(data),
        "XDG_STATE_HOME": str(state),
        "PATH": "/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
        "LC_ALL": "C.UTF-8",
    }


def start_job(
    action: str, *, environ: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Start one fixed operation in a worker detached from the MCP parent."""
    if action not in ACTIONS:
        raise ValueError("invalid action")
    env = os.environ if environ is None else environ
    directory = state_directory(env)
    with _state_lock(directory):
        if not _enabled_unlocked(directory):
            return {
                "accepted": False,
                "reason": "control_disabled",
            }
        _prune_terminal_records(directory)
        active = _active_record(directory)
        if active is not None:
            summary = {
                "job_id": active["job_id"],
                "action": active["action"],
                "state": active["state"],
            }
            if active["action"] == action:
                return {
                    "accepted": True,
                    "job_id": active["job_id"],
                    "action": action,
                    "state": active["state"],
                    "created_at": active["created_at"],
                    "idempotent": True,
                }
            return {
                "accepted": False,
                "reason": "operation_in_progress",
                "active_job": summary,
            }

        job_id = secrets.token_hex(16)
        created = _now()
        record: dict[str, Any] = {
            "schema_version": 1,
            "job_id": job_id,
            "action": action,
            "state": "queued",
            "created_at": created,
            "updated_at": created,
            "result": None,
            "worker_pid": None,
            "worker_identity": None,
        }
        _write_record(directory, record)
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "email_memory_store.control.worker",
                    job_id,
                ],
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                env=_worker_environment(env),
            )
            identity = _process_identity(process.pid)
            if identity is None:
                raise OSError("detached worker did not start")
            record.update(worker_pid=process.pid, worker_identity=identity)
            _write_record(directory, record)
        except OSError:
            record.update(
                state="failed",
                result="worker_start_failed",
                updated_at=_now(),
                worker_pid=None,
                worker_identity=None,
            )
            _write_record(directory, record)
            return {
                "accepted": False,
                "reason": "worker_start_failed",
                "job_id": job_id,
            }
        return {
            "accepted": True,
            "job_id": job_id,
            "action": action,
            "state": "queued",
            "created_at": created,
        }


def job_status(
    job_id: str, *, environ: Mapping[str, str] | None = None
) -> dict[str, Any]:
    if JOB_ID_PATTERN.fullmatch(job_id) is None:
        raise ValueError("invalid job identifier")
    directory = state_directory(environ)
    path = _record_path(directory, job_id)
    with _state_lock(directory):
        _active_record(directory)
        _prune_terminal_records(directory)
        try:
            record = _read_record(path)
        except FileNotFoundError:
            raise ValueError("job not found")
        return _public_record(record)


def _operation_command(action: str) -> list[str]:
    if action not in ACTIONS:
        raise ValueError("invalid action")
    return [
        sys.executable,
        "-I",
        "-m",
        "email_memory_store.control.operation",
        action,
    ]


def run_worker(job_id: str, *, environ: Mapping[str, str] | None = None) -> int:
    """Run a previously queued job and persist only a redacted outcome code."""
    env = os.environ if environ is None else environ
    directory = state_directory(env)
    path = _record_path(directory, job_id)
    with _state_lock(directory):
        record = _read_record(path)
        if record["state"] != "queued":
            return 0 if record["state"] == "succeeded" else 2
        # start_job holds this same lock from before Popen until after it has
        # durably published the child's exact identity.  Requiring that
        # publication here is the startup handshake: a child whose parent
        # exited, failed identity lookup, or failed the atomic record update
        # cannot cross into operation execution.
        worker_identity = _process_identity(os.getpid())
        if (
            record["worker_pid"] != os.getpid()
            or worker_identity is None
            or record["worker_identity"] != worker_identity
        ):
            record.update(
                state="failed",
                result="worker_start_unverified",
                updated_at=_now(),
                worker_pid=None,
                worker_identity=None,
            )
            _write_record(directory, record)
            _prune_terminal_records(directory)
            return 2
        record.update(
            state="running",
            updated_at=_now(),
        )
        _write_record(directory, record)

    try:
        completed = subprocess.run(
            _operation_command(str(record["action"])),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            check=False,
            env=_worker_environment(env),
        )
        returncode = int(completed.returncode)
    except OSError:
        returncode = 127

    result = (
        "operation_completed"
        if returncode == 0
        else "maintenance_busy"
        if returncode == 75
        else "operation_failed"
    )
    with _state_lock(directory):
        record = _read_record(path)
        if record["state"] == "running":
            record.update(
                state="succeeded" if returncode == 0 else "failed",
                result=result,
                updated_at=_now(),
                worker_pid=None,
                worker_identity=None,
            )
            _write_record(directory, record)
            _prune_terminal_records(directory)
    return returncode


def system_status(*, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return deployment readiness and current job state without paths or output."""
    env = os.environ if environ is None else environ
    directory = state_directory(env)
    with _state_lock(directory):
        enabled = _enabled_unlocked(directory)
        active = _active_record(directory)
        _prune_terminal_records(directory)
        active_summary = (
            {
                "job_id": active["job_id"],
                "action": active["action"],
                "state": active["state"],
            }
            if active is not None
            else None
        )
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "email_memory_store.deployment.cli",
                "doctor",
                "--probe-timeout",
                "10",
            ],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            check=False,
            timeout=15,
            env=_worker_environment(env),
        )
        if completed.returncode == 0:
            deployment = "ready"
        elif completed.returncode == 2:
            deployment = "awaiting_index"
        else:
            deployment = "not_ready"
    except OSError, subprocess.TimeoutExpired:
        deployment = "unavailable"
    return {
        "control": "enabled" if enabled else "disabled",
        "deployment": deployment,
        "active_job": active_summary,
        "paths_redacted": True,
    }

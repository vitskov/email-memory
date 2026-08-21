"""Transactional coordinator for the public email-memory deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import secrets
import signal
import stat
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

MANAGED_START = "# BEGIN email-memory-store managed"
MANAGED_END = "# END email-memory-store managed"
RECEIPT_CODES = (
    "release_staged",
    "configuration_loaded",
    "database_initialized",
    "runtime_doctor",
    "mail_connector",
    "fact_provider",
    "mcp_eof",
    "release_activated",
    "maintenance_preflight",
    "mcp_launcher",
    "scheduler",
)
INDEX_COLLECTIONS = (
    "holographic_facts",
    "action_items",
    "deadlines",
    "calendar_events",
    "decisions",
    "thread_summaries",
    "message_chunks",
)
PUBLIC_FACT_STORE_PROVIDER = (
    "email_memory_store.integrations.hermes_fact_store:MemoryStore"
)
_FACT_STORE_ROOT_ENV = "EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT"
_FACT_STORE_PROVIDER_ENV = "EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER"
_SAFE_CRON_PATH = re.compile(r"/[A-Za-z0-9_./-]+")
_SHELL_INJECTION_VARIABLES = {"BASH_ENV", "ENV", "BASHOPTS", "SHELLOPTS"}
_CONNECTOR_CONTROL_PREFIXES = ("HIMALAYA_", "HERMES_")
DEFAULT_PROBE_TIMEOUT_SECONDS = 60
DOCTOR_READY_EXIT = 0
DOCTOR_NOT_READY_EXIT = 1
DOCTOR_AWAITING_INDEX_EXIT = 2


class BootstrapError(RuntimeError):
    """A redaction-safe bootstrap failure."""


def _positive_timeout(value: str) -> int:
    try:
        timeout = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "probe timeout must be a positive integer"
        ) from error
    if timeout <= 0:
        raise argparse.ArgumentTypeError("probe timeout must be a positive integer")
    return timeout


def _installed_script(name: str) -> Path:
    return Path(__file__).with_name("scripts") / name


def _run(
    command: Sequence[str | Path],
    *,
    env: dict[str, str],
    input_bytes: bytes | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            [str(item) for item in command],
            env=env,
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BootstrapError("deployment check could not run") from error
    if completed.returncode != 0:
        raise BootstrapError("deployment check failed")
    return completed


def _git_environment() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_", "PYTHON"))
        and not key.startswith(_CONNECTOR_CONTROL_PREFIXES)
        and key not in _SHELL_INJECTION_VARIABLES
    }
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
        }
    )
    return env


def _git_command(*arguments: str | Path) -> list[str | Path]:
    return [
        "/usr/bin/git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "credential.helper=",
        *arguments,
    ]


def _atomic_symlink(target: Path | str, link: Path) -> None:
    temporary = link.parent / f".{link.name}.{os.getpid()}.{secrets.token_hex(4)}"
    temporary.symlink_to(target)
    os.replace(temporary, link)
    _fsync_directory(link.parent)


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise BootstrapError("deployment state could not be made durable") from error


def _sync_release_tree(candidate: Path, env: dict[str, str]) -> None:
    _run(["/usr/bin/sync", "-f", candidate], env=env)
    _fsync_directory(candidate)
    _fsync_directory(candidate.parent)


def _sync_mcp_publication(current: Path, stable: Path, env: dict[str, str]) -> None:
    for directory in (current.parent, stable.parent):
        _run(["/usr/bin/sync", "-f", directory], env=env)
        _fsync_directory(directory)


def _restore_link(link: Path, previous: str | None) -> None:
    if previous is None:
        if link.is_symlink():
            link.unlink()
            _fsync_directory(link.parent)
    else:
        _atomic_symlink(previous, link)


def _cron_executable(command: str) -> str:
    candidate = Path("/usr/bin/crontab") if command == "crontab" else Path(command)
    if not candidate.is_absolute():
        raise BootstrapError("crontab executable must be an absolute trusted path")
    try:
        metadata = candidate.lstat()
    except OSError as error:
        raise BootstrapError("crontab executable is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.getuid()}
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
        or not os.access(candidate, os.X_OK)
    ):
        raise BootstrapError("crontab executable is not trusted")
    _validate_trusted_ancestor_chain(candidate.parent)
    if candidate.resolve(strict=True) != candidate:
        raise BootstrapError("crontab executable is not trusted")
    if not candidate.is_file():
        raise BootstrapError("crontab executable is unavailable")
    return str(candidate)


def _read_crontab(command: str, env: dict[str, str]) -> bytes:
    try:
        completed = subprocess.run(
            [command, "-l"],
            env=env | {"LC_ALL": "C"},
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise BootstrapError("scheduler state could not be read") from error
    if completed.returncode == 0:
        return completed.stdout
    username = pwd.getpwuid(os.getuid()).pw_name.encode()
    if (
        completed.returncode == 1
        and completed.stdout == b""
        and completed.stderr == b"no crontab for " + username + b"\n"
    ):
        return b""
    raise BootstrapError("scheduler state could not be read")


def _without_managed_cron(original: bytes, replaced_command: bytes = b"") -> bytes:
    output: list[bytes] = []
    managed = False
    for line in original.splitlines(keepends=True):
        stripped = line.rstrip(b"\r\n")
        if stripped == MANAGED_START.encode():
            if managed:
                raise BootstrapError("scheduler state is invalid")
            managed = True
        elif stripped == MANAGED_END.encode():
            if not managed:
                raise BootstrapError("scheduler state is invalid")
            managed = False
        elif not managed:
            fields = stripped.split()
            if not (
                replaced_command and len(fields) == 6 and fields[5] == replaced_command
            ):
                output.append(line)
    if managed:
        raise BootstrapError("scheduler state is invalid")
    return b"".join(output)


def _managed_crontab(original: bytes, cron_line: str, replaced_command: str) -> bytes:
    base = _without_managed_cron(original, replaced_command.encode())
    separator = b"" if not base or base.endswith(b"\n") else b"\n"
    block = f"{MANAGED_START}\n{cron_line}\n{MANAGED_END}\n".encode()
    return base + separator + block


def _is_first_deployment(
    *,
    current: Path,
    mcp_current: Path,
    mcp_stable: Path,
    old_cron: bytes,
    candidate: Path,
) -> bool:
    """Return true only when no durable deployment footprint predates staging."""
    if current.is_symlink() or mcp_current.is_symlink() or mcp_stable.is_symlink():
        return False
    if _without_managed_cron(old_cron) != old_cron:
        return False
    try:
        releases = list(candidate.parent.iterdir())
    except FileNotFoundError:
        releases = []
    except OSError as error:
        raise BootstrapError("deployment history could not be verified") from error
    for release in releases:
        receipt = release / ".deployment-readiness.json"
        # The requested unreceipted candidate is transaction staging. Any sibling
        # release or candidate receipt proves that deployment previously progressed.
        if release != candidate or receipt.exists() or receipt.is_symlink():
            return False
    return True


def _install_crontab(command: str, content: bytes, env: dict[str, str]) -> None:
    completed = subprocess.run(
        [command, "-"], env=env, input=content, capture_output=True
    )
    if completed.returncode != 0:
        raise BootstrapError("scheduler state could not be installed")


def _validate_schedule(value: str) -> str:
    if re.fullmatch(r"[0-9*/,-]+(?: [0-9*/,-]+){4}", value) is None:
        raise BootstrapError("cron schedule must contain five safe fields")
    fields = value.split(" ")
    limits = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
    if len(fields) != 5 or any(
        not _valid_cron_field(field, minimum, maximum)
        for field, (minimum, maximum) in zip(fields, limits, strict=True)
    ):
        raise BootstrapError("cron schedule must contain five safe fields")
    return value


def _valid_cron_field(field: str, minimum: int, maximum: int) -> bool:
    for item in field.split(","):
        if not item:
            return False
        base, separator, step_text = item.partition("/")
        if separator and (
            re.fullmatch(r"[0-9]+", step_text) is None
            or int(step_text) < 1
            or "/" in step_text
        ):
            return False
        if base == "*":
            continue
        start_text, range_separator, end_text = base.partition("-")
        if re.fullmatch(r"[0-9]+", start_text) is None or (
            range_separator and re.fullmatch(r"[0-9]+", end_text) is None
        ):
            return False
        start = int(start_text)
        end = int(end_text) if range_separator else start
        if not minimum <= start <= end <= maximum:
            return False
    return True


def _validate_cron_path(value: str | Path, *, label: str) -> str:
    text = str(value)
    if _SAFE_CRON_PATH.fullmatch(text) is None:
        raise BootstrapError(f"{label} must be a cron-safe absolute path")
    return text


def _load_configuration(python: Path, env: dict[str, str]) -> dict[str, str]:
    completed = _run(
        [python, "-m", "email_memory_store.local_config", "--profile", "bootstrap"],
        env=env,
    )
    try:
        value = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BootstrapError("local configuration validation failed") from error
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise BootstrapError("local configuration validation failed")
    return value


def _candidate_scripts(python: Path, env: dict[str, str]) -> Path:
    probe = (
        "from pathlib import Path; import email_memory_store.deployment as d; "
        "print(Path(d.__file__).with_name('scripts'))"
    )
    output = _run([python, "-c", probe], env=env).stdout
    try:
        path = Path(output.decode().strip()).resolve(strict=True)
    except (OSError, UnicodeDecodeError) as error:
        raise BootstrapError("candidate deployment scripts are unavailable") from error
    if not path.is_dir() or not path.is_relative_to(python.parents[2].resolve()):
        raise BootstrapError("candidate deployment scripts are unavailable")
    return path


def _verify_doctor(output: bytes) -> None:
    try:
        payload = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BootstrapError("runtime doctor returned invalid output") from error
    if (
        not isinstance(payload, dict)
        or payload.get("ok") is not True
        or payload.get("paths_redacted") is not True
    ):
        raise BootstrapError("runtime doctor did not prove redacted readiness")


def _verify_mail(output: bytes) -> None:
    try:
        payload = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BootstrapError("mail connector returned invalid output") from error
    if not isinstance(payload, list | dict):
        raise BootstrapError("mail connector returned invalid output")


def _indexed_document_count(output: bytes) -> int:
    """Validate redacted embed status and return only its aggregate count."""
    try:
        payload = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BootstrapError(
            "retrieval index status returned invalid output"
        ) from error
    if not isinstance(payload, dict) or set(payload) != {"collections", "persist_path"}:
        raise BootstrapError("retrieval index status returned invalid output")
    collections = payload["collections"]
    if (
        not isinstance(collections, dict)
        or set(collections) != set(INDEX_COLLECTIONS)
        or not isinstance(payload["persist_path"], str)
        or not payload["persist_path"]
    ):
        raise BootstrapError("retrieval index status returned invalid output")
    counts = list(collections.values())
    if any(
        not isinstance(count, int) or isinstance(count, bool) or count < 0
        for count in counts
    ):
        raise BootstrapError("retrieval index status returned invalid output")
    return sum(counts)


def _verify_default_mail_account(output: bytes, selected_account: str) -> None:
    """Prove that the private policy selector names the unique connector default."""
    try:
        payload = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BootstrapError("mail connector account readiness failed") from error
    if not isinstance(payload, list):
        raise BootstrapError("mail connector account readiness failed")
    records = [item for item in payload if isinstance(item, dict)]
    selected = [item for item in records if item.get("name") == selected_account]
    defaults = [item for item in records if item.get("default") is True]
    if (
        len(records) != len(payload)
        or len(selected) != 1
        or len(defaults) != 1
        or selected[0] is not defaults[0]
    ):
        raise BootstrapError("mail connector account readiness failed")


def _probe_mail_connector(
    executable: str, selected_account: str, env: dict[str, str], *, timeout: int
) -> None:
    """Probe the selected default without placing private selectors in argv."""
    account_probe = _run(
        [executable, "account", "list", "--output", "json"],
        env=env,
        timeout=timeout,
    )
    _verify_default_mail_account(account_probe.stdout, selected_account)
    folder_probe = _run(
        [executable, "folder", "list", "--output", "json"],
        env=env,
        timeout=timeout,
    )
    _verify_mail(folder_probe.stdout)


def _check_fact_provider(
    python: Path, config: dict[str, str], env: dict[str, str]
) -> str:
    provider = config.get(_FACT_STORE_PROVIDER_ENV, "").strip()
    root = config.get(_FACT_STORE_ROOT_ENV, "").strip()
    if not provider and not root:
        return "disabled"
    if not provider or not root or provider != PUBLIC_FACT_STORE_PROVIDER:
        raise BootstrapError("fact provider configuration is invalid")
    probe = (
        "import json; "
        "from email_memory_store.integrations.hermes_fact_store import probe_fact_store; "
        "print(json.dumps(probe_fact_store(), sort_keys=True))"
    )
    probe_env = env | {
        _FACT_STORE_PROVIDER_ENV: provider,
        _FACT_STORE_ROOT_ENV: root,
    }
    output = _run([python, "-c", probe], env=probe_env).stdout
    try:
        result = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BootstrapError(
            "fact provider readiness returned invalid output"
        ) from error
    if result != {"status": "ready"}:
        raise BootstrapError("fact provider readiness failed")
    return "ready"


def _configuration_is_complete(config_home: Path) -> bool:
    directory = config_home / "email-memory-store"
    return all(
        (directory / name).is_file()
        for name in ("runtime.toml", "private.env.json", "policy.json")
    )


def _candidate_is_verified(candidate: Path) -> bool:
    try:
        if not candidate.is_dir() or candidate.is_symlink():
            return False
        required = (
            candidate / ".email-memory-release",
            candidate / "venv/bin/python",
            candidate / "venv/bin/email-memory-store",
            candidate / "venv/bin/email-memory-store-mcp",
            candidate / "bin/email-memory-store-deploy",
        )
        for path in (candidate, candidate / "venv", required[0]):
            if path.lstat().st_uid != os.getuid() or path.lstat().st_mode & 0o022:
                return False
        candidate_root = candidate.resolve(strict=True)
        for executable in required[1:]:
            if not executable.is_file() or not os.access(executable, os.X_OK):
                return False
            resolved = executable.resolve(strict=True)
            if not resolved.is_relative_to(candidate_root):
                return False
            if resolved.stat().st_uid != os.getuid() or resolved.stat().st_mode & 0o022:
                return False
        marker = required[0]
        return (
            marker.is_file() and not marker.is_symlink() and marker.stat().st_nlink == 1
        )
    except OSError:
        return False


def _validate_production_roots(
    *,
    home: Path,
    config_home: Path,
    data_home: Path,
    state_home: Path,
    deployment: Path,
    test_mode: bool,
) -> None:
    if test_mode:
        return
    canonical_home = Path(pwd.getpwuid(os.getuid()).pw_dir).absolute()
    expected = (
        canonical_home,
        canonical_home / ".config",
        canonical_home / ".local/share",
        canonical_home / ".local/state",
        canonical_home / ".local/share/email-memory-store",
    )
    if (home, config_home, data_home, state_home, deployment) != expected:
        raise BootstrapError(
            "production deployment paths must use canonical user locations"
        )


def _release_marker_matches(
    candidate: Path,
    *,
    public_revision: str,
    accelerator: str,
) -> bool:
    try:
        content = (candidate / ".email-memory-release").read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return False
    if not content.endswith("\n"):
        return False
    lines = content.splitlines()
    if len(lines) != 3:
        return False
    parsed: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or not key or not value or key in parsed:
            return False
        parsed[key] = value
    return parsed == {
        "public_revision": public_revision,
        "python": "3.14",
        "accelerator_request": accelerator,
    }


def _run_setup(cli: Path, env: dict[str, str], *, test_mode: bool) -> None:
    if test_mode:
        _run([cli, "setup-private"], env=env)
        return
    try:
        completed = subprocess.run([cli, "setup-private"], env=env, check=False)
    except OSError as error:
        raise BootstrapError("configuration setup could not run") from error
    if completed.returncode != 0:
        raise BootstrapError("configuration setup failed")


def _validate_trusted_ancestor_chain(directory: Path) -> None:
    current = directory
    while not current.exists():
        if current.is_symlink() or current == current.parent:
            raise BootstrapError("trusted path ancestor is invalid")
        current = current.parent
    current_uid = os.getuid()
    while True:
        metadata = current.lstat()
        owner_is_trusted = metadata.st_uid in {0, current_uid}
        writable_is_trusted = not metadata.st_mode & 0o022 or (
            metadata.st_uid == 0 and bool(metadata.st_mode & 0o1000)
        )
        if (
            current.is_symlink()
            or not current.is_dir()
            or not owner_is_trusted
            or not writable_is_trusted
        ):
            raise BootstrapError("trusted path ancestor is invalid")
        if current == current.parent:
            break
        current = current.parent


def _validate_trusted_checkout(checkout: Path) -> None:
    _validate_trusted_ancestor_chain(checkout)
    current_uid = os.getuid()
    try:
        entries = [checkout, *checkout.rglob("*")]
        for entry in entries:
            metadata = entry.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid not in {0, current_uid}
                or metadata.st_mode & 0o022
                or (
                    not stat.S_ISDIR(metadata.st_mode)
                    and (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1)
                )
            ):
                raise BootstrapError("deployment source checkout is not trusted")
    except OSError as error:
        raise BootstrapError("deployment source checkout is not trusted") from error


@contextmanager
def _transaction_signal_handlers() -> Iterator[None]:
    watched = (signal.SIGTERM, signal.SIGHUP)
    previous = {signum: signal.getsignal(signum) for signum in watched}

    def interrupt(signum: int, _frame: object) -> NoReturn:
        del signum
        raise BootstrapError("deployment interrupted")

    try:
        for signum in watched:
            signal.signal(signum, interrupt)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _release_identity(release: Path) -> str:
    marker = release / ".email-memory-release"
    try:
        marker_bytes = marker.read_bytes()
    except OSError as error:
        raise BootstrapError("active release marker is unavailable") from error
    if (
        not marker.is_file()
        or marker.is_symlink()
        or marker.stat().st_uid != os.getuid()
        or marker.stat().st_nlink != 1
        or marker.stat().st_mode & 0o077
    ):
        raise BootstrapError("active release marker is invalid")
    digest = hashlib.sha256()
    digest.update(release.name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(marker_bytes)
    return digest.hexdigest()


def _write_receipt(
    path: Path,
    release: Path,
    checks: dict[str, str],
    release_identity: str,
    *,
    status: str = "ready",
) -> None:
    if status not in {"ready", "awaiting-index"}:
        raise BootstrapError("deployment receipt status is invalid")
    payload = {
        "schema_version": 3,
        "status": status,
        "paths_redacted": True,
        "release_identity": release_identity,
        "checks": [{"code": code, "status": checks[code]} for code in RECEIPT_CODES],
    }
    _validate_trusted_ancestor_chain(release)
    release_metadata = release.lstat()
    if (
        path.parent != release
        or path.name != ".deployment-readiness.json"
        or path.is_symlink()
        or release.is_symlink()
        or not release.is_dir()
        or release_metadata.st_uid != os.getuid()
        or release_metadata.st_mode & 0o077
    ):
        raise BootstrapError("readiness receipt location is invalid")
    temporary = path.parent / f".{path.name}.{os.getpid()}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        metadata = path.stat()
        if (
            metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o077
        ):
            raise BootstrapError("readiness receipt security check failed")
    finally:
        temporary.unlink(missing_ok=True)


def _receipt_status(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    checks = payload.get("checks")
    if not isinstance(checks, list) or len(checks) != len(RECEIPT_CODES):
        return None
    statuses: dict[str, str] = {}
    for item in checks:
        if (
            not isinstance(item, dict)
            or set(item) != {"code", "status"}
            or not isinstance(item["code"], str)
            or not isinstance(item["status"], str)
            or item["code"] in statuses
        ):
            return None
        statuses[item["code"]] = item["status"]
    schema_version = payload.get("schema_version")
    receipt_status = payload.get("status")
    if (
        schema_version not in {2, 3}
        or receipt_status not in {"ready", "awaiting-index"}
        or (schema_version == 2 and receipt_status != "ready")
        or payload.get("paths_redacted") is not True
        or not isinstance(payload.get("release_identity"), str)
        or re.fullmatch(r"[0-9a-f]{64}", payload["release_identity"]) is None
        or set(statuses) != set(RECEIPT_CODES)
        or statuses["fact_provider"] not in {"disabled", "ready"}
    ):
        return None
    expected_mcp = "pass" if receipt_status == "ready" else "deferred"
    if statuses["mcp_eof"] != expected_mcp or any(
        statuses[code] != "pass"
        for code in RECEIPT_CODES
        if code not in {"fact_provider", "mcp_eof"}
    ):
        return None
    return receipt_status


def _receipt_is_ready(payload: object) -> bool:
    return _receipt_status(payload) == "ready"


def _mcp_links_are_ready(current: Path, stable: Path) -> bool:
    if not current.is_symlink() or not stable.is_symlink():
        return False
    try:
        _validate_trusted_ancestor_chain(current.parent)
        _validate_trusted_ancestor_chain(stable.parent)
        current_metadata = current.lstat()
        stable_metadata = stable.lstat()
        release = current.resolve(strict=True)
        launcher = stable.resolve(strict=True)
        expected_launcher = release / "email_memory_store_mcp_launcher.sh"
        environment = release / "email_memory_environment.sh"
        releases = (current.parent / "releases").resolve(strict=True)
    except OSError:
        return False
    return bool(
        current_metadata.st_uid == os.getuid()
        and stable_metadata.st_uid == os.getuid()
        and os.readlink(stable) == str(current / "email_memory_store_mcp_launcher.sh")
        and release.is_dir()
        and not release.is_symlink()
        and release.parent == releases
        and re.fullmatch(r"[0-9a-f]{64}", release.name) is not None
        and release.stat().st_uid == os.getuid()
        and not release.stat().st_mode & 0o022
        and launcher == expected_launcher
        and launcher.is_file()
        and os.access(launcher, os.X_OK)
        and launcher.stat().st_uid == os.getuid()
        and not launcher.stat().st_mode & 0o022
        and environment.is_file()
        and not environment.is_symlink()
        and environment.stat().st_uid == os.getuid()
        and not environment.stat().st_mode & 0o022
    )


def _rollback(
    *,
    crontab_command: str,
    old_cron: bytes,
    env: dict[str, str],
    cron_attempted: bool,
    mcp_attempted: bool,
    mcp_current: Path,
    old_mcp_current: str | None,
    mcp_stable: Path,
    old_mcp_stable: str | None,
    activated: bool,
    current: Path,
    old_current: str | None,
) -> bool:
    failed = False
    actions = []
    if cron_attempted:
        actions.append(lambda: _install_crontab(crontab_command, old_cron, env))
    if mcp_attempted:
        actions.extend(
            (
                lambda: _restore_link(mcp_current, old_mcp_current),
                lambda: _restore_link(mcp_stable, old_mcp_stable),
            )
        )
    if activated:
        actions.append(lambda: _restore_link(current, old_current))
    for action in actions:
        try:
            action()
        except BaseException:
            failed = True
    return failed


def _bootstrap(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser().absolute()
    config_home = Path(args.config_home or home / ".config").expanduser().absolute()
    data_home = Path(args.data_home or home / ".local/share").expanduser().absolute()
    state_home = Path(args.state_home or home / ".local/state").expanduser().absolute()
    deployment = Path(
        args.deployment_root or data_home / "email-memory-store"
    ).absolute()
    active_root = data_home / "email-memory-store"
    _validate_production_roots(
        home=home,
        config_home=config_home,
        data_home=data_home,
        state_home=state_home,
        deployment=deployment,
        test_mode=args.test_mode,
    )
    public_checkout = Path(args.public_checkout).expanduser().absolute()
    try:
        if public_checkout.resolve(strict=True) != public_checkout:
            raise BootstrapError("deployment source checkout is not trusted")
    except OSError as error:
        raise BootstrapError("deployment source checkout is not trusted") from error
    for trusted_path in (
        home,
        config_home,
        data_home,
        state_home,
        deployment,
        active_root,
    ):
        _validate_trusted_ancestor_chain(trusted_path)
    _validate_trusted_checkout(public_checkout)
    provisioner = public_checkout / "scripts/provision_email_memory_environment.sh"
    if not provisioner.is_file() or not (public_checkout / "pyproject.toml").is_file():
        raise BootstrapError("public checkout does not contain deployment operations")
    if not args.test_mode:
        status = _run(
            _git_command(
                "-C",
                public_checkout,
                "status",
                "--porcelain",
                "--untracked-files=normal",
            ),
            env=_git_environment(),
        )
        if status.stdout:
            raise BootstrapError("deployment source checkout is not clean")
    public_revision = (
        _run(
            _git_command("-C", public_checkout, "rev-parse", "--short=12", "HEAD"),
            env=_git_environment(),
        )
        .stdout.decode()
        .strip()
    )
    if re.fullmatch(r"[0-9a-f]{12}", public_revision) is None:
        raise BootstrapError("deployment source revision is invalid")
    if args.release_id:
        release_id = args.release_id
    else:
        release_id = f"{public_revision}-py314-{args.accelerator}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", release_id):
        raise BootstrapError("release identifier is invalid")
    schedule = _validate_schedule(args.cron_schedule)
    if args.replace_scheduler_command:
        replacement = Path(args.replace_scheduler_command).expanduser()
        args.replace_scheduler_command = _validate_cron_path(
            replacement, label="replacement scheduler command"
        )
    crontab_command = _cron_executable(args.crontab_command)
    candidate = deployment / "envs" / release_id
    venv = candidate / "venv"
    python = venv / "bin/python"
    cli = venv / "bin/email-memory-store"
    mcp = venv / "bin/email-memory-store-mcp"
    current = active_root / "current"
    nightly = current / "bin/email-memory-store-deploy"
    cron_nightly = _validate_cron_path(nightly, label="nightly scheduler command")
    receipt = candidate / ".deployment-readiness.json"
    env = dict(os.environ) | {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(data_home),
        "XDG_STATE_HOME": str(state_home),
        "EMAIL_MEMORY_STORE_DEPLOYMENT_ROOT": str(deployment),
    }
    env = {
        key: value
        for key, value in env.items()
        if not key.startswith(("PYTHON", *_CONNECTOR_CONTROL_PREFIXES))
        and key not in _SHELL_INJECTION_VARIABLES
    }
    env["PYTHONNOUSERSITE"] = "1"
    checks = {code: "pending" for code in RECEIPT_CODES}
    old_current = os.readlink(current) if current.is_symlink() else None
    if current.exists() and not current.is_symlink():
        raise BootstrapError("active release selector is invalid")
    deployment_status = "ready"
    old_cron = _read_crontab(crontab_command, env)
    activated = False
    cron_attempted = False
    mcp_attempted = False
    mcp_current = data_home / "email-memory-store/mcp-launcher/current"
    mcp_stable = home / ".local/bin/email_memory_store_mcp_hermes.sh"
    for link in (mcp_current, mcp_stable):
        if (link.exists() or link.is_symlink()) and not link.is_symlink():
            raise BootstrapError("MCP launcher destination is not a symlink")
    old_mcp_current = os.readlink(mcp_current) if mcp_current.is_symlink() else None
    old_mcp_stable = os.readlink(mcp_stable) if mcp_stable.is_symlink() else None
    initial_install = _is_first_deployment(
        current=current,
        mcp_current=mcp_current,
        mcp_stable=mcp_stable,
        old_cron=old_cron,
        candidate=candidate,
    )
    try:
        signal_context = _transaction_signal_handlers()
        signal_context.__enter__()
        if candidate.exists():
            if not _candidate_is_verified(candidate) or not _release_marker_matches(
                candidate,
                public_revision=public_revision,
                accelerator=args.accelerator,
            ):
                raise BootstrapError("existing candidate release is not verified")
        else:
            _run(
                [
                    "/bin/bash",
                    "-p",
                    provisioner,
                    "--public-checkout",
                    public_checkout,
                    "--deployment-root",
                    deployment,
                    "--release-id",
                    release_id,
                    "--accelerator",
                    args.accelerator,
                    "--no-activate",
                ],
                env=env,
            )
        if not _candidate_is_verified(candidate) or not _release_marker_matches(
            candidate,
            public_revision=public_revision,
            accelerator=args.accelerator,
        ):
            raise BootstrapError("candidate release is not verified")
        release_identity = _release_identity(candidate)
        checks["release_staged"] = "pass"
        scripts = _candidate_scripts(python, env)
        maintenance = scripts / "nightly_maintenance.sh"
        mcp_installer = scripts / "install_email_memory_mcp_launcher.sh"
        if any(
            not path.is_file()
            for path in (python, cli, mcp, maintenance, mcp_installer)
        ):
            raise BootstrapError("candidate release is incomplete")
        if args.regenerate_configuration or not _configuration_is_complete(config_home):
            _run_setup(cli, env, test_mode=args.test_mode)
        config = _load_configuration(python, env)
        if not config.get("EMAIL_MEMORY_CREDENTIAL_REFERENCE", "").strip():
            raise BootstrapError(
                "local configuration lacks a credential audit reference"
            )
        checks["configuration_loaded"] = "pass"
        runtime_config = config["EMAIL_MEMORY_STORE_RUNTIME_CONFIG"]
        runtime_env = env | {"EMAIL_MEMORY_STORE_RUNTIME_CONFIG": runtime_config}
        _run([cli, "init-db"], env=runtime_env)
        checks["database_initialized"] = "pass"
        doctor = _run(
            [
                cli,
                "runtime-doctor",
                "--require",
                "mail",
                "--require",
                "selected-llm",
            ],
            env=runtime_env,
        )
        _verify_doctor(doctor.stdout)
        checks["runtime_doctor"] = "pass"
        _probe_mail_connector(
            config["EMAIL_MEMORY_MAIL_CLIENT_EXECUTABLE"],
            config["ACCOUNT_NAME"],
            runtime_env,
            timeout=args.probe_timeout,
        )
        checks["mail_connector"] = "pass"
        checks["fact_provider"] = _check_fact_provider(python, config, runtime_env)
        maintenance_env = runtime_env | {
            "EMAIL_MEMORY_PREFLIGHT_ONLY": "1",
            "EMAIL_MEMORY_TEST_MODE": "1",
            "EMAIL_MEMORY_STORE_ENVIRONMENT": str(venv),
            "EMAIL_MEMORY_STORE_COMMAND": str(cli),
            "EMAIL_MEMORY_STORE_MCP_COMMAND": str(mcp),
            "EMAIL_MEMORY_OPERATIONAL_PYTHON": str(python),
        }
        _run(["/bin/bash", "-p", maintenance], env=maintenance_env)
        checks["maintenance_preflight"] = "pass"
        index_status = _run([cli, "embed-status"], env=runtime_env)
        indexed_documents = _indexed_document_count(index_status.stdout)
        if indexed_documents:
            _run(
                [mcp],
                env=runtime_env,
                input_bytes=b"",
                timeout=args.probe_timeout,
            )
            checks["mcp_eof"] = "pass"
        elif initial_install:
            deployment_status = "awaiting-index"
            checks["mcp_eof"] = "deferred"
        else:
            raise BootstrapError("candidate MCP readiness failed")
        _sync_release_tree(candidate, env)
        mcp_attempted = True
        _run(["/bin/bash", "-p", mcp_installer], env=env)
        if not _mcp_links_are_ready(mcp_current, mcp_stable):
            raise BootstrapError("MCP launcher links are not ready")
        _sync_mcp_publication(mcp_current, mcp_stable, env)
        checks["mcp_launcher"] = "pass"
        candidate_nightly = candidate / "bin/email-memory-store-deploy"
        if not candidate_nightly.is_file() or not os.access(candidate_nightly, os.X_OK):
            raise BootstrapError("active deployment executable is unavailable")
        cron_line = f"{schedule} {cron_nightly} nightly"
        replaced_command = args.replace_scheduler_command or ""
        cron_attempted = True
        _install_crontab(
            crontab_command,
            _managed_crontab(old_cron, cron_line, replaced_command),
            env,
        )
        checks["scheduler"] = "pass"
        checks["release_activated"] = "pass"
        _write_receipt(
            receipt,
            candidate,
            checks,
            release_identity,
            status=deployment_status,
        )
        activated = True
        _atomic_symlink(candidate, current)
        if args.fail_after_activation:
            if not args.test_mode:
                raise BootstrapError("failure injection requires explicit test mode")
            raise BootstrapError("injected post-activation failure")
    except BaseException as error:
        rollback_failed = _rollback(
            crontab_command=crontab_command,
            old_cron=old_cron,
            env=env,
            cron_attempted=cron_attempted,
            mcp_attempted=mcp_attempted,
            mcp_current=mcp_current,
            old_mcp_current=old_mcp_current,
            mcp_stable=mcp_stable,
            old_mcp_stable=old_mcp_stable,
            activated=activated,
            current=current,
            old_current=old_current,
        )
        if rollback_failed:
            if isinstance(error, BootstrapError):
                raise BootstrapError(f"{error}; rollback failed") from error
            error.add_note("deployment rollback failed")
        raise
    finally:
        if "signal_context" in locals():
            signal_context.__exit__(*sys.exc_info())
    print(
        json.dumps(
            {
                "schema_version": 1,
                "status": deployment_status,
                "paths_redacted": True,
            }
        )
    )
    return 0


def _doctor(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser().absolute()
    config_home = Path(args.config_home or home / ".config").absolute()
    data_home = Path(args.data_home or home / ".local/share").absolute()
    state_home = Path(args.state_home or home / ".local/state").absolute()
    deployment = Path(
        args.deployment_root or data_home / "email-memory-store"
    ).absolute()
    active_root = data_home / "email-memory-store"
    current = active_root / "current"
    mcp_current = data_home / "email-memory-store/mcp-launcher/current"
    mcp_stable = home / ".local/bin/email_memory_store_mcp_hermes.sh"
    env = dict(os.environ) | {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(data_home),
        "XDG_STATE_HOME": str(state_home),
    }
    env = {
        key: value
        for key, value in env.items()
        if not key.startswith(("PYTHON", *_CONNECTOR_CONTROL_PREFIXES))
        and key not in _SHELL_INJECTION_VARIABLES
    }
    env["PYTHONNOUSERSITE"] = "1"
    healthy = current.is_symlink()
    reported_status = "not-ready"
    try:
        _validate_production_roots(
            home=home,
            config_home=config_home,
            data_home=data_home,
            state_home=state_home,
            deployment=deployment,
            test_mode=bool(getattr(args, "test_mode", False)),
        )
        for trusted_path in (
            home,
            config_home,
            data_home,
            state_home,
            deployment,
            active_root,
        ):
            _validate_trusted_ancestor_chain(trusted_path)
        if not healthy:
            raise BootstrapError("active release selector is invalid")
        active = current.resolve(strict=True)
        receipt = active / ".deployment-readiness.json"
        if not (
            active.parent == (deployment / "envs").resolve(strict=True)
            and _candidate_is_verified(active)
        ):
            raise BootstrapError("deployment structural readiness validation failed")
        receipt_metadata = receipt.lstat()
        if (
            not stat.S_ISREG(receipt_metadata.st_mode)
            or receipt_metadata.st_uid != os.getuid()
            or receipt_metadata.st_nlink != 1
            or receipt_metadata.st_mode & 0o077
        ):
            raise BootstrapError("deployment receipt readiness validation failed")
        receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
        receipt_status = _receipt_status(receipt_payload)
        if (
            receipt_status is None
            or not isinstance(receipt_payload, dict)
            or receipt_payload.get("release_identity") != _release_identity(active)
            or not _mcp_links_are_ready(mcp_current, mcp_stable)
        ):
            raise BootstrapError("deployment receipt readiness validation failed")
        venv = active / "venv"
        config = _load_configuration(venv / "bin/python", env)
        runtime_env = env | {
            "EMAIL_MEMORY_STORE_RUNTIME_CONFIG": config[
                "EMAIL_MEMORY_STORE_RUNTIME_CONFIG"
            ]
        }
        result = _run(
            [
                venv / "bin/email-memory-store",
                "runtime-doctor",
                "--require",
                "mail",
                "--require",
                "selected-llm",
            ],
            env=runtime_env,
        )
        _verify_doctor(result.stdout)
        _probe_mail_connector(
            config["EMAIL_MEMORY_MAIL_CLIENT_EXECUTABLE"],
            config["ACCOUNT_NAME"],
            runtime_env,
            timeout=args.probe_timeout,
        )
        _check_fact_provider(venv / "bin/python", config, runtime_env)
        index_status = _run(
            [venv / "bin/email-memory-store", "embed-status"], env=runtime_env
        )
        indexed_documents = _indexed_document_count(index_status.stdout)
        if indexed_documents:
            _run(
                [venv / "bin/email-memory-store-mcp"],
                env=runtime_env,
                input_bytes=b"",
                timeout=args.probe_timeout,
            )
            live_status = "ready"
        elif receipt_status == "awaiting-index":
            live_status = "awaiting-index"
        else:
            raise BootstrapError("active MCP readiness failed")
        cron = _read_crontab(_cron_executable(args.crontab_command), env).decode()
        schedule = _validate_schedule(args.cron_schedule)
        nightly = current / "bin/email-memory-store-deploy"
        resolved_nightly = nightly.resolve(strict=True)
        healthy = (
            healthy
            and resolved_nightly.is_file()
            and os.access(resolved_nightly, os.X_OK)
        )
        expected = f"{schedule} {_validate_cron_path(nightly, label='nightly scheduler command')} nightly"
        expected_block = f"{MANAGED_START}\n{expected}\n{MANAGED_END}\n"
        _without_managed_cron(cron.encode())
        healthy = healthy and cron.count(expected_block) == 1
        if healthy:
            reported_status = live_status
    except BootstrapError, KeyError, OSError, UnicodeDecodeError, json.JSONDecodeError:
        healthy = False
    print(
        json.dumps(
            {
                "schema_version": 1,
                "status": reported_status,
                "paths_redacted": True,
            }
        )
    )
    if not healthy:
        return DOCTOR_NOT_READY_EXIT
    if reported_status == "awaiting-index":
        return DOCTOR_AWAITING_INDEX_EXIT
    return DOCTOR_READY_EXIT


def _nightly(_args: argparse.Namespace) -> NoReturn:
    launcher = _installed_script("nightly_cron_launcher.sh")
    if not launcher.is_file():
        raise BootstrapError("installed nightly launcher is unavailable")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PYTHON")
        and not key.startswith("EMAIL_MEMORY_")
        and not key.startswith(_CONNECTOR_CONTROL_PREFIXES)
        and key not in _SHELL_INJECTION_VARIABLES
        and key != "LLM_PREFLIGHT_EXPECTED_RESPONSE"
    }
    env["PYTHONNOUSERSITE"] = "1"
    canonical_home = pwd.getpwuid(os.getuid()).pw_dir
    env.update(
        {
            "HOME": canonical_home,
            "XDG_CONFIG_HOME": f"{canonical_home}/.config",
            "XDG_DATA_HOME": f"{canonical_home}/.local/share",
            "XDG_STATE_HOME": f"{canonical_home}/.local/state",
        }
    )
    os.execve("/bin/bash", ["/bin/bash", "-p", str(launcher)], env)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="email-memory-store-deploy",
        description="Deploy from a clean public email-memory Git checkout.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument(
        "--public-checkout", required=True, help="clean public Git checkout"
    )
    bootstrap.add_argument("--home", default=pwd.getpwuid(os.getuid()).pw_dir)
    bootstrap.add_argument("--config-home")
    bootstrap.add_argument("--data-home")
    bootstrap.add_argument("--state-home")
    bootstrap.add_argument("--deployment-root")
    bootstrap.add_argument("--release-id")
    bootstrap.add_argument(
        "--accelerator", choices=("auto", "cpu", "cuda", "mps"), default="auto"
    )
    bootstrap.add_argument(
        "--probe-timeout",
        type=_positive_timeout,
        default=DEFAULT_PROBE_TIMEOUT_SECONDS,
    )
    bootstrap.add_argument("--cron-schedule", default="30 2 * * *")
    bootstrap.add_argument("--crontab-command", default="crontab")
    bootstrap.add_argument("--replace-scheduler-command")
    bootstrap.add_argument("--regenerate-configuration", action="store_true")
    bootstrap.add_argument("--test-mode", action="store_true")
    bootstrap.add_argument(
        "--fail-after-activation", action="store_true", help=argparse.SUPPRESS
    )
    bootstrap.set_defaults(handler=_bootstrap)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--home", default=pwd.getpwuid(os.getuid()).pw_dir)
    doctor.add_argument("--config-home")
    doctor.add_argument("--data-home")
    doctor.add_argument("--state-home")
    doctor.add_argument("--deployment-root")
    doctor.add_argument("--cron-schedule", default="30 2 * * *")
    doctor.add_argument("--crontab-command", default="crontab")
    doctor.add_argument(
        "--probe-timeout",
        type=_positive_timeout,
        default=DEFAULT_PROBE_TIMEOUT_SECONDS,
    )
    doctor.set_defaults(handler=_doctor)
    nightly = commands.add_parser("nightly")
    nightly.set_defaults(handler=_nightly)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (BootstrapError, KeyError) as error:
        print(
            str(error)
            if isinstance(error, BootstrapError)
            else "deployment configuration is incomplete",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

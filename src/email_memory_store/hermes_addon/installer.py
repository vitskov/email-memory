"""Transactional installer for the optional Hermes Telegram topic add-on.

The installer changes only the active Hermes configuration and one user skill.
It deliberately has no gateway lifecycle capability: Hermes hot-loads the
configured DM topic when the next message arrives in that topic.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import tempfile
from typing import Mapping, Sequence

from ..control import jobs as control_jobs
from ..tui.private_setup import load_private_setup
from .skill import SKILL_CONTENT, SKILL_NAME


TOPIC_NAME = "Email Memory"
RETRIEVAL_SERVER = "email_memory_store"
CONTROL_SERVER = "email_memory_store_control"
DEFAULT_LAUNCHER_NAME = "email_memory_store_mcp_hermes.sh"
RAW_CONFIG_WRITER = r"""
import fcntl
import hashlib
import json
import os
import sys
from hermes_cli.config import (
    atomic_config_write,
    get_config_path,
    read_user_config_raw,
    validate_config_structure,
)

payload = json.load(sys.stdin)
if set(payload) != {"updates", "deletes", "expected_digest"}:
    raise SystemExit(2)
config_path = get_config_path()
lock_path = config_path.with_name(config_path.name + ".email-memory-addon.lock")
lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
try:
    os.fchmod(lock_fd, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    current = config_path.read_bytes()
    if hashlib.sha256(current).hexdigest() != payload["expected_digest"]:
        raise SystemExit(5)
    config = read_user_config_raw()
    for item in payload["updates"]:
        path, value = item
        cursor = config
        for component in path[:-1]:
            child = cursor.get(component)
            if child is None:
                child = {}
                cursor[component] = child
            if not isinstance(child, dict):
                raise SystemExit(3)
            cursor = child
        cursor[path[-1]] = value
    for path in payload["deletes"]:
        cursor = config
        for component in path[:-1]:
            child = cursor.get(component)
            if not isinstance(child, dict):
                cursor = None
                break
            cursor = child
        if cursor is not None:
            cursor.pop(path[-1], None)
    issues = validate_config_structure(config)
    if any(issue.severity == "error" for issue in issues):
        raise SystemExit(4)
    # Re-check under the transaction lock immediately before replacement.
    if hashlib.sha256(config_path.read_bytes()).hexdigest() != payload["expected_digest"]:
        raise SystemExit(5)
    atomic_config_write(
        config_path, config, default_flow_style=False, sort_keys=False
    )
    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    print(json.dumps({"digest": digest}, separators=(",", ":")))
finally:
    os.close(lock_fd)
"""


class HermesAddonError(RuntimeError):
    """A fail-closed add-on installation error without private values."""


@dataclass(frozen=True)
class HermesAddonResult:
    """Non-secret paths changed by a successful installation."""

    config_path: Path
    skill_path: Path


@dataclass(frozen=True)
class _FileSnapshot:
    existed: bool
    content: bytes = b""
    mode: int = 0o600


@dataclass(frozen=True)
class _WriterResult:
    digest: str
    rollback_owned: bool


def _assert_no_symlink_components(path: Path) -> None:
    candidate = path.absolute()
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            raise HermesAddonError("Hermes add-on path contains a symbolic link")


def _assert_trusted_ancestry(path: Path) -> None:
    current = path.resolve(strict=True) if path.exists() else path.absolute()
    current = current if current.is_dir() else current.parent
    while True:
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise HermesAddonError("Hermes add-on path ancestry is unsafe")
        mode = stat.S_IMODE(metadata.st_mode)
        trusted_sticky = (
            metadata.st_uid == 0
            and bool(mode & stat.S_ISVTX)
            and current in {Path("/tmp"), Path("/var/tmp")}
        )
        if metadata.st_uid not in {0, os.getuid()} or (
            mode & (stat.S_IWGRP | stat.S_IWOTH) and not trusted_sticky
        ):
            raise HermesAddonError("Hermes add-on path ancestry is unsafe")
        if current == current.parent:
            break
        current = current.parent


def _assert_private_directory(path: Path, *, create: bool = False) -> None:
    _assert_no_symlink_components(path)
    if create:
        path.mkdir(parents=False, exist_ok=True, mode=0o700)
    try:
        info = path.lstat()
    except OSError as error:
        raise HermesAddonError("Hermes add-on directory is unavailable") from error
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise HermesAddonError("Hermes add-on directory is unsafe")
    if stat.S_IMODE(info.st_mode) & (stat.S_IRWXG | stat.S_IRWXO):
        raise HermesAddonError("Hermes add-on directory must be owner-only")


def _assert_private_file(path: Path) -> None:
    _assert_no_symlink_components(path)
    try:
        info = path.lstat()
    except OSError as error:
        raise HermesAddonError("Hermes configuration is unavailable") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
    ):
        raise HermesAddonError("Hermes configuration is unsafe")
    if stat.S_IMODE(info.st_mode) & (stat.S_IRWXG | stat.S_IRWXO):
        raise HermesAddonError("Hermes configuration must be owner-only")


def _validate_executable(
    path: Path, *, label: str, allow_symlink: bool = False
) -> Path:
    if not path.is_absolute():
        raise HermesAddonError(f"{label} must be an absolute executable path")
    if path.is_symlink() and not allow_symlink:
        raise HermesAddonError(f"{label} cannot be a symbolic link")
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except (OSError, RuntimeError) as error:
        raise HermesAddonError(f"{label} is unavailable") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or not os.access(resolved, os.X_OK)
    ):
        raise HermesAddonError(f"{label} is not an executable file")
    if info.st_uid not in {0, os.getuid()} or stat.S_IMODE(info.st_mode) & (
        stat.S_IWGRP | stat.S_IWOTH
    ):
        raise HermesAddonError(f"{label} has unsafe ownership or permissions")
    _assert_no_symlink_components(path.parent)
    _assert_trusted_ancestry(path.parent)
    _assert_trusted_ancestry(resolved.parent)
    return path


def _validate_hermes_python(hermes_executable: Path) -> Path:
    """Validate the venv Python, including uv trees behind a sealed ancestor."""
    candidate = hermes_executable.resolve(strict=True).parent / "python"
    if not candidate.is_absolute():
        raise HermesAddonError("Hermes Python executable is unavailable")
    _assert_no_symlink_components(candidate.parent)
    if not _has_sealed_owner_ancestor(candidate.parent):
        _assert_trusted_ancestry(candidate.parent)
    try:
        link_info = candidate.lstat()
        resolved = candidate.resolve(strict=True)
        info = resolved.stat()
    except (OSError, RuntimeError) as error:
        raise HermesAddonError("Hermes Python executable is unavailable") from error
    if link_info.st_uid != os.getuid() or not (
        stat.S_ISREG(link_info.st_mode) or stat.S_ISLNK(link_info.st_mode)
    ):
        raise HermesAddonError("Hermes Python executable is unsafe")
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or (info.st_nlink != 1 or not os.access(resolved, os.X_OK))
    ):
        raise HermesAddonError("Hermes Python executable is unsafe")
    sealed = _has_sealed_owner_ancestor(resolved.parent)
    if not sealed:
        if stat.S_IMODE(info.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
            raise HermesAddonError("Hermes Python executable is unsafe")
        _assert_trusted_ancestry(resolved.parent)
    return candidate


def _has_sealed_owner_ancestor(path: Path) -> bool:
    current = path
    while current != current.parent:
        metadata = current.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise HermesAddonError("Hermes Python executable ancestry is unsafe")
        if metadata.st_uid == os.getuid() and not mode & (stat.S_IRWXG | stat.S_IRWXO):
            return True
        if metadata.st_uid != os.getuid():
            return False
        current = current.parent
    return False


def _command_environment(
    environ: Mapping[str, str] | None, *, hermes_home: Path | None
) -> dict[str, str]:
    source = os.environ if environ is None else environ
    home = Path(source.get("HOME") or pwd.getpwuid(os.geteuid()).pw_dir)
    if not home.is_absolute():
        raise HermesAddonError("Hermes command environment is invalid")
    command_env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": source.get("XDG_CONFIG_HOME", str(home / ".config")),
        "XDG_DATA_HOME": source.get("XDG_DATA_HOME", str(home / ".local/share")),
        "XDG_STATE_HOME": source.get("XDG_STATE_HOME", str(home / ".local/state")),
        "PATH": "/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
        "NO_COLOR": "1",
        "TERM": "dumb",
        "LC_ALL": "C.UTF-8",
    }
    for variable in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        selected = Path(command_env[variable])
        if not selected.is_absolute() or ".." in selected.parts:
            raise HermesAddonError("Hermes command environment is invalid")
    if hermes_home is not None:
        command_env["HERMES_HOME"] = str(hermes_home)
    return command_env


def _run_hermes(
    executable: Path,
    args: Sequence[str],
    *,
    command_env: Mapping[str, str],
    allow_missing: bool = False,
) -> str:
    allowed = {
        ("config", "path"),
        ("config", "env-path"),
        ("config", "check"),
        ("config", "get"),
    }
    if len(args) < 2 or tuple(args[:2]) not in allowed:
        raise HermesAddonError("unsupported Hermes configuration command")
    try:
        completed = subprocess.run(
            [str(executable), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(command_env),
        )
    except OSError, subprocess.SubprocessError:
        raise HermesAddonError("Hermes configuration command failed") from None
    if completed.returncode != 0:
        combined = f"{completed.stdout}\n{completed.stderr}".strip()
        if (
            allow_missing
            and completed.returncode == 1
            and combined.startswith("Config key not set:")
        ):
            return ""
        raise HermesAddonError("Hermes configuration command failed")
    return completed.stdout.strip()


def _run_structured_writer(
    python_executable: Path,
    *,
    updates: Sequence[tuple[Sequence[str], object]],
    deletes: Sequence[Sequence[str]],
    expected_digest: str,
    command_env: Mapping[str, str],
) -> str:
    payload = json.dumps(
        {
            "updates": [[list(path), value] for path, value in updates],
            "deletes": [list(path) for path in deletes],
            "expected_digest": expected_digest,
        },
        separators=(",", ":"),
    )
    try:
        completed = subprocess.run(
            [str(python_executable), "-I", "-c", RAW_CONFIG_WRITER],
            input=payload,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(command_env),
        )
    except OSError, subprocess.SubprocessError:
        raise HermesAddonError("Hermes structured configuration write failed") from None
    if completed.returncode != 0:
        raise HermesAddonError("Hermes structured configuration write failed")
    try:
        result = json.loads(completed.stdout)
        digest = result["digest"]
    except json.JSONDecodeError, KeyError, TypeError:
        raise HermesAddonError("Hermes structured configuration write failed") from None
    if not isinstance(digest, str) or len(digest) != 64:
        raise HermesAddonError("Hermes structured configuration write failed")
    return digest


def _read_json_setting(
    executable: Path,
    key: str,
    *,
    command_env: Mapping[str, str],
    absent: object,
) -> object:
    output = _run_hermes(
        executable,
        ("config", "get", "--json", key),
        command_env=command_env,
        allow_missing=True,
    )
    if not output:
        return absent
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        raise HermesAddonError(
            "Hermes returned an invalid configuration value"
        ) from None


def _run_structured_writer_resilient(
    python_executable: Path,
    *,
    updates: Sequence[tuple[Sequence[str], object]],
    deletes: Sequence[Sequence[str]],
    expected_digest: str,
    config_path: Path,
    hermes_executable: Path,
    command_env: Mapping[str, str],
) -> _WriterResult:
    """Recover safely when the writer committed but its response was lost."""
    try:
        return _WriterResult(
            digest=_run_structured_writer(
                python_executable,
                updates=updates,
                deletes=deletes,
                expected_digest=expected_digest,
                command_env=command_env,
            ),
            rollback_owned=True,
        )
    except HermesAddonError:
        absent = object()
        for path, expected in updates:
            actual = _read_json_setting(
                hermes_executable,
                ".".join(path),
                command_env=command_env,
                absent=absent,
            )
            if actual is absent or actual != expected:
                raise
        for path in deletes:
            actual = _read_json_setting(
                hermes_executable,
                ".".join(path),
                command_env=command_env,
                absent=absent,
            )
            if actual is not absent:
                raise
        current_digest = _path_digest(config_path)
        if current_digest is None:
            raise
        return _WriterResult(
            digest=current_digest,
            # If bytes changed without an authenticated writer response, the
            # target settings may simply have predated an unrelated CAS
            # failure. Never claim those observed bytes for rollback.
            rollback_owned=current_digest == expected_digest,
        )


def _read_private_text(path: Path, *, maximum_bytes: int = 64 * 1024) -> str:
    _assert_trusted_ancestry(path.parent)
    _assert_private_file(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise HermesAddonError("Hermes private environment is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & (stat.S_IRWXG | stat.S_IRWXO)
            or metadata.st_size > maximum_bytes
        ):
            raise HermesAddonError("Hermes private environment is unsafe")
        payload = os.read(descriptor, maximum_bytes + 1)
        if len(payload) > maximum_bytes:
            raise HermesAddonError("Hermes private environment is unsafe")
        return payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise HermesAddonError("Hermes private environment is invalid") from error
    finally:
        os.close(descriptor)


def _verify_owner_dm_preflight(
    executable: Path,
    *,
    chat_id: int,
    active_home: Path,
    command_env: Mapping[str, str],
) -> None:
    output = _run_hermes(executable, ("config", "env-path"), command_env=command_env)
    env_path = Path(output)
    if not env_path.is_absolute():
        raise HermesAddonError("Hermes owner authorization is invalid")
    try:
        content = _read_private_text(env_path)
    except HermesAddonError:
        raise HermesAddonError("Hermes owner authorization is invalid") from None
    assignments: dict[str, list[str]] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        assignments.setdefault(key.strip(), []).append(value.strip())
    values = assignments.get("TELEGRAM_ALLOWED_USERS", [])
    if (
        len(values) != 1
        or not values[0].isascii()
        or not values[0].isdigit()
        or values[0].startswith("0")
        or int(values[0]) != chat_id
    ):
        raise HermesAddonError("Hermes owner authorization is invalid")

    # Hermes authorization is a union of platform, global, group, bot, and
    # pairing grants. Reject every alternate ingress rather than assuming the
    # exact DM allowlist is the only effective source.
    forbidden_nonempty = (
        "GATEWAY_ALLOWED_USERS",
        "TELEGRAM_GROUP_ALLOWED_USERS",
        "TELEGRAM_GROUP_ALLOWED_CHATS",
    )
    forbidden_active = (
        "GATEWAY_ALLOW_ALL_USERS",
        "TELEGRAM_ALLOW_ALL_USERS",
        "TELEGRAM_GUEST_MODE",
    )
    if any(
        any(value for value in assignments.get(key, [])) for key in forbidden_nonempty
    ):
        raise HermesAddonError("Hermes owner authorization is invalid")
    inactive = {"", "0", "false", "no", "off", "none"}
    if any(
        any(value.strip().lower() not in inactive for value in assignments.get(key, []))
        for key in forbidden_active
    ):
        raise HermesAddonError("Hermes owner authorization is invalid")
    if any(
        value.strip().lower() not in inactive
        for value in assignments.get("TELEGRAM_ALLOW_BOTS", [])
    ):
        raise HermesAddonError("Hermes owner authorization is invalid")
    if any(
        value.strip().lower() in {"1", "true", "yes", "on"}
        for value in assignments.get("GATEWAY_MULTIPLEX_PROFILES", [])
    ):
        raise HermesAddonError("Hermes owner authorization is invalid")

    absent = object()
    exact_owner_keys = (
        "platforms.telegram.allow_from",
        "platforms.telegram.extra.allow_from",
    )
    for key in exact_owner_keys:
        configured = _read_json_setting(
            executable, key, command_env=command_env, absent=absent
        )
        if configured is absent:
            continue
        if isinstance(configured, list):
            configured_values = [str(item).strip() for item in configured]
        elif isinstance(configured, str):
            configured_values = [part.strip() for part in configured.split(",")]
        else:
            raise HermesAddonError("Hermes owner authorization is invalid")
        if configured_values != [str(chat_id)]:
            raise HermesAddonError("Hermes owner authorization is invalid")

    forbidden_config_keys = (
        "platforms.telegram.group_allow_from",
        "platforms.telegram.extra.group_allow_from",
        "platforms.telegram.group_allowed_chats",
        "platforms.telegram.extra.group_allowed_chats",
        "platforms.telegram.allow_admin_from",
        "platforms.telegram.extra.allow_admin_from",
        "platforms.telegram.group_allow_admin_from",
        "platforms.telegram.extra.group_allow_admin_from",
    )
    for key in forbidden_config_keys:
        configured = _read_json_setting(
            executable, key, command_env=command_env, absent=absent
        )
        if configured is not absent and configured not in (None, "", [], {}):
            raise HermesAddonError("Hermes owner authorization is invalid")

    safe_config_values = {
        "platforms.telegram.allow_bots": inactive,
        "platforms.telegram.extra.allow_bots": inactive,
        "platforms.telegram.guest_mode": inactive,
        "platforms.telegram.extra.guest_mode": inactive,
        "platforms.telegram.dm_policy": {"allowlist", "disabled"},
        "platforms.telegram.extra.dm_policy": {"allowlist", "disabled"},
        "platforms.telegram.group_policy": {"disabled"},
        "platforms.telegram.extra.group_policy": {"disabled"},
        "platforms.telegram.unauthorized_dm_behavior": {"ignore"},
        "platforms.telegram.extra.unauthorized_dm_behavior": {"ignore"},
    }
    for key, safe_values in safe_config_values.items():
        configured = _read_json_setting(
            executable, key, command_env=command_env, absent=absent
        )
        if configured is absent:
            continue
        normalized = str(configured).strip().lower()
        if normalized not in safe_values:
            raise HermesAddonError("Hermes owner authorization is invalid")

    for key in ("multiplex_profiles", "gateway.multiplex_profiles"):
        configured = _read_json_setting(
            executable, key, command_env=command_env, absent=absent
        )
        if configured is not absent and configured not in (
            False,
            None,
            0,
            "false",
            "0",
        ):
            raise HermesAddonError("Hermes owner authorization is invalid")

    # Do not instantiate PairingStore here: its constructor migrates files.
    for pairing_path in (
        active_home / "platforms" / "pairing" / "telegram-approved.json",
        active_home / "pairing" / "telegram-approved.json",
    ):
        if not pairing_path.exists() and not pairing_path.is_symlink():
            continue
        try:
            approved = json.loads(_read_private_text(pairing_path))
        except HermesAddonError, json.JSONDecodeError:
            raise HermesAddonError("Hermes owner authorization is invalid") from None
        if not isinstance(approved, dict) or any(
            str(user_id) != str(chat_id) for user_id in approved
        ):
            raise HermesAddonError("Hermes owner authorization is invalid")


def _merge_topic(existing: object, *, chat_id: int, thread_id: int) -> list[object]:
    if existing is None:
        topics_config: list[object] = []
    elif isinstance(existing, list):
        topics_config = json.loads(json.dumps(existing))
    else:
        raise HermesAddonError("Hermes Telegram topic configuration is malformed")

    matching_chat: dict[str, object] | None = None
    for entry in topics_config:
        if not isinstance(entry, dict):
            raise HermesAddonError("Hermes Telegram topic configuration is malformed")
        configured_chat = entry.get("chat_id")
        if str(configured_chat) == str(chat_id):
            if matching_chat is not None:
                raise HermesAddonError(
                    "Hermes Telegram topic configuration is ambiguous"
                )
            matching_chat = entry

    if matching_chat is None:
        topics_config.append(
            {
                "chat_id": chat_id,
                "topics": [
                    {
                        "name": TOPIC_NAME,
                        "thread_id": thread_id,
                        "skill": SKILL_NAME,
                    }
                ],
            }
        )
        return topics_config

    topics = matching_chat.get("topics")
    if not isinstance(topics, list):
        raise HermesAddonError("Hermes Telegram topic configuration is malformed")
    matching_topic: dict[str, object] | None = None
    for topic in topics:
        if not isinstance(topic, dict):
            raise HermesAddonError("Hermes Telegram topic configuration is malformed")
        same_name = topic.get("name") == TOPIC_NAME
        same_thread = str(topic.get("thread_id")) == str(thread_id)
        if same_name and not same_thread or same_thread and not same_name:
            raise HermesAddonError(
                "Hermes Telegram topic conflicts with the requested binding"
            )
        if same_name:
            if matching_topic is not None:
                raise HermesAddonError(
                    "Hermes Telegram topic configuration is ambiguous"
                )
            matching_topic = topic

    if matching_topic is None:
        topics.append({"name": TOPIC_NAME, "thread_id": thread_id, "skill": SKILL_NAME})
    else:
        existing_skill = matching_topic.get("skill")
        if existing_skill not in (None, SKILL_NAME):
            raise HermesAddonError("Hermes Telegram topic skill ownership conflicts")
        matching_topic["skill"] = SKILL_NAME
    return topics_config


def _remove_topic(existing: object, *, chat_id: int, thread_id: int) -> list[object]:
    merged = _merge_topic(existing, chat_id=chat_id, thread_id=thread_id)
    # _merge_topic also validates duplicates and conflicts. The requested
    # binding must already have existed for uninstall; remove exactly it.
    original = json.loads(json.dumps(existing)) if isinstance(existing, list) else []
    if merged != original:
        raise HermesAddonError("Hermes Telegram topic binding is not installed")
    result: list[object] = []
    for entry in original:
        if not isinstance(entry, dict) or str(entry.get("chat_id")) != str(chat_id):
            result.append(entry)
            continue
        topics = entry.get("topics")
        if not isinstance(topics, list):
            raise HermesAddonError("Hermes Telegram topic configuration is malformed")
        retained = [
            topic
            for topic in topics
            if not (
                isinstance(topic, dict)
                and topic.get("name") == TOPIC_NAME
                and str(topic.get("thread_id")) == str(thread_id)
                and topic.get("skill") == SKILL_NAME
            )
        ]
        if len(retained) == len(topics):
            raise HermesAddonError("Hermes Telegram topic binding is not installed")
        if retained:
            entry["topics"] = retained
            result.append(entry)
    return result


def _retrieval_config(launcher_path: Path) -> dict[str, object]:
    return {
        "command": str(launcher_path),
        "args": [],
        "enabled": True,
        "trust": "full",
        "timeout": 120,
        "connect_timeout": 60,
        "tools": {
            "include": ["search", "ask"],
            "resources": False,
            "prompts": False,
        },
    }


def _control_config(launcher_path: Path) -> dict[str, object]:
    return {
        "command": str(launcher_path),
        "args": ["--mode", "control"],
        "enabled": True,
        "trust": "untrusted",
        "timeout": 120,
        "connect_timeout": 60,
        "tools": {
            "include": ["system_status", "job_start", "job_status"],
            "resources": False,
            "prompts": False,
        },
    }


def _snapshot(path: Path) -> _FileSnapshot:
    if not path.exists() and not path.is_symlink():
        return _FileSnapshot(existed=False)
    _assert_private_file(path)
    return _FileSnapshot(
        existed=True,
        content=path.read_bytes(),
        mode=stat.S_IMODE(path.stat().st_mode),
    )


def _content_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _path_digest(path: Path) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    _assert_private_file(path)
    return _content_digest(path.read_bytes())


@contextmanager
def _addon_config_lock(config_path: Path):
    lock_path = config_path.with_name(config_path.name + ".email-memory-addon.lock")
    _assert_no_symlink_components(lock_path)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise HermesAddonError(
            "Hermes configuration transaction is unavailable"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise HermesAddonError("Hermes configuration transaction is unsafe")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


@contextmanager
def _addon_transaction_lock(active_home: Path):
    """Serialize complete install/disable state transitions for one Hermes home."""
    lock_path = active_home / ".email-memory-addon.transaction.lock"
    _assert_no_symlink_components(lock_path)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise HermesAddonError("Hermes add-on transaction is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise HermesAddonError("Hermes add-on transaction is unsafe")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, content: bytes, *, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def _restore(path: Path, snapshot: _FileSnapshot) -> None:
    if snapshot.existed:
        _atomic_write(path, snapshot.content, mode=snapshot.mode)
    elif path.exists():
        path.unlink()
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _restore_if_unchanged(
    path: Path, snapshot: _FileSnapshot, *, expected_digest: str | None
) -> bool:
    """Restore only if no later writer changed the path after our mutation."""
    if _path_digest(path) != expected_digest:
        return False
    _restore(path, snapshot)
    return True


def install_hermes_addon(
    *,
    config_home: str | Path | None = None,
    hermes_home: str | Path | None = None,
    launcher: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    _transaction_locked: bool = False,
) -> HermesAddonResult:
    """Install the owner-only skill and its exact Telegram topic binding.

    All private IDs come from the local setup bundle. Failed mutations restore
    prior bytes only when no later concurrent writer changed the same file.
    """
    bundle = load_private_setup(config_home=config_home, environ=environ)
    menu = (
        bundle.hermes_addon.get("telegram_menu")
        if bundle.hermes_addon is not None
        else None
    )
    if not isinstance(menu, dict):
        raise HermesAddonError("private setup does not configure a Telegram menu")
    executables = bundle.runtime.get("executables")
    if not isinstance(executables, dict) or not isinstance(
        executables.get("hermes"), str
    ):
        raise HermesAddonError("private setup does not configure Hermes")
    hermes_executable = _validate_executable(
        Path(executables["hermes"]), label="Hermes executable"
    )
    hermes_python = _validate_hermes_python(hermes_executable)

    explicit_home = Path(hermes_home).expanduser() if hermes_home is not None else None
    if explicit_home is not None and not explicit_home.is_absolute():
        raise HermesAddonError("Hermes home must be an absolute path")
    command_env = _command_environment(environ, hermes_home=explicit_home)
    if explicit_home is None:
        config_output = _run_hermes(
            hermes_executable, ("config", "path"), command_env=command_env
        )
        config_path = Path(config_output)
        if not config_path.is_absolute():
            raise HermesAddonError("Hermes returned an invalid configuration path")
        active_home = config_path.parent
    else:
        active_home = explicit_home
        config_path = active_home / "config.yaml"

    if not _transaction_locked:
        _assert_private_directory(active_home)
        _assert_trusted_ancestry(active_home)
        with _addon_transaction_lock(active_home):
            return install_hermes_addon(
                config_home=config_home,
                hermes_home=hermes_home,
                launcher=launcher,
                environ=environ,
                _transaction_locked=True,
            )

    _assert_private_directory(active_home)
    _assert_trusted_ancestry(active_home)
    _assert_private_file(config_path)
    _run_hermes(hermes_executable, ("config", "check"), command_env=command_env)
    config_snapshot = _snapshot(config_path)
    config_expected_digest = _content_digest(config_snapshot.content)
    config_rollback_owned = True

    env = os.environ if environ is None else environ
    home = Path(env.get("HOME", str(Path.home()))).expanduser()
    launcher_path = (
        Path(launcher).expanduser()
        if launcher is not None
        else (home / ".local" / "bin" / DEFAULT_LAUNCHER_NAME)
    )
    _validate_executable(
        launcher_path, label="email-memory MCP launcher", allow_symlink=True
    )

    try:
        chat_id = int(str(menu["chat_id"]))
        thread_id = int(str(menu["thread_id"]))
    except (KeyError, TypeError, ValueError) as error:
        raise HermesAddonError(
            "private Telegram menu configuration is invalid"
        ) from error

    _verify_owner_dm_preflight(
        hermes_executable,
        chat_id=chat_id,
        active_home=active_home,
        command_env=command_env,
    )

    current_topics = _read_json_setting(
        hermes_executable,
        "platforms.telegram.extra.dm_topics",
        command_env=command_env,
        absent=[],
    )
    merged_topics = _merge_topic(current_topics, chat_id=chat_id, thread_id=thread_id)

    skills_root = active_home / "skills"
    created_skills_root = not skills_root.exists()
    if not created_skills_root:
        _assert_private_directory(skills_root)
    skill_dir = skills_root / SKILL_NAME
    created_skill_dir = not skill_dir.exists()
    if not created_skill_dir:
        _assert_private_directory(skill_dir)
    skill_path = skill_dir / "SKILL.md"
    if skill_path.is_symlink():
        raise HermesAddonError("Hermes skill cannot be a symbolic link")

    skill_snapshot = _snapshot(skill_path)
    skill_expected_digest = _path_digest(skill_path)
    retrieval = _retrieval_config(launcher_path)
    control = _control_config(launcher_path)
    if skill_snapshot.existed and skill_snapshot.content != SKILL_CONTENT.encode(
        "utf-8"
    ):
        raise HermesAddonError("Hermes email-memory skill ownership conflicts")
    absent = object()
    existing_retrieval = _read_json_setting(
        hermes_executable,
        f"mcp_servers.{RETRIEVAL_SERVER}",
        command_env=command_env,
        absent=absent,
    )
    if existing_retrieval is not absent and (
        not isinstance(existing_retrieval, dict)
        or existing_retrieval.get("command") != str(launcher_path)
        or existing_retrieval.get("args") != []
    ):
        raise HermesAddonError("Hermes email-memory retrieval ownership conflicts")
    existing_control = _read_json_setting(
        hermes_executable,
        f"mcp_servers.{CONTROL_SERVER}",
        command_env=command_env,
        absent=absent,
    )
    if existing_control is not absent and existing_control != control:
        raise HermesAddonError("Hermes email-memory control ownership conflicts")
    updates = (
        ("platforms.telegram.extra.dm_topics", merged_topics),
        (f"mcp_servers.{RETRIEVAL_SERVER}", retrieval),
        (f"mcp_servers.{CONTROL_SERVER}", control),
    )
    structured_updates = (
        (("platforms", "telegram", "extra", "dm_topics"), merged_topics),
        (("mcp_servers", RETRIEVAL_SERVER), retrieval),
        (("mcp_servers", CONTROL_SERVER), control),
    )
    prior_enabled = control_jobs.is_enabled(environ=command_env)
    try:
        control_jobs.set_enabled(False, environ=command_env)
        if created_skills_root:
            _assert_private_directory(skills_root, create=True)
        if created_skill_dir:
            _assert_private_directory(skill_dir, create=True)
        _atomic_write(skill_path, SKILL_CONTENT.encode("utf-8"), mode=0o600)
        skill_expected_digest = _content_digest(SKILL_CONTENT.encode("utf-8"))
        writer_result = _run_structured_writer_resilient(
            hermes_python,
            updates=structured_updates,
            deletes=(),
            expected_digest=_content_digest(config_snapshot.content),
            config_path=config_path,
            hermes_executable=hermes_executable,
            command_env=command_env,
        )
        config_expected_digest = writer_result.digest
        config_rollback_owned = writer_result.rollback_owned
        _run_hermes(hermes_executable, ("config", "check"), command_env=command_env)
        for key, expected in updates:
            actual = _read_json_setting(
                hermes_executable, key, command_env=command_env, absent=None
            )
            if actual != expected:
                raise HermesAddonError(
                    "Hermes did not preserve the requested configuration"
                )
        _assert_private_file(config_path)
        _assert_private_file(skill_path)
        control_jobs.set_enabled(True, environ=command_env)
    except BaseException as error:
        rollback_complete = False
        try:
            config_restored = False
            if config_rollback_owned:
                with _addon_config_lock(config_path):
                    config_restored = _restore_if_unchanged(
                        config_path,
                        config_snapshot,
                        expected_digest=config_expected_digest,
                    )
            skill_restored = _restore_if_unchanged(
                skill_path, skill_snapshot, expected_digest=skill_expected_digest
            )
            rollback_complete = config_restored and skill_restored
            if rollback_complete and (
                created_skill_dir
                and skill_dir.exists()
                and not any(skill_dir.iterdir())
            ):
                skill_dir.rmdir()
            if rollback_complete and (
                created_skills_root
                and skills_root.exists()
                and not any(skills_root.iterdir())
            ):
                skills_root.rmdir()
        finally:
            control_jobs.set_enabled(
                prior_enabled if rollback_complete else False,
                environ=command_env,
            )
        if not rollback_complete:
            raise HermesAddonError(
                "Hermes add-on transaction conflicted with a concurrent change; "
                "new operations remain disabled"
            ) from error
        raise

    return HermesAddonResult(config_path=config_path, skill_path=skill_path)


def disable_hermes_addon(
    *,
    config_home: str | Path | None = None,
    hermes_home: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    _transaction_locked: bool = False,
) -> HermesAddonResult:
    """Disable new operations and remove only add-on-owned Hermes settings."""
    bundle = load_private_setup(config_home=config_home, environ=environ)
    menu = (
        bundle.hermes_addon.get("telegram_menu")
        if bundle.hermes_addon is not None
        else None
    )
    if not isinstance(menu, dict):
        raise HermesAddonError("private setup does not configure a Telegram menu")
    executables = bundle.runtime.get("executables")
    if not isinstance(executables, dict) or not isinstance(
        executables.get("hermes"), str
    ):
        raise HermesAddonError("private setup does not configure Hermes")
    hermes_executable = _validate_executable(
        Path(executables["hermes"]), label="Hermes executable"
    )
    hermes_python = _validate_hermes_python(hermes_executable)
    explicit_home = Path(hermes_home).expanduser() if hermes_home is not None else None
    if explicit_home is not None and not explicit_home.is_absolute():
        raise HermesAddonError("Hermes home must be an absolute path")
    command_env = _command_environment(environ, hermes_home=explicit_home)
    if explicit_home is None:
        config_output = _run_hermes(
            hermes_executable, ("config", "path"), command_env=command_env
        )
        config_path = Path(config_output)
        if not config_path.is_absolute():
            raise HermesAddonError("Hermes returned an invalid configuration path")
        active_home = config_path.parent
    else:
        active_home = explicit_home
        config_path = active_home / "config.yaml"
    if not _transaction_locked:
        _assert_private_directory(active_home)
        _assert_trusted_ancestry(active_home)
        with _addon_transaction_lock(active_home):
            return disable_hermes_addon(
                config_home=config_home,
                hermes_home=hermes_home,
                environ=environ,
                _transaction_locked=True,
            )
    _assert_private_directory(active_home)
    _assert_trusted_ancestry(active_home)
    _assert_private_file(config_path)
    _run_hermes(hermes_executable, ("config", "check"), command_env=command_env)
    config_snapshot = _snapshot(config_path)
    config_expected_digest = _content_digest(config_snapshot.content)
    config_rollback_owned = True
    try:
        chat_id = int(str(menu["chat_id"]))
        thread_id = int(str(menu["thread_id"]))
    except KeyError, TypeError, ValueError:
        raise HermesAddonError(
            "private Telegram menu configuration is invalid"
        ) from None

    skill_dir = active_home / "skills" / SKILL_NAME
    skill_path = skill_dir / "SKILL.md"
    skill_snapshot = _snapshot(skill_path)
    skill_expected_digest = _path_digest(skill_path)
    if skill_snapshot.existed and skill_snapshot.content != SKILL_CONTENT.encode(
        "utf-8"
    ):
        raise HermesAddonError("Hermes skill is not owned by this add-on")
    retrieval = _read_json_setting(
        hermes_executable,
        f"mcp_servers.{RETRIEVAL_SERVER}",
        command_env=command_env,
        absent=None,
    )
    control = _read_json_setting(
        hermes_executable,
        f"mcp_servers.{CONTROL_SERVER}",
        command_env=command_env,
        absent=None,
    )
    if (
        not isinstance(retrieval, dict)
        or not isinstance(retrieval.get("command"), str)
        or control != _control_config(Path(retrieval["command"]))
    ):
        raise HermesAddonError("Hermes control server is not owned by this add-on")
    prior_enabled = control_jobs.is_enabled(environ=command_env)
    try:
        control_jobs.set_enabled(False, environ=command_env)
        current_topics = _read_json_setting(
            hermes_executable,
            "platforms.telegram.extra.dm_topics",
            command_env=command_env,
            absent=[],
        )
        retained_topics = _remove_topic(
            current_topics, chat_id=chat_id, thread_id=thread_id
        )
        writer_result = _run_structured_writer_resilient(
            hermes_python,
            updates=(
                (("platforms", "telegram", "extra", "dm_topics"), retained_topics),
            ),
            deletes=(("mcp_servers", CONTROL_SERVER),),
            expected_digest=_content_digest(config_snapshot.content),
            config_path=config_path,
            hermes_executable=hermes_executable,
            command_env=command_env,
        )
        config_expected_digest = writer_result.digest
        config_rollback_owned = writer_result.rollback_owned
        _run_hermes(hermes_executable, ("config", "check"), command_env=command_env)
        if (
            _read_json_setting(
                hermes_executable,
                "platforms.telegram.extra.dm_topics",
                command_env=command_env,
                absent=[],
            )
            != retained_topics
        ):
            raise HermesAddonError("Hermes did not remove the requested topic binding")
        if (
            _read_json_setting(
                hermes_executable,
                f"mcp_servers.{CONTROL_SERVER}",
                command_env=command_env,
                absent=None,
            )
            is not None
        ):
            raise HermesAddonError("Hermes did not remove the control server")
        if skill_path.exists():
            _assert_private_file(skill_path)
            skill_path.unlink()
            descriptor = os.open(skill_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            skill_expected_digest = None
        if skill_dir.exists() and not any(skill_dir.iterdir()):
            skill_dir.rmdir()
            descriptor = os.open(skill_dir.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        _assert_private_file(config_path)
    except BaseException as error:
        rollback_complete = False
        try:
            config_restored = False
            if config_rollback_owned:
                with _addon_config_lock(config_path):
                    config_restored = _restore_if_unchanged(
                        config_path,
                        config_snapshot,
                        expected_digest=config_expected_digest,
                    )
            if (
                skill_snapshot.existed
                and skill_expected_digest is None
                and not skill_dir.exists()
            ):
                skill_dir.parent.mkdir(mode=0o700, exist_ok=True)
                skill_dir.mkdir(mode=0o700)
            skill_restored = _restore_if_unchanged(
                skill_path, skill_snapshot, expected_digest=skill_expected_digest
            )
            rollback_complete = config_restored and skill_restored
        finally:
            control_jobs.set_enabled(
                prior_enabled if rollback_complete else False,
                environ=command_env,
            )
        if not rollback_complete:
            raise HermesAddonError(
                "Hermes add-on transaction conflicted with a concurrent change; "
                "new operations remain disabled"
            ) from error
        raise
    return HermesAddonResult(config_path=config_path, skill_path=skill_path)


def main(argv: Sequence[str] | None = None) -> None:
    """Install from the canonical local setup bundle without printing IDs."""
    parser = argparse.ArgumentParser(
        prog="email-memory-store-hermes-addon",
        description=(
            "Install the Email Memory button-menu skill and Telegram topic binding "
            "from the owner-only local setup bundle."
        ),
    )
    parser.add_argument(
        "--disable",
        action="store_true",
        help="Disable operations and remove only the Email Memory add-on binding.",
    )
    args = parser.parse_args(argv)
    if args.disable:
        disable_hermes_addon()
        print("Hermes email-memory Telegram topic add-on disabled.")
    else:
        install_hermes_addon()
        print("Hermes email-memory Telegram topic add-on installed.")

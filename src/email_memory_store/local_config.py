"""Load least-privilege profiles from the local configuration attachment."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
import os
from pathlib import Path
import shlex
import stat
import sys

from .promotion.llm import LLMProviderSpec
from .runtime import RuntimeSettings, resolve_runtime_settings
from .tui.private_setup import (
    FACT_STORE_PROVIDER,
    SUPPORTED_ALERT_TARGETS,
    load_private_setup,
    private_setup_paths,
)


_EXECUTABLE_EXPORTS = {
    "EMAIL_MEMORY_MAIL_CLIENT_EXECUTABLE",
    "EMAIL_MEMORY_HERMES_EXECUTABLE",
    "EMAIL_MEMORY_CODEX_EXECUTABLE",
    "EMAIL_MEMORY_CLAUDE_EXECUTABLE",
}
_PROFILES: dict[str, frozenset[str]] = {
    "maintenance": frozenset(
        {
            "EMAIL_MEMORY_ROOT",
            "EMAIL_MEMORY_STORE_RUNTIME_CONFIG",
            "EMAIL_MEMORY_ACCOUNT_NAME",
            "EMAIL_MEMORY_ACCOUNT_EMAIL",
            "EMAIL_MEMORY_INCLUDE_FOLDERS_JSON",
            "EMAIL_MEMORY_EXCLUDE_FOLDERS_JSON",
            "HERMES_ALERT_TARGET",
            "EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT",
            "EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER",
            "EMAIL_MEMORY_MAIL_CLIENT_EXECUTABLE",
            "EMAIL_MEMORY_HERMES_EXECUTABLE",
        }
    ),
    "cron": frozenset(
        {
            "EMAIL_MEMORY_ROOT",
            "HERMES_ALERT_TARGET",
            "EMAIL_MEMORY_HERMES_EXECUTABLE",
        }
    ),
    "status": frozenset({"EMAIL_MEMORY_STORE_RUNTIME_CONFIG"}),
    "ingestion": frozenset(
        {
            "EMAIL_MEMORY_ROOT",
            "EMAIL_MEMORY_STORE_RUNTIME_CONFIG",
            "ACCOUNT_NAME",
            "EMAIL_ADDRESS",
            "INCLUDE_FOLDERS",
            "EXCLUDE_FOLDERS",
        }
    ),
    "backup": frozenset(),
    "bootstrap": frozenset(
        {
            "EMAIL_MEMORY_STORE_RUNTIME_CONFIG",
            "ACCOUNT_NAME",
            "EMAIL_MEMORY_CREDENTIAL_REFERENCE",
            "EMAIL_MEMORY_MAIL_CLIENT_EXECUTABLE",
            "EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT",
            "EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER",
        }
    ),
    "triage": frozenset(
        {
            "EMAIL_MEMORY_ROOT",
            "EMAIL_MEMORY_MAIN_DB",
            "EMAIL_MEMORY_ENTITY_DB",
            "EMAIL_MEMORY_WORK_DB",
            "EMAIL_MEMORY_STORE_RUNTIME_CONFIG",
            "ACCOUNT_NAME",
            "EMAIL_MEMORY_MAIL_CLIENT_EXECUTABLE",
            "RETENTION_INBOX_FOLDER",
            "RETENTION_DEPARTMENT_FOLDER",
            "RETENTION_SERVICE_FOLDER",
            "RETENTION_ARCHIVE_FOLDER",
            "RETENTION_SENDER_ARCHIVE_RULES",
            "RETENTION_CLASSIFICATION_DEFINITIONS",
        }
    ),
}
_OPTIONAL_PROFILE_EXPORTS = {
    "maintenance": frozenset(
        {
            "EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT",
            "EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER",
        }
    ),
    "bootstrap": frozenset(
        {
            "EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT",
            "EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER",
        }
    ),
    "triage": frozenset({"EMAIL_MEMORY_WORK_DB"}),
}
_INVALID_EXECUTABLE_ERROR = (
    "runtime manifest must select an existing absolute executable that is secure"
)


def _path_owner_is_trusted(metadata: os.stat_result, *, current_uid: int) -> bool:
    return metadata.st_uid in {0, current_uid}


def _directory_component_is_secure(
    metadata: os.stat_result, *, current_uid: int
) -> bool:
    if not stat.S_ISDIR(metadata.st_mode) or not _path_owner_is_trusted(
        metadata, current_uid=current_uid
    ):
        return False
    broadly_writable = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    if not broadly_writable:
        return True
    return metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)


def _validate_configured_executable(configured: str) -> None:
    """Validate an executable and every lexical ancestor without following links."""
    candidate = Path(configured)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(_INVALID_EXECUTABLE_ERROR)

    try:
        current_uid = os.geteuid()
        current = Path(candidate.anchor)
        if not _directory_component_is_secure(current.lstat(), current_uid=current_uid):
            raise ValueError
        for part in candidate.parts[1:-1]:
            current /= part
            if not _directory_component_is_secure(
                current.lstat(), current_uid=current_uid
            ):
                raise ValueError

        metadata = candidate.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not _path_owner_is_trusted(metadata, current_uid=current_uid)
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or not metadata.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            or metadata.st_nlink != 1
            or not os.access(candidate, os.X_OK)
        ):
            raise ValueError
    except OSError, ValueError:
        raise ValueError(_INVALID_EXECUTABLE_ERROR) from None


def _check_owner_only_path(
    path: Path, *, artifact: str, directory: bool = False
) -> None:
    """Reject missing, linked, mistyped, foreign-owned, or broadly readable paths."""
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeError(f"missing local configuration {artifact}") from error
    correct_type = (
        stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    )
    if not correct_type:
        raise RuntimeError(f"local configuration {artifact} has an invalid type")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError(
            f"local configuration {artifact} must be current-user owner-only"
        )


def _required_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _string_list(value: object, *, field: str) -> str:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{field} must be a list of non-empty strings")
    return "\n".join(value)


def _json_string_list(value: object, *, field: str) -> str:
    _string_list(value, field=field)
    return json.dumps(value, separators=(",", ":"))


def _generic_alert_target(value: str) -> str:
    if value not in SUPPORTED_ALERT_TARGETS:
        raise ValueError("private environment alert destination is unsupported")
    return value


def _selected_llm_executable_export(main_db: Path) -> str:
    selected_provider = "hermes-default"
    if main_db.is_file():
        try:
            import duckdb

            connection = duckdb.connect(str(main_db), read_only=True)
            try:
                table_row = connection.execute(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_name = 'metadata'"
                ).fetchone()
                if table_row and table_row[0]:
                    row = connection.execute(
                        "SELECT value FROM metadata WHERE key = 'promotion_llm_config'"
                    ).fetchone()
                    if row:
                        raw_config = json.loads(row[0])
                        selected_provider = LLMProviderSpec.from_dict(
                            (raw_config or {}).get("provider")
                        ).name
            finally:
                connection.close()
        except Exception as error:
            raise ValueError(
                "main database selected LLM configuration is unreadable"
            ) from error
    exports = {
        "hermes-default": "EMAIL_MEMORY_HERMES_EXECUTABLE",
        "codex-cli": "EMAIL_MEMORY_CODEX_EXECUTABLE",
        "claude-code-cli": "EMAIL_MEMORY_CLAUDE_EXECUTABLE",
    }
    try:
        return exports[selected_provider]
    except KeyError as error:
        raise ValueError("main database selects an unsupported LLM provider") from error


def _runtime_values(settings: RuntimeSettings, runtime_path: Path) -> dict[str, str]:
    values = {
        "EMAIL_MEMORY_ROOT": str(settings.runtime_root),
        "EMAIL_MEMORY_MAIN_DB": str(settings.main_db),
        "EMAIL_MEMORY_ENTITY_DB": str(settings.entity_db),
        "EMAIL_MEMORY_STORE_RUNTIME_CONFIG": str(runtime_path),
    }
    if settings.work_db is not None:
        values["EMAIL_MEMORY_WORK_DB"] = str(settings.work_db)
    for export, attribute in (
        ("EMAIL_MEMORY_MAIL_CLIENT_EXECUTABLE", "mail_client_executable"),
        ("EMAIL_MEMORY_HERMES_EXECUTABLE", "hermes_executable"),
        ("EMAIL_MEMORY_CODEX_EXECUTABLE", "codex_executable"),
        ("EMAIL_MEMORY_CLAUDE_EXECUTABLE", "claude_executable"),
    ):
        executable = getattr(settings, attribute)
        if executable is not None:
            values[export] = str(executable)
    return values


def _policy_values(policy: Mapping[str, object]) -> dict[str, str]:
    account_email = _required_string(
        policy["account_email"], field="policy.account_email"
    )
    values = {
        "ACCOUNT_NAME": _required_string(
            policy["account_label"], field="policy.account_label"
        ),
        "EMAIL_MEMORY_ACCOUNT_NAME": _required_string(
            policy["account_label"], field="policy.account_label"
        ),
        "EMAIL_ADDRESS": account_email,
        "EMAIL_MEMORY_ACCOUNT_EMAIL": account_email,
        "EMAIL_MEMORY_INCLUDE_FOLDERS_JSON": _json_string_list(
            policy["include_folders"], field="policy.include_folders"
        ),
        "EMAIL_MEMORY_EXCLUDE_FOLDERS_JSON": _json_string_list(
            policy["exclude_folders"], field="policy.exclude_folders"
        ),
        "INCLUDE_FOLDERS": _string_list(
            policy["include_folders"], field="policy.include_folders"
        ),
        "EXCLUDE_FOLDERS": _string_list(
            policy["exclude_folders"], field="policy.exclude_folders"
        ),
    }
    retention = policy.get("retention")
    if not isinstance(retention, Mapping):
        return values
    for field, export in (
        ("inbox_folder", "RETENTION_INBOX_FOLDER"),
        ("department_folder", "RETENTION_DEPARTMENT_FOLDER"),
        ("service_folder", "RETENTION_SERVICE_FOLDER"),
        ("archive_folder", "RETENTION_ARCHIVE_FOLDER"),
    ):
        if field in retention:
            values[export] = _required_string(
                retention[field], field=f"policy.retention.{field}"
            )
    if "sender_archive_rules" in retention:
        values["RETENTION_SENDER_ARCHIVE_RULES"] = json.dumps(
            retention["sender_archive_rules"], sort_keys=True
        )
    if "classification_definitions" in retention:
        values["RETENTION_CLASSIFICATION_DEFINITIONS"] = json.dumps(
            retention["classification_definitions"], sort_keys=True
        )
    return values


def load_bundle(
    profile: str, *, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Load one validated least-privilege profile from the local attachment."""
    if profile not in _PROFILES:
        raise ValueError(f"unknown local configuration profile: {profile}")
    env = os.environ if environ is None else environ
    paths = private_setup_paths(environ=env)
    _check_owner_only_path(paths.config_dir, artifact="directory", directory=True)
    for path, artifact in (
        (paths.runtime_manifest, "runtime manifest"),
        (paths.private_env, "private environment"),
        (paths.policy, "policy"),
    ):
        _check_owner_only_path(path, artifact=artifact)

    bundle = load_private_setup(environ=env)
    try:
        runtime_settings = resolve_runtime_settings(
            runtime_root=None,
            work_root=None,
            runtime_config=paths.runtime_manifest,
            environ={},
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError("invalid runtime manifest") from error

    values = _runtime_values(runtime_settings, paths.runtime_manifest)
    values.update(_policy_values(bundle.policy))
    private_exports = {
        "alert_destination": "HERMES_ALERT_TARGET",
        "credential_reference": "EMAIL_MEMORY_CREDENTIAL_REFERENCE",
        "fact_store_module_root": "EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT",
        "fact_store_provider": "EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER",
    }
    for field, export in private_exports.items():
        if field in bundle.private_env:
            values[export] = _required_string(
                bundle.private_env[field], field=f"private environment.{field}"
            )
    fact_root = values.get("EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT")
    fact_provider = values.get("EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER")
    if bool(fact_root) != bool(fact_provider):
        raise ValueError(
            "private environment must configure fact-store root and provider together"
        )
    if fact_provider is not None and fact_provider != FACT_STORE_PROVIDER:
        raise ValueError("private environment fact-store provider is unsupported")

    profile_exports = set(_PROFILES[profile])
    if profile in {"maintenance", "cron"}:
        alert_target = values.get("HERMES_ALERT_TARGET")
        if alert_target:
            values["HERMES_ALERT_TARGET"] = _generic_alert_target(alert_target)
    if profile == "triage":
        if runtime_settings.main_db is None:
            raise ValueError("runtime manifest must configure the main database")
        profile_exports.add(_selected_llm_executable_export(runtime_settings.main_db))

    for export in sorted(profile_exports & _EXECUTABLE_EXPORTS):
        configured = values.get(export)
        if not configured:
            continue
        _validate_configured_executable(configured)

    optional = _OPTIONAL_PROFILE_EXPORTS.get(profile, frozenset())
    missing = sorted(
        name for name in profile_exports - optional if not values.get(name)
    )
    if missing:
        raise ValueError(
            f"local configuration profile {profile} is missing required field(s): "
            f"{', '.join(missing)}"
        )
    return {name: values[name] for name in profile_exports if name in values}


def shell_exports(profile: str, *, environ: Mapping[str, str] | None = None) -> str:
    """Render a profile as safely quoted POSIX shell exports."""
    values = load_bundle(profile, environ=environ)
    return "\n".join(
        f"export {name}={shlex.quote(value)}" for name, value in sorted(values.items())
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=sorted(_PROFILES))
    parser.add_argument("--shell", action="store_true")
    args = parser.parse_args(argv)
    try:
        output = (
            shell_exports(args.profile)
            if args.shell
            else json.dumps(load_bundle(args.profile), sort_keys=True)
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"local configuration error: {error}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

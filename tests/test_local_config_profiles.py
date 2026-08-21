from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import duckdb
import pytest

import email_memory_store.local_config as local_config
from email_memory_store.tui.private_setup import (
    FACT_STORE_PROVIDER,
    PrivateSetupValues,
    write_private_setup,
)


PROFILE_EXPORTS = {
    "maintenance": {
        "EMAIL_MEMORY_ACCOUNT_NAME",
        "EMAIL_MEMORY_ACCOUNT_EMAIL",
        "EMAIL_MEMORY_EXCLUDE_FOLDERS_JSON",
        "EMAIL_MEMORY_INCLUDE_FOLDERS_JSON",
        "EMAIL_MEMORY_HERMES_EXECUTABLE",
        "EMAIL_MEMORY_MAIL_CLIENT_EXECUTABLE",
        "EMAIL_MEMORY_ROOT",
        "EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER",
        "EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT",
        "EMAIL_MEMORY_STORE_RUNTIME_CONFIG",
        "HERMES_ALERT_TARGET",
    },
    "cron": {
        "EMAIL_MEMORY_HERMES_EXECUTABLE",
        "EMAIL_MEMORY_ROOT",
        "HERMES_ALERT_TARGET",
    },
    "status": {"EMAIL_MEMORY_STORE_RUNTIME_CONFIG"},
    "ingestion": {
        "ACCOUNT_NAME",
        "EMAIL_ADDRESS",
        "EMAIL_MEMORY_ROOT",
        "EMAIL_MEMORY_STORE_RUNTIME_CONFIG",
        "EXCLUDE_FOLDERS",
        "INCLUDE_FOLDERS",
    },
    "backup": set(),
    "bootstrap": {
        "ACCOUNT_NAME",
        "EMAIL_MEMORY_CREDENTIAL_REFERENCE",
        "EMAIL_MEMORY_MAIL_CLIENT_EXECUTABLE",
        "EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER",
        "EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT",
        "EMAIL_MEMORY_STORE_RUNTIME_CONFIG",
    },
    "triage": {
        "ACCOUNT_NAME",
        "EMAIL_MEMORY_ENTITY_DB",
        "EMAIL_MEMORY_HERMES_EXECUTABLE",
        "EMAIL_MEMORY_MAIL_CLIENT_EXECUTABLE",
        "EMAIL_MEMORY_MAIN_DB",
        "EMAIL_MEMORY_ROOT",
        "EMAIL_MEMORY_STORE_RUNTIME_CONFIG",
        "EMAIL_MEMORY_WORK_DB",
        "RETENTION_ARCHIVE_FOLDER",
        "RETENTION_CLASSIFICATION_DEFINITIONS",
        "RETENTION_DEPARTMENT_FOLDER",
        "RETENTION_INBOX_FOLDER",
        "RETENTION_SENDER_ARCHIVE_RULES",
        "RETENTION_SERVICE_FOLDER",
    },
}


@pytest.fixture
def config_environment(tmp_path: Path) -> dict[str, str]:
    executable_directory = tmp_path / "bin"
    executable_directory.mkdir()
    executable_directory.chmod(0o700)
    executables: dict[str, Path] = {}
    for name in ("himalaya", "hermes", "codex", "claude"):
        executable = executable_directory / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
        executables[name] = executable

    runtime_root = tmp_path / "runtime"
    values = PrivateSetupValues(
        runtime_root=str(runtime_root),
        main_db=str(runtime_root / "main.duckdb"),
        entity_db=str(runtime_root / "entity.duckdb"),
        vector_store=str(runtime_root / "vectors"),
        work_db=str(runtime_root / "work.duckdb"),
        himalaya_executable=str(executables["himalaya"]),
        hermes_executable=str(executables["hermes"]),
        codex_executable=str(executables["codex"]),
        claude_executable=str(executables["claude"]),
        fact_store_module_root=str(tmp_path / "fact-store"),
        fact_store_provider=FACT_STORE_PROVIDER,
        account_label="mailbox",
        account_email="operator@example.test",
        include_folders="INBOX, Archive",
        exclude_folders="Trash",
        retention_inbox_folder="INBOX",
        retention_department_folder="Archive/Department",
        retention_service_folder="Archive/Service",
        retention_archive_folder="Archive",
        retention_sender_archive_rules=(
            '[{"folder":"Archive/Research","domains":["example.test"]}]'
        ),
        retention_classification_definitions='{"notice":"Synthetic notice"}',
        alert_destination="telegram",
        credential_reference="keyring:mailbox",
    )
    write_private_setup(values, config_home=tmp_path / "config")
    return {"XDG_CONFIG_HOME": str(tmp_path / "config")}


@pytest.mark.parametrize("profile", PROFILE_EXPORTS)
def test_profile_exports_only_declared_consumer_values(
    config_environment: dict[str, str], profile: str
) -> None:
    loaded = local_config.load_bundle(profile, environ=config_environment)

    assert set(loaded) == PROFILE_EXPORTS[profile]


def test_shell_exports_are_sorted_and_safely_quoted(
    config_environment: dict[str, str],
) -> None:
    loaded = local_config.load_bundle("ingestion", environ=config_environment)

    assert local_config.shell_exports(
        "ingestion", environ=config_environment
    ) == "\n".join(
        f"export {name}={shlex.quote(value)}" for name, value in sorted(loaded.items())
    )


def test_ingestion_preserves_present_empty_folder_lists(
    config_environment: dict[str, str],
) -> None:
    paths = local_config.private_setup_paths(environ=config_environment)
    policy = json.loads(paths.policy.read_text(encoding="utf-8"))
    policy["include_folders"] = []
    policy["exclude_folders"] = []
    paths.policy.write_text(json.dumps(policy, sort_keys=True), encoding="utf-8")
    paths.policy.chmod(0o600)

    loaded = local_config.load_bundle("ingestion", environ=config_environment)

    assert loaded["INCLUDE_FOLDERS"] == ""
    assert loaded["EXCLUDE_FOLDERS"] == ""
    shell = local_config.shell_exports("ingestion", environ=config_environment)
    assert "export INCLUDE_FOLDERS=''" in shell
    assert "export EXCLUDE_FOLDERS=''" in shell


def test_maintenance_supports_disabled_fact_integration_without_shell_exports(
    config_environment: dict[str, str],
) -> None:
    paths = local_config.private_setup_paths(environ=config_environment)
    private_env = json.loads(paths.private_env.read_text(encoding="utf-8"))
    private_env.pop("fact_store_module_root")
    private_env.pop("fact_store_provider")
    paths.private_env.write_text(
        json.dumps(private_env, sort_keys=True), encoding="utf-8"
    )
    paths.private_env.chmod(0o600)

    loaded = local_config.load_bundle("maintenance", environ=config_environment)
    shell = local_config.shell_exports("maintenance", environ=config_environment)

    assert "EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT" not in loaded
    assert "EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER" not in loaded
    assert "EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT" not in shell
    assert "EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER" not in shell


def test_maintenance_uses_standard_account_email_without_legacy_export(
    config_environment: dict[str, str],
) -> None:
    loaded = local_config.load_bundle("maintenance", environ=config_environment)

    assert loaded["EMAIL_MEMORY_ACCOUNT_EMAIL"] == "operator@example.test"
    assert "EMAIL_ADDRESS" not in loaded


@pytest.mark.parametrize("profile", ["maintenance", "cron"])
def test_generic_alert_transport_token_is_exported(
    config_environment: dict[str, str], profile: str
) -> None:
    loaded = local_config.load_bundle(profile, environ=config_environment)

    assert loaded["HERMES_ALERT_TARGET"] == "telegram"


@pytest.mark.parametrize("profile", ["maintenance", "cron"])
@pytest.mark.parametrize(
    "unsupported_target",
    ["pagerduty", "telegram" + ":" + "private-channel-123", "Telegram"],
)
def test_alert_target_must_be_a_supported_generic_transport(
    config_environment: dict[str, str], profile: str, unsupported_target: str
) -> None:
    paths = local_config.private_setup_paths(environ=config_environment)
    private_env = json.loads(paths.private_env.read_text(encoding="utf-8"))
    private_env["alert_destination"] = unsupported_target
    paths.private_env.write_text(
        json.dumps(private_env, sort_keys=True), encoding="utf-8"
    )
    paths.private_env.chmod(0o600)

    with pytest.raises(
        ValueError, match="alert destination is unsupported"
    ) as captured:
        local_config.load_bundle(profile, environ=config_environment)
    assert unsupported_target not in str(captured.value)


@pytest.mark.parametrize("supported_target", ["telegram", "slack", "discord"])
def test_maintenance_accepts_each_supported_generic_transport(
    config_environment: dict[str, str], supported_target: str
) -> None:
    paths = local_config.private_setup_paths(environ=config_environment)
    private_env = json.loads(paths.private_env.read_text(encoding="utf-8"))
    private_env["alert_destination"] = supported_target
    paths.private_env.write_text(json.dumps(private_env), encoding="utf-8")
    paths.private_env.chmod(0o600)

    assert (
        local_config.load_bundle("maintenance", environ=config_environment)[
            "HERMES_ALERT_TARGET"
        ]
        == supported_target
    )


@pytest.mark.parametrize(
    "missing_field", ["fact_store_module_root", "fact_store_provider"]
)
def test_rejects_half_configured_fact_integration_globally(
    config_environment: dict[str, str], missing_field: str
) -> None:
    paths = local_config.private_setup_paths(environ=config_environment)
    private_env = json.loads(paths.private_env.read_text(encoding="utf-8"))
    private_env.pop(missing_field)
    paths.private_env.write_text(
        json.dumps(private_env, sort_keys=True), encoding="utf-8"
    )
    paths.private_env.chmod(0o600)

    with pytest.raises(ValueError, match="root and provider together"):
        local_config.load_bundle("backup", environ=config_environment)


def test_rejects_unsupported_fact_store_provider_without_disclosing_it(
    config_environment: dict[str, str],
) -> None:
    paths = local_config.private_setup_paths(environ=config_environment)
    private_env = json.loads(paths.private_env.read_text(encoding="utf-8"))
    unsupported_provider = "private_adapter.module:Store"
    private_env["fact_store_provider"] = unsupported_provider
    paths.private_env.write_text(json.dumps(private_env), encoding="utf-8")
    paths.private_env.chmod(0o600)

    with pytest.raises(
        ValueError, match="fact-store provider is unsupported"
    ) as captured:
        local_config.load_bundle("maintenance", environ=config_environment)
    assert unsupported_provider not in str(captured.value)


@pytest.mark.parametrize(
    ("provider_name", "selected_export"),
    [
        (None, "EMAIL_MEMORY_HERMES_EXECUTABLE"),
        ("codex-cli", "EMAIL_MEMORY_CODEX_EXECUTABLE"),
        ("claude-code-cli", "EMAIL_MEMORY_CLAUDE_EXECUTABLE"),
    ],
)
def test_triage_exports_only_selected_llm_executable(
    config_environment: dict[str, str],
    provider_name: str | None,
    selected_export: str,
) -> None:
    paths = local_config.private_setup_paths(environ=config_environment)
    runtime = local_config.resolve_runtime_settings(
        runtime_root=None,
        work_root=None,
        runtime_config=paths.runtime_manifest,
        environ={},
    )
    if provider_name is not None:
        assert runtime.main_db is not None
        runtime.main_db.parent.mkdir(parents=True)
        connection = duckdb.connect(str(runtime.main_db))
        try:
            connection.execute(
                "CREATE TABLE metadata (key VARCHAR PRIMARY KEY, value VARCHAR)"
            )
            connection.execute(
                "INSERT INTO metadata VALUES ('promotion_llm_config', ?)",
                [json.dumps({"provider": {"name": provider_name}})],
            )
        finally:
            connection.close()

    loaded = local_config.load_bundle("triage", environ=config_environment)
    llm_exports = {
        "EMAIL_MEMORY_HERMES_EXECUTABLE",
        "EMAIL_MEMORY_CODEX_EXECUTABLE",
        "EMAIL_MEMORY_CLAUDE_EXECUTABLE",
    }

    assert set(loaded) & llm_exports == {selected_export}


def test_ambient_runtime_overrides_cannot_replace_manifest_values(
    config_environment: dict[str, str], tmp_path: Path
) -> None:
    hostile_manifest = tmp_path / "hostile.toml"
    environ = {
        **config_environment,
        "EMAIL_MEMORY_STORE_RUNTIME_CONFIG": str(hostile_manifest),
        "EMAIL_MEMORY_ROOT": "/untrusted/root",
        "ACCOUNT_NAME": "untrusted-account",
    }

    loaded = local_config.load_bundle("ingestion", environ=environ)

    assert loaded["EMAIL_MEMORY_ROOT"] != "/untrusted/root"
    assert loaded["ACCOUNT_NAME"] == "mailbox"
    assert loaded["EMAIL_MEMORY_STORE_RUNTIME_CONFIG"] != str(hostile_manifest)


@pytest.mark.parametrize(
    "artifact", ["runtime.toml", "private.env.json", "policy.json"]
)
def test_configuration_artifacts_must_not_be_symlinks(
    config_environment: dict[str, str], tmp_path: Path, artifact: str
) -> None:
    paths = local_config.private_setup_paths(environ=config_environment)
    artifact_path = paths.config_dir / artifact
    replacement = tmp_path / f"replacement-{artifact}"
    replacement.write_bytes(artifact_path.read_bytes())
    replacement.chmod(0o600)
    artifact_path.unlink()
    artifact_path.symlink_to(replacement)

    with pytest.raises(RuntimeError, match="invalid type"):
        local_config.load_bundle("backup", environ=config_environment)


def test_configuration_paths_must_be_current_user_owner_only(
    config_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = local_config.private_setup_paths(environ=config_environment)
    paths.policy.chmod(0o640)
    with pytest.raises(RuntimeError, match="current-user owner-only"):
        local_config.load_bundle("backup", environ=config_environment)

    paths.policy.chmod(0o600)
    current_uid = os.geteuid()
    monkeypatch.setattr(local_config.os, "geteuid", lambda: current_uid + 1)
    with pytest.raises(RuntimeError, match="current-user owner-only"):
        local_config.load_bundle("backup", environ=config_environment)


@pytest.mark.parametrize(
    ("profile", "executable_name", "required"),
    [
        ("maintenance", "himalaya", True),
        ("cron", "himalaya", False),
        ("bootstrap", "himalaya", True),
        ("status", "hermes", False),
        ("triage", "hermes", True),
        ("triage", "codex", False),
    ],
)
def test_only_profile_selected_executables_must_be_usable(
    config_environment: dict[str, str],
    profile: str,
    executable_name: str,
    required: bool,
) -> None:
    paths = local_config.private_setup_paths(environ=config_environment)
    runtime = local_config.resolve_runtime_settings(
        runtime_root=None,
        work_root=None,
        runtime_config=paths.runtime_manifest,
        environ={},
    )
    attribute = {
        "himalaya": "mail_client_executable",
        "hermes": "hermes_executable",
        "codex": "codex_executable",
    }[executable_name]
    executable = getattr(runtime, attribute)
    assert executable is not None
    executable.unlink()

    if required:
        with pytest.raises(ValueError, match="existing absolute executable"):
            local_config.load_bundle(profile, environ=config_environment)
    else:
        local_config.load_bundle(profile, environ=config_environment)


@pytest.mark.parametrize("mode", [0o777, 0o720, 0o702, 0o600])
def test_rejects_insecure_executable_modes(tmp_path: Path, mode: int) -> None:
    executable_directory = tmp_path / "secure-bin"
    executable_directory.mkdir(mode=0o700)
    executable = executable_directory / "tool"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(mode)

    with pytest.raises(ValueError, match="existing absolute executable"):
        local_config._validate_configured_executable(str(executable))


def test_rejects_symlink_non_regular_and_hardlinked_executables(
    tmp_path: Path,
) -> None:
    executable_directory = tmp_path / "secure-bin"
    executable_directory.mkdir(mode=0o700)
    executable = executable_directory / "tool"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)

    linked = executable_directory / "linked"
    linked.symlink_to(executable)
    with pytest.raises(ValueError, match="existing absolute executable"):
        local_config._validate_configured_executable(str(linked))

    directory = executable_directory / "directory"
    directory.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="existing absolute executable"):
        local_config._validate_configured_executable(str(directory))

    hardlink = executable_directory / "hardlink"
    hardlink.hardlink_to(executable)
    with pytest.raises(ValueError, match="existing absolute executable"):
        local_config._validate_configured_executable(str(executable))


@pytest.mark.parametrize("mode", [0o770, 0o707])
def test_rejects_broadly_writable_executable_ancestors(
    tmp_path: Path, mode: int
) -> None:
    executable_directory = tmp_path / "insecure-bin"
    executable_directory.mkdir(mode=0o700)
    executable = executable_directory / "tool"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    executable_directory.chmod(mode)

    with pytest.raises(ValueError, match="existing absolute executable"):
        local_config._validate_configured_executable(str(executable))


def test_rejects_symlinked_executable_ancestor(tmp_path: Path) -> None:
    real_directory = tmp_path / "real-bin"
    real_directory.mkdir(mode=0o700)
    executable = real_directory / "tool"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    linked_directory = tmp_path / "linked-bin"
    linked_directory.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(ValueError, match="existing absolute executable"):
        local_config._validate_configured_executable(
            str(linked_directory / executable.name)
        )


def test_rejects_foreign_owned_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable_directory = tmp_path / "secure-bin"
    executable_directory.mkdir(mode=0o700)
    executable = executable_directory / "tool"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    real_lstat = Path.lstat

    def fake_lstat(path: Path):
        result = real_lstat(path)
        if path == executable:
            values = list(result)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "lstat", fake_lstat)

    with pytest.raises(ValueError, match="existing absolute executable"):
        local_config._validate_configured_executable(str(executable))


def test_module_cli_emits_json_and_shell_profiles(
    config_environment: dict[str, str],
) -> None:
    environ = {**os.environ, **config_environment}
    json_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "email_memory_store.local_config",
            "--profile",
            "status",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environ,
    )
    shell_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "email_memory_store.local_config",
            "--profile",
            "cron",
            "--shell",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environ,
    )

    assert json_result.returncode == 0
    assert set(json.loads(json_result.stdout)) == PROFILE_EXPORTS["status"]
    assert shell_result.returncode == 0
    assert {
        line.removeprefix("export ").partition("=")[0]
        for line in shell_result.stdout.splitlines()
    } == PROFILE_EXPORTS["cron"]


def test_cli_errors_do_not_disclose_local_paths(
    config_environment: dict[str, str],
) -> None:
    paths = local_config.private_setup_paths(environ=config_environment)
    runtime = local_config.resolve_runtime_settings(
        runtime_root=None,
        work_root=None,
        runtime_config=paths.runtime_manifest,
        environ={},
    )
    assert runtime.hermes_executable is not None
    runtime.hermes_executable.chmod(0o600)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "email_memory_store.local_config",
            "--profile",
            "maintenance",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **config_environment},
    )

    canaries = {
        config_environment["XDG_CONFIG_HOME"],
        str(paths.config_dir),
        str(paths.runtime_manifest),
        str(runtime.runtime_root),
        str(runtime.hermes_executable),
        str(runtime.hermes_executable.parent),
    }
    assert result.returncode == 2
    assert all(canary not in result.stderr for canary in canaries)

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
from typing import Any

import pytest

from email_memory_store.deployment import cli as deployment_cli


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _fixture_args(tmp_path: Path) -> tuple[argparse.Namespace, Path, Path]:
    root = tmp_path / "public-checkout"
    (root / "scripts").mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    (root / "pyproject.toml").write_text(
        "[project]\nname='fixture'\n", encoding="utf-8"
    )
    (root / "pyproject.toml").chmod(0o600)
    provisioner = root / "scripts/provision_email_memory_environment.sh"
    provisioner.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    provisioner.chmod(0o600)
    home = tmp_path / "home"
    config = tmp_path / "config"
    data = tmp_path / "data"
    state = tmp_path / "state"
    deployment = data / "deployment"
    for directory in (home, config, data, state):
        directory.mkdir(mode=0o700)
    config_dir = config / "email-memory-store"
    config_dir.mkdir(mode=0o700)
    for name in ("runtime.toml", "private.env.json", "policy.json"):
        (config_dir / name).write_text("{}\n", encoding="utf-8")
    release = deployment / "envs/release-py314-cpu"
    venv = release / "venv"
    venv.mkdir(parents=True)
    deployment.chmod(0o700)
    (deployment / "envs").chmod(0o700)
    release.chmod(0o700)
    venv.chmod(0o700)
    for name in (
        "python",
        "email-memory-store",
        "email-memory-store-mcp",
        "email-memory-store-control-mcp",
        "email-memory-store-hermes-addon",
    ):
        _executable(venv / "bin" / name)
    _executable(release / "bin/email-memory-store-deploy")
    marker = release / ".email-memory-release"
    marker.write_text(
        "public_revision=0123456789ab\npython=3.14\naccelerator_request=cpu\n",
        encoding="utf-8",
    )
    marker.chmod(0o600)
    scripts = tmp_path / "candidate-scripts"
    scripts.mkdir()
    maintenance = _executable(scripts / "nightly_maintenance.sh")
    installer = _executable(scripts / "install_email_memory_mcp_launcher.sh")
    maintenance.chmod(0o600)
    installer.chmod(0o600)
    args = argparse.Namespace(
        public_checkout=str(root),
        home=str(home),
        config_home=str(config),
        data_home=str(data),
        state_home=str(state),
        deployment_root=str(deployment),
        release_id="release-py314-cpu",
        accelerator="cpu",
        probe_timeout=1,
        cron_schedule="30 2 * * *",
        crontab_command="crontab",
        replace_scheduler_command=None,
        regenerate_configuration=False,
        test_mode=True,
        fail_after_activation=False,
    )
    return args, release, scripts


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    args: argparse.Namespace,
    scripts: Path,
    captured_commands: list[list[str]] | None = None,
    captured_environments: list[dict[str, str]] | None = None,
) -> list[bytes]:
    installed_crontabs: list[bytes] = []
    home = Path(args.home)
    data = Path(args.data_home)

    def fake_run(
        command: Any,
        *,
        env: dict[str, str],
        input_bytes: bytes | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        del input_bytes, timeout
        parts = [str(item) for item in command]
        if captured_commands is not None:
            captured_commands.append(parts)
        if captured_environments is not None:
            captured_environments.append(dict(env))
        if "rev-parse" in parts:
            output = b"0123456789ab\n"
        elif "runtime-doctor" in parts:
            output = b'{"ok":true,"paths_redacted":true}\n'
        elif "embed-status" in parts:
            output = json.dumps(
                {
                    "collections": {
                        name: 1 for name in deployment_cli.INDEX_COLLECTIONS
                    },
                    "persist_path": "/redacted-by-coordinator",
                }
            ).encode()
        elif len(parts) > 1 and parts[1:3] == ["account", "list"]:
            output = b'[{"name":"synthetic-account","default":true}]\n'
        elif len(parts) > 1 and parts[1:3] == ["folder", "list"]:
            output = b'{"connector":"ready"}\n'
        else:
            output = b""
        if parts[:3] == [
            "/bin/bash",
            "-p",
            str(scripts / "install_email_memory_mcp_launcher.sh"),
        ]:
            current = data / "email-memory-store/mcp-launcher/current"
            stable = home / ".local/bin/email_memory_store_mcp_hermes.sh"
            launcher_identity = "0" * 64
            launcher_release = (
                data / f"email-memory-store/mcp-launcher/releases/{launcher_identity}"
            )
            launcher_release.mkdir(parents=True, exist_ok=True)
            (data / "email-memory-store").chmod(0o700)
            (data / "email-memory-store/mcp-launcher/releases").chmod(0o700)
            launcher_release.chmod(0o700)
            _executable(launcher_release / "email_memory_store_mcp_launcher.sh")
            environment = launcher_release / "email_memory_environment.sh"
            environment.write_text("# synthetic\n", encoding="utf-8")
            environment.chmod(0o600)
            current.parent.mkdir(parents=True, exist_ok=True)
            current.parent.chmod(0o700)
            stable.parent.mkdir(parents=True, exist_ok=True)
            (home / ".local").chmod(0o700)
            stable.parent.chmod(0o700)
            current.unlink(missing_ok=True)
            stable.unlink(missing_ok=True)
            current.symlink_to(f"releases/{launcher_identity}")
            stable.symlink_to(current / "email_memory_store_mcp_launcher.sh")
        return subprocess.CompletedProcess(parts, 0, output, b"")

    monkeypatch.setattr(deployment_cli, "_run", fake_run)
    monkeypatch.setattr(
        deployment_cli, "_candidate_scripts", lambda python, env: scripts
    )
    monkeypatch.setattr(deployment_cli, "_cron_executable", lambda command: command)
    monkeypatch.setattr(deployment_cli, "_read_crontab", lambda command, env: b"")
    monkeypatch.setattr(
        deployment_cli,
        "_install_crontab",
        lambda command, content, env: installed_crontabs.append(content),
    )
    monkeypatch.setattr(
        deployment_cli,
        "_load_configuration",
        lambda python, env: {
            "EMAIL_MEMORY_STORE_RUNTIME_CONFIG": str(
                Path(args.config_home) / "email-memory-store/runtime.toml"
            ),
            "EMAIL_MEMORY_MAIL_CLIENT_EXECUTABLE": "/synthetic/mail",
            "ACCOUNT_NAME": "synthetic-account",
            "EMAIL_MEMORY_CREDENTIAL_REFERENCE": "keyring:synthetic",
        },
    )
    return installed_crontabs


def test_transaction_persists_one_stable_cron_entry_and_disabled_fact_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile_connector_environment = {
        "HIMALAYA_CONFIG": "/hostile/himalaya.toml",
        "HERMES_HOME": "/hostile/hermes-home",
        "HERMES_PROFILE": "hostile-profile",
        "HERMES_MODEL": "hostile-model",
        "HERMES_PROVIDER": "hostile-provider",
        "HERMES_PLUGIN_PATH": "/hostile/plugins",
        "HERMES_TEST_MODE": "1",
    }
    for key, value in hostile_connector_environment.items():
        monkeypatch.setenv(key, value)
    args, release, scripts = _fixture_args(tmp_path)
    captured_commands: list[list[str]] = []
    captured_environments: list[dict[str, str]] = []
    installed = _install_fakes(
        monkeypatch,
        args,
        scripts,
        captured_commands,
        captured_environments,
    )

    assert deployment_cli._bootstrap(args) == 0

    current = Path(args.data_home) / "email-memory-store/current"
    assert current.resolve() == release
    assert len(installed) == 1
    cron = installed[0].decode()
    stable = current / "bin/email-memory-store-deploy"
    assert cron.count(deployment_cli.MANAGED_START) == 1
    assert cron.count("email-memory-store-deploy nightly") == 1
    assert str(stable) in cron
    assert str(release) not in cron
    assert "flock" not in cron
    receipt = release / ".deployment-readiness.json"
    checks = {
        item["code"]: item["status"]
        for item in json.loads(receipt.read_text())["checks"]
    }
    assert checks["fact_provider"] == "disabled"
    assert all(
        status == "pass" for code, status in checks.items() if code != "fact_provider"
    )
    payload = json.loads(receipt.read_text())
    assert payload["schema_version"] == 3
    assert len(payload["release_identity"]) == 64
    payload["schema_version"] = 2
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        deployment_cli, "_read_crontab", lambda command, env: installed[-1]
    )
    assert deployment_cli._doctor(args) == deployment_cli.DOCTOR_READY_EXIT
    runtime_config = str(Path(args.config_home) / "email-memory-store/runtime.toml")
    private_values = (
        "synthetic-account",
        "owner@example.test",
        "private-folder",
        runtime_config,
    )
    package_runtime_invocations = [
        (command, environment)
        for command, environment in zip(
            captured_commands, captured_environments, strict=True
        )
        if Path(command[0]).name
        in {
            "email-memory-store",
            "email-memory-store-mcp",
            "email-memory-store-control-mcp",
        }
    ]
    assert package_runtime_invocations
    for command, environment in package_runtime_invocations:
        rendered = "\0".join(command[1:])
        assert not any(value in rendered for value in private_values)
        assert "--runtime-config" not in command
        if command[1:] != ["setup-private"]:
            assert environment["EMAIL_MEMORY_STORE_RUNTIME_CONFIG"] == runtime_config
        assert not set(hostile_connector_environment) & set(environment)
    assert all(
        not set(hostile_connector_environment) & set(environment)
        for environment in captured_environments
    )
    assert any(command[1:3] == ["account", "list"] for command in captured_commands)
    assert any(command[1:3] == ["folder", "list"] for command in captured_commands)

    helper = (
        Path(__file__).resolve().parents[1]
        / "src/email_memory_store/deployment/scripts/email_memory_environment.sh"
    )
    helper_result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f'source "{helper}"; printf %s "$EMAIL_MEMORY_STORE_ENVIRONMENT"',
        ],
        env={
            "HOME": str(args.home),
            "XDG_DATA_HOME": str(args.data_home),
            "EMAIL_MEMORY_TEST_MODE": "1",
        },
        capture_output=True,
        check=True,
        text=True,
    )
    assert helper_result.stdout == str(
        Path(args.data_home) / "email-memory-store/current"
    )


def test_fresh_install_activates_awaiting_index_without_weakening_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args, release, scripts = _fixture_args(tmp_path)
    captured_commands: list[list[str]] = []
    captured_environments: list[dict[str, str]] = []
    installed = _install_fakes(
        monkeypatch,
        args,
        scripts,
        captured_commands,
        captured_environments,
    )
    fake_run = deployment_cli._run
    indexed_documents = 0
    mcp_probes = 0

    def empty_index(
        command: Any,
        *,
        env: dict[str, str],
        input_bytes: bytes | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal mcp_probes
        parts = [str(item) for item in command]
        if "embed-status" in parts:
            output = json.dumps(
                {
                    "collections": {
                        name: indexed_documents
                        for name in deployment_cli.INDEX_COLLECTIONS
                    },
                    "persist_path": "/private/runtime/path",
                }
            ).encode()
            return subprocess.CompletedProcess(parts, 0, output, b"")
        if parts == [str(release / "venv/bin/email-memory-store-mcp")]:
            mcp_probes += 1
            if not indexed_documents:
                raise AssertionError("an empty index must not be treated as MCP-ready")
        return fake_run(command, env=env, input_bytes=input_bytes, timeout=timeout)

    monkeypatch.setattr(deployment_cli, "_run", empty_index)

    assert deployment_cli._bootstrap(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "schema_version": 1,
        "status": "awaiting-index",
        "paths_redacted": True,
    }
    receipt = json.loads((release / ".deployment-readiness.json").read_text())
    checks = {item["code"]: item["status"] for item in receipt["checks"]}
    assert receipt["status"] == "awaiting-index"
    assert checks["mcp_eof"] == "deferred"
    maintenance_index = next(
        index
        for index, command in enumerate(captured_commands)
        if command[:3] == ["/bin/bash", "-p", str(scripts / "nightly_maintenance.sh")]
    )
    assert (
        captured_environments[maintenance_index]["EMAIL_MEMORY_PREFLIGHT_ONLY"] == "1"
    )
    assert (Path(args.data_home) / "email-memory-store/current").resolve() == release
    monkeypatch.setattr(
        deployment_cli, "_read_crontab", lambda command, env: installed[-1]
    )

    assert deployment_cli._doctor(args) == deployment_cli.DOCTOR_AWAITING_INDEX_EXIT
    awaiting_output = json.loads(capsys.readouterr().out)
    assert awaiting_output["status"] == "awaiting-index"
    assert mcp_probes == 0

    indexed_documents = 1
    assert deployment_cli._doctor(args) == deployment_cli.DOCTOR_READY_EXIT
    ready_output = json.loads(capsys.readouterr().out)
    assert ready_output["status"] == "ready"
    assert mcp_probes == 1


@pytest.mark.parametrize(
    "footprint",
    ["managed-cron", "mcp-current", "mcp-stable", "receipt", "other-release"],
)
def test_removed_current_with_prior_footprint_cannot_defer_empty_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    footprint: str,
) -> None:
    args, release, scripts = _fixture_args(tmp_path)
    _install_fakes(monkeypatch, args, scripts)
    fake_run = deployment_cli._run
    current = Path(args.data_home) / "email-memory-store/current"
    current.parent.mkdir(mode=0o700)
    current.symlink_to(release)
    current.unlink()

    if footprint == "managed-cron":
        managed = (
            f"{deployment_cli.MANAGED_START}\n"
            "30 2 * * * /stale/deploy nightly\n"
            f"{deployment_cli.MANAGED_END}\n"
        ).encode()
        monkeypatch.setattr(
            deployment_cli, "_read_crontab", lambda command, env: managed
        )
    elif footprint in {"mcp-current", "mcp-stable"}:
        link = (
            Path(args.data_home) / "email-memory-store/mcp-launcher/current"
            if footprint == "mcp-current"
            else Path(args.home) / ".local/bin/email_memory_store_mcp_hermes.sh"
        )
        link.parent.mkdir(parents=True, mode=0o700)
        link.symlink_to("stale-target")
    elif footprint == "receipt":
        receipt = release / ".deployment-readiness.json"
        receipt.write_text("{}\n", encoding="utf-8")
        receipt.chmod(0o600)
    else:
        (release.parent / "prior-release").mkdir(mode=0o700)

    def empty_index(
        command: Any,
        *,
        env: dict[str, str],
        input_bytes: bytes | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        parts = [str(item) for item in command]
        if "embed-status" in parts:
            output = json.dumps(
                {
                    "collections": {
                        name: 0 for name in deployment_cli.INDEX_COLLECTIONS
                    },
                    "persist_path": "/private/runtime/path",
                }
            ).encode()
            return subprocess.CompletedProcess(parts, 0, output, b"")
        if parts == [str(release / "venv/bin/email-memory-store-mcp")]:
            raise AssertionError("a prior deployment footprint cannot defer MCP")
        return fake_run(command, env=env, input_bytes=input_bytes, timeout=timeout)

    monkeypatch.setattr(deployment_cli, "_run", empty_index)

    with pytest.raises(deployment_cli.BootstrapError, match="MCP readiness"):
        deployment_cli._bootstrap(args)

    assert not current.exists()
    assert not current.is_symlink()


def test_upgrade_with_existing_index_requires_mcp_probe_and_records_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, first_release, scripts = _fixture_args(tmp_path)
    _install_fakes(monkeypatch, args, scripts)
    assert deployment_cli._bootstrap(args) == 0

    replacement = first_release.parent / "replacement-py314-cpu"
    shutil.copytree(first_release, replacement)
    args.release_id = replacement.name
    captured_commands: list[list[str]] = []
    captured_environments: list[dict[str, str]] = []
    _install_fakes(
        monkeypatch,
        args,
        scripts,
        captured_commands,
        captured_environments,
    )

    assert deployment_cli._bootstrap(args) == 0

    embed_index = next(
        index
        for index, command in enumerate(captured_commands)
        if "embed-status" in command
    )
    mcp_index = next(
        index
        for index, command in enumerate(captured_commands)
        if command == [str(replacement / "venv/bin/email-memory-store-mcp")]
    )
    assert embed_index < mcp_index
    receipt = json.loads((replacement / ".deployment-readiness.json").read_text())
    checks = {item["code"]: item["status"] for item in receipt["checks"]}
    assert receipt["status"] == "ready"
    assert checks["mcp_eof"] == "pass"


def test_upgrade_with_empty_index_preserves_ready_active_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, first_release, scripts = _fixture_args(tmp_path)
    _install_fakes(monkeypatch, args, scripts)
    assert deployment_cli._bootstrap(args) == 0

    replacement = first_release.parent / "replacement-py314-cpu"
    shutil.copytree(first_release, replacement)
    (replacement / ".deployment-readiness.json").unlink()
    args.release_id = replacement.name
    _install_fakes(monkeypatch, args, scripts)
    fake_run = deployment_cli._run

    def empty_index(
        command: Any,
        *,
        env: dict[str, str],
        input_bytes: bytes | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        parts = [str(item) for item in command]
        if "embed-status" in parts:
            output = json.dumps(
                {
                    "collections": {
                        name: 0 for name in deployment_cli.INDEX_COLLECTIONS
                    },
                    "persist_path": "/private/runtime/path",
                }
            ).encode()
            return subprocess.CompletedProcess(parts, 0, output, b"")
        if parts == [str(replacement / "venv/bin/email-memory-store-mcp")]:
            raise AssertionError("an empty upgrade must not be treated as MCP-ready")
        return fake_run(command, env=env, input_bytes=input_bytes, timeout=timeout)

    monkeypatch.setattr(deployment_cli, "_run", empty_index)

    with pytest.raises(deployment_cli.BootstrapError, match="MCP readiness"):
        deployment_cli._bootstrap(args)

    current = Path(args.data_home) / "email-memory-store/current"
    assert current.resolve() == first_release
    assert not (replacement / ".deployment-readiness.json").exists()


def test_coordinator_runs_non_executable_provisioner_through_bash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, release, scripts = _fixture_args(tmp_path)
    staged_fixture = tmp_path / "staged-fixture"
    shutil.copytree(release, staged_fixture)
    shutil.rmtree(release)
    _install_fakes(monkeypatch, args, scripts)
    fake_run = deployment_cli._run
    provision_commands: list[list[str]] = []

    def stage_candidate(
        command: Any,
        *,
        env: dict[str, str],
        input_bytes: bytes | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        parts = [str(item) for item in command]
        if "--no-activate" in parts:
            provision_commands.append(parts)
            shutil.copytree(staged_fixture, release)
        return fake_run(command, env=env, input_bytes=input_bytes, timeout=timeout)

    monkeypatch.setattr(deployment_cli, "_run", stage_candidate)

    assert deployment_cli._bootstrap(args) == 0
    provisioner = (
        Path(args.public_checkout) / "scripts/provision_email_memory_environment.sh"
    )
    assert provision_commands[0][:3] == ["/bin/bash", "-p", str(provisioner)]


def test_coordinator_shell_children_receive_sanitized_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _release, scripts = _fixture_args(tmp_path)
    for key, value in {
        "BASH_ENV": "/hostile/bash-env",
        "ENV": "/hostile/env",
        "BASHOPTS": "extglob",
        "SHELLOPTS": "braceexpand",
        "PYTHONPYCACHEPREFIX": "/hostile/pycache",
        "PYTHONWARNINGS": "error",
    }.items():
        monkeypatch.setenv(key, value)
    _install_fakes(monkeypatch, args, scripts)
    fake_run = deployment_cli._run
    shell_environments: list[dict[str, str]] = []

    def record_shell_environment(
        command: Any,
        *,
        env: dict[str, str],
        input_bytes: bytes | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        if [str(item) for item in command][:2] == ["/bin/bash", "-p"]:
            shell_environments.append(env)
        return fake_run(command, env=env, input_bytes=input_bytes, timeout=timeout)

    monkeypatch.setattr(deployment_cli, "_run", record_shell_environment)

    assert deployment_cli._bootstrap(args) == 0
    assert len(shell_environments) == 2
    for child_env in shell_environments:
        assert {key for key in child_env if key.startswith("PYTHON")} == {
            "PYTHONNOUSERSITE"
        }
        assert not {"BASH_ENV", "ENV", "BASHOPTS", "SHELLOPTS"} & child_env.keys()


def test_scheduler_failure_rolls_back_current_mcp_and_crontab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _release, scripts = _fixture_args(tmp_path)
    deployment = Path(args.deployment_root)
    old_release = deployment / "envs/old"
    old_release.mkdir(parents=True)
    current = Path(args.data_home) / "email-memory-store/current"
    current.parent.mkdir(mode=0o700)
    current.symlink_to(old_release)
    mcp_current = Path(args.data_home) / "email-memory-store/mcp-launcher/current"
    mcp_stable = Path(args.home) / ".local/bin/email_memory_store_mcp_hermes.sh"
    mcp_current.parent.mkdir(parents=True)
    mcp_stable.parent.mkdir(parents=True)
    mcp_current.symlink_to("old-current")
    mcp_stable.symlink_to("old-stable")
    _install_fakes(monkeypatch, args, scripts)
    original_cron = b"15 1 * * * /unrelated/job\n"
    monkeypatch.setattr(
        deployment_cli, "_read_crontab", lambda command, env: original_cron
    )
    installed: list[bytes] = []

    def fail_managed(command: str, content: bytes, env: dict[str, str]) -> None:
        del command, env
        installed.append(content)
        if deployment_cli.MANAGED_START.encode() in content:
            raise deployment_cli.BootstrapError(
                "scheduler state could not be installed"
            )

    monkeypatch.setattr(deployment_cli, "_install_crontab", fail_managed)
    restore_order: list[Path] = []
    original_restore = deployment_cli._restore_link

    def record_restore(link: Path, previous: str | None) -> None:
        restore_order.append(link)
        original_restore(link, previous)

    monkeypatch.setattr(deployment_cli, "_restore_link", record_restore)

    with pytest.raises(deployment_cli.BootstrapError, match="scheduler"):
        deployment_cli._bootstrap(args)

    assert current.resolve() == old_release
    assert os.readlink(mcp_current) == "old-current"
    assert os.readlink(mcp_stable) == "old-stable"
    assert installed[-1] == original_cron
    assert restore_order[:2] == [mcp_current, mcp_stable]


@pytest.mark.parametrize(
    "failed_restore", ["crontab", "mcp-current", "mcp-stable", "active"]
)
def test_each_rollback_failure_does_not_skip_other_restorations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_restore: str,
) -> None:
    args, _release, scripts = _fixture_args(tmp_path)
    args.fail_after_activation = True
    deployment = Path(args.deployment_root)
    old_release = deployment / "envs/old"
    old_release.mkdir(parents=True)
    current = Path(args.data_home) / "email-memory-store/current"
    current.parent.mkdir(mode=0o700)
    current.symlink_to(old_release)
    mcp_current = Path(args.data_home) / "email-memory-store/mcp-launcher/current"
    mcp_stable = Path(args.home) / ".local/bin/email_memory_store_mcp_hermes.sh"
    mcp_current.parent.mkdir(parents=True)
    mcp_stable.parent.mkdir(parents=True)
    mcp_current.symlink_to("old-current")
    mcp_stable.symlink_to("old-stable")
    _install_fakes(monkeypatch, args, scripts)
    original_cron = b"15 1 * * * /unrelated/job\n"
    monkeypatch.setattr(
        deployment_cli, "_read_crontab", lambda command, env: original_cron
    )
    cron_attempts: list[bytes] = []

    def install_then_maybe_fail_restore(
        command: str, content: bytes, env: dict[str, str]
    ) -> None:
        del command, env
        cron_attempts.append(content)
        if content == original_cron and failed_restore == "crontab":
            raise deployment_cli.BootstrapError("sensitive restoration detail")

    monkeypatch.setattr(
        deployment_cli, "_install_crontab", install_then_maybe_fail_restore
    )
    restored: list[Path] = []
    original_restore = deployment_cli._restore_link

    def fail_selected_restore(link: Path, previous: str | None) -> None:
        restored.append(link)
        labels = {
            mcp_current: "mcp-current",
            mcp_stable: "mcp-stable",
            current: "active",
        }
        if labels[link] == failed_restore:
            raise OSError("sensitive restoration detail")
        original_restore(link, previous)

    monkeypatch.setattr(deployment_cli, "_restore_link", fail_selected_restore)

    with pytest.raises(
        deployment_cli.BootstrapError,
        match="injected post-activation failure; rollback failed",
    ) as captured:
        deployment_cli._bootstrap(args)

    assert "sensitive" not in str(captured.value)
    assert cron_attempts[-1] == original_cron
    assert restored == [mcp_current, mcp_stable, current]
    if failed_restore != "mcp-current":
        assert os.readlink(mcp_current) == "old-current"
    if failed_restore != "mcp-stable":
        assert os.readlink(mcp_stable) == "old-stable"
    if failed_restore != "active":
        assert current.resolve() == old_release


def test_interruption_after_activation_rolls_back_without_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _release, scripts = _fixture_args(tmp_path)
    deployment = Path(args.deployment_root)
    old_release = deployment / "envs/old"
    old_release.mkdir(parents=True)
    current = Path(args.data_home) / "email-memory-store/current"
    current.parent.mkdir(mode=0o700)
    current.symlink_to(old_release)
    _install_fakes(monkeypatch, args, scripts)
    fake_run = deployment_cli._run

    def interrupt_maintenance(
        command: Any,
        *,
        env: dict[str, str],
        input_bytes: bytes | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        if [str(item) for item in command][:3] == [
            "/bin/bash",
            "-p",
            str(scripts / "nightly_maintenance.sh"),
        ]:
            raise KeyboardInterrupt
        return fake_run(command, env=env, input_bytes=input_bytes, timeout=timeout)

    monkeypatch.setattr(deployment_cli, "_run", interrupt_maintenance)

    with pytest.raises(KeyboardInterrupt):
        deployment_cli._bootstrap(args)

    assert current.resolve() == old_release


def test_doctor_rejects_receipt_replayed_for_different_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, release, scripts = _fixture_args(tmp_path)
    installed = _install_fakes(monkeypatch, args, scripts)
    assert deployment_cli._bootstrap(args) == 0
    monkeypatch.setattr(
        deployment_cli, "_read_crontab", lambda command, env: installed[-1]
    )
    other = release.parent / "other-py314-cpu"
    shutil.copytree(release, other)
    current = Path(args.data_home) / "email-memory-store/current"
    current.unlink()
    current.symlink_to(other)

    assert deployment_cli._doctor(args) == 1


@pytest.mark.parametrize(
    "marker",
    [
        "public_revision=ffffffffffff\npython=3.14\naccelerator_request=cpu\n",
        "public_revision=0123456789ab\npython=3.13\naccelerator_request=cpu\n",
        "public_revision=0123456789ab\npython=3.14\naccelerator_request=auto\n",
        "public_revision=0123456789ab\npython=3.14\naccelerator_request=cpu\nextra=value\n",
        "public_revision=0123456789ab\npython=3.14\naccelerator_request=cpu",
    ],
)
def test_existing_candidate_must_match_requested_checkout_and_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker: str,
) -> None:
    args, release, scripts = _fixture_args(tmp_path)
    (release / ".email-memory-release").write_text(marker, encoding="utf-8")
    (release / ".email-memory-release").chmod(0o600)
    _install_fakes(monkeypatch, args, scripts)
    fake_run = deployment_cli._run
    commands: list[list[str]] = []

    def record_run(
        command: Any,
        *,
        env: dict[str, str],
        input_bytes: bytes | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        commands.append([str(item) for item in command])
        return fake_run(command, env=env, input_bytes=input_bytes, timeout=timeout)

    monkeypatch.setattr(deployment_cli, "_run", record_run)

    with pytest.raises(deployment_cli.BootstrapError, match="candidate release"):
        deployment_cli._bootstrap(args)

    assert any(
        command[0] == "/usr/bin/git" and "rev-parse" in command for command in commands
    )
    assert not any(
        "init-db" in command or "runtime-doctor" in command for command in commands
    )


def test_doctor_structural_failure_executes_no_candidate_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _release, scripts = _fixture_args(tmp_path)
    installed = _install_fakes(monkeypatch, args, scripts)
    assert deployment_cli._bootstrap(args) == 0
    monkeypatch.setattr(
        deployment_cli, "_read_crontab", lambda command, env: installed[-1]
    )
    current = Path(args.data_home) / "email-memory-store/current"
    receipt = current.resolve() / ".deployment-readiness.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["release_identity"] = "f" * 64
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    receipt.chmod(0o600)
    candidate_executed = False

    def reject_candidate_execution(*args: Any, **kwargs: Any) -> Any:
        nonlocal candidate_executed
        candidate_executed = True
        raise AssertionError("candidate execution must be structurally gated")

    monkeypatch.setattr(deployment_cli, "_run", reject_candidate_execution)
    monkeypatch.setattr(
        deployment_cli, "_load_configuration", reject_candidate_execution
    )

    assert deployment_cli._doctor(args) == 1
    assert candidate_executed is False


def test_sigterm_during_final_activation_rolls_back_and_restores_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _release, scripts = _fixture_args(tmp_path)
    deployment = Path(args.deployment_root)
    old_release = deployment / "envs/old"
    old_release.mkdir(parents=True)
    current = Path(args.data_home) / "email-memory-store/current"
    current.parent.mkdir(mode=0o700)
    current.symlink_to(old_release)
    _install_fakes(monkeypatch, args, scripts)
    previous = {
        signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGHUP)
    }
    original_atomic = deployment_cli._atomic_symlink
    interrupted = False

    def interrupt_after_publish(target: Path | str, link: Path) -> None:
        nonlocal interrupted
        original_atomic(target, link)
        if link == current and not interrupted:
            interrupted = True
            signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(deployment_cli, "_atomic_symlink", interrupt_after_publish)

    with pytest.raises(deployment_cli.BootstrapError, match="interrupted"):
        deployment_cli._bootstrap(args)

    assert current.resolve() == old_release
    assert {signum: signal.getsignal(signum) for signum in previous} == previous


@pytest.mark.parametrize("failure_point", ["before-current", "after-current"])
def test_failed_upgrade_preserves_old_release_receipt_and_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    args, first_release, scripts = _fixture_args(tmp_path)
    installed = _install_fakes(monkeypatch, args, scripts)
    assert deployment_cli._bootstrap(args) == 0
    current = Path(args.data_home) / "email-memory-store/current"
    old_receipt = (first_release / ".deployment-readiness.json").read_bytes()
    old_cron = installed[-1]
    monkeypatch.setattr(deployment_cli, "_read_crontab", lambda command, env: old_cron)

    second_release = first_release.parent / "replacement-py314-cpu"
    shutil.copytree(first_release, second_release)
    args.release_id = second_release.name
    original_atomic = deployment_cli._atomic_symlink

    if failure_point == "before-current":

        def fail_publication(target: Path | str, link: Path) -> None:
            if link == current and Path(target) == second_release:
                raise deployment_cli.BootstrapError(
                    "synthetic current publication failure"
                )
            original_atomic(target, link)

        monkeypatch.setattr(deployment_cli, "_atomic_symlink", fail_publication)
    else:
        args.fail_after_activation = True

    with pytest.raises(deployment_cli.BootstrapError):
        deployment_cli._bootstrap(args)

    assert current.resolve() == first_release
    assert (first_release / ".deployment-readiness.json").read_bytes() == old_receipt
    assert installed[-1] == old_cron
    assert deployment_cli._doctor(args) == 0


def test_receipt_directory_is_synced_before_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, release, scripts = _fixture_args(tmp_path)
    _install_fakes(monkeypatch, args, scripts)
    synced: list[Path] = []
    monkeypatch.setattr(deployment_cli, "_fsync_directory", synced.append)

    assert deployment_cli._bootstrap(args) == 0

    active_root = Path(args.data_home) / "email-memory-store"
    assert synced == [
        release,
        release.parent,
        Path(args.data_home) / "email-memory-store/mcp-launcher",
        Path(args.home) / ".local/bin",
        release,
        active_root,
    ]


def test_release_tree_barrier_failure_prevents_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, release, scripts = _fixture_args(tmp_path)
    installed = _install_fakes(monkeypatch, args, scripts)

    def fail_barrier(_candidate: Path, _env: dict[str, str]) -> None:
        raise deployment_cli.BootstrapError("deployment check failed")

    monkeypatch.setattr(deployment_cli, "_sync_release_tree", fail_barrier)

    with pytest.raises(deployment_cli.BootstrapError, match="deployment check failed"):
        deployment_cli._bootstrap(args)

    current = Path(args.data_home) / "email-memory-store/current"
    assert not current.exists()
    assert installed == []
    assert not (release / ".deployment-readiness.json").exists()


def test_mcp_durability_barrier_failure_prevents_receipt_and_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, release, scripts = _fixture_args(tmp_path)
    installed = _install_fakes(monkeypatch, args, scripts)

    def fail_barrier(_current: Path, _stable: Path, _env: dict[str, str]) -> None:
        raise deployment_cli.BootstrapError(
            "deployment state could not be made durable"
        )

    monkeypatch.setattr(deployment_cli, "_sync_mcp_publication", fail_barrier)

    with pytest.raises(
        deployment_cli.BootstrapError, match="could not be made durable"
    ):
        deployment_cli._bootstrap(args)

    current = Path(args.data_home) / "email-memory-store/current"
    assert not current.exists()
    assert installed == []
    assert not (release / ".deployment-readiness.json").exists()


def test_unlink_restore_fsyncs_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = tmp_path / "current"
    link.symlink_to("candidate")
    synced: list[Path] = []
    monkeypatch.setattr(deployment_cli, "_fsync_directory", synced.append)

    deployment_cli._restore_link(link, None)

    assert not link.exists()
    assert synced == [tmp_path]


def test_crontab_read_failure_preserves_scheduler_and_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, release, scripts = _fixture_args(tmp_path)
    old_release = release.parent / "old"
    old_release.mkdir()
    current = Path(args.data_home) / "email-memory-store/current"
    current.parent.mkdir(mode=0o700)
    current.symlink_to(old_release)
    installed = _install_fakes(monkeypatch, args, scripts)

    def fail_read(_command: str, _env: dict[str, str]) -> bytes:
        raise deployment_cli.BootstrapError("scheduler state could not be read")

    monkeypatch.setattr(deployment_cli, "_read_crontab", fail_read)

    with pytest.raises(deployment_cli.BootstrapError, match="scheduler state"):
        deployment_cli._bootstrap(args)

    assert current.resolve() == old_release
    assert installed == []
    assert not (release / ".deployment-readiness.json").exists()

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
from typing import Any

import pytest

from email_memory_store.deployment.cli import (
    BootstrapError,
    DEFAULT_PROBE_TIMEOUT_SECONDS,
    MANAGED_END,
    MANAGED_START,
    RECEIPT_CODES,
    PUBLIC_FACT_STORE_PROVIDER,
    _candidate_is_verified,
    _check_fact_provider,
    _git_command,
    _git_environment,
    _managed_crontab,
    _nightly,
    _parser,
    _read_crontab,
    _run,
    _validate_production_roots,
    _validate_schedule,
    _verify_default_mail_account,
    _write_receipt,
    main,
)


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["bootstrap", "--public-checkout", "/trusted/checkout"], "bootstrap"),
        (["doctor"], "doctor"),
    ],
)
def test_deployment_commands_allow_cold_start_by_default(
    argv: list[str], command: str
) -> None:
    args = _parser().parse_args(argv)

    assert args.command == command
    assert args.probe_timeout == DEFAULT_PROBE_TIMEOUT_SECONDS == 60


@pytest.mark.parametrize("command", ["bootstrap", "doctor"])
@pytest.mark.parametrize("timeout", ["0", "-1", "not-a-number"])
def test_deployment_commands_reject_nonpositive_probe_timeout(
    command: str, timeout: str
) -> None:
    argv = [command]
    if command == "bootstrap":
        argv.extend(["--public-checkout", "/trusted/checkout"])
    argv.extend(["--probe-timeout", timeout])

    with pytest.raises(SystemExit) as error:
        _parser().parse_args(argv)

    assert error.value.code == 2


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        [{"name": "selected", "default": False}],
        [{"name": "different", "default": True}],
        [
            {"name": "selected", "default": True},
            {"name": "selected", "default": False},
        ],
        [
            {"name": "selected", "default": True},
            {"name": "different", "default": True},
        ],
    ],
)
def test_mail_account_readiness_fails_closed_without_one_selected_default(
    payload: object,
) -> None:
    with pytest.raises(BootstrapError, match="mail connector account readiness failed"):
        _verify_default_mail_account(json.dumps(payload).encode(), "selected")


def test_mail_account_readiness_accepts_one_selected_default() -> None:
    _verify_default_mail_account(b'[{"name":"selected","default":true}]', "selected")


def _secure_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True, mode=0o700)
    checkout.chmod(0o700)
    pyproject = checkout / "pyproject.toml"
    pyproject.write_text("[project]\nname='fixture'\n", encoding="utf-8")
    pyproject.chmod(0o600)
    provisioner = scripts / "provision_email_memory_environment.sh"
    provisioner.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    provisioner.chmod(0o600)
    return checkout


def _fake_revision_run(
    command: Any,
    *,
    env: dict[str, str],
    input_bytes: bytes | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[bytes]:
    del env, input_bytes, timeout
    parts = [str(item) for item in command]
    output = b"0123456789ab\n" if "rev-parse" in parts else b""
    return subprocess.CompletedProcess(parts, 0, output, b"")


def test_scheduler_replaces_only_the_explicit_direct_command() -> None:
    old = b"/opt/old/nightly.sh"
    original = (
        b"MAILTO=ops@example.test\n"
        b"15 1 * * * /unrelated/job\n"
        b"5 4 * * 2 /opt/old/nightly.sh\n"
        + b"# BEGIN email-memory-store managed\n"
        + b"30 2 * * * /old/managed nightly\n"
        + b"# END email-memory-store managed\n"
    )

    result = _managed_crontab(
        original,
        "30 2 * * * /xdg/current/bin/email-memory-store-deploy nightly",
        old.decode(),
    ).decode()

    assert "/unrelated/job" in result
    assert old.decode() not in result
    assert result.count(MANAGED_START) == 1
    assert result.count(MANAGED_END) == 1
    assert result.count("email-memory-store-deploy nightly") == 1


def test_candidate_verification_fails_closed_on_filesystem_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()

    def fail_is_dir(_path: Path) -> bool:
        raise OSError("synthetic filesystem failure")

    monkeypatch.setattr(Path, "is_dir", fail_is_dir)
    assert _candidate_is_verified(candidate) is False


def test_git_environment_discards_ambient_git_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", "/hostile")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/hostile/hooks")

    env = _git_environment()

    assert "GIT_DIR" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_git_commands_disable_repository_fsmonitor(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["/usr/bin/git", "init", "-q", repository], check=True)
    marker = tmp_path / "fsmonitor-ran"
    fsmonitor = tmp_path / "fsmonitor"
    fsmonitor.write_text(
        f"#!/bin/sh\n/usr/bin/touch '{marker}'\nexit 1\n", encoding="utf-8"
    )
    fsmonitor.chmod(0o700)
    subprocess.run(
        ["/usr/bin/git", "-C", repository, "config", "core.fsmonitor", str(fsmonitor)],
        check=True,
    )

    _run(
        _git_command("-C", repository, "status", "--porcelain"),
        env=_git_environment(),
    )

    assert not marker.exists()


def test_crontab_exit_one_is_empty_only_for_exact_no_crontab_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    username = pwd.getpwuid(os.getuid()).pw_name.encode()

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, b"", b"no crontab for " + username + b"\n"
        ),
    )
    assert _read_crontab("/usr/bin/crontab", {}) == b""


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        (b"", b"permission denied\n"),
        (b"partial\n", b"no crontab for user\n"),
        (b"", b"spool read failed\n"),
    ],
)
def test_crontab_exit_one_failures_are_redacted(
    monkeypatch: pytest.MonkeyPatch, stdout: bytes, stderr: bytes
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout, stderr),
    )

    with pytest.raises(
        BootstrapError, match="scheduler state could not be read"
    ) as error:
        _read_crontab("/usr/bin/crontab", {})

    assert "permission" not in str(error.value)
    assert "spool" not in str(error.value)


def test_privileged_bash_internal_call_ignores_bash_env(tmp_path: Path) -> None:
    marker = tmp_path / "bash-env-ran"
    bash_env = tmp_path / "bash-env"
    bash_env.write_text(f"/usr/bin/touch '{marker}'\n", encoding="utf-8")
    script = tmp_path / "operation.sh"
    script.write_text("#!/bin/bash -p\nexit 0\n", encoding="utf-8")
    script.chmod(0o700)

    _run(["/bin/bash", "-p", script], env=os.environ | {"BASH_ENV": str(bash_env)})

    assert not marker.exists()


def test_nightly_clears_test_and_executable_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    hostile = {
        "EMAIL_MEMORY_TEST_MODE": "1",
        "EMAIL_MEMORY_TEST_EXECUTABLE": "/hostile/test",
        "EMAIL_MEMORY_STORE_COMMAND": "/hostile/cli",
        "EMAIL_MEMORY_STORE_MCP_COMMAND": "/hostile/mcp",
        "EMAIL_MEMORY_OPERATIONAL_PYTHON": "/hostile/python",
        "EMAIL_MEMORY_MAINTENANCE_SCRIPT": "/hostile/maintenance",
        "EMAIL_MEMORY_PREFLIGHT_ONLY": "1",
        "EMAIL_MEMORY_UNRELATED_TEST_KNOB": "hostile",
        "LLM_PREFLIGHT_EXPECTED_RESPONSE": "hostile",
        "HIMALAYA_CONFIG": "/hostile/himalaya.toml",
        "HERMES_HOME": "/hostile/hermes-home",
        "HERMES_PROFILE": "hostile-profile",
        "HERMES_MODEL": "hostile-model",
        "HERMES_PROVIDER": "hostile-provider",
        "HERMES_PLUGIN_PATH": "/hostile/plugins",
        "HERMES_TEST_MODE": "1",
        "PYTHONPATH": "/hostile/modules",
    }
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)

    def capture_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(path=path, argv=argv, env=env)
        raise RuntimeError("captured")

    monkeypatch.setattr(os, "execve", capture_execve)
    with pytest.raises(RuntimeError, match="captured"):
        _nightly(argparse.Namespace())

    assert captured["path"] == "/bin/bash"
    assert isinstance(captured["argv"], list)
    assert captured["argv"][:2] == ["/bin/bash", "-p"]
    launched_env = captured["env"]
    assert isinstance(launched_env, dict)
    assert not set(hostile) & set(launched_env)
    assert launched_env["PYTHONNOUSERSITE"] == "1"
    canonical_home = pwd.getpwuid(os.getuid()).pw_dir
    assert launched_env["HOME"] == canonical_home
    assert launched_env["XDG_CONFIG_HOME"] == f"{canonical_home}/.config"
    assert launched_env["XDG_DATA_HOME"] == f"{canonical_home}/.local/share"
    assert launched_env["XDG_STATE_HOME"] == f"{canonical_home}/.local/state"


def test_production_roots_reject_overrides_but_test_mode_allows_them(
    tmp_path: Path,
) -> None:
    roots = {
        "home": tmp_path / "home",
        "config_home": tmp_path / "config",
        "data_home": tmp_path / "data",
        "state_home": tmp_path / "state",
        "deployment": tmp_path / "deployment",
    }

    with pytest.raises(BootstrapError, match="canonical user locations"):
        _validate_production_roots(**roots, test_mode=False)

    _validate_production_roots(**roots, test_mode=True)


@pytest.mark.parametrize(
    "schedule",
    [
        "0 0 ? * *",
        "0 0 * * #",
        "0 0 * *",
        "@daily",
        "60 0 * * *",
        "0 24 * * *",
        "0 0 0 * *",
        "0 0 * 13 *",
        "0 0 * * 8",
        "0 0 * * */0",
        "0\t0 * * *",
        "0 0 * * *\n",
    ],
)
def test_scheduler_rejects_unsupported_cron_grammar(schedule: str) -> None:
    with pytest.raises(BootstrapError):
        _validate_schedule(schedule)


def test_fact_provider_reports_disabled_when_unconfigured(tmp_path: Path) -> None:
    assert (
        _check_fact_provider(Path(sys.executable), {}, dict(os.environ)) == "disabled"
    )


def test_fact_provider_reports_ready_only_after_import_probe(tmp_path: Path) -> None:
    module_root = tmp_path / "adapter"
    provider_package = module_root / "plugins/memory/holographic"
    provider_package.mkdir(parents=True)
    for directory in (module_root, provider_package, *provider_package.parents):
        if directory.is_relative_to(module_root):
            directory.chmod(0o700)
    for package in (
        module_root / "plugins",
        module_root / "plugins/memory",
        provider_package,
    ):
        initializer = package / "__init__.py"
        initializer.write_text("", encoding="utf-8")
        initializer.chmod(0o600)
    store = provider_package / "store.py"
    store.write_text("class MemoryStore:\n    pass\n", encoding="utf-8")
    store.chmod(0o600)
    config = {
        "EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT": str(module_root),
        "EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER": PUBLIC_FACT_STORE_PROVIDER,
    }

    assert (
        _check_fact_provider(Path(sys.executable), config, dict(os.environ)) == "ready"
    )

    config["EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER"] = "adapter:MemoryStore"
    with pytest.raises(BootstrapError, match="configuration is invalid"):
        _check_fact_provider(Path(sys.executable), config, dict(os.environ))


@pytest.mark.parametrize(
    "config",
    [
        {"EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT": "/configured"},
        {"EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER": PUBLIC_FACT_STORE_PROVIDER},
    ],
)
def test_fact_provider_rejects_half_configured_adapter(config: dict[str, str]) -> None:
    with pytest.raises(BootstrapError, match="configuration is invalid"):
        _check_fact_provider(Path(sys.executable), config, dict(os.environ))


def test_receipt_rejects_symlinked_release(tmp_path: Path) -> None:
    real_release = tmp_path / "real-release"
    real_release.mkdir(mode=0o700)
    release = tmp_path / "release"
    release.symlink_to(real_release, target_is_directory=True)
    receipt = release / ".deployment-readiness.json"

    with pytest.raises(BootstrapError, match="ancestor|location"):
        _write_receipt(
            receipt, release, {code: "pass" for code in RECEIPT_CODES}, "0" * 64
        )

    assert not (real_release / ".deployment-readiness.json").exists()


def test_receipt_rejects_symlink_above_release(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    release = linked_parent / "release"
    receipt = release / ".deployment-readiness.json"

    with pytest.raises(BootstrapError, match="ancestor"):
        _write_receipt(
            receipt,
            release,
            {code: "pass" for code in RECEIPT_CODES},
            "0" * 64,
        )

    assert not (real_parent / "release").exists()


def test_receipt_rejects_writable_release(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    release.chmod(0o770)
    receipt = release / ".deployment-readiness.json"

    with pytest.raises(BootstrapError, match="ancestor"):
        _write_receipt(
            receipt,
            release,
            {code: "pass" for code in RECEIPT_CODES},
            "0" * 64,
        )


@pytest.mark.parametrize(
    "unsafe",
    [
        "/safe/job name",
        "/safe/job%payload",
        "/safe/job#comment",
        "/safe/job;command",
        "/safe/job\n* * * * * command",
        "/safe/job\targument",
        "/safe/job$HOME",
    ],
)
def test_bootstrap_rejects_cron_metacharacters_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    root = _secure_checkout(tmp_path)
    monkeypatch.setattr("email_memory_store.deployment.cli._run", _fake_revision_run)
    home = tmp_path / "home"
    for directory in (home,):
        directory.mkdir(mode=0o700)
    crontab = tmp_path / "crontab"
    crontab.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    crontab.chmod(0o700)

    result = main(
        [
            "bootstrap",
            "--public-checkout",
            str(root),
            "--home",
            str(home),
            "--replace-scheduler-command",
            unsafe,
            "--crontab-command",
            str(crontab),
            "--release-id",
            "fixture-py314-cpu",
            "--test-mode",
        ]
    )

    assert result == 1
    assert not (home / ".local/share/email-memory-store/envs").exists()


@pytest.mark.parametrize("destination", ["current", "stable"])
def test_bootstrap_never_replaces_regular_mcp_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, destination: str
) -> None:
    root = _secure_checkout(tmp_path)
    monkeypatch.setattr("email_memory_store.deployment.cli._run", _fake_revision_run)
    home = tmp_path / "home"
    data = tmp_path / "data"
    state = tmp_path / "state"
    config = tmp_path / "config"
    for directory in (home, data, state, config):
        directory.mkdir(mode=0o700)
    crontab = tmp_path / "crontab"
    crontab.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    crontab.chmod(0o700)
    current = data / "email-memory-store/mcp-launcher/current"
    stable = home / ".local/bin/email_memory_store_mcp_hermes.sh"
    protected = current if destination == "current" else stable
    protected.parent.mkdir(parents=True)
    protected.write_text("user-owned\n", encoding="utf-8")

    result = main(
        [
            "bootstrap",
            "--public-checkout",
            str(root),
            "--home",
            str(home),
            "--config-home",
            str(config),
            "--data-home",
            str(data),
            "--state-home",
            str(state),
            "--release-id",
            "fixture-py314-cpu",
            "--crontab-command",
            str(crontab),
            "--test-mode",
        ]
    )

    assert result == 1
    assert protected.read_text(encoding="utf-8") == "user-owned\n"


@pytest.mark.parametrize("unsafe_kind", ["writable", "symlink", "hardlink"])
def test_checkout_validation_rejects_untrusted_content(
    tmp_path: Path, unsafe_kind: str
) -> None:
    checkout = _secure_checkout(tmp_path)
    target = checkout / "payload"
    target.write_text("payload\n", encoding="utf-8")
    target.chmod(0o600)
    if unsafe_kind == "writable":
        target.chmod(0o620)
    elif unsafe_kind == "symlink":
        target.unlink()
        target.symlink_to(checkout / "pyproject.toml")
    else:
        os.link(target, checkout / "payload-link")

    with pytest.raises(BootstrapError, match="checkout is not trusted"):
        from email_memory_store.deployment import cli as deployment_cli

        deployment_cli._validate_trusted_checkout(checkout)


def test_crontab_resolution_never_uses_ambient_path(tmp_path: Path) -> None:
    from email_memory_store.deployment import cli as deployment_cli

    with pytest.raises(BootstrapError, match="absolute trusted path"):
        deployment_cli._cron_executable("custom-crontab")

    custom = tmp_path / "crontab"
    custom.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    custom.chmod(0o720)
    with pytest.raises(BootstrapError, match="not trusted"):
        deployment_cli._cron_executable(str(custom))

    custom.chmod(0o700)
    alias = tmp_path / "crontab-alias"
    alias.symlink_to(custom)
    with pytest.raises(BootstrapError, match="not trusted"):
        deployment_cli._cron_executable(str(alias))

    alias.unlink()
    os.link(custom, alias)
    with pytest.raises(BootstrapError, match="not trusted"):
        deployment_cli._cron_executable(str(custom))


def test_bootstrap_rejects_symlinked_checkout_before_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _secure_checkout(tmp_path)
    linked_checkout = tmp_path / "linked-checkout"
    linked_checkout.symlink_to(checkout, target_is_directory=True)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    subprocess_called = False

    def unexpected_run(*args: Any, **kwargs: Any) -> Any:
        nonlocal subprocess_called
        subprocess_called = True
        raise AssertionError("untrusted checkout must fail before subprocess")

    monkeypatch.setattr("email_memory_store.deployment.cli._run", unexpected_run)

    result = main(
        [
            "bootstrap",
            "--public-checkout",
            str(linked_checkout),
            "--home",
            str(home),
            "--release-id",
            "fixture-py314-cpu",
            "--test-mode",
        ]
    )

    assert result == 1
    assert subprocess_called is False


def test_public_deployment_sources_do_not_name_a_private_checkout() -> None:
    root = Path(__file__).resolve().parents[1]
    deployment_sources = [
        *sorted((root / "src/email_memory_store/deployment").rglob("*")),
        root / "scripts/deploy.sh",
        root / "scripts/provision_email_memory_environment.sh",
    ]
    forbidden = b"private" + b"/operations"
    for path in deployment_sources:
        if path.is_file():
            assert forbidden not in path.read_bytes(), path

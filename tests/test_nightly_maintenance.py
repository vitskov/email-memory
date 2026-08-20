from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "src/email_memory_store/deployment/scripts"
FACT_STORE_PROVIDER = "email_memory_store.integrations.hermes_fact_store:MemoryStore"


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)
    return path


def _nightly_fixture(
    tmp_path: Path, *, fact_enabled: bool, hermes_chat_response: str = "OK"
) -> tuple[Path, dict[str, str], Path]:
    scripts = tmp_path / "scripts"
    scripts.mkdir(mode=0o700)
    maintenance = scripts / "nightly_maintenance.sh"
    maintenance.write_bytes((SCRIPTS / maintenance.name).read_bytes())
    maintenance.chmod(0o700)
    environment = scripts / "email_memory_environment.sh"
    environment.write_bytes((SCRIPTS / environment.name).read_bytes())
    environment.chmod(0o700)

    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    runtime_config = tmp_path / "runtime.toml"
    runtime_config.write_text("synthetic\n", encoding="utf-8")
    runtime_config.chmod(0o600)
    command_log = tmp_path / "commands.log"
    alert_log = tmp_path / "alerts.log"
    connector_env_log = tmp_path / "connector-env.log"
    tool_dir = tmp_path / "tools"
    tool_dir.mkdir(mode=0o700)

    mail_client = _write_executable(
        tool_dir / "mail-client",
        "#!/bin/bash\n"
        f"/usr/bin/env | /usr/bin/grep -E '^(HIMALAYA_|HERMES_)' >>{shlex.quote(str(connector_env_log))} || true\n"
        f"printf 'mail %s\\n' \"$*\" >>{shlex.quote(str(command_log))}\n"
        "if [[ \"$1 $2\" == 'account list' ]]; then\n"
        "  if [[ -n \"${MAIL_ACCOUNT_JSON:-}\" ]]; then\n"
        "    printf '%s\\n' \"$MAIL_ACCOUNT_JSON\"\n"
        "  else\n"
        "    printf '%s\\n' '[{\"name\":\"synthetic-account\",\"default\":true}]'\n"
        "  fi\n"
        "else\n"
        "  printf '%s\\n' '{\"ready\":true}'\n"
        "fi\n",
    )
    hermes = _write_executable(
        tool_dir / "hermes",
        "#!/bin/bash\n"
        f"/usr/bin/env | /usr/bin/grep -E '^(HIMALAYA_|HERMES_)' >>{shlex.quote(str(connector_env_log))} || true\n"
        "if [[ \"$1\" == 'chat' ]]; then\n"
        f"  printf '%s\\n' {shlex.quote(hermes_chat_response)}\n"
        "  exit 0\n"
        "fi\n"
        f"printf '%s\\n' \"$1\" >>{shlex.quote(str(alert_log))}\n",
    )
    command = _write_executable(
        tool_dir / "email-memory-store",
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" >>{shlex.quote(str(command_log))}\n"
        'case "$*" in\n'
        "  *'nightly-update'*) printf '%s\\n' '{\"folder_fetch_failures\":[]}' ;;\n"
        "  *'run-llm-promotions'*)\n"
        '    if [[ -n "${PROMOTION_RESULT:-}" ]]; then\n'
        "      printf '%s\\n' \"$PROMOTION_RESULT\"\n"
        "    else\n"
        "      printf '%s\\n' '{\"errors\":0}'\n"
        "    fi\n"
        "    ;;\n"
        "  *) printf '%s\\n' '{}' ;;\n"
        "esac\n",
    )
    operational_python = tool_dir / "python"
    exports = {
        "EMAIL_MEMORY_ACCOUNT_NAME": "synthetic-account",
        "EMAIL_MEMORY_ACCOUNT_EMAIL": "operator@example.test",
        "EMAIL_MEMORY_INCLUDE_FOLDERS_JSON": json.dumps(
            ["Private Inbox", "Private Archive"]
        ),
        "EMAIL_MEMORY_EXCLUDE_FOLDERS_JSON": json.dumps(["Private Trash"]),
        "EMAIL_MEMORY_HERMES_EXECUTABLE": str(hermes),
        "EMAIL_MEMORY_MAIL_CLIENT_EXECUTABLE": str(mail_client),
        "EMAIL_MEMORY_ROOT": str(runtime_root),
        "EMAIL_MEMORY_STORE_RUNTIME_CONFIG": str(runtime_config),
        "HERMES_ALERT_TARGET": "telegram",
    }
    if fact_enabled:
        exports.update(
            {
                "EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER": FACT_STORE_PROVIDER,
                "EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT": str(tmp_path / "facts"),
            }
        )
    rendered_exports = "\n".join(
        f"export {key}={shlex.quote(value)}" for key, value in exports.items()
    )
    _write_executable(
        operational_python,
        "#!/bin/bash\n"
        "if [[ \"$1\" == '-m' && \"$2\" == 'email_memory_store.local_config' ]]; then\n"
        "  if [[ \"${LOCAL_CONFIG_FAIL:-0}\" == '1' ]]; then exit 42; fi\n"
        f"  printf '%s\\n' {shlex.quote(rendered_exports)}\n"
        "  exit 0\n"
        "fi\n"
        f'exec {shlex.quote(sys.executable)} "$@"\n',
    )
    env = {
        **os.environ,
        "EMAIL_MEMORY_OPERATIONAL_PYTHON": str(operational_python),
        "EMAIL_MEMORY_STORE_COMMAND": str(command),
        "EMAIL_MEMORY_STORE_ENVIRONMENT": str(tmp_path / "environment"),
        "EMAIL_MEMORY_TEST_MODE": "1",
        "PYTHONPATH": str(ROOT / "src"),
    }
    return maintenance, env, command_log


def test_nightly_skips_llm_promotions_when_fact_integration_is_disabled(
    tmp_path: Path,
) -> None:
    maintenance, env, command_log = _nightly_fixture(tmp_path, fact_enabled=False)

    completed = subprocess.run(
        [str(maintenance)], env=env, capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert "run-llm-promotions" not in command_log.read_text(encoding="utf-8")


def test_nightly_keeps_private_selectors_out_of_child_process_argv(
    tmp_path: Path,
) -> None:
    maintenance, env, command_log = _nightly_fixture(tmp_path, fact_enabled=False)

    completed = subprocess.run(
        [str(maintenance)], env=env, capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr
    commands = command_log.read_text(encoding="utf-8")
    for private_value in (
        "synthetic-account",
        "operator@example.test",
        str(tmp_path / "runtime.toml"),
        str(tmp_path / "runtime"),
        str(tmp_path / "facts"),
        "Private Inbox",
        "Private Archive",
        "Private Trash",
    ):
        assert private_value not in commands
    assert "mail account list --output json" in commands
    assert "mail folder list --output json" in commands
    assert "nightly-update --embed" in commands
    assert "--account" not in commands
    assert "--runtime-config" not in commands


def test_nightly_scrubs_unrelated_connector_and_hermes_environment(
    tmp_path: Path,
) -> None:
    maintenance, env, _command_log = _nightly_fixture(tmp_path, fact_enabled=False)
    hostile = {
        "HIMALAYA_CONFIG": "/hostile/himalaya.toml",
        "HIMALAYA_TEST_MODE": "1",
        "HERMES_HOME": "/hostile/hermes-home",
        "HERMES_PROFILE": "hostile-profile",
        "HERMES_MODEL": "hostile-model",
        "HERMES_PROVIDER": "hostile-provider",
        "HERMES_PLUGIN_PATH": "/hostile/plugins",
        "HERMES_TEST_MODE": "1",
    }
    env.update(hostile)

    completed = subprocess.run(
        [str(maintenance)], env=env, capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr
    connector_environment = (tmp_path / "connector-env.log").read_text(
        encoding="utf-8"
    )
    assert connector_environment == ""


def test_nightly_fails_closed_and_redacts_account_selection_mismatch(
    tmp_path: Path,
) -> None:
    maintenance, env, command_log = _nightly_fixture(tmp_path, fact_enabled=False)
    env["MAIL_ACCOUNT_JSON"] = json.dumps(
        [{"name": "different-private-account", "default": True}]
    )

    completed = subprocess.run(
        [str(maintenance)], env=env, capture_output=True, text=True, check=False
    )

    assert completed.returncode != 0
    combined_output = completed.stdout + completed.stderr
    assert "synthetic-account" not in combined_output
    assert "different-private-account" not in combined_output
    report_text = "\n".join(
        report.read_text(encoding="utf-8")
        for report in (tmp_path / "runtime/reports").glob("nightly_*.jsonl")
    )
    assert "mail_account_selection_failed" in report_text
    assert "synthetic-account" not in report_text
    assert "different-private-account" not in report_text
    assert "mail account list --output json" in command_log.read_text(
        encoding="utf-8"
    )


def test_ambient_expected_response_cannot_bypass_llm_preflight(
    tmp_path: Path,
) -> None:
    invalid_response = "ambient-invalid-response"
    maintenance, env, _command_log = _nightly_fixture(
        tmp_path, fact_enabled=False, hermes_chat_response=invalid_response
    )
    env["LLM_PREFLIGHT_EXPECTED_RESPONSE"] = invalid_response

    completed = subprocess.run(
        [str(maintenance)], env=env, capture_output=True, text=True, check=False
    )

    assert completed.returncode != 0
    output = completed.stdout + completed.stderr
    assert invalid_response not in output
    report_text = "\n".join(
        report.read_text(encoding="utf-8")
        for report in (tmp_path / "runtime/reports").glob("nightly_*.jsonl")
    )
    assert "llm_preflight_unexpected_response" in report_text
    assert invalid_response not in report_text


def test_maintenance_config_failure_cannot_use_ambient_profile_values(
    tmp_path: Path,
) -> None:
    maintenance, env, _command_log = _nightly_fixture(tmp_path, fact_enabled=False)
    marker = tmp_path / "ambient-tool-ran"
    malicious_tool = _write_executable(
        tmp_path / "malicious-tool",
        f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 0\n",
    )
    hostile_root = tmp_path / "ambient-root"
    hostile_root.mkdir(mode=0o700)
    pending = hostile_root / "pending-private-artifact"
    pending.write_text("unchanged\n", encoding="utf-8")
    before = pending.read_bytes()
    env.update(
        {
            "LOCAL_CONFIG_FAIL": "1",
            "EMAIL_MEMORY_ROOT": str(hostile_root),
            "EMAIL_MEMORY_STORE_RUNTIME_CONFIG": str(
                tmp_path / "ambient-runtime.toml"
            ),
            "EMAIL_MEMORY_ACCOUNT_NAME": "ambient-account",
            "EMAIL_MEMORY_ACCOUNT_EMAIL": "ambient@example.test",
            "EMAIL_MEMORY_INCLUDE_FOLDERS_JSON": '["ambient-folder"]',
            "EMAIL_MEMORY_EXCLUDE_FOLDERS_JSON": '["ambient-trash"]',
            "EMAIL_MEMORY_MAIL_CLIENT_EXECUTABLE": str(malicious_tool),
            "EMAIL_MEMORY_HERMES_EXECUTABLE": str(malicious_tool),
            "EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT": str(hostile_root),
            "EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER": "ambient:provider",
            "HERMES_ALERT_TARGET": "ambient-destination",
            "HIMALAYA_CONFIG": "/hostile/himalaya.toml",
            "HERMES_HOME": "/hostile/hermes-home",
            "HERMES_PROFILE": "hostile-profile",
            "HERMES_MODEL": "hostile-model",
            "HERMES_PROVIDER": "hostile-provider",
            "HERMES_PLUGIN_PATH": "/hostile/plugins",
            "HERMES_TEST_MODE": "1",
        }
    )

    completed = subprocess.run(
        [str(maintenance)], env=env, capture_output=True, text=True, check=False
    )

    assert completed.returncode == 2
    assert not marker.exists()
    assert pending.read_bytes() == before
    assert not (hostile_root / "reports").exists()
    output = completed.stdout + completed.stderr
    for private_value in (
        "ambient-account",
        "ambient@example.test",
        "ambient-folder",
        "ambient-destination",
        str(hostile_root),
    ):
        assert private_value not in output


def test_cron_config_failure_cannot_mutate_or_deliver_from_ambient_values(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "cron-scripts"
    scripts.mkdir(mode=0o700)
    launcher = scripts / "nightly_cron_launcher.sh"
    launcher.write_bytes((SCRIPTS / launcher.name).read_bytes())
    launcher.chmod(0o700)
    helper = scripts / "email_memory_environment.sh"
    helper.write_bytes((SCRIPTS / helper.name).read_bytes())
    helper.chmod(0o700)
    operational_python = _write_executable(
        tmp_path / "failing-python",
        "#!/bin/sh\nexit 42\n",
    )
    marker = tmp_path / "ambient-cron-tool-ran"
    malicious_tool = _write_executable(
        tmp_path / "malicious-cron-tool",
        f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 0\n",
    )
    hostile_root = tmp_path / "ambient-cron-root"
    pending_dir = hostile_root / "reports/nightly_alerts"
    pending_dir.mkdir(parents=True, mode=0o700)
    pending = pending_dir / "pending.jsonl"
    pending.write_text('{"private":"unchanged"}\n', encoding="utf-8")
    before = pending.read_bytes()
    env = {
        **os.environ,
        "EMAIL_MEMORY_TEST_MODE": "1",
        "EMAIL_MEMORY_OPERATIONAL_PYTHON": str(operational_python),
        "EMAIL_MEMORY_ROOT": str(hostile_root),
        "EMAIL_MEMORY_STORE_RUNTIME_CONFIG": str(tmp_path / "ambient.toml"),
        "EMAIL_MEMORY_ACCOUNT_NAME": "ambient-account",
        "EMAIL_MEMORY_ACCOUNT_EMAIL": "ambient@example.test",
        "EMAIL_MEMORY_INCLUDE_FOLDERS_JSON": '["ambient-folder"]',
        "EMAIL_MEMORY_EXCLUDE_FOLDERS_JSON": '["ambient-trash"]',
        "EMAIL_MEMORY_MAIL_CLIENT_EXECUTABLE": str(malicious_tool),
        "EMAIL_MEMORY_HERMES_EXECUTABLE": str(malicious_tool),
        "EMAIL_MEMORY_MAINTENANCE_SCRIPT": str(malicious_tool),
        "EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT": str(hostile_root),
        "EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER": "ambient:provider",
        "HERMES_ALERT_TARGET": "ambient-destination",
        "HIMALAYA_CONFIG": "/hostile/himalaya.toml",
        "HERMES_HOME": "/hostile/hermes-home",
        "HERMES_PROFILE": "hostile-profile",
        "HERMES_MODEL": "hostile-model",
        "HERMES_PROVIDER": "hostile-provider",
        "HERMES_PLUGIN_PATH": "/hostile/plugins",
        "HERMES_TEST_MODE": "1",
    }

    completed = subprocess.run(
        [str(launcher)], env=env, capture_output=True, text=True, check=False
    )

    assert completed.returncode == 2
    assert not marker.exists()
    assert pending.read_bytes() == before
    assert list(pending_dir.iterdir()) == [pending]
    output = completed.stdout + completed.stderr
    for private_value in (
        "ambient-account",
        "ambient@example.test",
        "ambient-folder",
        "ambient-destination",
        str(hostile_root),
    ):
        assert private_value not in output


def test_nightly_runs_llm_promotions_when_fact_integration_is_enabled(
    tmp_path: Path,
) -> None:
    maintenance, env, command_log = _nightly_fixture(tmp_path, fact_enabled=True)

    completed = subprocess.run(
        [str(maintenance)], env=env, capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert "run-llm-promotions" in command_log.read_text(encoding="utf-8")


@pytest.mark.parametrize("errors", [1, 3])
def test_nightly_fails_and_alerts_when_promotions_report_errors(
    tmp_path: Path, errors: int
) -> None:
    maintenance, env, command_log = _nightly_fixture(tmp_path, fact_enabled=True)
    env["PROMOTION_RESULT"] = json.dumps({"promoted": 2, "errors": errors})

    completed = subprocess.run(
        [str(maintenance)], env=env, capture_output=True, text=True, check=False
    )

    assert completed.returncode != 0
    assert "run-llm-promotions" in command_log.read_text(encoding="utf-8")
    events = [
        json.loads(line)
        for report in (tmp_path / "runtime/reports").glob("nightly_*.jsonl")
        for line in report.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        event["event_code"] == "llm_promotions_reported_errors"
        and event["severity"] == "error"
        and event["count"] == errors
        for event in events
    )
    assert any(event["event_code"] == "alert_delivered" for event in events)

from __future__ import annotations

import json
from pathlib import Path
import stat
import tomllib

import pytest

import email_memory_store.tui.private_setup as private_setup
from email_memory_store.tui.private_setup import (
    FACT_STORE_PROVIDER,
    PrivateSetupApp,
    PrivateSetupValues,
    load_private_setup,
    parse_folders,
    private_setup_paths,
    validate_private_setup,
    write_private_setup,
)
from textual.widgets import Input


def _values(**changes: str) -> PrivateSetupValues:
    values = {
        "runtime_root": "/var/lib/email-memory",
        "work_root": "/var/tmp/email-memory-work",
        "fact_store_db": "/var/lib/facts/facts.db",
        "himalaya_executable": "/bin/true",
        "hermes_executable": "/bin/true",
        "codex_executable": "/bin/true",
        "claude_executable": "/bin/true",
        "fact_store_module_root": "/opt/local-facts",
        "fact_store_provider": "",
        "account_label": "primary",
        "account_email": "person@example.test",
        "include_folders": "Inbox, Archive/Projects",
        "exclude_folders": "Junk",
        "alert_destination": "telegram",
        "credential_reference": "keyring:mailbox",
    }
    values.update(changes)
    return PrivateSetupValues(**values)


def _is_owner_only(path: Path) -> bool:
    return not (stat.S_IMODE(path.stat().st_mode) & (stat.S_IRWXG | stat.S_IRWXO))


def test_private_setup_paths_use_xdg_config_home():
    paths = private_setup_paths(environ={"XDG_CONFIG_HOME": "/tmp/config"})

    assert paths.config_dir == Path("/tmp/config/email-memory-store")
    assert paths.runtime_manifest.name == "runtime.toml"
    assert paths.private_env.name == "private.env.json"
    assert paths.policy.name == "policy.json"


def test_write_private_setup_creates_separate_owner_only_artifacts(tmp_path):
    paths = write_private_setup(_values(), config_home=tmp_path)

    assert all(_is_owner_only(path) for path in (paths.config_dir, *paths.artifacts))
    assert tomllib.loads(paths.runtime_manifest.read_text(encoding="utf-8")) == {
        "schema_version": 2,
        "storage": {
            "runtime_root": "/var/lib/email-memory",
            "main_db": "/var/lib/email-memory/email_memory.duckdb",
            "entity_db": "/var/lib/email-memory/entity_memory.duckdb",
            "vector_store": "/var/lib/email-memory/chroma",
            "work_db": "/var/tmp/email-memory-work/email_memory.work.duckdb",
            "fact_store_db": "/var/lib/facts/facts.db",
        },
        "executables": {
            "himalaya": str(Path("/bin/true").resolve(strict=True)),
            "hermes": str(Path("/bin/true").resolve(strict=True)),
            "codex": str(Path("/bin/true").resolve(strict=True)),
            "claude": str(Path("/bin/true").resolve(strict=True)),
        },
    }
    assert json.loads(paths.private_env.read_text(encoding="utf-8")) == {
        "alert_destination": "telegram",
        "credential_reference": "keyring:mailbox",
        "fact_store_module_root": "/opt/local-facts",
        "fact_store_provider": FACT_STORE_PROVIDER,
        "schema_version": 1,
    }
    assert json.loads(paths.policy.read_text(encoding="utf-8")) == {
        "account_email": "person@example.test",
        "account_label": "primary",
        "exclude_folders": ["Junk"],
        "include_folders": ["Inbox", "Archive/Projects"],
        "schema_version": 1,
    }
    assert load_private_setup(config_home=tmp_path).policy["account_label"] == "primary"


def test_write_private_setup_requires_explicit_overwrite(tmp_path):
    write_private_setup(_values(), config_home=tmp_path)

    with pytest.raises(FileExistsError, match="explicit overwrite"):
        write_private_setup(_values(account_label="replacement"), config_home=tmp_path)

    paths = write_private_setup(
        _values(account_label="replacement"), config_home=tmp_path, overwrite=True
    )
    assert json.loads(paths.policy.read_text(encoding="utf-8"))["account_label"] == "replacement"


def test_write_private_setup_persists_strict_executable_target(tmp_path):
    executable = tmp_path / "real-tool"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    linked = tmp_path / "linked-tool"
    linked.symlink_to(executable)

    paths = write_private_setup(
        _values(himalaya_executable=str(linked)), config_home=tmp_path / "config"
    )
    manifest = tomllib.loads(paths.runtime_manifest.read_text(encoding="utf-8"))

    assert manifest["executables"]["himalaya"] == str(executable.resolve(strict=True))


def test_write_private_setup_supports_validated_local_retention_policy(tmp_path):
    paths = write_private_setup(
        _values(
            retention_inbox_folder="Inbox",
            retention_department_folder="Departments",
            retention_service_folder="Services",
            retention_archive_folder="Archive",
            retention_sender_archive_rules=(
                '[{"folder":"Archive/Newsletters","emails":["sender@example.test"]}]'
            ),
            retention_classification_definitions='{"newsletter":"Recurring update"}',
        ),
        config_home=tmp_path,
    )

    retention = json.loads(paths.policy.read_text(encoding="utf-8"))["retention"]
    assert retention == {
        "inbox_folder": "Inbox",
        "department_folder": "Departments",
        "service_folder": "Services",
        "archive_folder": "Archive",
        "sender_archive_rules": [
            {"folder": "Archive/Newsletters", "emails": ["sender@example.test"]}
        ],
        "classification_definitions": {"newsletter": "Recurring update"},
    }
    assert load_private_setup(config_home=tmp_path).policy["retention"] == retention


def test_write_private_setup_supports_all_sender_archive_matchers(tmp_path):
    rules = [
        {
            "folder": "Archive/Matched",
            "emails": ["contact@example.test"],
            "domains": ["example.test"],
            "address_contains": ["alerts@"],
            "name_contains": ["Example Service"],
        }
    ]
    paths = write_private_setup(
        _values(retention_sender_archive_rules=json.dumps(rules)),
        config_home=tmp_path,
    )

    policy = json.loads(paths.policy.read_text(encoding="utf-8"))
    assert policy["retention"]["sender_archive_rules"] == rules
    assert load_private_setup(config_home=tmp_path).policy["retention"] == policy["retention"]


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (_values(runtime_root="relative-root"), "absolute local path"),
        (_values(account_label=""), "account label is required"),
        (_values(account_email="not-an-email"), "valid email address"),
        (_values(retention_sender_archive_rules="not JSON"), "valid JSON"),
        (
            _values(retention_sender_archive_rules='[{"folder":"Archive","unexpected":true}]'),
            "unsupported key",
        ),
        (
            _values(retention_sender_archive_rules='[{"folder":"Archive","domains":"example.test"}]'),
            "must be a list of non-empty strings",
        ),
        (
            _values(retention_sender_archive_rules='[{"folder":"Archive","emails":[],"domains":[]}]'),
            "at least one non-empty matcher array",
        ),
        (
            _values(retention_sender_archive_rules='[{"emails":["contact@example.test"]}]'),
            "must contain folder",
        ),
        (
            _values(retention_sender_archive_rules='[{"folder":"   ","emails":["contact@example.test"]}]'),
            "must be a non-empty string",
        ),
        (
            _values(retention_sender_archive_rules='[{"folder":"Archive","domains":["   "]}]'),
            "must be a list of non-empty strings",
        ),
        (
            _values(retention_classification_definitions='{"kind": 1}'),
            "string-to-string mapping",
        ),
        (
            _values(retention_classification_definitions='{"   ": "definition"}'),
            "string-to-string mapping",
        ),
        (
            _values(retention_classification_definitions='{"kind": "   "}'),
            "string-to-string mapping",
        ),
    ],
)
def test_private_setup_validates_required_and_portable_values(values, message):
    with pytest.raises(ValueError, match=message):
        validate_private_setup(values)


def test_parse_folders_discards_empty_values():
    assert parse_folders(" Inbox, , Archive/Projects ") == ["Inbox", "Archive/Projects"]


def test_main_launches_private_setup(monkeypatch):
    launched = False

    def launch() -> None:
        nonlocal launched
        launched = True

    monkeypatch.setattr(private_setup, "launch_private_setup", launch)
    private_setup.main()

    assert launched


def test_load_private_setup_rejects_unsupported_version_and_weak_permissions(tmp_path):
    paths = write_private_setup(_values(), config_home=tmp_path)
    paths.policy.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "account_label": "primary",
                "account_email": "person@example.test",
                "include_folders": [],
                "exclude_folders": [],
            }
        ),
        encoding="utf-8",
    )
    paths.policy.chmod(0o600)
    with pytest.raises(ValueError, match="schema_version"):
        load_private_setup(config_home=tmp_path)

    write_private_setup(_values(), config_home=tmp_path, overwrite=True)
    paths.private_env.chmod(0o644)
    with pytest.raises(PermissionError, match="accessible by group or other"):
        load_private_setup(config_home=tmp_path)


def test_load_private_setup_rejects_unknown_retention_keys(tmp_path):
    paths = write_private_setup(_values(), config_home=tmp_path)
    paths.policy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "account_label": "primary",
                "account_email": "person@example.test",
                "include_folders": [],
                "exclude_folders": [],
                "retention": {"unsupported": "value"},
            }
        ),
        encoding="utf-8",
    )
    paths.policy.chmod(0o600)

    with pytest.raises(ValueError, match="unsupported key"):
        load_private_setup(config_home=tmp_path)


@pytest.mark.asyncio
async def test_private_setup_app_starts(tmp_path):
    app = PrivateSetupApp(config_home=tmp_path)
    async with app.run_test():
        assert app.query_one("#runtime-root", Input) is not None
        assert not app.query("#fact-store-provider")


def test_private_setup_derives_the_public_fact_store_provider(tmp_path):
    paths = write_private_setup(
        _values(fact_store_provider=""), config_home=tmp_path
    )

    private_env = json.loads(paths.private_env.read_text(encoding="utf-8"))
    assert private_env["fact_store_provider"] == FACT_STORE_PROVIDER


def test_private_setup_rejects_an_arbitrary_fact_store_provider():
    provider = "private_adapter.module:Store"

    with pytest.raises(ValueError, match="fact-store provider is unsupported") as captured:
        validate_private_setup(_values(fact_store_provider=provider))
    assert provider not in str(captured.value)


def test_private_setup_rejects_an_arbitrary_alert_target_without_disclosing_it():
    target = "telegram" + ":" + "private-route"

    with pytest.raises(ValueError, match="alert destination is unsupported") as captured:
        validate_private_setup(_values(alert_destination=target))
    assert target not in str(captured.value)


def test_private_setup_supports_disabled_fact_integration(tmp_path):
    paths = write_private_setup(
        _values(fact_store_module_root="", fact_store_provider=""),
        config_home=tmp_path,
    )

    private_env = json.loads(paths.private_env.read_text(encoding="utf-8"))
    assert "fact_store_module_root" not in private_env
    assert "fact_store_provider" not in private_env


@pytest.mark.asyncio
async def test_private_setup_prefills_executables_from_path(tmp_path, monkeypatch):
    candidates = {
        "himalaya": "/opt/tools/himalaya-current",
        "hermes": "/opt/tools/hermes-current",
        "codex": "/opt/tools/codex-current",
        "claude": None,
    }
    monkeypatch.setattr(private_setup.shutil, "which", candidates.get)

    app = PrivateSetupApp(config_home=tmp_path)
    async with app.run_test():
        assert app.query_one("#himalaya-executable", Input).value == candidates["himalaya"]
        assert app.query_one("#hermes-executable", Input).value == candidates["hermes"]
        assert app.query_one("#codex-executable", Input).value == candidates["codex"]
        assert app.query_one("#claude-executable", Input).value == ""


def test_private_setup_rejects_a_non_executable_target(tmp_path):
    target = tmp_path / "not-executable"
    target.write_text("placeholder", encoding="utf-8")

    with pytest.raises(ValueError, match="regular executable"):
        validate_private_setup(_values(himalaya_executable=str(target)))


def test_private_setup_rejects_effective_database_path_collisions():
    with pytest.raises(ValueError, match="must be distinct"):
        validate_private_setup(_values(
            main_db="/srv/state/../shared.db",
            entity_db="/srv/shared.db",
        ))


def test_load_private_setup_rejects_database_path_collisions(tmp_path):
    paths = write_private_setup(_values(), config_home=tmp_path)
    manifest = tomllib.loads(paths.runtime_manifest.read_text(encoding="utf-8"))
    manifest["storage"]["entity_db"] = manifest["storage"]["main_db"]
    storage = manifest["storage"]
    executables = manifest["executables"]
    rendered = ["schema_version = 2", "", "[storage]"]
    rendered.extend(f'{key} = {json.dumps(value)}' for key, value in storage.items())
    rendered.extend(("", "[executables]"))
    rendered.extend(f'{key} = {json.dumps(value)}' for key, value in executables.items())
    paths.runtime_manifest.write_text("\n".join(rendered) + "\n", encoding="utf-8")
    paths.runtime_manifest.chmod(0o600)

    with pytest.raises(ValueError, match="must be distinct"):
        load_private_setup(config_home=tmp_path)

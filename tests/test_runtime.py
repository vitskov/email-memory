from __future__ import annotations

from pathlib import Path

import pytest

import email_memory_store.runtime as runtime
from email_memory_store.runtime import (
    RUNTIME_CONFIG_ENV,
    default_runtime_root,
    load_runtime_config,
    load_runtime_provider,
    resolve_runtime_settings,
)


def test_default_runtime_root_uses_xdg_state_home():
    assert default_runtime_root(environ={"XDG_STATE_HOME": "/tmp/state"}) == Path(
        "/tmp/state/email-memory-store"
    )


def test_runtime_settings_default_to_xdg_state_home_without_local_attachment():
    settings = resolve_runtime_settings(
        runtime_root=None,
        work_root=None,
        runtime_config=None,
        environ={"XDG_STATE_HOME": "/tmp/state"},
    )

    assert settings.runtime_root == Path("/tmp/state/email-memory-store")
    assert settings.work_root is None
    assert settings.fact_store_db is None


def test_runtime_config_from_environment_supplies_runtime_and_work_roots(tmp_path):
    config = tmp_path / "runtime.toml"
    config.write_text(
        (
            'runtime_root = "~/private-email-memory"\n'
            'work_root = "/tmp/email-memory-work"\n'
            'fact_store_db = "/tmp/fact-store.db"\n'
        ),
        encoding="utf-8",
    )

    settings = resolve_runtime_settings(
        runtime_root=None,
        work_root=None,
        runtime_config=None,
        environ={RUNTIME_CONFIG_ENV: str(config)},
    )

    assert settings.runtime_root == Path("~/private-email-memory").expanduser()
    assert settings.work_root == Path("/tmp/email-memory-work")
    assert settings.fact_store_db == Path("/tmp/fact-store.db")


def test_versioned_runtime_manifest_accepts_the_supported_schema(tmp_path):
    config = tmp_path / "runtime.toml"
    config.write_text(
        '''schema_version = 2
[storage]
runtime_root = "/configured/root"
main_db = "/configured/main.duckdb"
entity_db = "/configured/entity.duckdb"
vector_store = "/configured/chroma"
[executables]
himalaya = "/usr/local/bin/himalaya"
hermes = "/usr/local/bin/hermes"
codex = "/usr/local/bin/codex"
claude = "/usr/local/bin/claude"
''',
        encoding="utf-8",
    )

    assert load_runtime_config(config).runtime_root == Path("/configured/root")


@pytest.mark.parametrize("manifest", ["schema_version = 3\n", 'schema_version = "1"\n'])
def test_runtime_manifest_rejects_unsupported_schema_versions(tmp_path, manifest):
    config = tmp_path / "runtime.toml"
    config.write_text(manifest, encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version"):
        load_runtime_config(config)


def test_v2_runtime_manifest_supplies_exact_storage_and_executables(tmp_path):
    config = tmp_path / "runtime.toml"
    config.write_text('''schema_version = 2
[storage]
runtime_root = "/srv/email-memory"
main_db = "/data/main.duckdb"
entity_db = "/data/entity.duckdb"
vector_store = "/vectors/email"
work_db = "/work/email.duckdb"
fact_store_db = "/facts/store.db"
[executables]
himalaya = "/opt/bin/himalaya-current"
hermes = "/opt/bin/hermes-current"
codex = "/opt/bin/codex-current"
claude = "/opt/bin/claude-current"
''', encoding="utf-8")

    settings = resolve_runtime_settings(
        runtime_root=None, work_root=None, runtime_config=config, environ={},
    )

    assert settings.main_db == Path("/data/main.duckdb")
    assert settings.entity_db == Path("/data/entity.duckdb")
    assert settings.vector_store == Path("/vectors/email")
    assert settings.work_db == Path("/work/email.duckdb")
    assert settings.fact_store_db == Path("/facts/store.db")
    assert settings.mail_client_executable == Path("/opt/bin/himalaya-current")
    assert settings.executable_for_provider("codex-cli") == Path("/opt/bin/codex-current")


def test_v2_runtime_manifest_rejects_relative_paths(tmp_path):
    config = tmp_path / "runtime.toml"
    config.write_text('''schema_version = 2
[storage]
runtime_root = "/srv/email-memory"
main_db = "relative.duckdb"
entity_db = "/data/entity.duckdb"
vector_store = "/vectors/email"
''', encoding="utf-8")

    with pytest.raises(ValueError, match="absolute path"):
        load_runtime_config(config)


def test_v2_runtime_manifest_rejects_runtime_provider(tmp_path):
    config = tmp_path / "runtime.toml"
    config.write_text('''schema_version = 2
[storage]
runtime_root = "/srv/email-memory"
main_db = "/data/main.duckdb"
entity_db = "/data/entity.duckdb"
vector_store = "/vectors/email"
[runtime_provider]
name = "legacy-provider"
''', encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported field"):
        load_runtime_config(config)


def test_legacy_runtime_has_no_path_discovered_executables(tmp_path):
    config = tmp_path / "runtime.toml"
    config.write_text('schema_version = 1\nruntime_root = "/legacy"\n', encoding="utf-8")
    settings = resolve_runtime_settings(
        runtime_root=None, work_root=None, runtime_config=config, environ={},
    )

    assert settings.mail_client_executable is None
    with pytest.raises(ValueError, match="not configured"):
        settings.executable_for_provider("hermes-default")


def test_runtime_manifest_rejects_unknown_top_level_fields(tmp_path):
    config = tmp_path / "runtime.toml"
    config.write_text('schema_version = 1\nprivate_detail = "not allowed"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported field"):
        load_runtime_config(config)


def test_cli_runtime_paths_override_runtime_config(tmp_path):
    config = tmp_path / "runtime.toml"
    config.write_text(
        (
            'runtime_root = "/configured/root"\n'
            'work_root = "/configured/work"\n'
            'fact_store_db = "/configured/facts.db"\n'
        ),
        encoding="utf-8",
    )

    settings = resolve_runtime_settings(
        runtime_root="/cli/root",
        work_root="/cli/work",
        runtime_config=config,
        fact_store_db="/cli/facts.db",
        environ={},
    )

    assert settings.runtime_root == Path("/cli/root")
    assert settings.work_root == Path("/cli/work")
    assert settings.fact_store_db == Path("/cli/facts.db")


def test_explicit_path_objects_are_accepted_for_runtime_settings(tmp_path):
    settings = resolve_runtime_settings(
        runtime_root=tmp_path / "runtime",
        work_root=tmp_path / "work",
        runtime_config=None,
        fact_store_db=tmp_path / "facts.db",
        environ={},
    )

    assert settings.runtime_root == tmp_path / "runtime"
    assert settings.work_root == tmp_path / "work"
    assert settings.fact_store_db == tmp_path / "facts.db"


def test_named_runtime_provider_supplies_local_settings(tmp_path, monkeypatch):
    config = tmp_path / "runtime.toml"
    config.write_text('[runtime_provider]\nname = "local"\n', encoding="utf-8")

    class EntryPoint:
        def load(self):
            return lambda: {
                "runtime_root": "/provider/root",
                "work_root": "/provider/work",
                "fact_store_db": "/provider/facts.db",
            }

    class EntryPoints:
        def select(self, *, group, name):
            assert group == "email_memory_store.runtime_providers"
            return [EntryPoint()] if name == "local" else []

    monkeypatch.setattr(runtime.metadata, "entry_points", lambda: EntryPoints())
    settings = resolve_runtime_settings(
        runtime_root=None,
        work_root=None,
        runtime_config=config,
        environ={},
    )

    assert settings.runtime_root == Path("/provider/root")
    assert settings.work_root == Path("/provider/work")
    assert settings.fact_store_db == Path("/provider/facts.db")


def test_unknown_runtime_provider_fails_closed(monkeypatch):
    class EntryPoints:
        def select(self, *, group, name):
            return []

    monkeypatch.setattr(runtime.metadata, "entry_points", lambda: EntryPoints())

    with pytest.raises(ValueError, match="was not found"):
        load_runtime_provider("missing")


def test_runtime_rejects_colliding_database_paths(tmp_path):
    config = tmp_path / "runtime.toml"
    config.write_text('''schema_version = 2
[storage]
runtime_root = "/runtime"
main_db = "/data/shared.duckdb"
entity_db = "/data/nested/../shared.duckdb"
vector_store = "/data/vectors"
''', encoding="utf-8")

    with pytest.raises(ValueError, match="must be distinct"):
        resolve_runtime_settings(
            runtime_root=None, work_root=None, runtime_config=config, environ={},
        )


def test_runtime_provider_rejects_relative_storage_paths(tmp_path, monkeypatch):
    config = tmp_path / "runtime.toml"
    config.write_text('[runtime_provider]\nname = "local"\n', encoding="utf-8")

    class EntryPoint:
        def load(self):
            return lambda: {"runtime_root": "relative/runtime"}

    class EntryPoints:
        def select(self, *, group, name):
            return [EntryPoint()]

    monkeypatch.setattr(runtime.metadata, "entry_points", lambda: EntryPoints())
    with pytest.raises(ValueError, match="absolute path"):
        resolve_runtime_settings(
            runtime_root=None, work_root=None, runtime_config=config, environ={},
        )


def test_runtime_provider_settings_object_rejects_relative_storage(tmp_path, monkeypatch):
    config = tmp_path / "runtime.toml"
    config.write_text('[runtime_provider]\nname = "local"\n', encoding="utf-8")

    class EntryPoint:
        def load(self):
            return lambda: runtime.RuntimeSettings(runtime_root=Path("relative/runtime"))

    class EntryPoints:
        def select(self, *, group, name):
            return [EntryPoint()]

    monkeypatch.setattr(runtime.metadata, "entry_points", lambda: EntryPoints())
    with pytest.raises(ValueError, match="relative runtime_root"):
        resolve_runtime_settings(
            runtime_root=None, work_root=None, runtime_config=config, environ={},
        )


def test_runtime_provider_settings_object_rejects_relative_executable(tmp_path, monkeypatch):
    config = tmp_path / "runtime.toml"
    config.write_text('[runtime_provider]\nname = "local"\n', encoding="utf-8")

    class EntryPoint:
        def load(self):
            return lambda: runtime.RuntimeSettings(
                runtime_root=Path("/runtime"),
                codex_executable=Path("relative/codex"),
            )

    class EntryPoints:
        def select(self, *, group, name):
            return [EntryPoint()]

    monkeypatch.setattr(runtime.metadata, "entry_points", lambda: EntryPoints())
    with pytest.raises(ValueError, match="relative codex_executable"):
        resolve_runtime_settings(
            runtime_root=None, work_root=None, runtime_config=config, environ={},
        )

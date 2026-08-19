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
        'schema_version = 1\nruntime_root = "/configured/root"\n',
        encoding="utf-8",
    )

    assert load_runtime_config(config).runtime_root == Path("/configured/root")


@pytest.mark.parametrize("manifest", ["schema_version = 2\n", 'schema_version = "1"\n'])
def test_runtime_manifest_rejects_unsupported_schema_versions(tmp_path, manifest):
    config = tmp_path / "runtime.toml"
    config.write_text(manifest, encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version 1"):
        load_runtime_config(config)


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

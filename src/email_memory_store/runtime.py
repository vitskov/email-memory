"""Resolve the local runtime attachment for the public command-line interface."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
import os
from pathlib import Path
import tomllib
from typing import Mapping


RUNTIME_CONFIG_ENV = "EMAIL_MEMORY_STORE_RUNTIME_CONFIG"
RUNTIME_PROVIDER_ENTRY_POINT_GROUP = "email_memory_store.runtime_providers"
RUNTIME_MANIFEST_SCHEMA_VERSION = 1
_RUNTIME_MANIFEST_FIELDS = {
    "schema_version",
    "runtime_root",
    "work_root",
    "fact_store_db",
    "runtime_provider",
}


@dataclass(frozen=True)
class RuntimeSettings:
    """Filesystem locations provided by a local runtime attachment."""

    runtime_root: Path
    work_root: Path | None = None
    fact_store_db: Path | None = None


@dataclass(frozen=True)
class RuntimeConfig:
    """Generic fields from the local runtime manifest."""

    runtime_root: Path | None = None
    work_root: Path | None = None
    fact_store_db: Path | None = None
    provider_name: str | None = None


def default_runtime_root(*, environ: Mapping[str, str] | None = None) -> Path:
    """Return the XDG state location used when no local attachment is configured."""
    env = os.environ if environ is None else environ
    state_home = env.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "email-memory-store"


def _path(value: object, *, field: str, config_path: Path | None = None) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        location = f" in {config_path}" if config_path else ""
        raise ValueError(f"{field}{location} must be a non-empty path")
    return Path(value).expanduser()


def load_runtime_config(path: str | Path) -> RuntimeConfig:
    """Load the generic runtime fields from a local TOML manifest.

    The file is deliberately outside the package contract: it is a local
    deployment attachment and may contain paths that must not be published.
    Unversioned manifests remain supported for existing local deployments;
    bootstrap-generated manifests declare ``schema_version = 1``.
    """
    config_path = Path(path).expanduser()
    with config_path.open("rb") as config_file:
        raw = tomllib.load(config_file)

    if not isinstance(raw, dict):
        raise ValueError(f"runtime config {config_path} must contain a TOML table")
    unknown_fields = set(raw) - _RUNTIME_MANIFEST_FIELDS
    if unknown_fields:
        raise ValueError(
            f"runtime config {config_path} has unsupported field(s): "
            f"{', '.join(sorted(map(str, unknown_fields)))}"
        )
    schema_version = raw.get("schema_version")
    if schema_version is not None and (
        type(schema_version) is not int
        or schema_version != RUNTIME_MANIFEST_SCHEMA_VERSION
    ):
        raise ValueError(
            f"runtime config {config_path} must use schema_version "
            f"{RUNTIME_MANIFEST_SCHEMA_VERSION}"
        )

    runtime_root_value = raw.get("runtime_root")
    runtime_root = (
        _path(runtime_root_value, field="runtime_root", config_path=config_path)
        if runtime_root_value is not None
        else None
    )
    work_root_value = raw.get("work_root")
    work_root = (
        _path(work_root_value, field="work_root", config_path=config_path)
        if work_root_value is not None
        else None
    )
    fact_store_db_value = raw.get("fact_store_db")
    fact_store_db = (
        _path(fact_store_db_value, field="fact_store_db", config_path=config_path)
        if fact_store_db_value is not None
        else None
    )
    provider = raw.get("runtime_provider")
    if provider is None:
        provider_name = None
    elif not isinstance(provider, dict):
        raise ValueError(f"runtime_provider in {config_path} must be a TOML table")
    else:
        provider_name_value = provider.get("name")
        if not isinstance(provider_name_value, str) or not provider_name_value:
            raise ValueError(f"runtime_provider.name in {config_path} must be a non-empty string")
        provider_name = provider_name_value

    return RuntimeConfig(
        runtime_root=runtime_root,
        work_root=work_root,
        fact_store_db=fact_store_db,
        provider_name=provider_name,
    )


def _provider_settings(value: object, *, provider_name: str) -> RuntimeSettings:
    if isinstance(value, RuntimeSettings):
        return value
    if not isinstance(value, Mapping):
        raise ValueError(
            f"runtime provider {provider_name!r} must return RuntimeSettings or a mapping"
        )

    allowed_keys = {"runtime_root", "work_root", "fact_store_db"}
    unknown_keys = set(value) - allowed_keys
    if unknown_keys:
        raise ValueError(
            f"runtime provider {provider_name!r} returned unsupported fields: "
            f"{', '.join(sorted(map(str, unknown_keys)))}"
        )
    if "runtime_root" not in value:
        raise ValueError(f"runtime provider {provider_name!r} must provide runtime_root")

    runtime_root = _path(value["runtime_root"], field="runtime_root")
    work_root_value = value.get("work_root")
    work_root = _path(work_root_value, field="work_root") if work_root_value is not None else None
    fact_store_db_value = value.get("fact_store_db")
    fact_store_db = (
        _path(fact_store_db_value, field="fact_store_db")
        if fact_store_db_value is not None
        else None
    )
    return RuntimeSettings(
        runtime_root=runtime_root,
        work_root=work_root,
        fact_store_db=fact_store_db,
    )


def load_runtime_provider(name: str) -> RuntimeSettings:
    """Load the explicitly named local runtime provider entry point.

    Providers are never discovered implicitly. A local provider exposes a
    zero-argument ``load_runtime_settings`` callable through the
    ``email_memory_store.runtime_providers`` entry-point group.
    """
    entry_points = metadata.entry_points()
    matches = list(entry_points.select(group=RUNTIME_PROVIDER_ENTRY_POINT_GROUP, name=name))
    if len(matches) != 1:
        raise ValueError(f"runtime provider {name!r} was not found")

    provider = matches[0].load()
    if not callable(provider):
        raise ValueError(f"runtime provider {name!r} is not callable")
    return _provider_settings(provider(), provider_name=name)


def resolve_runtime_settings(
    *,
    runtime_root: str | Path | None,
    work_root: str | Path | None,
    runtime_config: str | Path | None,
    fact_store_db: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RuntimeSettings:
    """Resolve CLI, local-manifest, and generic default runtime locations.

    Explicit CLI paths take precedence over the corresponding manifest value.
    The manifest path may be supplied by ``--runtime-config`` or the
    ``EMAIL_MEMORY_STORE_RUNTIME_CONFIG`` environment variable.
    """
    env = os.environ if environ is None else environ
    selected_config = runtime_config if runtime_config is not None else env.get(RUNTIME_CONFIG_ENV)
    configured = load_runtime_config(selected_config) if selected_config else None
    provided = load_runtime_provider(configured.provider_name) if configured and configured.provider_name else None

    resolved_runtime_root = (
        _path(runtime_root, field="runtime_root")
        if runtime_root is not None
        else configured.runtime_root if configured and configured.runtime_root else provided.runtime_root if provided else default_runtime_root(environ=env)
    )
    resolved_work_root = (
        _path(work_root, field="work_root")
        if work_root is not None
        else configured.work_root if configured and configured.work_root else provided.work_root if provided else None
    )
    resolved_fact_store_db = (
        _path(fact_store_db, field="fact_store_db")
        if fact_store_db is not None
        else configured.fact_store_db
        if configured and configured.fact_store_db
        else provided.fact_store_db
        if provided
        else None
    )
    return RuntimeSettings(
        runtime_root=resolved_runtime_root,
        work_root=resolved_work_root,
        fact_store_db=resolved_fact_store_db,
    )

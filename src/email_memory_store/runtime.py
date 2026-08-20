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
RUNTIME_MANIFEST_SCHEMA_VERSION = 2
LEGACY_RUNTIME_MANIFEST_SCHEMA_VERSION = 1
_LEGACY_RUNTIME_MANIFEST_FIELDS = {
    "schema_version",
    "runtime_root",
    "work_root",
    "fact_store_db",
    "runtime_provider",
}
_RUNTIME_MANIFEST_FIELDS = {"schema_version", "storage", "executables"}
_STORAGE_FIELDS = {
    "runtime_root", "main_db", "entity_db", "vector_store", "work_db", "fact_store_db",
}
_REQUIRED_STORAGE_FIELDS = {"runtime_root", "main_db", "entity_db", "vector_store"}
_EXECUTABLE_FIELDS = {"himalaya", "hermes", "codex", "claude"}


@dataclass(frozen=True)
class RuntimeSettings:
    """Filesystem locations provided by a local runtime attachment."""

    runtime_root: Path
    work_root: Path | None = None
    fact_store_db: Path | None = None
    main_db: Path | None = None
    entity_db: Path | None = None
    vector_store: Path | None = None
    work_db: Path | None = None
    mail_client_executable: Path | None = None
    hermes_executable: Path | None = None
    codex_executable: Path | None = None
    claude_executable: Path | None = None

    def executable_for_provider(self, provider_name: str) -> Path:
        """Return the selected executable for a supported LLM provider."""
        executables = {
            "hermes-default": self.hermes_executable,
            "codex-cli": self.codex_executable,
            "claude-code-cli": self.claude_executable,
        }
        if provider_name not in executables:
            raise ValueError("the selected LLM provider is unsupported")
        executable = executables[provider_name]
        if executable is None:
            raise ValueError("the selected LLM provider executable is not configured")
        return executable

    def __post_init__(self) -> None:
        root = self.runtime_root
        if self.main_db is None:
            object.__setattr__(self, "main_db", root / "email_memory.duckdb")
        if self.entity_db is None:
            object.__setattr__(self, "entity_db", root / "entity_memory.duckdb")
        if self.vector_store is None:
            object.__setattr__(self, "vector_store", root / "chroma")
        if self.work_db is None and self.work_root is not None:
            object.__setattr__(self, "work_db", self.work_root / "email_memory.work.duckdb")


@dataclass(frozen=True)
class RuntimeConfig:
    """Generic fields from the local runtime manifest."""

    runtime_root: Path | None = None
    work_root: Path | None = None
    fact_store_db: Path | None = None
    main_db: Path | None = None
    entity_db: Path | None = None
    vector_store: Path | None = None
    work_db: Path | None = None
    provider_name: str | None = None
    mail_client_executable: Path | None = None
    hermes_executable: Path | None = None
    codex_executable: Path | None = None
    claude_executable: Path | None = None


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


def _absolute_path(value: object, *, field: str, config_path: Path | None = None) -> Path:
    path = _path(value, field=field, config_path=config_path)
    if not path.is_absolute():
        location = f" in {config_path}" if config_path else ""
        raise ValueError(f"{field}{location} must be an absolute path")
    return path


def load_runtime_config(path: str | Path) -> RuntimeConfig:
    """Load the generic runtime fields from a local TOML manifest.

    The file is deliberately outside the package contract: it is a local
    deployment attachment and may contain paths that must not be published.
    Unversioned manifests remain supported for existing local deployments;
    bootstrap-generated manifests declare ``schema_version = 2``.
    """
    config_path = Path(path).expanduser()
    with config_path.open("rb") as config_file:
        raw = tomllib.load(config_file)

    if not isinstance(raw, dict):
        raise ValueError(f"runtime config {config_path} must contain a TOML table")
    schema_version = raw.get("schema_version")
    if schema_version is None or schema_version == LEGACY_RUNTIME_MANIFEST_SCHEMA_VERSION:
        allowed_fields = _LEGACY_RUNTIME_MANIFEST_FIELDS
        storage = raw
        executables: Mapping[str, object] = {}
    elif schema_version == RUNTIME_MANIFEST_SCHEMA_VERSION:
        allowed_fields = _RUNTIME_MANIFEST_FIELDS
        storage_value = raw.get("storage", {})
        executables_value = raw.get("executables", {})
        if not isinstance(storage_value, dict):
            raise ValueError(f"storage in {config_path} must be a TOML table")
        if not isinstance(executables_value, dict):
            raise ValueError(f"executables in {config_path} must be a TOML table")
        unknown_paths = set(storage_value) - _STORAGE_FIELDS
        unknown_executables = set(executables_value) - _EXECUTABLE_FIELDS
        if unknown_paths or unknown_executables:
            unknown = sorted(map(str, unknown_paths | unknown_executables))
            raise ValueError(
                f"runtime config {config_path} has unsupported field(s): {', '.join(unknown)}"
            )
        missing_storage = _REQUIRED_STORAGE_FIELDS - set(storage_value)
        if missing_storage:
            raise ValueError(
                f"runtime config {config_path} is missing required storage field(s): "
                f"{', '.join(sorted(missing_storage))}"
            )
        storage = storage_value
        executables = executables_value
    else:
        raise ValueError(
            f"runtime config {config_path} must use schema_version "
            f"{LEGACY_RUNTIME_MANIFEST_SCHEMA_VERSION} or {RUNTIME_MANIFEST_SCHEMA_VERSION}"
        )

    unknown_fields = set(raw) - allowed_fields
    if unknown_fields:
        raise ValueError(
            f"runtime config {config_path} has unsupported field(s): "
            f"{', '.join(sorted(map(str, unknown_fields)))}"
        )
    if schema_version is not None and type(schema_version) is not int:
        raise ValueError(
            f"runtime config {config_path} schema_version must be an integer"
        )

    path_loader = _absolute_path if schema_version == RUNTIME_MANIFEST_SCHEMA_VERSION else _path
    runtime_root_value = storage.get("runtime_root")
    runtime_root = (
        path_loader(runtime_root_value, field="runtime_root", config_path=config_path)
        if runtime_root_value is not None
        else None
    )
    work_root_value = storage.get("work_root")
    work_root = (
        path_loader(work_root_value, field="work_root", config_path=config_path)
        if work_root_value is not None
        else None
    )
    fact_store_db_value = storage.get("fact_store_db")
    fact_store_db = (
        path_loader(fact_store_db_value, field="fact_store_db", config_path=config_path)
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
        main_db=(
            _absolute_path(storage["main_db"], field="storage.main_db", config_path=config_path)
            if "main_db" in storage else None
        ),
        entity_db=(
            _absolute_path(storage["entity_db"], field="storage.entity_db", config_path=config_path)
            if "entity_db" in storage else None
        ),
        vector_store=(
            _absolute_path(storage["vector_store"], field="storage.vector_store", config_path=config_path)
            if "vector_store" in storage else None
        ),
        work_db=(
            _absolute_path(storage["work_db"], field="storage.work_db", config_path=config_path)
            if "work_db" in storage else None
        ),
        mail_client_executable=(
            _absolute_path(executables["himalaya"], field="executables.himalaya", config_path=config_path)
            if "himalaya" in executables else None
        ),
        hermes_executable=(
            _absolute_path(executables["hermes"], field="executables.hermes", config_path=config_path)
            if "hermes" in executables else None
        ),
        codex_executable=(
            _absolute_path(executables["codex"], field="executables.codex", config_path=config_path)
            if "codex" in executables else None
        ),
        claude_executable=(
            _absolute_path(executables["claude"], field="executables.claude", config_path=config_path)
            if "claude" in executables else None
        ),
    )


def _validate_provider_settings(
    settings: RuntimeSettings, *, provider_name: str,
) -> RuntimeSettings:
    for field in (
        "runtime_root", "work_root", "fact_store_db", "main_db", "entity_db",
        "vector_store", "work_db", "mail_client_executable", "hermes_executable",
        "codex_executable", "claude_executable",
    ):
        value = getattr(settings, field)
        if value is None:
            continue
        try:
            _absolute_path(value, field=field)
        except ValueError as error:
            raise ValueError(
                f"runtime provider {provider_name!r} returned a relative {field} path"
            ) from error
    return settings


def _provider_settings(value: object, *, provider_name: str) -> RuntimeSettings:
    if isinstance(value, RuntimeSettings):
        return _validate_provider_settings(value, provider_name=provider_name)
    if not isinstance(value, Mapping):
        raise ValueError(
            f"runtime provider {provider_name!r} must return RuntimeSettings or a mapping"
        )

    allowed_keys = {
        "runtime_root", "work_root", "fact_store_db", "main_db", "entity_db", "vector_store", "work_db",
        "mail_client_executable", "hermes_executable", "codex_executable", "claude_executable",
    }
    unknown_keys = set(value) - allowed_keys
    if unknown_keys:
        raise ValueError(
            f"runtime provider {provider_name!r} returned unsupported fields: "
            f"{', '.join(sorted(map(str, unknown_keys)))}"
        )
    if "runtime_root" not in value:
        raise ValueError(f"runtime provider {provider_name!r} must provide runtime_root")

    runtime_root = _absolute_path(value["runtime_root"], field="runtime_root")
    work_root_value = value.get("work_root")
    work_root = _absolute_path(work_root_value, field="work_root") if work_root_value is not None else None
    fact_store_db_value = value.get("fact_store_db")
    fact_store_db = (
        _absolute_path(fact_store_db_value, field="fact_store_db")
        if fact_store_db_value is not None
        else None
    )
    settings = RuntimeSettings(
        runtime_root=runtime_root,
        work_root=work_root,
        fact_store_db=fact_store_db,
        main_db=_absolute_path(value["main_db"], field="main_db") if value.get("main_db") else None,
        entity_db=_absolute_path(value["entity_db"], field="entity_db") if value.get("entity_db") else None,
        vector_store=_absolute_path(value["vector_store"], field="vector_store") if value.get("vector_store") else None,
        work_db=_absolute_path(value["work_db"], field="work_db") if value.get("work_db") else None,
        mail_client_executable=_absolute_path(value["mail_client_executable"], field="mail_client_executable") if value.get("mail_client_executable") else None,
        hermes_executable=_absolute_path(value["hermes_executable"], field="hermes_executable") if value.get("hermes_executable") else None,
        codex_executable=_absolute_path(value["codex_executable"], field="codex_executable") if value.get("codex_executable") else None,
        claude_executable=_absolute_path(value["claude_executable"], field="claude_executable") if value.get("claude_executable") else None,
    )
    return _validate_provider_settings(settings, provider_name=provider_name)


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
    explicit_root = _path(runtime_root, field="runtime_root") if runtime_root is not None else None
    resolved_main_db = (
        explicit_root / "email_memory.duckdb" if explicit_root is not None
        else configured.main_db if configured and configured.main_db
        else provided.main_db if provided else resolved_runtime_root / "email_memory.duckdb"
    )
    resolved_entity_db = (
        explicit_root / "entity_memory.duckdb" if explicit_root is not None
        else configured.entity_db if configured and configured.entity_db
        else provided.entity_db if provided else resolved_runtime_root / "entity_memory.duckdb"
    )
    resolved_vector_store = (
        explicit_root / "chroma" if explicit_root is not None
        else configured.vector_store if configured and configured.vector_store
        else provided.vector_store if provided else resolved_runtime_root / "chroma"
    )
    resolved_work_db = (
        resolved_work_root / "email_memory.work.duckdb" if work_root is not None and resolved_work_root
        else configured.work_db if configured and configured.work_db
        else provided.work_db if provided else None
    )
    database_paths = {
        "main_db": resolved_main_db,
        "entity_db": resolved_entity_db,
        "work_db": resolved_work_db,
        "fact_store_db": resolved_fact_store_db,
    }
    path_owners: dict[Path, str] = {}
    for field, path in database_paths.items():
        if path is None:
            continue
        normalized = path.resolve(strict=False)
        if normalized in path_owners:
            raise ValueError(
                f"runtime database paths for {path_owners[normalized]} and {field} must be distinct"
            )
        path_owners[normalized] = field

    return RuntimeSettings(
        runtime_root=resolved_runtime_root,
        work_root=resolved_work_root,
        fact_store_db=resolved_fact_store_db,
        main_db=resolved_main_db,
        entity_db=resolved_entity_db,
        vector_store=resolved_vector_store,
        work_db=resolved_work_db,
        mail_client_executable=(configured.mail_client_executable if configured and configured.mail_client_executable else provided.mail_client_executable if provided else None),
        hermes_executable=(configured.hermes_executable if configured and configured.hermes_executable else provided.hermes_executable if provided else None),
        codex_executable=(configured.codex_executable if configured and configured.codex_executable else provided.codex_executable if provided else None),
        claude_executable=(configured.claude_executable if configured and configured.claude_executable else provided.claude_executable if provided else None),
    )

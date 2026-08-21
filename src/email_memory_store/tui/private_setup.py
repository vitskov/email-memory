"""Local-only bootstrap wizard for an installed email-memory deployment.

The public package owns the format and secure creation of local configuration,
but never ships deployment values.  This module intentionally has no logging:
its inputs can identify a mailbox or notification destination.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import tomllib
from typing import Any, Mapping

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button, Checkbox, Footer, Input, Label, Static

from ..runtime import RUNTIME_MANIFEST_SCHEMA_VERSION


CONFIG_DIRECTORY_NAME = "email-memory-store"
RUNTIME_MANIFEST_NAME = "runtime.toml"
PRIVATE_ENV_NAME = "private.env.json"
POLICY_NAME = "policy.json"
PRIVATE_SETUP_SCHEMA_VERSION = 1
FACT_STORE_PROVIDER = "email_memory_store.integrations.hermes_fact_store:MemoryStore"
SUPPORTED_ALERT_TARGETS = frozenset({"discord", "slack", "telegram"})


@dataclass(frozen=True)
class PrivateSetupPaths:
    """Names of the private deployment artifacts kept outside the source tree."""

    config_dir: Path
    runtime_manifest: Path
    private_env: Path
    policy: Path

    @property
    def artifacts(self) -> tuple[Path, Path, Path]:
        return (self.runtime_manifest, self.private_env, self.policy)


@dataclass(frozen=True)
class PrivateSetupValues:
    """Values collected locally by the bootstrap wizard."""

    runtime_root: str
    work_root: str = ""
    fact_store_db: str = ""
    main_db: str = ""
    entity_db: str = ""
    vector_store: str = ""
    work_db: str = ""
    himalaya_executable: str = ""
    hermes_executable: str = ""
    codex_executable: str = ""
    claude_executable: str = ""
    fact_store_module_root: str = ""
    fact_store_provider: str = ""
    account_label: str = ""
    account_email: str = ""
    include_folders: str = ""
    exclude_folders: str = ""
    retention_inbox_folder: str = ""
    retention_department_folder: str = ""
    retention_service_folder: str = ""
    retention_archive_folder: str = ""
    retention_sender_archive_rules: str = ""
    retention_classification_definitions: str = ""
    alert_destination: str = ""
    credential_reference: str = ""


@dataclass(frozen=True)
class PrivateSetupBundle:
    """Validated contents of the local deployment attachment."""

    runtime: Mapping[str, object]
    private_env: Mapping[str, object]
    policy: Mapping[str, object]


def private_setup_paths(
    *,
    config_home: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> PrivateSetupPaths:
    """Return the XDG configuration locations without creating them."""
    env = os.environ if environ is None else environ
    if config_home is None:
        configured_home = env.get("XDG_CONFIG_HOME")
        base = Path(configured_home).expanduser() if configured_home else Path.home() / ".config"
    else:
        base = Path(config_home).expanduser()
    config_dir = base / CONFIG_DIRECTORY_NAME
    return PrivateSetupPaths(
        config_dir=config_dir,
        runtime_manifest=config_dir / RUNTIME_MANIFEST_NAME,
        private_env=config_dir / PRIVATE_ENV_NAME,
        policy=config_dir / POLICY_NAME,
    )


def _optional_path(value: str, *, field: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{field} must be an absolute local path")
    return str(path)


def _optional_text(value: str, *, field: str) -> str | None:
    text = value.strip()
    if "\x00" in text:
        raise ValueError(f"{field} cannot contain a null byte")
    return text or None


def _optional_executable(value: str, *, field: str) -> str | None:
    rendered = _optional_path(value, field=field)
    if rendered is None:
        return None
    try:
        path = Path(rendered).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(f"{field} must be a regular executable file") from None
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"{field} must be a regular executable file")
    return str(path)


def parse_folders(value: str) -> list[str]:
    """Parse a comma-separated selection without exposing it outside the caller."""
    folders = [item.strip() for item in value.split(",") if item.strip()]
    if any("\x00" in folder for folder in folders):
        raise ValueError("folder selection cannot contain a null byte")
    return folders


def _optional_json(value: str, *, field: str) -> object | None:
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"{field} must be valid JSON") from error


def _retention_from_values(values: PrivateSetupValues) -> dict[str, object] | None:
    retention: dict[str, object] = {}
    for values_field, policy_key in (
        ("retention_inbox_folder", "inbox_folder"),
        ("retention_department_folder", "department_folder"),
        ("retention_service_folder", "service_folder"),
        ("retention_archive_folder", "archive_folder"),
    ):
        rendered = _optional_text(getattr(values, values_field), field=policy_key.replace("_", " "))
        if rendered is not None:
            retention[policy_key] = rendered

    rules = _optional_json(values.retention_sender_archive_rules, field="sender archive rules")
    if rules is not None:
        retention["sender_archive_rules"] = rules
    definitions = _optional_json(
        values.retention_classification_definitions,
        field="classification definitions",
    )
    if definitions is not None:
        retention["classification_definitions"] = definitions
    _validate_retention(retention, artifact="retention")
    return retention or None


def _effective_runtime_storage(values: PrivateSetupValues) -> dict[str, str]:
    """Render effective storage values without normalizing their persisted spelling."""
    runtime_root = Path(_optional_path(values.runtime_root, field="runtime root") or "")
    storage = {
        "runtime_root": str(runtime_root),
        "main_db": _optional_path(values.main_db, field="main database") or str(runtime_root / "email_memory.duckdb"),
        "entity_db": _optional_path(values.entity_db, field="entity database") or str(runtime_root / "entity_memory.duckdb"),
        "vector_store": _optional_path(values.vector_store, field="vector store") or str(runtime_root / "chroma"),
    }
    work_db = _optional_path(values.work_db, field="work database")
    if work_db is None:
        work_root = _optional_path(values.work_root, field="work root")
        work_db = str(Path(work_root) / "email_memory.work.duckdb") if work_root else None
    if work_db is not None:
        storage["work_db"] = work_db
    fact_store_db = _optional_path(values.fact_store_db, field="fact-store database")
    if fact_store_db is not None:
        storage["fact_store_db"] = fact_store_db
    return storage


def _validate_distinct_database_paths(storage: Mapping[str, str]) -> None:
    owners: dict[Path, str] = {}
    for field in ("main_db", "entity_db", "work_db", "fact_store_db"):
        value = storage.get(field)
        if value is None:
            continue
        normalized = Path(value).resolve(strict=False)
        if normalized in owners:
            raise ValueError(
                f"runtime database paths for {owners[normalized]} and {field} must be distinct"
            )
        owners[normalized] = field


def validate_private_setup(values: PrivateSetupValues) -> None:
    """Validate portable local configuration values before writing artifacts."""
    if _optional_path(values.runtime_root, field="runtime root") is None:
        raise ValueError("runtime root is required")
    if _optional_text(values.account_label, field="account label") is None:
        raise ValueError("account label is required")
    email = _optional_text(values.account_email, field="account email")
    if email is None or email.count("@") != 1 or email.startswith("@") or email.endswith("@"):
        raise ValueError("account email must be a valid email address")

    _optional_path(values.work_root, field="work root")
    _optional_path(values.fact_store_db, field="fact-store database")
    for field, label in (
        ("main_db", "main database"),
        ("entity_db", "entity database"),
        ("vector_store", "vector store"),
        ("work_db", "work database"),
    ):
        _optional_path(getattr(values, field), field=label)
    for field, label in (
        ("himalaya_executable", "Himalaya executable"),
        ("hermes_executable", "Hermes executable"),
        ("codex_executable", "Codex executable"),
        ("claude_executable", "Claude executable"),
    ):
        _optional_executable(getattr(values, field), field=label)
    fact_store_root = _optional_path(
        values.fact_store_module_root, field="fact-store module root"
    )
    fact_store_provider = _optional_text(
        values.fact_store_provider, field="fact-store provider"
    )
    if fact_store_root is None and fact_store_provider is not None:
        raise ValueError(
            "fact-store root and provider must be configured together"
        )
    if fact_store_provider is not None and fact_store_provider != FACT_STORE_PROVIDER:
        raise ValueError("fact-store provider is unsupported")
    alert_destination = _optional_text(
        values.alert_destination, field="alert destination"
    )
    if (
        alert_destination is not None
        and alert_destination not in SUPPORTED_ALERT_TARGETS
    ):
        raise ValueError("alert destination is unsupported")
    _optional_text(values.credential_reference, field="credential reference")
    parse_folders(values.include_folders)
    parse_folders(values.exclude_folders)
    _retention_from_values(values)
    _validate_distinct_database_paths(_effective_runtime_storage(values))


def render_runtime_manifest(values: PrivateSetupValues) -> str:
    """Render the public runtime contract without private policy values."""
    validate_private_setup(values)
    storage = _effective_runtime_storage(values)

    lines = [f"schema_version = {RUNTIME_MANIFEST_SCHEMA_VERSION}", "", "[storage]"]
    lines.extend(f"{key} = {json.dumps(value)}" for key, value in storage.items())
    lines.extend(("", "[executables]"))
    for key, field, label in (
        ("himalaya", "himalaya_executable", "Himalaya executable"),
        ("hermes", "hermes_executable", "Hermes executable"),
        ("codex", "codex_executable", "Codex executable"),
        ("claude", "claude_executable", "Claude executable"),
    ):
        executable = _optional_executable(getattr(values, field), field=label)
        if executable is not None:
            lines.append(f"{key} = {json.dumps(executable)}")
    return "\n".join(lines) + "\n"


def render_private_env(values: PrivateSetupValues) -> str:
    """Render deployment references that should not be passed through the core."""
    validate_private_setup(values)
    payload: dict[str, object] = {"schema_version": PRIVATE_SETUP_SCHEMA_VERSION}
    for field in ("alert_destination", "credential_reference"):
        rendered = _optional_text(getattr(values, field), field=field.replace("_", " "))
        if rendered is not None:
            payload[field] = rendered
    module_root = _optional_path(values.fact_store_module_root, field="fact-store module root")
    if module_root is not None:
        payload["fact_store_module_root"] = module_root
        payload["fact_store_provider"] = FACT_STORE_PROVIDER
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def render_policy(values: PrivateSetupValues) -> str:
    """Render account and folder policy as a local-only document."""
    validate_private_setup(values)
    policy: dict[str, object] = {
        "account_email": _optional_text(values.account_email, field="account email"),
        "account_label": _optional_text(values.account_label, field="account label"),
        "exclude_folders": parse_folders(values.exclude_folders),
        "include_folders": parse_folders(values.include_folders),
        "schema_version": PRIVATE_SETUP_SCHEMA_VERSION,
    }
    retention = _retention_from_values(values)
    if retention is not None:
        policy["retention"] = retention
    return json.dumps(policy, indent=2, sort_keys=True) + "\n"


def _assert_owner_only(path: Path, *, directory: bool) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    forbidden = stat.S_IRWXG | stat.S_IRWXO
    if mode & forbidden:
        kind = "directory" if directory else "file"
        raise PermissionError(f"private {kind} {path} is accessible by group or other users")


def _require_schema_version(raw: Mapping[str, Any], *, artifact: str) -> None:
    if raw.get("schema_version") != PRIVATE_SETUP_SCHEMA_VERSION:
        raise ValueError(
            f"{artifact} must declare schema_version = {PRIVATE_SETUP_SCHEMA_VERSION}"
        )


def _require_exact_keys(
    raw: Mapping[str, Any],
    *,
    artifact: str,
    required: set[str],
    optional: set[str],
) -> None:
    keys = set(raw)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ValueError(f"{artifact} is missing required key(s): {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{artifact} contains unsupported key(s): {', '.join(sorted(unknown))}")


def _validate_string(value: object, *, artifact: str, key: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{artifact}.{key} must be a non-empty string")


def _validate_string_list(value: object, *, artifact: str, key: str) -> None:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{artifact}.{key} must be a list of non-empty strings")


def _validate_retention(value: object, *, artifact: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{artifact} must be an object")
    allowed = {
        "inbox_folder",
        "department_folder",
        "service_folder",
        "archive_folder",
        "sender_archive_rules",
        "classification_definitions",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{artifact} contains unsupported key(s): {', '.join(sorted(unknown))}")
    for key in {
        "inbox_folder",
        "department_folder",
        "service_folder",
        "archive_folder",
    } & set(value):
        _validate_string(value[key], artifact=artifact, key=key)

    if "sender_archive_rules" in value:
        rules = value["sender_archive_rules"]
        if not isinstance(rules, list):
            raise ValueError(f"{artifact}.sender_archive_rules must be a list")
        for rule in rules:
            if not isinstance(rule, dict):
                raise ValueError(
                    f"{artifact}.sender_archive_rules entries must be objects"
                )
            matcher_keys = {"emails", "domains", "address_contains", "name_contains"}
            unknown_rule_keys = set(rule) - {"folder"} - matcher_keys
            if unknown_rule_keys:
                raise ValueError(
                    f"{artifact}.sender_archive_rules entries contain unsupported key(s): "
                    f"{', '.join(sorted(unknown_rule_keys))}"
                )
            if "folder" not in rule:
                raise ValueError(
                    f"{artifact}.sender_archive_rules entries must contain folder"
                )
            _validate_string(rule["folder"], artifact=artifact, key="sender_archive_rules.folder")
            configured_matchers = matcher_keys & set(rule)
            for key in configured_matchers:
                _validate_string_list(
                    rule[key], artifact=artifact, key=f"sender_archive_rules.{key}"
                )
            if not any(rule[key] for key in configured_matchers):
                raise ValueError(
                    f"{artifact}.sender_archive_rules entries require at least one "
                    "non-empty matcher array"
                )

    if "classification_definitions" in value:
        definitions = value["classification_definitions"]
        if not isinstance(definitions, dict) or not all(
            isinstance(key, str)
            and key.strip()
            and isinstance(definition, str)
            and definition.strip()
            for key, definition in definitions.items()
        ):
            raise ValueError(
                f"{artifact}.classification_definitions must be a string-to-string mapping"
            )


def _load_json_artifact(path: Path, *, artifact: str) -> Mapping[str, Any]:
    _assert_owner_only(path, directory=False)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {artifact}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{artifact} must contain a JSON object")
    return value


def _load_runtime_artifact(path: Path) -> Mapping[str, Any]:
    _assert_owner_only(path, directory=False)
    try:
        with path.open("rb") as input_file:
            value = tomllib.load(input_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError("cannot read runtime manifest") from error
    if not isinstance(value, dict):
        raise ValueError("runtime manifest must contain a TOML table")
    if value.get("schema_version") != RUNTIME_MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"runtime manifest must declare schema_version = {RUNTIME_MANIFEST_SCHEMA_VERSION}"
        )
    _require_exact_keys(
        value,
        artifact="runtime manifest",
        required={"schema_version", "storage", "executables"},
        optional=set(),
    )
    storage = value["storage"]
    if not isinstance(storage, dict):
        raise ValueError("runtime manifest.storage must be a table")
    _require_exact_keys(
        storage,
        artifact="runtime manifest.storage",
        required={"runtime_root", "main_db", "entity_db", "vector_store"},
        optional={"work_db", "fact_store_db"},
    )
    for key, item in storage.items():
        _validate_string(item, artifact="runtime manifest.storage", key=key)
        if not Path(item).expanduser().is_absolute():
            raise ValueError(f"runtime manifest.storage.{key} must be an absolute path")
    _validate_distinct_database_paths({key: str(item) for key, item in storage.items()})
    executables = value["executables"]
    if not isinstance(executables, dict):
        raise ValueError("runtime manifest.executables must be a table")
    _require_exact_keys(
        executables,
        artifact="runtime manifest.executables",
        required=set(),
        optional={"himalaya", "hermes", "codex", "claude"},
    )
    for key, item in executables.items():
        _validate_string(item, artifact="runtime manifest.executables", key=key)
        if not Path(item).expanduser().is_absolute():
            raise ValueError(f"runtime manifest.executables.{key} must be an absolute path")
    return value


def load_private_setup(
    *,
    config_home: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> PrivateSetupBundle:
    """Load only a complete, owner-restricted, supported local attachment."""
    paths = private_setup_paths(config_home=config_home, environ=environ)
    _assert_owner_only(paths.config_dir, directory=True)
    runtime = _load_runtime_artifact(paths.runtime_manifest)
    private_env = _load_json_artifact(paths.private_env, artifact="private environment")
    _require_schema_version(private_env, artifact="private environment")
    _require_exact_keys(
        private_env,
        artifact="private environment",
        required={"schema_version"},
        optional={
            "credential_reference",
            "alert_destination",
            "fact_store_module_root",
            "fact_store_provider",
        },
    )
    for key in set(private_env) - {"schema_version"}:
        _validate_string(private_env[key], artifact="private environment", key=key)
    fact_store_root = private_env.get("fact_store_module_root")
    fact_store_provider = private_env.get("fact_store_provider")
    if bool(fact_store_root) != bool(fact_store_provider):
        raise ValueError(
            "private environment must configure fact-store root and provider together"
        )
    if fact_store_provider is not None and fact_store_provider != FACT_STORE_PROVIDER:
        raise ValueError("private environment fact-store provider is unsupported")

    policy = _load_json_artifact(paths.policy, artifact="policy")
    _require_schema_version(policy, artifact="policy")
    _require_exact_keys(
        policy,
        artifact="policy",
        required={"schema_version", "account_label", "account_email", "include_folders", "exclude_folders"},
        optional={"retention"},
    )
    _validate_string(policy["account_label"], artifact="policy", key="account_label")
    _validate_string(policy["account_email"], artifact="policy", key="account_email")
    _validate_string_list(policy["include_folders"], artifact="policy", key="include_folders")
    _validate_string_list(policy["exclude_folders"], artifact="policy", key="exclude_folders")
    if "retention" in policy:
        _validate_retention(policy["retention"], artifact="policy.retention")
    return PrivateSetupBundle(runtime=runtime, private_env=private_env, policy=policy)


def _prepare_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("private configuration directory cannot be a symbolic link")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise NotADirectoryError(f"private configuration path is not a directory: {path}")
    os.chmod(path, 0o700)
    _assert_owner_only(path, directory=True)


def _write_owner_only(path: Path, content: str, *, overwrite: bool) -> None:
    """Write a private file atomically with permissions independent of umask."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if overwrite:
            os.replace(temporary_path, path)
        else:
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                raise FileExistsError(f"private configuration already exists: {path}") from None
            temporary_path.unlink()
        os.chmod(path, 0o600)
        _assert_owner_only(path, directory=False)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def write_private_setup(
    values: PrivateSetupValues,
    *,
    config_home: str | Path | None = None,
    overwrite: bool = False,
    environ: Mapping[str, str] | None = None,
) -> PrivateSetupPaths:
    """Create the local attachment after an explicit overwrite decision.

    Existing artifacts are never replaced unless ``overwrite`` is true.  No
    input value is logged or returned, preventing the UI caller from leaking
    its private configuration in ordinary status output.
    """
    validate_private_setup(values)
    paths = private_setup_paths(config_home=config_home, environ=environ)
    _prepare_private_directory(paths.config_dir)
    if not overwrite:
        existing = [path for path in paths.artifacts if path.exists()]
        if existing:
            raise FileExistsError("private configuration exists; explicit overwrite is required")

    content_by_path = {
        paths.runtime_manifest: render_runtime_manifest(values),
        paths.private_env: render_private_env(values),
        paths.policy: render_policy(values),
    }
    for path, content in content_by_path.items():
        _write_owner_only(path, content, overwrite=overwrite)
    load_private_setup(config_home=config_home, environ=environ)
    return paths


class PrivateSetupApp(App[None]):
    """Small Textual wizard that creates the local deployment attachment."""

    TITLE = "Local Email Memory Setup"
    BINDINGS = [("ctrl+c", "quit", "Quit")]

    def __init__(self, *, config_home: str | Path | None = None) -> None:
        super().__init__()
        self._config_home = config_home

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="private-setup-form"):
            yield Label("Runtime root")
            yield Input(placeholder="Required absolute local path", id="runtime-root")
            yield Label("Work root")
            yield Input(placeholder="Optional absolute local path", id="work-root")
            yield Label("Fact-store database")
            yield Input(placeholder="Optional absolute local path", id="fact-store-db")
            yield Label("Main database")
            yield Input(placeholder="Defaults under runtime root", id="main-db")
            yield Label("Entity database")
            yield Input(placeholder="Defaults under runtime root", id="entity-db")
            yield Label("Vector store")
            yield Input(placeholder="Defaults under runtime root", id="vector-store")
            yield Label("Work database")
            yield Input(placeholder="Optional absolute local path", id="work-db")
            yield Label("Himalaya executable")
            yield Input(
                value=shutil.which("himalaya") or "",
                placeholder="Optional absolute executable path",
                id="himalaya-executable",
            )
            yield Label("Hermes executable")
            yield Input(
                value=shutil.which("hermes") or "",
                placeholder="Optional absolute executable path",
                id="hermes-executable",
            )
            yield Label("Codex executable")
            yield Input(
                value=shutil.which("codex") or "",
                placeholder="Optional absolute executable path",
                id="codex-executable",
            )
            yield Label("Claude executable")
            yield Input(
                value=shutil.which("claude") or "",
                placeholder="Optional absolute executable path",
                id="claude-executable",
            )
            yield Label("Fact-store module root")
            yield Input(placeholder="Optional absolute local path", id="fact-store-module-root")
            yield Label("Account label")
            yield Input(placeholder="Required local label", id="account-label")
            yield Label("Account email")
            yield Input(placeholder="Required mailbox address", id="account-email")
            yield Label("Included folders")
            yield Input(placeholder="Optional comma-separated folders", id="include-folders")
            yield Label("Excluded folders")
            yield Input(placeholder="Optional comma-separated folders", id="exclude-folders")
            yield Label("Retention inbox folder")
            yield Input(placeholder="Optional local folder", id="retention-inbox-folder")
            yield Label("Retention department folder")
            yield Input(placeholder="Optional local folder", id="retention-department-folder")
            yield Label("Retention service folder")
            yield Input(placeholder="Optional local folder", id="retention-service-folder")
            yield Label("Retention archive folder")
            yield Input(placeholder="Optional local folder", id="retention-archive-folder")
            yield Label("Sender archive rules JSON")
            yield Input(
                placeholder='Optional [{"folder":"...","emails":["..."]}]',
                password=True,
                id="retention-sender-archive-rules",
            )
            yield Label("Classification definitions JSON")
            yield Input(
                placeholder='Optional {"category":"definition"}',
                password=True,
                id="retention-classification-definitions",
            )
            yield Label("Alert destination")
            yield Input(
                placeholder="Optional: telegram, slack, or discord",
                id="alert-destination",
            )
            yield Label("Credential reference")
            yield Input(placeholder="Optional local reference", password=True, id="credential-reference")
            yield Checkbox("I confirm replacement of any existing local configuration", id="confirm-overwrite")
            yield Button("Write local configuration", id="write-private-setup", variant="primary")
            yield Static("", id="setup-status")
        yield Footer()

    def _values(self) -> PrivateSetupValues:
        def value(input_id: str) -> str:
            return self.query_one(f"#{input_id}", Input).value

        return PrivateSetupValues(
            runtime_root=value("runtime-root"),
            work_root=value("work-root"),
            fact_store_db=value("fact-store-db"),
            main_db=value("main-db"),
            entity_db=value("entity-db"),
            vector_store=value("vector-store"),
            work_db=value("work-db"),
            himalaya_executable=value("himalaya-executable"),
            hermes_executable=value("hermes-executable"),
            codex_executable=value("codex-executable"),
            claude_executable=value("claude-executable"),
            fact_store_module_root=value("fact-store-module-root"),
            account_label=value("account-label"),
            account_email=value("account-email"),
            include_folders=value("include-folders"),
            exclude_folders=value("exclude-folders"),
            retention_inbox_folder=value("retention-inbox-folder"),
            retention_department_folder=value("retention-department-folder"),
            retention_service_folder=value("retention-service-folder"),
            retention_archive_folder=value("retention-archive-folder"),
            retention_sender_archive_rules=value("retention-sender-archive-rules"),
            retention_classification_definitions=value("retention-classification-definitions"),
            alert_destination=value("alert-destination"),
            credential_reference=value("credential-reference"),
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "write-private-setup":
            return
        confirmed = self.query_one("#confirm-overwrite", Checkbox).value
        status = self.query_one("#setup-status", Static)
        try:
            write_private_setup(
                self._values(),
                config_home=self._config_home,
                overwrite=confirmed,
            )
        except FileExistsError:
            status.update("Existing local configuration needs the confirmation checkbox before replacement.")
        except (OSError, ValueError):
            status.update("Configuration was not written. Review the local values and permissions.")
        else:
            status.update("Local configuration written with owner-only permissions.")


def launch_private_setup(*, config_home: str | Path | None = None) -> None:
    """Run the private setup wizard without importing deployment-specific code."""
    PrivateSetupApp(config_home=config_home).run()


def main() -> None:
    """Console entry point used by the public CLI subcommand."""
    launch_private_setup()

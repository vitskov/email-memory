# Local Configuration Bundle

[Documentation index](README.md) | [Typical deployment](DEPLOYMENT.md) |
[Post-install usage](USAGE.md)

## Purpose

The installed core and the package-owned deployment operations receive private
deployment information through a structured local configuration bundle. The
bundle is created by the interactive `setup-private` command (opened
automatically by a first deployment) and stays outside the checkout, package
distribution, test fixtures, and public Git history.

The core owns the bundle format and validates the public runtime contract. It
does not discover local data or private configuration implicitly.

## What Setup Collects

The setup interface asks for four kinds of local information:

1. durable storage locations for databases, indexes, caches, and reports;
2. absolute executable paths for the selected mail and LLM capabilities;
3. references to external credentials and generic alert transport selection;
4. local account, folder-selection, exclusion, and optional retention policy.

These values describe how one installation attaches to local services and
state. They are never appropriate public fixtures. Choose the deployed or
standalone command surface from [Usage](USAGE.md#choose-the-command-surface)
rather than rediscovering executables through `PATH`.

## Location and Permissions

For standalone setup, run the logical command through the installed application
entry point:

```bash
email-memory-store setup-private
```

The setup interface writes the following bundle under
`${XDG_CONFIG_HOME:-$HOME/.config}/email-memory-store/`:

| File | Purpose |
| --- | --- |
| `runtime.toml` | Authoritative runtime storage and executable locations. |
| `private.env.json` | Private deployment references that must not enter the core configuration. |
| `policy.json` | Local source-selection policy. |

The directory is created and checked as owner-only (`0700`). Each artifact is
written atomically and checked as owner-only (`0600`). The setup process rejects
a symbolic-link configuration directory and refuses to replace existing bundle
artifacts without an explicit overwrite confirmation.

Treat the bundle as confidential even when it holds references rather than
secrets: paths, labels, addresses, selections, and destinations are private
deployment data.

Standalone CLI setup follows the XDG location above. The transactional
production deploy instead uses the invoking account's canonical passwd home and
rejects ambient `HOME`/XDG root changes, so its bundle is fixed at
`<canonical-user-home>/.config/email-memory-store/`.

## Runtime Manifest

`runtime.toml` is the only bundle file consumed directly by the public CLI and
the MCP launcher. Deployment and scheduled operations load the complete bundle
through the package's validated local-configuration profiles, while this
schema-v2 manifest remains their centralized storage and executable authority.
Its supported schema is:

```toml
schema_version = 2

[storage]
runtime_root = "/absolute/path/to/durable-local-state"
main_db = "/absolute/path/to/email-memory.duckdb"
entity_db = "/absolute/path/to/entity-memory.duckdb"
vector_store = "/absolute/path/to/vector-store"
work_db = "/absolute/path/to/optional-work.duckdb"
fact_store_db = "/absolute/path/to/optional-fact-store.db"

[executables]
himalaya = "/absolute/path/to/himalaya"
hermes = "/absolute/path/to/hermes"
codex = "/absolute/path/to/codex"
claude = "/absolute/path/to/claude"
```

The current runtime schema is `2`. The four primary `storage` fields are
required; `work_db`, `fact_store_db`, and every executable are optional. All
supplied values must be absolute. Schema v2 deliberately rejects a
second provider layer: the selected manifest is the single authoritative source
of runtime paths.
The setup interface suggests executable candidates using `PATH` only while the
form is open, validates supplied candidates as executable files, and writes the
selected path without resolving stable symlinks. Normal commands never search
`PATH`: ingestion requires `executables.himalaya`, while each LLM capability
requires the executable for its selected provider. DB-only commands and MCP
`search` do not require an executable.

Older manifests remain readable for compatibility, but they are not a setup or
deployment path. Regenerate them with `setup-private` before using mail or LLM
capabilities.

The launcher that starts the CLI or MCP server owns the manifest path. The
public process resolves the selected attachment once at startup instead of
discovering runtime locations from inside the stdio session.

Do not add credentials, identity details, source-selection data, notification
targets, or arbitrary extra keys to `runtime.toml`.

## Private References

`private.env.json` is a versioned local-only document. The current schema is:

```json
{
  "schema_version": 1,
  "alert_destination": "telegram",
  "credential_reference": "local-reference-only",
  "fact_store_module_root": "/absolute/path/to/local-module-root",
  "fact_store_provider": "email_memory_store.integrations.hermes_fact_store:MemoryStore"
}
```

`schema_version` is required and currently `1`; all other keys are optional.
`alert_destination` is a generic transport selection and must be exactly one of
`telegram`, `slack`, or `discord`; it is not an account, workspace, channel, or
recipient identifier. `credential_reference` is a reference, not a credential
value. Never place passwords, tokens, private keys, message content, or specific
routing targets in this file. `fact_store_module_root`, when present, is an
absolute local path. Setup derives the corresponding provider as the exact
public adapter
`email_memory_store.integrations.hermes_fact_store:MemoryStore`; arbitrary
provider import paths are not accepted.

Normal core runtime resolution does not consume this file. A local-only
integration may explicitly load it after validating its schema and permissions.
The public deployment coordinator and packaged scheduler also load it through
bounded package-owned profiles. A deployment requires a nonempty
`credential_reference` as an audit reference, then separately proves actual
mail authentication by proving that the policy-selected label is the mail
connector's unique default account and then making a read-only default-account
folder-list request. The reference itself is never treated as a credential or
as proof of access.

### Process visibility boundary

Package-owned deployment and scheduler commands keep credentials, account
labels and addresses, personal notification destinations, and complete folder
policy arrays out of process arguments. They also select the owner-only runtime
manifest through `EMAIL_MEMORY_STORE_RUNTIME_CONFIG`, not a command-line path.
Scheduled connector calls rely on the verified default account, so its private
label remains only in owner-readable configuration, process environment, and
Python memory.

The maintenance profile carries the exact include/exclude arrays as validated
JSON environment values. Scheduled `nightly-update` consumes those arrays only
after default-account verification and only when explicit interactive folder
flags are absent; this preserves local folder scope without copying the policy
into the package-owned command line.

The third-party mail connector still requires per-operation folder names and
message identifiers as command-line operands; its CLI provides no environment
or standard-input form for those operands. On a multi-user host where local
users are mutually untrusted, run email-memory under a dedicated OS account or
configure the operating system to restrict cross-user process inspection.

## Local Policy

`policy.json` is also a versioned local-only document. The current schema is:

```json
{
  "schema_version": 1,
  "account_label": "local-label",
  "account_email": "user@example.test",
  "include_folders": ["local-selection"],
  "exclude_folders": []
}
```

All five keys are required. `schema_version` is currently `1`.
`account_label` and `account_email` identify the local message source.
`include_folders` and `exclude_folders` are arrays and may be empty. These values
are private policy, not public configuration. The setup interface requires a
nonempty label and a syntactically valid address, normalizes comma-separated
selections into the arrays, and does not log entered values.

### Optional Retention Policy

`policy.json` may include one optional `retention` object. It is local-only and
strict: the object may contain only the fields below, and unknown nested fields
are rejected.

```json
{
  "retention": {
    "inbox_folder": "Incoming",
    "department_folder": "Departments",
    "service_folder": "Services",
    "archive_folder": "Archive",
    "sender_archive_rules": [
      {
        "folder": "People/Example",
        "emails": ["contact@example.test"],
        "domains": ["example.test"],
        "address_contains": ["notifications@"],
        "name_contains": ["Example Service"]
      }
    ],
    "classification_definitions": {
      "newsletter": "recurring update"
    }
  }
}
```

The `retention` object may be omitted. Every field inside it is optional, but
when present `inbox_folder`, `department_folder`, `service_folder`, and
`archive_folder` must be non-whitespace strings. `sender_archive_rules` must be an
array whose entries contain `folder` (a non-whitespace string) and may contain only
the matcher arrays `emails`, `domains`, `address_contains`, and
`name_contains`. Each supplied matcher must be an array of non-whitespace
strings,
and every rule must have at least one nonempty matcher array. A rule containing
only `folder` and `emails` remains valid. `classification_definitions` must be
a mapping whose keys and values are non-whitespace strings. These names and
values remain private deployment policy and must never be placed in public
tests, documentation examples with real identities, or a public release
archive.

## Runtime Resolution

Select the generated runtime manifest explicitly:

```bash
email-memory-store --runtime-config /path/to/runtime.toml status
```

Or set its path in a local launcher:

```bash
export EMAIL_MEMORY_STORE_RUNTIME_CONFIG=/path/to/runtime.toml
email-memory-store status
```

For the schema-version-2 manifest written by `setup-private`, each setting
resolves in this order:

1. The corresponding command-line option: `--root`, `--work-root`, or
   `--fact-store-db`.
2. The matching field in the selected `runtime.toml`.
3. For `runtime_root` only, the generic XDG state default.

For a schema-v2 manifest, the exact `main_db`, `entity_db`, `vector_store`, and
optional `work_db` paths are authoritative; they need not be children of
`runtime_root`. Supplying `--root` derives database and vector paths from that
root for the current invocation and therefore replaces the corresponding
manifest paths. The manifest selection itself resolves `--runtime-config` before
`EMAIL_MEMORY_STORE_RUNTIME_CONFIG`. There is no default for `work_root` or
`fact_store_db`.

The MCP launcher uses the same field precedence at process start but does not
use the CLI's XDG runtime fallback. It requires `--root`, `--runtime-config`, or
`EMAIL_MEMORY_STORE_RUNTIME_CONFIG`. It fails before stdio opens if the
attachment is missing or invalid, or if `storage.vector_store` is not an
existing initialized store with indexed application data. MCP never creates a
replacement index at startup.

Use `email-memory-store --runtime-config /path/to/runtime.toml runtime-doctor`
for a redacted, automation-friendly check. It prints only boolean health and
configuration state, never configured paths, and exits nonzero when required
storage is absent. Unusable optional executables are reported but do not fail
the default DB-only check.

Capability-specific preflight checks are explicit and repeatable:

```bash
email-memory-store --runtime-config /path/to/runtime.toml runtime-doctor \
  --require mail --require selected-llm
```

`mail` requires a usable configured Himalaya executable. `selected-llm` reads
the persisted provider selection from the main database without modifying it
(defaulting to Hermes when no selection exists) and requires that provider's
configured executable. Doctor output remains boolean-only and exits nonzero
when a requested capability is unavailable.

## Regeneration

`setup-private` is the supported way to create or regenerate the complete
bundle. It does not print entered values in ordinary status output. If any
artifact already exists, the command stops without changing it until the
overwrite confirmation is explicitly selected. On confirmed regeneration, all
three documents are rendered from the new local inputs and written with the same
owner-only permission checks.

Regeneration does not copy private values into the repository and does not
modify runtime databases. Back up the bundle with local confidential-data
procedures, not through a public source-control remote.

## Deployment And Scheduler Profiles

The package owns validated profiles for bootstrap, maintenance, cron, status,
ingestion, triage, and related operations. They derive only the bounded
environment variables each operation needs from `runtime.toml`,
`private.env.json`, and `policy.json`.
Ambient variables cannot override those loaded values. This makes the public
deployment and scheduling scripts self-contained without a second path/provider
configuration layer.

Together, the cron and maintenance profiles resolve the configured account,
alert reference, absolute Himalaya and Hermes executables, and runtime manifest;
the hardened installed launcher supplies the release-local operational Python.
The package-owned `email-memory-store-deploy nightly` launcher always supplies
an ISO-week batch path, whether cron or an operator invokes it, and delivers
failures on the selected weekly alert day. Only direct low-level maintenance
scripts or application diagnostics outside that launcher lack its batch context;
they are not the recommended deployed pipeline.

Hermes executable configuration authorizes only the email-memory operations
implemented by the package, such as `hermes chat` and `hermes send`.
Email-memory never controls the Hermes gateway lifecycle: it never starts,
stops, restarts, reloads, signals, or supervises that process.

See [Deployment](DEPLOYMENT.md) for how these profiles participate in staging,
readiness checks, activation, scheduler installation, and automatic rollback.

## Operational Artifacts

Generated reports are private runtime data. The public package provides
`email_memory_store.operational_artifacts` as the standard boundary for local
launchers that persist operational state. It creates directories with mode
`0700`, files with mode `0600`, refuses symbolic-link paths and non-regular
artifacts, rejects files owned by another user or with multiple hard links,
and supports atomic private writes and age-based pruning.

Its structured JSONL event format accepts only bounded event, run, stage,
severity, numeric, and boolean fields. It deliberately has no free-text field
for message content, addresses, account or folder labels, destinations,
exceptions, credentials, or filesystem paths. A local notification adapter
should resolve its destination only when sending and render validated events
into an ephemeral private file.

Some explicit exports, including promotion batches, contain application data
by design. Their writer uses the same atomic owner-only file boundary, but the
local operator remains responsible for selecting a private runtime location
and an appropriate retention period. Generated artifacts must never be written
inside the Git checkout.

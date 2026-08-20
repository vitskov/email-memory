# Local Configuration Bundle

## Purpose

The installed core receives private deployment information through a structured
local configuration bundle. The bundle is created by the interactive
`setup-private` command and stays outside the checkout, package distribution,
test fixtures, and public Git history.

The core owns the bundle format and validates the public runtime contract. It
does not discover local data or private configuration implicitly.

## Location and Permissions

Run:

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

## Runtime Manifest

`runtime.toml` is the only bundle file consumed directly by the public CLI and
the MCP launcher. Its supported schema is:

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
`runtime_provider` table so that the selected manifest remains the single
authoritative source of runtime paths.
The setup interface suggests executable candidates using `PATH` only while the
form is open, validates supplied candidates as executable files, and writes the
selected path without resolving stable symlinks. Normal commands never search
`PATH`: ingestion requires `executables.himalaya`, while each LLM capability
requires the executable for its selected provider. DB-only commands and MCP
`search` do not require an executable.

Unversioned and schema-v1 flat manifests, including their legacy named runtime
provider references, remain readable for compatibility. They are not written
by the setup command and do not gain implicit executable discovery; regenerate
them as v2 before using mail or LLM capabilities.

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
  "alert_destination": "local-reference-only",
  "credential_reference": "local-reference-only",
  "fact_store_module_root": "/absolute/path/to/local-module-root",
  "fact_store_provider": "local-reference-only"
}
```

`schema_version` is required and currently `1`; all other keys are optional.
`alert_destination` and `credential_reference` are references, not credential
values. Point them at an appropriate local secret or delivery mechanism; never
place passwords, tokens, private keys, or message content in this file.
`fact_store_module_root`, when present, is an absolute local path.
`fact_store_provider`, when present, is a local provider reference.

Normal core runtime resolution does not consume this file. A local-only
deployment attachment may explicitly load the complete bundle after validating
its schema and permissions.

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
        "emails": ["contact@example.test"]
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
`archive_folder` must be nonempty strings. `sender_archive_rules` must be an
array whose entries contain exactly `folder` (a nonempty string) and `emails`
(an array of nonempty strings). `classification_definitions` must be a
string-to-string mapping. These names and values remain private deployment
policy and must never be placed in public tests, documentation examples with
real identities, or a public release archive.

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

Each setting resolves in this order:

1. The corresponding command-line option: `--root`, `--work-root`, or
   `--fact-store-db`.
2. The matching field in the selected `runtime.toml`.
3. For legacy unversioned or schema-v1 manifests only, the matching field
   returned by their explicitly named local runtime provider.
4. For `runtime_root` only, the generic XDG state default.

For a schema-v2 manifest, the exact `main_db`, `entity_db`, `vector_store`, and
optional `work_db` paths are authoritative; they need not be children of
`runtime_root`. Explicit legacy root options derive legacy child paths and take
precedence when supplied. The manifest selection itself resolves
`--runtime-config` before
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

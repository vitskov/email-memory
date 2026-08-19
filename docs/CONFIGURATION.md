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
| `runtime.toml` | Runtime locations and the optional named local runtime provider. |
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

`runtime.toml` is the only bundle file consumed directly by the public CLI. Its
supported schema is:

```toml
schema_version = 1
runtime_root = "/absolute/path/to/durable-local-state"
work_root = "/absolute/path/to/optional-local-work-state"
fact_store_db = "/absolute/path/to/optional-local-fact-store.db"

[runtime_provider]
name = "optional-installed-local-provider"
```

`schema_version` and `runtime_root` are required; the current schema version is
`1`. `work_root`, `fact_store_db`, and the `runtime_provider` table are
optional. The setup interface requires absolute paths for every location it
writes. The provider name is an explicit reference to a locally installed
provider; providers are never discovered automatically.

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
email-memory-store --runtime-config "$HOME/.config/email-memory-store/runtime.toml" status
```

Or set its path in a local launcher:

```bash
export EMAIL_MEMORY_STORE_RUNTIME_CONFIG="$HOME/.config/email-memory-store/runtime.toml"
email-memory-store status
```

Each setting resolves in this order:

1. The corresponding command-line option: `--root`, `--work-root`, or
   `--fact-store-db`.
2. The matching field in the selected `runtime.toml`.
3. The matching field returned by the explicitly named local runtime provider.
4. For `runtime_root` only, `$XDG_STATE_HOME/email-memory-store`, or
   `$HOME/.local/state/email-memory-store` when `XDG_STATE_HOME` is unset.

The manifest selection itself resolves `--runtime-config` before
`EMAIL_MEMORY_STORE_RUNTIME_CONFIG`. There is no default for `work_root` or
`fact_store_db`.

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

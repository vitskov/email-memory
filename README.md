# email-memory-store

`email-memory-store` is a local-first library and command-line application for
building a searchable, durable memory index from email. It stores normalized
messages, derived facts, thread summaries, calendar data, and retrieval indexes
in local state that the installed application controls.

The repository contains the reusable core. A structured local configuration
bundle supplies state locations and deployment-specific policy. The core never
embeds an identity, a message source, routing destination, machine path, or
credential.

## Security Boundary

The public core and local data have separate responsibilities:

- The repository contains code, synthetic tests, generic documentation, schemas,
  and packaged default assets.
- The local runtime contains databases, raw message artifacts, caches, reports,
  locally customized promotion assets, and other generated state.
- Credential references, connection profiles, policy, and notification delivery
  are local deployment concerns. They are not repository configuration.
- An installed core has no implicit access to local data. It needs an explicit
  runtime location through a command-line path or a local runtime manifest.

See [Configuration](docs/CONFIGURATION.md) for the runtime manifest contract and
[Architecture Overview](docs/ARCHITECTURE_OVERVIEW.md) for component boundaries.

## Install

Use a supported Python environment, then install the core from a checkout:

```bash
python -m pip install -e .
```

For contributor verification:

```bash
python -m pytest -q
ruff check .
mypy src
```

## Quick Start

Choose a local runtime directory explicitly:

```bash
email-memory-store --root "$HOME/.local/state/email-memory-store" init-db
email-memory-store --root "$HOME/.local/state/email-memory-store" status
```

The runtime is created on first initialization. It is local state, not source
code, and must not be added to version control.

For a stable local deployment, create the owner-only local configuration bundle:

```bash
email-memory-store setup-private
```

The interactive setup writes three files under
`${XDG_CONFIG_HOME:-$HOME/.config}/email-memory-store/`: `runtime.toml`,
`private.env.json`, and `policy.json`. It writes the directory with mode `0700`
and each file with mode `0600`. Existing files are never replaced until the
explicit overwrite confirmation is selected.

Run the core against the generated runtime manifest:

```bash
email-memory-store --runtime-config "$HOME/.config/email-memory-store/runtime.toml" init-db
email-memory-store --runtime-config "$HOME/.config/email-memory-store/runtime.toml" status
```

The bundle schema, regeneration behavior, and runtime precedence rules are in
[Configuration](docs/CONFIGURATION.md).

## Capabilities

The core provides:

- durable DuckDB storage for normalized messages and derived records
- incremental ingestion and targeted recovery operations
- thread and identity reconciliation
- extraction of facts, decisions, actions, deadlines, and calendar events
- lexical and semantic retrieval with provenance
- local vector-index backfill and reconciliation
- auditable promotion planning for compact downstream facts
- a CLI, MCP stdio entry point, and terminal browser

Run `email-memory-store --help` for the complete command surface. Commands that
read from a message source require explicit local connector configuration and
command inputs; the public core does not provide deployment defaults for them.

## Repository Policy

This repository is intended to remain safe to publish. Do not add local runtime
state, configuration bundles, credentials, message content, generated reports,
or deployment automation here. Credential fields in local configuration are for
references; never place secret values in them. Use synthetic fixtures and reserved example
domains in tests and documentation.

Release checks must examine the complete publishable Git history and archive, not
only the working tree. The public remote belongs only to the sanitized publishing
clone; operational working copies should not have a writable public push remote.

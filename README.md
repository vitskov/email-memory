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
- Stable deployments select local data explicitly through a command-line path
  or local runtime manifest. The CLI retains a generic XDG default for local
  bootstrap; the MCP service requires an explicit attachment.

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
email-memory-store --root /path/to/runtime-root init-db
email-memory-store --root /path/to/runtime-root status
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
email-memory-store --runtime-config /path/to/runtime.toml init-db
email-memory-store --runtime-config /path/to/runtime.toml status
```

The bundle schema, regeneration behavior, and runtime precedence rules are in
[Configuration](docs/CONFIGURATION.md).

## MCP Startup

The `email-memory-store-mcp` launcher uses the same runtime attachment contract
as the CLI. Pass either an explicit root or a runtime manifest at startup:

```bash
email-memory-store-mcp --root /path/to/runtime-root
email-memory-store-mcp --runtime-config /path/to/runtime.toml
```

The launcher resolves the attachment once before stdio opens. If the runtime
attachment is missing or invalid, or `<runtime_root>/chroma` is not an existing
initialized store with indexed data, startup fails without creating a
replacement index. The same configured retrieval engine serves both MCP tools.

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

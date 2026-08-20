# email-memory-store

Turn email into a private, searchable memory that you can query from a terminal
or an MCP-compatible assistant.

`email-memory-store` is a local-first Python application for indexing message
history as durable, traceable knowledge. It preserves normalized messages and
threads, derives useful records such as facts, decisions, action items, and
deadlines, and combines lexical and semantic retrieval so results retain their
source context.

## Why use it?

- Find commitments, decisions, people, dates, and prior conversations without
  manually searching mailbox folders.
- Keep the database, vector index, cached artifacts, and policy on the machine
  where the application runs.
- Use the same retrieval engine interactively through the CLI or from an
  MCP-compatible client.
- Rebuild and reconcile indexes from durable source records instead of treating
  the vector store as the source of truth.

## How it works

```text
local mail connector
        |
        v
ingest and normalize --> DuckDB records and provenance
        |                            |
        v                            v
extract structured memory     build local vector indexes
        |                            |
        +------------+---------------+
                     v
             search or cited answers
                     |
                     v
       optional downstream fact promotion
```

A typical lifecycle is:

1. An explicitly configured local connector supplies messages to an ingestion
   command.
2. The store normalizes messages, threads, identities, and resumable ingestion
   state in DuckDB.
3. Extraction produces structured facts, decisions, actions, deadlines,
   calendar events, and summaries.
4. Reconciliation builds local vector collections from the durable records.
5. The CLI or MCP service performs hybrid retrieval and returns provenance;
   answer synthesis adds inline citation handles when an LLM provider is used.

The publishable package and private deployment stay deliberately separate:

| Public core | Local runtime |
| --- | --- |
| Schemas, storage and retrieval code, CLI and MCP entry points, generic assets, synthetic tests | Messages, databases, vector indexes, caches, reports, connector policy, credential references |
| Safe to clone and test without personal data | Selected explicitly and kept outside the checkout |
| Defines integration contracts | Owns connector, scheduling, notification, and downstream-service choices |

See the [Architecture Overview](docs/ARCHITECTURE_OVERVIEW.md) for component and
trust boundaries.

## Install

You need Git and `uv >= 0.12.5`. The bootstrap script installs the project's
Python 3.14 environment and locked dependencies:

```bash
git clone https://github.com/vitskov/email-memory.git
cd email-memory
./scripts/bootstrap.sh
```

Create the owner-only local configuration bundle, then initialize and inspect
the runtime selected by its manifest:

```bash
./.venv/bin/email-memory-store setup-private
RUNTIME_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/email-memory-store/runtime.toml"
./.venv/bin/email-memory-store --runtime-config "$RUNTIME_CONFIG" init-db
./.venv/bin/email-memory-store --runtime-config "$RUNTIME_CONFIG" status
```

Runtime data belongs outside the repository and must not be committed. For a
stable setup, keep using the generated manifest rather than repeating storage
or executable paths in launch commands. The complete public-core installation,
accelerator, upgrade, and packaging procedures are in
[Installation](docs/INSTALLATION.md); manifest schemas and precedence rules are
in [Configuration](docs/CONFIGURATION.md). Connector installation,
authentication, scheduling, and notification delivery belong to the local
deployment and are intentionally not prescribed by this public package.

## CLI or MCP

Both interfaces use the same local retrieval engine but serve different jobs:

| Interface | Best for | Runtime selection |
| --- | --- | --- |
| `email-memory-store` | Setup, ingestion, extraction, indexing, maintenance, terminal browsing, and direct queries | Explicit `--root` or runtime manifest; a generic XDG default is available for local bootstrap |
| `email-memory-store-mcp` | Giving an MCP-compatible assistant the `search` and `ask` tools | An existing indexed runtime must be attached explicitly; there is no default runtime |

For example, after indexing data, search it directly:

```bash
./.venv/bin/email-memory-store --runtime-config /path/to/runtime.toml \
  search --query "project decisions from last month"
```

Or register this stdio command with an MCP host:

```bash
./.venv/bin/email-memory-store-mcp --runtime-config /path/to/runtime.toml
```

The MCP launcher fails closed when the runtime attachment is absent, invalid,
or has no initialized vector index. See [MCP Integration](docs/MCP_INTEGRATION.md)
for host-agnostic registration, tool behavior, and troubleshooting.

## Optional Hermes integration

Hermes is an optional LLM command-line integration, not an installation or MCP
requirement. Ingestion, indexing, maintenance, browsing, CLI search, and the MCP
`search` tool do not use it.

The cited-answer and promotion workflows do require a supported LLM command-line
provider. `hermes-default` is used when no provider is specified; `codex-cli`
and `claude-code-cli` are also supported when an explicit model is supplied.
Install and configure whichever provider you choose separately from this
project. The [MCP Integration guide](docs/MCP_INTEGRATION.md#optional-llm-providers)
explains the boundary.

## Choose your path

- **First-time user:** follow [Installation](docs/INSTALLATION.md), then create a
  private runtime using [Configuration](docs/CONFIGURATION.md).
- **Operator:** use [Configuration](docs/CONFIGURATION.md) for runtime selection,
  local policy, permissions, and generated operational artifacts.
- **MCP integrator:** start with [MCP Integration](docs/MCP_INTEGRATION.md).
- **Contributor:** read the [Architecture Overview](docs/ARCHITECTURE_OVERVIEW.md)
  before changing storage, ingestion, retrieval, or runtime boundaries.
- **Maintainer preparing a public release:** follow
  [Privacy Release Controls](docs/PRIVACY_RELEASE_CONTROLS.md).

Run `./.venv/bin/email-memory-store --help` for the full command surface.
Detailed operating and release procedures remain in the linked guides so this
page can stay a map of the project rather than a deployment runbook.

## Privacy boundary

Do not add runtime state, message content, credentials, local configuration,
generated reports, private identifiers, or deployment automation to this
repository. Public fixtures and examples must be synthetic. The release gate
checks tracked content, reachable Git history, source archives, and built
distributions; deployment owners must also apply their private, ignored
identifier denylist before publication. See
[Privacy Release Controls](docs/PRIVACY_RELEASE_CONTROLS.md) for the complete
procedure.

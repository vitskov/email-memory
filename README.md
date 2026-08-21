# Email Memory Store

Turn email history into private, searchable memory with traceable sources.

`email-memory-store` is a local-first Python application that ingests messages,
preserves normalized email and thread records, extracts structured knowledge,
and builds hybrid lexical and semantic indexes. You can explore that memory from
the terminal or expose the same retrieval engine to an MCP-compatible assistant.

The public repository contains the reusable application and deployment
mechanisms. Messages, databases, indexes, credentials, machine paths, connector
policy, and reports remain in an owner-only local runtime outside the checkout.

## What it does

- Ingests messages incrementally from an explicitly configured local mail
  connector, with resumable progress and repairable cursor state.
- Preserves messages, labels, threads, contacts, and provenance in DuckDB.
- Extracts facts, decisions, action items, deadlines, events, and summaries.
- Builds local vector collections and reconciles them against durable source
  records rather than treating the vector index as the source of truth.
- Searches with lexical and semantic retrieval, filters, and source context.
- Produces citation-constrained answers when an optional LLM provider is
  configured.
- Serves the same `search` and `ask` capabilities over MCP.
- Runs package-owned nightly maintenance with ISO-week alert batching and
  owner-only structured reports.

## How it works

```text
configured mail connector
          |
          v
  ingest and normalize --------> DuckDB records + provenance
          |                                  |
          v                                  v
  structured extraction             local vector indexes
          |                                  |
          +----------------+-----------------+
                           v
                 hybrid search + cited answers
                           |
                           v
             optional downstream fact promotion
```

One schema-version-2 runtime manifest provides the exact database, vector-store,
and executable paths used by the CLI, MCP server, deployment checks, and
scheduled maintenance. This central attachment prevents different entry points
from silently selecting different data or executables. See the
[architecture overview](docs/ARCHITECTURE_OVERVIEW.md) for component, state, and
trust boundaries, or start from the task-oriented
[documentation index](docs/README.md).

## Privacy model

| Public repository | Owner-only local installation |
| --- | --- |
| Python package, schemas, CLI and MCP entry points | Email content and derived records |
| Generic deployment and maintenance mechanisms | DuckDB databases, vector indexes, caches, and reports |
| Synthetic tests and value-free documentation | Runtime manifest, local policy, and credential references |
| Privacy gates for tracked files, Git history, and release artifacts | A deployment-specific identifier denylist used before publication |

Cloning or testing the repository does not grant access to local email data.
Conversely, installing the public code does not place private state in Git: the
setup interface creates a separate `0700` configuration directory with `0600`
files, and deployment keeps durable state outside both the checkout and
installed releases. Credential values belong in their provider's credential
store; local configuration records references only.

Read [Privacy Release Controls](docs/PRIVACY_RELEASE_CONTROLS.md) before
publishing a fork or release.

## Quick start: typical Linux deployment

The supported end-to-end deployment requires Linux, Git, GNU coreutils,
`crontab`, and `uv >= 0.12.5`. The project requires Python 3.14 or newer; `uv`
installs the pinned Python 3.14 interpreter inside each release, so you do not
need to replace the system Python.

Clone into an owner-only source directory and run the transactional deployer:

```bash
install -d -m 0700 "$HOME/.local/src"
umask 077
git clone https://github.com/vitskov/email-memory.git "$HOME/.local/src/email-memory"
cd "$HOME/.local/src/email-memory"
uv self update
./scripts/deploy.sh --accelerator auto
```

On the first run, the setup interface asks for durable storage locations,
absolute connector and LLM executable paths, and local policy. Deployment then
creates an immutable release, verifies real read-only mail access, initializes
the databases, checks the maintenance path, and installs the package-owned MCP
launcher and managed nightly schedule. The deployment guide is authoritative
for the initial indexing and readiness sequence required before the first MCP
connection. Transaction failure restores the previous active release, MCP
pointers, and scheduler state.

`--accelerator auto` selects CUDA only when Linux exposes a usable NVIDIA GPU
and driver; otherwise it installs the CPU profile. The standalone package
bootstrap also supports Apple MPS, but the transactional deployment is currently
Linux-only. See [Installation](docs/INSTALLATION.md) for prerequisites,
accelerator selection, standalone and development environments, upgrades, and
package builds. See [Deployment](docs/DEPLOYMENT.md) for the transaction,
installed layout, readiness doctor, scheduler, and rollback contract.

## Everyday use

Resolve the active CLI and central runtime manifest from the current account,
then inspect or query the store:

```bash
email_memory_home="$(getent passwd "$(id -u)" | cut -d: -f6)"
email_memory_cli="$email_memory_home/.local/share/email-memory-store/current/venv/bin/email-memory-store"
runtime_config="$email_memory_home/.config/email-memory-store/runtime.toml"

"$email_memory_cli" --runtime-config "$runtime_config" status
"$email_memory_cli" --runtime-config "$runtime_config" search \
  --query "decisions about the launch date"
"$email_memory_cli" --runtime-config "$runtime_config" browse
```

Run the deployed readiness doctor after an upgrade or integration change:

```bash
email_memory_home="$(getent passwd "$(id -u)" | cut -d: -f6)"
"$email_memory_home/.local/share/email-memory-store/current/bin/email-memory-store-deploy" doctor
```

Use `email-memory-store --help` and `email-memory-store <command> --help` for the
complete command surface. The [Configuration guide](docs/CONFIGURATION.md)
explains runtime selection, setup regeneration, local policy, permissions, and
operational artifacts. [Usage](docs/USAGE.md) covers health checks, ingestion,
indexing, retrieval, maintenance, and recovery after installation.

## MCP integration

The transactional deployer installs and verifies a stable package-owned MCP
launcher. The MCP server requires an explicit initialized runtime attachment and
fails closed instead of creating or searching an empty default index. It exposes:

| Tool | Purpose | LLM required |
| --- | --- | --- |
| `search` | Hybrid retrieval with date, thread, and effort filters | No |
| `ask` | A grounded answer whose claims use inline source handles | Yes |

See [MCP Integration](docs/MCP_INTEGRATION.md) for host-agnostic registration,
tool behavior, startup guarantees, and troubleshooting.

## Hermes and LLM integration

Hermes is one supported command-line LLM provider; Codex CLI and Claude Code CLI
are also supported. A standalone package installation can ingest, index, browse,
run CLI search, start MCP, and call the MCP `search` tool without an LLM.

The supported transactional deployment has a stricter operational contract: it
requires a configured Hermes executable for package-owned alert delivery and
one selected LLM provider for maintenance. The selected LLM may be Hermes,
Codex CLI, or Claude Code CLI. These requirements do not make Email Memory Store
the owner of the Hermes gateway.

**Email Memory Store never controls the Hermes gateway lifecycle.** Deployment,
upgrade, MCP, and maintenance code never starts, stops, restarts, reloads,
signals, or supervises the gateway. The package may invoke configured
`hermes chat` and `hermes send` commands, while the Hermes host remains solely
responsible for its gateway process. The [MCP Integration guide's provider
section](docs/MCP_INTEGRATION.md#optional-llm-providers) documents the supported
values and model requirements.

## Choose your path

| Guide | Use it for |
| --- | --- |
| [Documentation Index](docs/README.md) | The shortest route for a new user, operator, contributor, or release maintainer |
| [Installation](docs/INSTALLATION.md) | Prerequisites, Python 3.14 and `uv`, accelerators, standalone/development setup, upgrades, and builds |
| [Deployment](docs/DEPLOYMENT.md) | Typical Linux installation, immutable releases, readiness checks, scheduling, and rollback |
| [Configuration](docs/CONFIGURATION.md) | Central paths and executables, local policy, permissions, regeneration, and runtime selection |
| [Usage](docs/USAGE.md) | Deployed and standalone command surfaces, common workflows, diagnostics, and safe recovery |
| [MCP Integration](docs/MCP_INTEGRATION.md) | MCP registration, `search` and `ask`, LLM providers, and troubleshooting |
| [Architecture Overview](docs/ARCHITECTURE_OVERVIEW.md) | Components, data flow, persistent state, operational boundaries, and release invariants |
| [Privacy Release Controls](docs/PRIVACY_RELEASE_CONTROLS.md) | Generic hosted checks and the required local identifier-denylist gate |

For development, use the locked environment and run the same local checks as
hosted CI:

```bash
./scripts/bootstrap.sh --dev
./scripts/run_ci_locally.sh
```

Public examples and tests must remain synthetic. Never commit messages,
databases, indexes, caches, reports, runtime manifests, credentials, local paths,
identities, notification destinations, or operational history.

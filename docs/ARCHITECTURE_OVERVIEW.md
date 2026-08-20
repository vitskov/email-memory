# Email Memory Store Architecture Overview

## Purpose

`email-memory-store` is a local-first system for turning email into durable,
searchable memory. The public core owns algorithms, schemas, command interfaces,
and generic packaged assets. A separately managed local runtime owns all
deployment-specific state and integrations.

This boundary allows the same core to be installed from a public repository while
keeping personal data, credentials, local policy, and machine-specific details
outside source control.

## Component Boundaries

### Public core

The publishable package contains:

- CLI and MCP stdio entry points
- data schemas and DuckDB store layer
- ingestion, extraction, identity, promotion, and retrieval services
- vector-index adapters and reconciliation logic
- packaged default promotion assets
- synthetic tests and generic documentation

The core may define interfaces for local connectors and downstream integrations,
but it contains no site-specific endpoint, identity, destination, or filesystem
assumption.

### Local runtime attachment

The local runtime is selected explicitly at process start. It contains:

- primary and companion databases
- raw message artifacts, cache files, reports, and vector indexes
- locally seeded or customized promotion assets
- optional transient working database location
- exact main, entity, optional work, fact-store, and vector-store locations

The package-owned local configuration bundle manages credential references,
connection policy, scheduling notification references, and optional fact-store
integration. Private values remain outside the runtime-manifest schema and the
checkout, while validated package profiles expose only the bounded fields needed
by deployment and scheduled operations.

### Runtime resolution

The CLI resolves runtime fields once at process startup through this precedence
order:

1. `--root`, `--work-root`, and `--fact-store-db` command-line options
2. `--runtime-config <path>` local TOML manifest
3. `EMAIL_MEMORY_STORE_RUNTIME_CONFIG` pointing to that manifest
4. the generic XDG state default for `runtime_root`

The schema-v2 manifest supplies an authoritative `[storage]` table with exact
main database, entity database, vector store, and optional work/fact-store
paths. Its optional `[executables]` table selects absolute mail and LLM command
paths. Executable discovery is confined to the setup UI; capability invocation
fails closed when its selected executable is absent. The manifest remains a
location selector, not a credential store. See [Configuration](CONFIGURATION.md).

The MCP launcher uses the same resolver and field precedence after an
attachment is selected, but it deliberately has no XDG fallback. It requires
`--root`, `--runtime-config`, or `EMAIL_MEMORY_STORE_RUNTIME_CONFIG`, validates
the exact configured vector store contains indexed application data,
and constructs one retrieval engine before opening stdio. This makes a wrong
deployment attachment a visible startup failure instead of an apparently
healthy empty service.

## Persistent State

The main DuckDB database stores normalized messages, labels, threads, contacts,
derived facts, action items, deadlines, decisions, calendar events, ingestion
state, promotion provenance, and durable metadata. A companion database holds
person identity resolution and reverse message links.

The runtime filesystem also holds:

- `raw/` for locally retained message artifacts
- `cache/` for intermediary files
- `reports/` for owner-only generated operational output; local launchers use
  the packaged structured-artifact boundary rather than persisting raw command
  streams
- `config/promotion/` for runtime-local copies of customizable packaged assets
- persistent vector-index data at the exact configured storage path

These paths are runtime data and must be excluded from a public source archive.

## Data Flow

1. A local connector supplies message structure and content to explicit ingestion
   commands.
2. The ingestion service normalizes and persists records while tracking resumable
   progress.
3. Body processing improves message identity and thread membership when RFC
   headers are available.
4. Extraction derives structured facts, decisions, actions, deadlines, events,
   and summaries.
5. Retrieval combines lexical and vector search, returns provenance, and can
   produce citation-constrained answers.
6. Promotion creates auditable candidates for a separately configured downstream
   fact store.
7. Maintenance commands repair durable state, retry incomplete processing,
   reconcile indexes, and remove eligible expired records through explicit
   operations.

## Core Services

| Area | Responsibility |
| --- | --- |
| `store.py` | Database lifecycle, schema access, durable metadata, and maintenance operations. |
| `ingestion/service.py` | Incremental persistence, body recovery, message identity, and thread reconciliation. |
| `extraction/service.py` | Structured extraction from stored messages. |
| `entity_store.py` | Person identity resolution and message-to-entity links. |
| `retrieval/` | Embeddings, vector persistence, hybrid retrieval, grounded answers, and MCP entry point. |
| `promotion/` | Candidate selection, local policy assets, LLM planning, and writeback lifecycle. |
| `runtime.py` | Resolution of the explicit local runtime attachment. |
| `cli.py` | User-facing command parsing and service orchestration. |

## Operational Model

The command-line application is intentionally local. It does not discover data
sources or runtime directories on its own beyond the generic XDG fallback. The
public package owns the transactional deployment coordinator, immutable release
provisioner, MCP launcher, nightly maintenance pipeline, cron launcher, and
weekly alert batching. These operations load the owner-only local bundle; they
do not encode local values in the public scripts.

The transactional deploy and scheduler installation path is Linux-only today
because its trust and atomicity checks rely on GNU coreutils semantics and
`crontab`. Cross-platform accelerator support in the lower-level package
bootstrap does not change that deployment boundary.

Production deployment derives fixed configuration, data, and state roots from
the invoking user's canonical passwd home. It rejects ambient `HOME`/XDG roots
and a custom deployment root, preventing MCP and cron from resolving a different
release than the coordinator and doctor.

Deployment stages a release-local Python 3.14 environment and wheel under an
immutable versioned directory. It verifies the schema-v2 manifest, database and
runtime doctor, real mail authentication, selected LLM, MCP startup, maintenance
preflight, MCP launcher, and managed scheduler before atomically replacing the
`current` pointer. A redacted readiness receipt binds those checks to the active
release identity. The receipt lives inside the immutable candidate, so the
receipt and installed code become authoritative through the same atomic
`current` update. Transaction failures attempt to restore the prior active
release, MCP pointers, and crontab independently.

All installed entry points resolve code through the active release while exact
storage and executable locations come from the centralized schema-v2 manifest.
This preserves a stable operational surface without mutating an environment in
place or coupling durable data to package upgrades.

Hermes may be a configured LLM and notification executable, but email-memory
never controls the Hermes gateway lifecycle: it does not start, stop, restart,
reload, signal, or supervise that process.

See [Deployment](DEPLOYMENT.md) for the public transaction and operational
layout.

Commands that change stored data are explicit. Maintenance and recovery commands
report their work, and destructive cleanup requires an apply flag. The core keeps
durable state and local deployment control separate so that updating the package
does not expose or migrate private data by itself.

Provider failures are bounded and recorded as resumable or retryable state.
Full reconciliation compares every supported vector collection with its durable
source rows, while incremental indexing can make newly created records
searchable sooner. Cursor state distinguishes completed work from actionable
continuations without turning ordinary bounded scans into false failures.

## Public Release Invariants

A publishable tree must satisfy all of the following:

- no runtime databases, raw messages, indexes, caches, reports, or generated
  state
- no credentials, connection profiles, or local runtime manifests
- only generic package-owned deployment and scheduling scripts, with no local
  paths, identities, destinations, or operational history
- no personal identifiers, source-specific names, routing destinations, or local
  machine paths
- no production fixtures or operational history
- only synthetic test data and reserved example domains
- a fresh public Git history with no reachable private-history objects

The publishing process must validate these invariants against both the Git object
graph and a clean archive before a public push.

Enforcement is deliberately split. Hosted CI applies generic value-free rules
to tracked content, reachable history, the source archive, and built
distributions. A local-only release gate applies an ignored deployment-specific
denylist to the candidate repository and its complete reachable history. This
keeps exact private identifiers out of the public repository while still making
their absence a release requirement. See
[Privacy release controls](PRIVACY_RELEASE_CONTROLS.md).

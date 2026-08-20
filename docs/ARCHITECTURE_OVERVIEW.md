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
- the vector store at `<runtime_root>/chroma`

Separate local deployment configuration manages connector credentials, connection
profiles, scheduling, notifications, and any external fact-store integration.
Those settings are not part of the package runtime-manifest schema and are never
read from the checkout by default.

### Runtime resolution

The CLI resolves runtime fields once at process startup through this precedence
order:

1. `--root`, `--work-root`, and `--fact-store-db` command-line options
2. `--runtime-config <path>` local TOML manifest
3. `EMAIL_MEMORY_STORE_RUNTIME_CONFIG` pointing to that manifest
4. the generic XDG state default for `runtime_root`

The manifest can supply `runtime_root`, `work_root`, `fact_store_db`, and an
optional `runtime_provider` table. It is a location selector, not a credential
store. See [Configuration](CONFIGURATION.md).

The MCP launcher uses the same resolver and field precedence after an
attachment is selected, but it deliberately has no XDG fallback. It requires
`--root`, `--runtime-config`, or `EMAIL_MEMORY_STORE_RUNTIME_CONFIG`, validates
the existing `<runtime_root>/chroma` store contains indexed application data,
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
- persistent vector-index data colocated with the selected runtime

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
7. Maintenance commands repair indexes, retry failed body processing, reconcile
   every supported vector collection, normalize only provably completed legacy
   body cursors, and remove eligible expired time-bound records.

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
sources or runtime directories on its own beyond the generic XDG fallback. A
deployment wrapper may provide environment variables, a runtime manifest, and
connector setup, but that wrapper belongs in local-only operations material.

Commands that change stored data are explicit. Maintenance and recovery commands
report their work, and destructive cleanup requires an apply flag. The core keeps
durable state and local deployment control separate so that updating the package
does not expose or migrate private data by itself.

Mail-provider commands are bounded. A timed-out provider process is reaped and
reported through the same retryable failure path as another transient provider
error; it cannot hold a maintenance lock indefinitely. Initial and repair scans
preserve their resumable cursor on such a failure, while the bounded nightly
scan records the affected folder as partial and continues with the others.

`embed-backfill` is the authoritative full reconciliation pass. It compares each
supported retrieval collection with its persisted source rows, adds missing
vectors, and removes safe orphans. Incremental commands can embed newly created
records sooner, but do not replace the full reconciliation pass.

Initial envelope cursors drive resumable scans. Body cursors record body
processing health and are never independent continuation instructions. A normal
bounded nightly scan is complete even when its final page is full; only actual
body failures remain partial. `reconcile-ingestion-cursors --apply` is an
explicit maintenance operation for closing legacy body cursor residue when the
matching envelope scan is already complete and its retry queue is empty.

## Public Release Invariants

A publishable tree must satisfy all of the following:

- no runtime databases, raw messages, indexes, caches, reports, or generated
  state
- no credentials, connection profiles, local runtime manifests, or deployment
  wrappers
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

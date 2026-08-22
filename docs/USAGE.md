# Usage

[Documentation index](README.md) | [Deployment](DEPLOYMENT.md) |
[Configuration](CONFIGURATION.md)

This guide begins after installation. For a fresh transactional installation,
follow [First index and readiness](DEPLOYMENT.md#first-index-and-readiness)
through its staged initialization and readiness sequence before connecting an
MCP host. Do not substitute commands from this page for that first-run
transaction.

All examples use synthetic queries and generic paths. The runtime manifest,
mailbox data, account values, credentials, and notification destinations remain
in owner-only local storage outside the repository.

## Choose The Command Surface

Transactional deployment installs three distinct command surfaces:

| Surface | Typical path | Responsibility |
| --- | --- | --- |
| Application CLI | `<account-home>/.local/share/email-memory-store/current/venv/bin/email-memory-store` | Status, ingestion, extraction, indexing, retrieval, promotion, and data repair |
| Deployment control | `<account-home>/.local/share/email-memory-store/current/bin/email-memory-store-deploy` | Readiness doctor and package-owned nightly maintenance |
| Optional Hermes add-on | `<account-home>/.local/share/email-memory-store/current/venv/bin/email-memory-store-hermes-addon` | One-time installation of the packaged Telegram topic skill and exact MCP bindings |

All installed paths resolve through `current`, so they follow the atomically activated
release. Do not call a versioned directory beneath `envs/` directly.

A standalone or contributor bootstrap instead creates these checkout-local
entry points:

```text
./.venv/bin/email-memory-store
./.venv/bin/email-memory-store-mcp
./.venv/bin/email-memory-store-control-mcp
./.venv/bin/email-memory-store-hermes-addon
```

Those entry points are appropriate for standalone use and development, not for
operating a transactional deployment. Persistent launchers should execute an
installed entry point directly; do not wrap them in `uv run`.

For the remaining deployed examples, resolve the canonical account home and
central runtime manifest once:

```bash
email_memory_home="$(getent passwd "$(id -u)" | cut -d: -f6)"
email_memory_cli="$email_memory_home/.local/share/email-memory-store/current/venv/bin/email-memory-store"
email_memory_deploy="$email_memory_home/.local/share/email-memory-store/current/bin/email-memory-store-deploy"
runtime_config="$email_memory_home/.config/email-memory-store/runtime.toml"
```

Pass the same `--runtime-config "$runtime_config"` selection to every direct
application command. The examples below keep that selector visible.

The deployed control launcher loads its validated package-owned profile and
central attachment itself. It does not accept a second runtime selector.
Operational commands can print configured paths, provider metadata, or
email-derived text. Treat their terminal output, redirected JSON, logs,
screenshots, and copied results as private runtime data.

## Check health and progress

Start with read-only application status:

```bash
"$email_memory_cli" --runtime-config "$runtime_config" runtime-doctor
"$email_memory_cli" --runtime-config "$runtime_config" status
"$email_memory_cli" --runtime-config "$runtime_config" pipeline-status
"$email_memory_cli" --runtime-config "$runtime_config" extraction-status
"$email_memory_cli" --runtime-config "$runtime_config" embed-status
```

- `runtime-doctor` reports redacted storage and executable capability checks.
- `status` summarizes durable store contents.
- `pipeline-status` reports ingestion and processing progress.
- `extraction-status` compares extractable threads with derived records.
- `embed-status` reports vector-index coverage.

Use the deployment doctor to validate the complete active installation,
including the release identity, centralized configuration, required connectors,
MCP attachment, and managed scheduler:

```bash
"$email_memory_deploy" doctor
```

The doctor is stricter than application status. If a fresh runtime is staged
but not yet indexed, complete the exact sequence in
[First index and readiness](DEPLOYMENT.md#first-index-and-readiness) before
expecting `ready`.

## Ingestion and scheduled maintenance

Transactional deployment owns the nightly maintenance launcher and one managed
crontab block. Scheduled maintenance reads account and folder policy from the
owner-only validated configuration profile; public scripts contain no account,
folder, or destination values. The deployed nightly launcher always queues
failures into weekly alert batches, whether cron or an operator invokes it.

Run the same package-owned pipeline manually when diagnosing or catching up:

```bash
"$email_memory_deploy" nightly
```

This is a write workflow: it can ingest messages, update derived state, refresh
indexes, run configured promotions, and generate reports. Do not run it in
parallel with another maintenance writer. Consult
[Deployment](DEPLOYMENT.md#mcp-and-scheduled-maintenance) for scheduler and
locking behavior. Direct application commands do not inherit this launcher's
weekly alert context, but they are separate diagnostic or recovery surfaces—not
a substitute for the recommended deployed pipeline.

For initial ingestion, use the staged
[First index and readiness](DEPLOYMENT.md#first-index-and-readiness) procedure.
For standalone operation, inspect the direct command before supplying local
account values:

```bash
./.venv/bin/email-memory-store initial-ingest --help
./.venv/bin/email-memory-store nightly-update --help
```

Direct ingestion options can appear in the operating system's process listing.
Never place passwords, tokens, or other credential values in command arguments;
use the configured connector's credential mechanism.

## Extraction and indexing

Inspect coverage first, then process a bounded batch when needed:

```bash
"$email_memory_cli" --runtime-config "$runtime_config" extraction-status
"$email_memory_cli" --runtime-config "$runtime_config" extract-threads \
  --limit 100 --embed
"$email_memory_cli" --runtime-config "$runtime_config" embed-status
"$email_memory_cli" --runtime-config "$runtime_config" embed-backfill \
  --batch-size 100
```

`extract-threads --embed` writes derived records and immediately adds their
retrieval documents. `embed-backfill` reconciles missing vectors in bounded
batches. Re-run the two status commands to measure completion rather than
assuming a bounded command processed the entire store.

## Search, ask, and browse

Search uses hybrid lexical and semantic retrieval and does not require an LLM:

```bash
"$email_memory_cli" --runtime-config "$runtime_config" search \
  --query "decisions about the sample launch" \
  --effort medium --limit 10
```

`ask` uses the same retrieval layer and a configured optional LLM provider to
produce an answer with inline source handles:

```bash
"$email_memory_cli" --runtime-config "$runtime_config" ask \
  --query "What follow-up actions were agreed for the sample launch?" \
  --effort medium --limit 10
```

Open the terminal browser in read-only mode, or use a temporary snapshot while
another process may hold the database write lock:

```bash
"$email_memory_cli" --runtime-config "$runtime_config" browse --read-only
"$email_memory_cli" --runtime-config "$runtime_config" browse --snapshot
```

## MCP

Transactional deployment installs a stable package-owned MCP launcher. After
initial indexing, the readiness doctor validates its runtime-backed startup.
Register that launcher through the host's supported MCP configuration flow only
after the doctor reports `ready`. For a standalone bootstrap, register the
checkout-local entry point with an explicit manifest:

```text
/absolute/path/to/email-memory/.venv/bin/email-memory-store-mcp
    --runtime-config /absolute/path/to/runtime.toml
```

The retrieval server refuses to start without an explicit initialized
attachment. It exposes `search` without an LLM and `ask` with a configured
optional provider. The separate control mode exposes only redacted status and
fixed asynchronous operations; it never accepts a command line, executable,
path, environment value, or arbitrary action. See
[MCP Integration](MCP_INTEGRATION.md) for registration, tool schemas, provider
rules, failure behavior, and troubleshooting.

**Email-memory never controls the Hermes gateway lifecycle.** It may invoke
configured `hermes chat` and `hermes send` commands, but it never starts, stops,
restarts, reloads, signals, or supervises the gateway. Reconnect MCP through the
host's documented user-facing operation; gateway lifecycle remains outside this
project.

## Telegram Button Menu

The optional Hermes add-on routes one pre-created Telegram DM topic to the
packaged `email-memory` skill. After installation and MCP reload, a confirmed
`/new` must create the first new topic session so Hermes injects that automatic
skill. At the new session start, after a completed action, or when the owner
sends `menu`, Hermes presents Search, Ask, Status, and Operations through its
built-in `clarify` interface. The four choices render as native numbered inline
buttons, one per row, with Hermes's additional Other choice. This is
intentionally a compact surrogate interface, not a Telegram Mini App or a
custom keyboard implementation.

Search and Ask call only the retrieval server. Status calls the read-only
`system_status` tool and works when a fresh deployment is honestly staged at
`awaiting-index`; retrieval still fails closed until the first index is ready.
Operations offers Update, Retry failures, Reconcile, and Main menu. Each write
operation requires a second cancel-first confirmation, starts a fixed
asynchronous job, returns an opaque job ID, and can be checked with
`job_status`.

The topic is focused session routing and UX, not a capability sandbox or MCP
scope boundary. In the current single-profile design, both enabled MCP servers
are available to every Hermes-authorized platform session in that profile, not
only the Email Memory Telegram topic. Hermes's existing per-platform
authorization and tool policy therefore remain authoritative. The control
server is `untrusted`: Hermes approval gates write-capable `job_start`, while
the annotated read-only `system_status` and `job_status` tools may be
approval-exempt. Fixed actions, shared maintenance locking, redacted records,
and the absence of shell or gateway-lifecycle interfaces provide the server-side
boundary.

Send `menu` after an expired choice only once the new session has loaded the
skill. It cannot bootstrap the skill into a session that predates installation.
A started worker survives an ordinary MCP disconnect. A full Hermes service
restart can terminate the worker; the durable record then becomes
`worker_interrupted`, and the package never replays the operation automatically.
Review state before deliberately starting it again.

For prerequisite topic setup, owner-only routing configuration, installer
commands, the confirmed `/reload-mcp`, and the required confirmed `/new`, follow
[Hermes Telegram button menu](MCP_INTEGRATION.md#hermes-telegram-button-menu).

## Promotions

Promotion selection is separate from retrieval and requires an explicitly
configured local policy. Inspect the current configuration and preview bounded
candidate sets before recording anything:

```bash
"$email_memory_cli" --runtime-config "$runtime_config" promotion-status
"$email_memory_cli" --runtime-config "$runtime_config" select-promotions \
  --limit 20
"$email_memory_cli" --runtime-config "$runtime_config" promote-to-fact-store \
  --limit 20
```

Without `--record`, both selection commands are previews. Adding `--record`
writes promotion lifecycle records. LLM-assisted promotion and downstream
fact-store writes additionally require their configured local providers. Review
command-specific help and the [architecture boundary](ARCHITECTURE_OVERVIEW.md#data-flow)
before enabling those workflows.

## Repair and recovery

Begin with status and a preview whenever one exists. Run repair writers only
when scheduled maintenance and other database writers are idle.

Preview ingestion-cursor reconciliation:

```bash
"$email_memory_cli" --runtime-config "$runtime_config" \
  reconcile-ingestion-cursors
```

The command changes cursor state only with `--apply`. Other recovery commands
have narrower purposes and may contact the mail provider or rewrite durable
indexes; inspect their options before use:

```bash
"$email_memory_cli" --runtime-config "$runtime_config" retry-failed-bodies --help
"$email_memory_cli" --runtime-config "$runtime_config" repair-ingestion-state --help
"$email_memory_cli" --runtime-config "$runtime_config" repair-messages-index --help
"$email_memory_cli" --runtime-config "$runtime_config" repair-entity-index --help
"$email_memory_cli" --runtime-config "$runtime_config" repair-email-entity-index --help
```

Use [Configuration](CONFIGURATION.md#operational-artifacts) to locate the
owner-only structured reports that identify which stage failed. Provider
throttling and partial scans are recorded as resumable state; do not delete a
database or cursor merely because one bounded run did not finish all work.

## Preview cleanup

Retention cleanup is a dry run unless `--apply` is present. Preview a temporary
grace-period override without deleting records:

```bash
"$email_memory_cli" --runtime-config "$runtime_config" cleanup-expired \
  --grace-days 30
```

Review the reported candidates before repeating the command with `--apply`.
Applying cleanup deletes eligible derived records and removes their vectors.
Back up the owner-only runtime according to local policy before destructive
maintenance.

## Command help

The installed CLI is the authoritative command reference:

```bash
"$email_memory_cli" --help
"$email_memory_cli" search --help
"$email_memory_deploy" --help
```

For setup and upgrade procedures, return to [Installation](INSTALLATION.md) and
[Deployment](DEPLOYMENT.md). For storage, executable, and policy changes, use
[Configuration](CONFIGURATION.md) rather than duplicating paths in scripts.

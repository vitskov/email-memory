# Deployment

[Documentation index](README.md) | [Installation](INSTALLATION.md) |
[Post-install usage](USAGE.md)

## Scope

The repository includes the supported deployment coordinator, MCP launcher, and
nightly scheduler as package-owned public operations. A typical installation
starts from a clean public Git clone and keeps the checkout, immutable installed
releases, local configuration, and durable email state separate.

Email-memory never controls the Hermes gateway lifecycle. This is an absolute
lifecycle boundary: the deployment coordinator never starts, stops, restarts,
reloads, signals, or supervises that process. Email-memory may invoke configured
`hermes chat` and `hermes send` commands, but the operator or Hermes host remains
solely responsible for gateway lifecycle.

## Fresh Deployment

The transactional deployment path is currently Linux-only. It depends on GNU
`stat`, `readlink`, `mv`, and related coreutils behavior plus `crontab`.
Prerequisites are Git, `uv >= 0.12.5`, those Linux utilities, and the local mail
and LLM executables you select during configuration. `uv` installs a
release-local Python 3.14 interpreter; changing the system Python is neither
required nor recommended.

Production deployment resolves the invoking user's canonical passwd home from
the system account database and uses fixed roots beneath it:
`<canonical-user-home>/.config`, `.local/share`, and `.local/state`. It rejects
ambient `HOME`/XDG root changes and a custom deployment root. These fixed roots
keep the active launcher, MCP launcher, scheduler, configuration, and doctor on
one deployment identity.

From a clean public clone:

```bash
install -d -m 0700 "$HOME/.local/src"
umask 077
git clone https://github.com/vitskov/email-memory.git "$HOME/.local/src/email-memory"
cd "$HOME/.local/src/email-memory"
uv self update
./scripts/deploy.sh --accelerator auto
```

`deploy.sh` requires a clean, trusted checkout and a trusted absolute `uv`
executable. The owner-only source root and restrictive clone umask are required:
deployment rejects linked, hard-linked, foreign-owned, or group/world-writable
checkout paths and ancestors. On Linux, `--accelerator auto` chooses CUDA only when a usable
NVIDIA device is detected and CPU otherwise. Explicit `cpu` and `cuda` modes
fail closed when their requirements are not met. The lower-level package
bootstrap supports MPS on Apple silicon macOS, but the transactional
`deploy.sh` workflow does not currently support macOS.

On the first run, the coordinator opens `email-memory-store setup-private` to
create the local configuration bundle. Supply absolute durable storage paths,
absolute executable paths, local credential and alert references, and the mail
source policy. Credential values do not belong in the bundle. The deployment
does not treat a reference as proof of access: it also performs a real,
read-only mail authentication probe by proving that the configured account is
the connector's unique default and listing folders without placing its private
label in process arguments. It initializes the databases and requires
`runtime-doctor` to prove mail and the selected LLM are usable.

Alert delivery is selected from the closed generic enum `telegram`, `slack`, or
`discord`; no account or channel identifier belongs in public configuration.
The first-deployment bundle is written below
`<canonical-user-home>/.config/email-memory-store/`. When an optional local
fact-store module root is supplied, setup derives the exact public adapter
`email_memory_store.integrations.hermes_fact_store:MemoryStore`. It does not
accept an arbitrary provider import path.

### First index and readiness

Deployment never hides ingestion, extraction, promotion, or cleanup inside the
installation transaction. If the selected vector store already contains
indexed data, the coordinator proves live MCP startup and reports `ready`. If a
genuinely new installation has no indexed data, it instead activates a
structurally valid `awaiting-index` release. MCP remains fail-closed, while the
package-owned scheduler and launcher are installed for the first data run.

The bootstrap output is redacted JSON:

```json
{"schema_version": 1, "status": "awaiting-index", "paths_redacted": true}
```

For `awaiting-index`, run the installed maintenance pipeline once, then check
the complete deployment again:

```bash
email_memory_home="$(getent passwd "$(id -u)" | cut -d: -f6)"
email_memory_deploy="$email_memory_home/.local/share/email-memory-store/current/bin/email-memory-store-deploy"

"$email_memory_deploy" nightly
"$email_memory_deploy" doctor
```

The first maintenance run can take substantially longer than deployment because
it performs real mail ingestion, extraction, and indexing. Its failures use the
same ISO-week alert batch as scheduled maintenance. Do not connect an MCP host
until `doctor` prints `ready` and exits `0`. If no records are eligible for
indexing, the deployment remains honestly staged at `awaiting-index`; inspect
the pipeline and embedding status as described in [Usage](USAGE.md#check-health-and-progress).

## Transaction And Layout

The coordinator stages a new release beneath:

```text
<canonical-user-home>/.local/share/email-memory-store/
├── envs/<revision>-py314-<accelerator>/
│   ├── python/                release-local uv-managed Python
│   ├── venv/                  non-editable installed package
│   ├── bin/                   hardened operational launchers
│   ├── .deployment-readiness.json
│   └── .email-memory-release  immutable release marker
└── current -> envs/<release>  active release pointer
```

Each release is built from the locked dependencies, installed from a wheel, and
verified before activation. Persistent processes and cron use `current`, so a
successful activation is one atomic pointer replacement rather than an
in-place environment mutation. The deployment also installs a stable,
package-owned MCP launcher under `~/.local/bin/` whose internal `current`
pointer is updated transactionally.

Before activation, the coordinator verifies all of the following:

1. the release, package entry points, dependency graph, and accelerator receipt;
2. the schema-v2 runtime manifest and owner-only local configuration;
3. database initialization and redacted capability-aware runtime doctor output;
4. a real mail connector probe and optional fact-provider readiness;
5. nightly-maintenance preflight and either live MCP startup for an indexed
   runtime or an explicit deferred MCP state for a truly new empty runtime;
6. the installed MCP launcher and package-owned managed crontab block.

Only after those checks pass does it write the receipt inside the immutable
candidate and point `current` at that candidate. The receipt and code therefore
become active through the same atomic pointer replacement. A missing `current`
link is not enough to qualify for deferred readiness: existing managed cron,
MCP links, sibling releases, or release receipts prove prior deployment state
and make an empty-index activation fail closed.

## Central Runtime Contract

The owner-only `runtime.toml` schema version 2 is the single authoritative
location manifest. Its `[storage]` table names the exact main database, entity
database, vector store, and optional work/fact-store paths. Its
`[executables]` table names absolute mail and LLM executables. Normal runtime,
MCP, deployment probes, and scheduled maintenance all load that same manifest;
they do not rediscover executables from `PATH` or maintain a second provider
configuration layer. See [Configuration](CONFIGURATION.md).

The checkout and every installed release contain code only. Databases, vector
indexes, credentials, policy, and reports stay outside both.

## MCP And Scheduled Maintenance

Deployment installs the packaged MCP launcher and a single managed crontab
block. The default schedule is `30 2 * * *`; override it with
`--cron-schedule` followed by five validated cron fields. The cron command
resolves the active release through `current` and runs the packaged `nightly`
entry point.

Nightly maintenance performs runtime, mail, and LLM preflight before updating
and embedding data. It writes redacted structured JSONL reports in the private
runtime and serializes maintenance with a lock. The package-owned
`email-memory-store-deploy nightly` launcher always supplies an ISO-week batch
path, whether cron or an operator invokes it. Failures are delivered on the
configured weekly alert day; delivered batches receive `.sent` markers and are
pruned under the configured retention rules. Only direct low-level maintenance
scripts or application diagnostics outside that launcher lack its batch context;
they are not a substitute for the deployed pipeline.

The scheduler may use Hermes for LLM calls and alert delivery, but it never
manages the Hermes gateway process.

## Receipt And Doctor

A successful transaction writes an owner-only, redacted schema-version-3
receipt inside the immutable release. The canonical active receipt is selected
through:

```text
<canonical-user-home>/.local/share/email-memory-store/current/.deployment-readiness.json
```

The receipt records a release identity and pass, disabled, or narrowly bounded
deferred status for the staged release, configuration, databases, runtime
doctor, live mail probe, optional fact provider, MCP probe, maintenance
preflight, MCP launcher, scheduler, and activation. `deferred` is valid only for
the MCP check of a true first deployment whose aggregate index count is zero.
It contains no configured paths or private values.

Check the complete deployed surface through the active release with the
release-local `email-memory-store-deploy doctor` command:

```bash
email_memory_deploy_root="$(getent passwd "$(id -u)" | cut -d: -f6)/.local/share/email-memory-store"
"$email_memory_deploy_root/current/bin/email-memory-store-deploy" doctor
```

The doctor revalidates the receipt and release identity, `current`, MCP links,
the schema-v2 configuration, runtime capabilities, real mail access, optional
fact provider, exact managed cron block, current index count, and live MCP
startup when data exists. Its JSON output is redacted and has an automation-safe
exit contract:

| Exit | Status | Meaning |
| --- | --- | --- |
| `0` | `ready` | The complete deployment, including live MCP startup, is ready. |
| `1` | `not-ready` | A structural, configuration, integration, or readiness check failed. |
| `2` | `awaiting-index` | The first deployment is structurally valid but has no indexed data yet. |

## Failure And Rollback

Rollback is automatic within a deployment transaction. If MCP installation,
scheduler installation, activation, or a later transaction step fails, the
coordinator attempts every restoration independently: the previous crontab,
MCP launcher pointers, and active `current` pointer. A failed candidate is never
silently presented as ready, and a rollback failure is reported explicitly.

There is currently no public manual `rollback` subcommand. To return to an older
revision through the supported interface, check out that revision in a clean
public clone and run `./scripts/deploy.sh --accelerator auto`; it is staged and
validated as a new transaction before becoming current. Do not edit `current`,
MCP symlinks, a release-local receipt, or the managed crontab block independently.

## Upgrade

Deploy upgrades from a clean checkout rather than mutating the active release:

```bash
git pull --ff-only
uv lock --check
./scripts/deploy.sh --accelerator auto
```

Use `--regenerate-configuration` only when intentionally replacing the complete
local configuration bundle. Existing durable storage is selected by the
manifest and is not stored in or replaced with the release environment.

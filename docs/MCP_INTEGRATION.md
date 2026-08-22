# MCP Integration

[Documentation index](README.md) | [Typical deployment](DEPLOYMENT.md) |
[Post-install usage](USAGE.md)

`email-memory-store` includes two deliberately separate stdio MCP servers. The
retrieval server queries an existing local email index. The control server
reports redacted readiness and starts only three fixed asynchronous operations.
No server accepts shell commands, executable paths, environment values, or
arbitrary lifecycle actions.

## Before registration

Install the package and prepare the runtime through the CLI first. The MCP server
requires an existing Chroma index containing application data and never creates
an empty replacement during startup.

A transactional deployment can finish at `awaiting-index` when its new runtime
has no indexed data. Complete the first maintenance run in
[Deployment](DEPLOYMENT.md#first-index-and-readiness), then require deployment
doctor status `ready` before registration.

With a runtime manifest created as described in
[Configuration](CONFIGURATION.md), verify the attachment:

```bash
./.venv/bin/email-memory-store --runtime-config /path/to/runtime.toml status
./.venv/bin/email-memory-store --runtime-config /path/to/runtime.toml embed-status
```

If the vector collections are not populated, complete the ingestion, extraction,
and `embed-backfill` procedures appropriate to the local connector before
registering MCP.

## Register the stdio server

Transactional deployment installs this stable, package-owned launcher:

```text
<account-home>/.local/bin/email_memory_store_mcp_hermes.sh
```

Register that exact launcher with the MCP host and pass no arguments for the
retrieval server. The launcher resolves the active immutable release through
`current`, validates the owner-only local bundle, and supplies the runtime
manifest through the child environment so its private path does not appear in
process arguments. The same launcher accepts the exact `--mode control`
arguments for the optional bounded control server. Any other argument or mode
fails closed.

For a standalone or contributor bootstrap, configure the host to run the
checkout-local executable and pass one explicit runtime attachment. A host
configuration commonly has this shape:

```json
{
  "mcpServers": {
    "email-memory-store": {
      "command": "/path/to/email-memory/.venv/bin/email-memory-store-mcp",
      "args": [
        "--runtime-config",
        "/path/to/runtime.toml"
      ]
    }
  }
}
```

Adapt the outer configuration keys and generic paths to the MCP host. Keep the
runtime manifest and runtime data outside the source checkout; the executable
may remain in the bootstrap-created `.venv`. A direct runtime root is also
supported for standalone use:

```text
/path/to/email-memory/.venv/bin/email-memory-store-mcp --root <runtime-root>
```

The `EMAIL_MEMORY_STORE_RUNTIME_CONFIG` environment variable can select the
manifest when a host cannot pass arguments. Command-line selection takes
precedence. Do not put credentials or message-source policy in the MCP
registration; the manifest is a location selector, and private connector
configuration stays in the local bundle.

Run the installed entry point directly. Do not use `uv run` in a persistent MCP
registration, because it couples startup to a checkout and environment sync.

Email-memory installation and MCP launchers never restart, reload, signal, or
otherwise control the MCP host. Use the host's documented reconnect operation
after changing its registration or activating a new package release.

Email-memory never controls the Hermes gateway lifecycle: it never starts,
stops, restarts, reloads, signals, or supervises that process.

## Available tools

### Retrieval server

| Tool | Behavior | External LLM needed |
| --- | --- | --- |
| `search` | Hybrid lexical and semantic retrieval across facts, actions, deadlines, decisions, summaries, and message chunks; supports effort, date, and thread filters | No |
| `ask` | Retrieves context, asks a configured provider to synthesize an answer, and returns only citations whose handles appear in the answer | Yes |

Both tools use the retrieval engine constructed at server startup. If retrieval
for `ask` is empty, or the provider response does not contain valid inline
citation handles, the tool returns an insufficient-information response rather
than an unsupported answer.

The retrieval registration is full-trust only for the exact `search` and `ask`
allowlist. This does not grant the Telegram topic arbitrary MCP tools.

### Control server

| Tool | Behavior | Mutating |
| --- | --- | --- |
| `system_status` | Returns redacted deployment readiness and the current active-job summary | No |
| `job_start` | Accepts exactly one action from `maintenance`, `retry_failed_bodies`, or `reconcile`; returns an opaque job ID without waiting | Yes |
| `job_status` | Returns state, timestamps, and a bounded redacted result for one server-issued job ID | No |

The Hermes add-on registers the control server as `untrusted` and includes only
these three tools. Hermes approval gates the write-capable `job_start` tool in
addition to the button workflow's explicit confirmation. `system_status` and
`job_status` carry `readOnlyHint=true` and may be approval-exempt. `job_start`
deduplicates the same active action and rejects a different operation while one
is active. All write operations share the package-owned maintenance lock and
map from a fixed enum to in-process package behavior. The server cannot execute
a supplied shell command, select an executable, or accept local paths and
environment values.

Durable owner-only job records store no command output. A detached worker
continues across an ordinary MCP disconnect. A full Hermes service restart may
terminate that worker; the next status check records `worker_interrupted`, and
the package never replays the operation automatically.

## Hermes Telegram Button Menu

The optional add-on gives an existing generic Hermes installation a focused
Email Memory topic in the owner's Telegram direct-message chat. It installs a
public packaged skill and supported Hermes configuration only. It does not
patch Hermes, create a Telegram Mini App, install a custom callback handler, or
manage the gateway process.

Hermes's built-in `clarify` tool supplies the interaction surface. Each menu has
at most four configured choices, rendered as numbered native inline buttons one
per row, plus Hermes's Other choice. The main menu is Search, Ask, Status, and
Operations. A result menu can offer Search again, Ask about this, Main menu, and
Exit. This compact vertical choice list is the intended surrogate application;
there is no custom horizontal keyboard.

### Prerequisites

Complete these prerequisites before running the add-on installer:

1. Finish the core transactional deployment. `doctor` may report `ready` or the
   honest first-install state `awaiting-index`; Status remains usable in either
   case, while Search and Ask remain fail-closed until indexed data exists.
2. Use an already configured and authorized Hermes Telegram bot. The add-on
   fails closed unless the Telegram allowlist contains exactly the one numeric
   owner-DM identifier being bound; wildcard, username, or multi-user access is
   rejected. Hermes authorization remains authoritative after installation.
3. In the bot's private chat, the owner enables Telegram Topics and creates the
   topic that will be used for Email Memory. The package cannot enable Topics or
   create the external topic.
4. Through the existing authorized Telegram/Hermes administration path, obtain
   the positive numeric owner-DM chat ID and positive numeric topic thread ID.
   Do not expose the bot token or paste either identifier into a public file,
   command line, issue, or log.
5. Run the central `setup-private` form and supply both optional Telegram menu
   fields. A fresh deployment opens that form automatically. For an existing
   transactional installation, rerun deployment from a clean checkout with
   `--regenerate-configuration`, re-enter the complete local bundle, and confirm
   replacement:

```bash
./scripts/deploy.sh --accelerator auto --regenerate-configuration
```

Setup records the identifiers only in the separate owner-only
`hermes-addon.json`. They never enter schema-version-1 `private.env.json`, the
runtime manifest, policy, installed release, public checkout, or Git history.
Installation later copies the selected binding into the separate owner-only
Hermes configuration. The package never reads or stores the Telegram bot token.

### Install and activate

Run the installer through the active immutable release:

```bash
email_memory_home="$(getent passwd "$(id -u)" | cut -d: -f6)"
email_memory_addon="$email_memory_home/.local/share/email-memory-store/current/venv/bin/email-memory-store-hermes-addon"

"$email_memory_addon"
```

The installer validates the owner-only attachment, configured Hermes
executable, stable package-owned MCP launcher, active Hermes configuration, and
requested topic binding. It then transactionally installs the `email-memory`
skill and exact server registrations:

| Registration | Launcher invocation | Trust and tool allowlist |
| --- | --- | --- |
| `email_memory_store` | stable launcher with no arguments | `full`; `search`, `ask` only |
| `email_memory_store_control` | stable launcher with `--mode control` | `untrusted`; `system_status`, `job_start`, `job_status` only |

The add-on owns these names. Installation rejects an existing `email-memory`
skill whose content is not the packaged skill, an existing
`email_memory_store_control` registration that is not the exact packaged
control configuration, or an `email_memory_store` registration that does not
point to the selected stable launcher with no arguments. A package-owned core
retrieval registration may be hardened to the exact trust, timeout, and tool
allowlist shown above; an unrelated registration is never overwritten.

An owner-only add-on lock serializes Email Memory add-on install and disable
operations with each other. It is not a shared Hermes lock: ordinary
`hermes config` commands and other Hermes configuration writers do not acquire
it. Do not run any other Hermes configuration mutation while add-on install or
disable is in progress.

The structured writer uses a configuration digest as a compare-and-swap guard,
and failure recovery restores prior bytes only when the current digest still
matches the add-on's last write. If a concurrent change is detected, the
transaction fails closed, control jobs remain disabled, and a later unrelated
edit is preserved rather than overwritten by rollback. This is conditional
rollback, not an absolute rollback guarantee against an uncoordinated writer;
inspect the Hermes configuration and resolve the conflict before retrying.

The installer does not start, stop, restart, reload, signal, or supervise the
Hermes gateway. Private routing values are supplied to the bounded
configuration writer through standard input, not child-process arguments, and
are not printed or logged. After success, the setup attachment remains the
Email Memory source and the owner-only Hermes configuration contains the active
copy used for DM-topic routing.

### Profile scope

Both registrations are enabled at the active Hermes profile level. In the
current single-profile design, `search`, `ask`, `system_status`, `job_start`, and
`job_status` are therefore available to every Hermes-authorized platform
session in that profile, not only messages in the Email Memory Telegram topic.
The exact tool allowlists limit each server, but the topic does not scope the
registrations.

Retrieval is registered as `full`, so an authorized session can call `search`
or `ask` without the control-server approval boundary. Control is registered as
`untrusted`, preserving Hermes approval as an independent guard for the
write-capable `job_start` call in addition to fixed action validation, locking,
and the topic skill's cancel-first confirmation. The read-only `system_status`
and `job_status` tools may be approval-exempt. Operators must set Hermes
authorization for every enabled platform accordingly.

After a successful install, send `/reload-mcp` through Hermes and complete its
built-in confirmation prompt when MCP reload confirmation is enabled. The
command ignores trailing words; adding `now` does not bypass or complete the
confirmation. Then open the configured Email Memory topic, send `/new`, and
complete that command's confirmation when prompted. A newly created session is
required because Hermes injects the topic's automatic skill only during session
creation.

After that first confirmed `/new`, `menu` restores the main choices following a
timeout, interrupted prompt, or completed workflow. It cannot load the skill
into a topic session that predates installation. The Email Memory package does
not send or confirm either owner command.

### Disable

Check that no operation is active, then disable the add-on through the same
installed entry point:

```bash
"$email_memory_addon" --disable
```

Disable first closes the owner-only control activation switch, then removes only
the add-on-owned topic binding, control MCP registration, and packaged skill. It
leaves the retrieval MCP registration, email databases, indexes, durable job
history, and private configuration attachment intact. A worker that was already
running is not terminated, which is why the status check comes first. The same
add-on-only serialization, digest compare-and-swap, conditional rollback, and
fail-closed conflict behavior described above applies to disable. Do not run
`hermes config` or another Hermes configuration writer concurrently with it.

Send `/reload-mcp` through Hermes after disabling and complete its built-in
confirmation when enabled so the active MCP set reflects the remaining
retrieval registration. The command still does not control the gateway
lifecycle.

This teardown is mandatory before deploying or downgrading to a revision that
predates add-on/control support. In order:

1. Use Status and verify `active_job` is empty. Do not disable while a control
   worker is active.
2. Run the current release's `"$email_memory_addon" --disable` while that
   executable still exists.
3. Send `/reload-mcp` through Hermes and complete its confirmation when enabled.
4. Only then check out and deploy the pre-add-on revision.

Reversing that order can leave an external Hermes control registration and
skill pointing at an older stable launcher that cannot accept `--mode control`,
while the older release no longer contains the supported disable command.
Automatic transactional rollback to a previous add-on-capable release is
unaffected because that release retains the control launcher and disable
contract.

### Interaction and safety contract

- Search and Ask use only the retrieval MCP server. Normal text or Other is the
  query input; the skill does not invent a query.
- Status uses only `system_status`, does not reveal paths or raw logs, and then
  restores the main menu.
- Operations presents Update, Retry failures, Reconcile, and Main menu. Update
  maps to `maintenance`; Retry failures maps to `retry_failed_bodies`; Reconcile
  maps to `reconcile`.
- Every mutation requires a second prompt whose recommended first choice is
  Cancel. Timeout is cancellation. After confirmation, the skill returns the
  opaque asynchronous job ID instead of holding the Telegram request open.
- The control server enforces fixed actions, owner-only state, shared locks,
  redacted output, no shell execution, and no gateway-lifecycle operation even
  if a prompt or model behaves unexpectedly.

The topic binding is a user-experience and session-routing boundary, not a hard
capability sandbox or MCP scope boundary. Any Hermes-authorized platform session
in the same profile can reach the enabled MCP allowlists. The topic skill cannot
replace Hermes's own authorization or policy. Operators who require a strict
capability boundary must enforce it with a separately scoped Hermes profile or
equivalent Hermes isolation; the single-profile add-on makes no stronger claim.

## Optional LLM providers

For a standalone package installation, Hermes is not needed to build the index,
start the MCP server, or call `search`. The `ask` tool and LLM-assisted promotion
commands need one of these separately installed command-line providers:

| Provider value | Invocation | Model rule |
| --- | --- | --- |
| `hermes-default` | `hermes chat` | The provider default may be used; a model can be supplied |
| `codex-cli` | `codex exec` | An explicit model is required |
| `claude-code-cli` | `claude` | An explicit model is required |

When an `ask` call omits `provider`, `hermes-default` is selected. An MCP host
that uses only `search` does not need any LLM provider. Provider installation,
authentication, model access, and process policy are external to this package
and should be configured according to the chosen provider's documentation. MCP
uses only the provider's absolute executable path from `runtime.toml`; it never
falls back to command-name lookup on `PATH`.

The transactional deployment is intentionally stricter than MCP itself. Its
package-owned maintenance contract requires a configured Hermes executable for
alert delivery and one selected LLM provider, which may be Hermes, Codex CLI, or
Claude Code CLI. This is a deployment requirement, not an MCP `search`
requirement.

### Hermes roles and boundary

Hermes can participate in three independent ways: as an MCP host that calls
`search` or `ask`, as the optional `hermes-default` LLM provider, and as a
configured alert transport used by package-owned maintenance. None of those
roles transfers gateway ownership to this project. Email-memory never starts,
stops, restarts, reloads, signals, or supervises the Hermes gateway.

## Startup and Failure Behavior

Before the retrieval server opens stdio, the launcher resolves the runtime once
and validates that:

- an explicit `--root`, `--runtime-config`, or
  `EMAIL_MEMORY_STORE_RUNTIME_CONFIG` selection exists;
- the manifest is readable and valid when selected;
- the exact configured `storage.vector_store` is an initialized, non-symlinked
  Chroma store; and
- at least one supported collection contains indexed data.

Failure exits with status `2` and a redacted configuration message. Repair the
attachment or index instead of pointing the host at a new empty directory.

Control mode instead validates the owner-only deployment attachment and job
state boundary. It can open while the deployment is `awaiting-index` so Status
can explain the staged condition. This does not weaken retrieval startup:
`search` and `ask` still cannot attach to an empty index.

## Troubleshooting

- **The server reports that an explicit attachment is required.** Add `--root`
  or `--runtime-config` to the host registration, or set the manifest environment
  variable in the host process.
- **The runtime has no initialized store or indexed data.** Verify the same
  manifest with CLI `status` and `embed-status`, then run the relevant indexing
  procedure.
- **`search` works but `ask` fails.** Verify that the selected LLM command is
  installed and authenticated in the MCP host's environment. Pass both provider
  and model when the selected provider requires a model.
- **A new version is installed but the host still exposes the old one.** Confirm
  the registered executable path, run its `--help` command outside the host, and
  use the host's supported MCP reconnect operation. Package installation should
  not restart or signal the host application.
- **The add-on says the Telegram menu is not configured.** Enable Topics in the
  owner bot DM, create the topic, then regenerate the complete private setup with
  both positive numeric routing identifiers. Confirm that the owner-only
  `hermes-addon.json` exists without printing its contents.
- **The topic opens but the menu is absent.** Send `/reload-mcp` through Hermes
  and complete its confirmation when enabled. Then send `/new` in the configured
  topic and complete that command's confirmation. `menu` is a recovery command
  only after the new session has loaded the skill; it cannot repair a
  pre-install session. Recheck Telegram authorization and the topic binding if
  routing still does not activate.
- **A control job reports `worker_interrupted`.** A full Hermes service restart
  may have terminated the detached worker. Inspect current Email Memory status
  and the affected operation before deliberately confirming a new run; jobs are
  never replayed automatically.

For public-core bootstrap and package upgrade checks, see
[Installation](INSTALLATION.md). For runtime precedence and permission checks,
see [Configuration](CONFIGURATION.md). For the package/runtime trust boundary,
see the [Architecture Overview](ARCHITECTURE_OVERVIEW.md).

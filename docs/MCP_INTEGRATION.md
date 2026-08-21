# MCP Integration

[Documentation index](README.md) | [Typical deployment](DEPLOYMENT.md) |
[Post-install usage](USAGE.md)

`email-memory-store` includes a stdio MCP server for querying an existing local
email index from an MCP-compatible host. The server exposes retrieval; ingestion,
indexing, repairs, and other lifecycle operations remain CLI responsibilities.

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

Register that exact launcher with the MCP host and pass no arguments. The
launcher resolves the active immutable release through `current`, validates the
owner-only local bundle, and supplies the runtime manifest through the child
environment so its private path does not appear in process arguments.

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

## Available tools

| Tool | Behavior | External LLM needed |
| --- | --- | --- |
| `search` | Hybrid lexical and semantic retrieval across facts, actions, deadlines, decisions, summaries, and message chunks; supports effort, date, and thread filters | No |
| `ask` | Retrieves context, asks a configured provider to synthesize an answer, and returns only citations whose handles appear in the answer | Yes |

Both tools use the retrieval engine constructed at server startup. If retrieval
for `ask` is empty, or the provider response does not contain valid inline
citation handles, the tool returns an insufficient-information response rather
than an unsupported answer.

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

## Startup and failure behavior

Before opening stdio, the launcher resolves the runtime once and validates that:

- an explicit `--root`, `--runtime-config`, or
  `EMAIL_MEMORY_STORE_RUNTIME_CONFIG` selection exists;
- the manifest is readable and valid when selected;
- the exact configured `storage.vector_store` is an initialized, non-symlinked
  Chroma store; and
- at least one supported collection contains indexed data.

Failure exits with status `2` and a redacted configuration message. Repair the
attachment or index instead of pointing the host at a new empty directory.

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

For public-core bootstrap and package upgrade checks, see
[Installation](INSTALLATION.md). For runtime precedence and permission checks,
see [Configuration](CONFIGURATION.md). For the package/runtime trust boundary,
see the [Architecture Overview](ARCHITECTURE_OVERVIEW.md).

# Documentation

Use this page to find the shortest path through the Email Memory Store guides.
The repository documents a typical installation; it does not assume access to
any particular machine, mailbox, or private deployment.

## New User

1. Read [Installation](INSTALLATION.md) for prerequisites, Python 3.14, `uv`,
   accelerator choices, and the difference between transactional deployment and
   standalone bootstrap.
2. Follow [Deployment](DEPLOYMENT.md) for the supported Linux installation,
   first-run setup, staged initialization, readiness checks, and rollback
   behavior.
3. Use [Configuration](CONFIGURATION.md) when choosing durable storage,
   connector executables, local policy, or credential references.
4. Continue with [Usage](USAGE.md) for post-install health checks, ingestion,
   indexing, retrieval, MCP, promotion previews, and recovery commands.
5. If an assistant will query the store, finish with
   [MCP Integration](MCP_INTEGRATION.md).
6. To add a focused Telegram interface to an existing Hermes installation,
   follow [Hermes Telegram button menu](MCP_INTEGRATION.md#hermes-telegram-button-menu)
   after core deployment and indexing are healthy.

## Operator

- [Usage](USAGE.md) separates application commands from deployment-control
  commands and identifies preview, write, and optional-provider workflows.
- [Deployment](DEPLOYMENT.md) defines the immutable release layout, active
  `current` pointer, managed scheduler, readiness receipt, doctor, and automatic
  transaction rollback.
- [Configuration](CONFIGURATION.md) defines the central runtime manifest,
  owner-only local bundle, regeneration procedure, and operational artifacts.
- [MCP Integration](MCP_INTEGRATION.md) covers attachment validation, tool
  behavior, provider requirements, startup troubleshooting, and the optional
  Hermes Telegram topic add-on.

Email Memory Store may invoke configured Hermes commands, but it never starts,
stops, restarts, reloads, signals, or supervises the Hermes gateway. Gateway
lifecycle remains the responsibility of the Hermes host.

## Contributor Or Maintainer

- Start with the [Architecture Overview](ARCHITECTURE_OVERVIEW.md) before
  changing storage, ingestion, retrieval, runtime resolution, deployment, or
  trust boundaries.
- Use the development and packaging sections of
  [Installation](INSTALLATION.md) to create the locked development environment,
  run local CI, and build distributions.
- Apply [Privacy Release Controls](PRIVACY_RELEASE_CONTROLS.md) to the working
  tree, reachable Git history, source archive, and built distributions before
  publication. The local identifier denylist stays outside Git.

## Terms

| Term | Meaning |
| --- | --- |
| Public checkout | The Git clone containing publishable code, synthetic tests, package metadata, and generic documentation. It contains no runtime data. |
| Local configuration bundle | Owner-only manifest, policy, and credential references created outside the checkout. Credential values remain in their provider's store. |
| Runtime manifest | The schema-version-2 TOML file that centrally selects exact database, vector-store, and executable paths. |
| Runtime state | Durable messages, derived records, databases, vector indexes, caches, and reports stored outside the checkout and installed releases. |
| Release | An immutable, versioned Python environment and non-editable package installation created by transactional deployment. |
| `current` | The atomic pointer to the active deployed release. Persistent launchers resolve through it instead of naming a versioned release directly. |
| Deployment control | The packaged `email-memory-store-deploy` launcher used for readiness checks and package-owned maintenance. It is distinct from the application CLI. |
| MCP attachment | The explicit initialized runtime selected when the MCP server starts. MCP has no implicit empty-runtime fallback. |
| Control MCP | A separate redacted server with only `system_status`, `job_start`, and `job_status`; it cannot accept commands, paths, or arbitrary arguments. |
| Hermes Telegram add-on | An optional packaged skill and DM-topic configuration for an existing generic Hermes installation. It is focused UX, not a Hermes core patch, capability sandbox, or MCP scope boundary. |
| Hermes add-on attachment | The optional owner-only `hermes-addon.json` file containing the local Telegram owner-DM chat and topic identifiers. It is separate from `private.env.json`. |
| Promotion | Auditable selection or processing of derived records for an optional separately configured downstream fact store. |

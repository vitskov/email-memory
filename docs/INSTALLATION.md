# Installation

## Installation Contract

The GitHub repository is the source of truth for public code, package metadata,
the dependency lock, tests, and generic documentation. A local installation has
four separate layers:

| Layer | Contents | Version controlled |
| --- | --- | --- |
| Public checkout | Source, tests, `pyproject.toml`, and `uv.lock` | Yes |
| Python environment | Interpreter and installed packages | No; reproducible |
| Local configuration | Runtime manifest, connector policy, credential references | No |
| Runtime state | Databases, vector indexes, caches, and reports | No |

The package never requires configuration, credentials, or runtime state to be
placed in its Git checkout. A fresh clone can be built and tested with synthetic
fixtures before any private attachment exists.

## Prerequisites

- Git
- `uv >= 0.12.5`
- A supported operating system for the locked binary dependencies
- Local mail and model connectors only when their corresponding workflows are
  used

Update an existing `uv` installation with:

```bash
uv self update
```

The project requires Python `>= 3.14` and pins `3.14` as its default development
and deployment interpreter. `uv` manages the interpreter, so a system-wide
Python installation is not required:

```bash
uv python install 3.14
uv python find 3.14
```

## Python Package Dependencies

Direct runtime requirements are declared in `pyproject.toml`:

| Package | Purpose |
| --- | --- |
| `duckdb` | Durable relational and analytical state |
| `textual` | Terminal user interfaces |
| `chromadb` | Persistent vector collections |
| `sentence-transformers` | Local embedding models |
| `torch` | CPU tensor runtime used by embeddings |
| `dateparser` | Date normalization |
| `mcp` | MCP stdio service protocol |

The committed `uv.lock` pins these requirements and every transitive package.
The repository's `uv` bootstrap resolves PyTorch from the explicit CPU wheel
index and does not download CUDA or NVIDIA runtime packages. Standard Python
wheel metadata cannot encode a package index: consumers installing the wheel
outside this locked `uv` workflow must select the appropriate PyTorch index
themselves and do not receive the repository's exact transitive dependency
graph. Development tools (`pytest`, `pytest-asyncio`, `ruff`, and `mypy`) are in
the `dev` extra and are also locked.

## Clone And Bootstrap

For a runtime installation:

```bash
git clone https://github.com/vitskov/email-memory.git
cd email-memory
./scripts/bootstrap.sh
```

The default `--accelerator auto` mode selects an Apple MPS device on Apple
silicon macOS, an NVIDIA CUDA build only when `nvidia-smi` confirms a usable GPU
and driver, and CPU otherwise. Override the decision when provisioning:

```bash
./scripts/bootstrap.sh --accelerator cpu
./scripts/bootstrap.sh --accelerator cuda
./scripts/bootstrap.sh --accelerator mps
```

Explicit `cuda` and `mps` selections fail closed when the requested device is
unavailable. CUDA mode keeps the project versions pinned by `uv.lock` while
using `uv pip --torch-backend auto` to choose the driver-compatible official
PyTorch wheel. MPS uses the standard macOS PyTorch wheel. The verified result is
recorded at `<environment>/share/email-memory-store/accelerator.json`; the
embedding runtime reads that receipt and selects the same device. The receipt
contains only the backend, device, and PyTorch version.

The script performs these fail-closed steps:

1. verifies `uv` is available and satisfies the project requirement;
2. installs a managed Python 3.14 interpreter;
3. runs `uv sync --locked --no-editable` against the committed lock;
4. applies and verifies the requested PyTorch accelerator profile;
5. checks the installed dependency graph;
6. imports the package and smoke-tests both console entry points;
7. records the verified accelerator selection inside the environment.

The default runtime environment is `.venv`, which is ignored by Git and contains
no runtime data or credentials. Runtime mode installs a non-editable build, so
subsequent checkout edits cannot change the installed code. `--dev` intentionally
uses an editable install for contributor iteration.

For development:

```bash
./scripts/bootstrap.sh --dev
./scripts/run_ci_locally.sh
```

## Local Configuration

Create the owner-only configuration bundle with the installed CLI:

```bash
./.venv/bin/email-memory-store setup-private
```

The wizard writes configuration under
`${XDG_CONFIG_HOME:-$HOME/.config}/email-memory-store/` with a `0700` directory
and `0600` files. Store only credential references there, never credential
values. See [Configuration](CONFIGURATION.md) for the schemas and precedence
rules.

Initialize and inspect the configured runtime:

```bash
RUNTIME_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/email-memory-store/runtime.toml"
./.venv/bin/email-memory-store --runtime-config "$RUNTIME_CONFIG" init-db
./.venv/bin/email-memory-store --runtime-config "$RUNTIME_CONFIG" runtime-doctor
./.venv/bin/email-memory-store --runtime-config "$RUNTIME_CONFIG" \
  runtime-doctor --require mail --require selected-llm
./.venv/bin/email-memory-store --runtime-config "$RUNTIME_CONFIG" status
```

Message ingestion and indexing require the locally selected connector and
policy. The setup form suggests installed command paths, but runtime operations
use only the absolute executables recorded in `runtime.toml`; they are
intentionally not inferred from the checkout or rediscovered from `PATH`.

## MCP Registration

Register the installed executable and pass the runtime manifest explicitly:

```text
/absolute/path/to/email-memory/.venv/bin/email-memory-store-mcp
    --runtime-config /absolute/path/to/runtime.toml
```

The MCP service refuses to start if the attachment is absent, weakly
permissioned, symlinked, invalid, or lacks indexed application data. Validate a
new executable with the MCP host's standalone probe before reconnecting it.

Email-memory installation and upgrade scripts must never stop, restart, reload,
signal, or otherwise control the host application. Reconnect through the host's
documented user-facing MCP operation.

## Upgrade

Review dependency upgrades separately from ordinary source updates:

```bash
git pull --ff-only
uv lock --check
./scripts/bootstrap.sh
./scripts/run_ci_locally.sh
```

`bootstrap.sh` uses `uv sync --locked --no-editable`, so it refuses a checkout
whose package metadata and lock disagree and does not couple the installed code
to the checkout. Maintainers intentionally refresh dependencies with
`uv lock --upgrade`, run all checks on Python 3.14, review the lock diff, and
commit the metadata and lock together.

The static-analysis gate intentionally uses the installed dependency types; it
does not pass `--no-site-packages` or suppress third-party imports. Run the
same check that hosted CI runs with:

```bash
uv run --locked --extra dev mypy --config-file pyproject.toml src
```

`uv sync` restores the baseline locked environment, including its CPU PyTorch
selection on non-macOS hosts. Always use `bootstrap.sh` to rebuild a deployment
environment so a selected CUDA overlay is reapplied, verified, and recorded.
Run installed entry points directly rather than putting `uv run` in a persistent
MCP registration.

## Build A Wheel

Build standard source and wheel distributions with:

```bash
uv build --no-sources --python 3.14
```

Artifacts are written to `dist/`. Package verification must install the wheel,
not only the editable checkout, and smoke-test both `email-memory-store` and
`email-memory-store-mcp` entry points.

## Troubleshooting

Check the interpreter and dependency graph:

```bash
uv run --locked python --version
uv pip check --python .venv/bin/python
```

If `uv sync --locked` reports that the lock is stale, do not bypass it in a
deployment. Update the checkout or have a maintainer regenerate and review the
lock. If an MCP attachment fails validation, repair the local configuration or
index; do not allow the service to create a replacement default store.

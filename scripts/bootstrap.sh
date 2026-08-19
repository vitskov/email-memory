#!/usr/bin/env bash
# Reproduce the locked public package environment without reading private data.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE=runtime
PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$ROOT_DIR/.venv}"
ACCELERATOR=auto
UV_BIN="${UV_BIN:-uv}"

usage() {
  cat <<'EOF'
Usage: scripts/bootstrap.sh [--dev] [--environment PATH] [--accelerator MODE]

  --dev               Install the locked developer tools and test dependencies.
  --environment PATH  Place the virtual environment at PATH instead of .venv.
  --accelerator MODE   Select auto, cpu, cuda, or mps (default: auto).
EOF
}

while (( $# )); do
  case "$1" in
    --dev)
      MODE=dev
      shift
      ;;
    --environment)
      if [[ $# -lt 2 || -z "$2" ]]; then
        usage >&2
        exit 2
      fi
      PROJECT_ENVIRONMENT=$2
      shift 2
      ;;
    --accelerator)
      if [[ $# -lt 2 || -z "$2" ]]; then
        usage >&2
        exit 2
      fi
      ACCELERATOR=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

case "$ACCELERATOR" in
  auto|cpu|cuda|mps) ;;
  *)
    echo "unsupported accelerator: $ACCELERATOR" >&2
    usage >&2
    exit 2
    ;;
esac

has_nvidia_gpu() {
  command -v nvidia-smi >/dev/null 2>&1 \
    && nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null \
      | grep -q '[^[:space:]]'
}

detected_accelerator() {
  if [[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]]; then
    printf 'mps\n'
  elif has_nvidia_gpu; then
    printf 'cuda\n'
  else
    printf 'cpu\n'
  fi
}

REQUESTED_ACCELERATOR=$ACCELERATOR
if [[ "$ACCELERATOR" == "auto" ]]; then
  ACCELERATOR=$(detected_accelerator)
fi

if [[ "$ACCELERATOR" == "cuda" ]] && ! has_nvidia_gpu; then
  echo "CUDA was selected, but no usable NVIDIA GPU/driver was detected" >&2
  exit 1
fi
if [[ "$ACCELERATOR" == "mps" ]] \
  && [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "MPS was selected, but this is not an Apple silicon macOS host" >&2
  exit 1
fi

if ! command -v "$UV_BIN" >/dev/null 2>&1; then
  echo "uv is required; install it from https://docs.astral.sh/uv/" >&2
  exit 1
fi

cd "$ROOT_DIR"
export UV_PROJECT_ENVIRONMENT="$PROJECT_ENVIRONMENT"

"$UV_BIN" python install 3.14

sync_args=(sync --locked --python 3.14)
if [[ "$MODE" == dev ]]; then
  sync_args+=(--extra dev)
else
  sync_args+=(--no-editable)
fi
"$UV_BIN" "${sync_args[@]}"

python_bin="$PROJECT_ENVIRONMENT/bin/python"
cli_bin="$PROJECT_ENVIRONMENT/bin/email-memory-store"
mcp_bin="$PROJECT_ENVIRONMENT/bin/email-memory-store-mcp"

if [[ "$ACCELERATOR" == "cuda" ]]; then
  torch_version=$(
    "$python_bin" -c \
      'from importlib.metadata import version; print(version("torch").partition("+")[0])'
  )
  if ! "$UV_BIN" pip install \
    --python "$python_bin" \
    --torch-backend auto \
    --reinstall \
    "torch==$torch_version"; then
    echo "CUDA package selection failed; restoring the locked CPU environment" >&2
    "$UV_BIN" "${sync_args[@]}"
    if [[ "$REQUESTED_ACCELERATOR" != "auto" ]]; then
      exit 1
    fi
    ACCELERATOR=cpu
  fi
fi

if ! "$python_bin" - "$ACCELERATOR" <<'PY'
import sys

import torch

device = sys.argv[1]
available = {
    "cpu": True,
    "cuda": torch.cuda.is_available(),
    "mps": bool(getattr(torch.backends, "mps", None))
    and torch.backends.mps.is_available(),
}[device]
raise SystemExit(0 if available else 1)
PY
then
  if [[ "$REQUESTED_ACCELERATOR" != "auto" ]]; then
    if [[ "$ACCELERATOR" == "cuda" ]]; then
      "$UV_BIN" "${sync_args[@]}"
    fi
    echo "$ACCELERATOR was selected, but PyTorch cannot use it" >&2
    exit 1
  fi
  echo "$ACCELERATOR is unavailable to PyTorch; using CPU" >&2
  if [[ "$ACCELERATOR" == "cuda" ]]; then
    "$UV_BIN" "${sync_args[@]}"
  fi
  ACCELERATOR=cpu
fi

"$UV_BIN" pip check --python "$python_bin"
"$python_bin" -c 'import email_memory_store'
"$cli_bin" --help >/dev/null
"$mcp_bin" --help >/dev/null

"$python_bin" - "$ACCELERATOR" <<'PY'
import json
from importlib.metadata import version
from pathlib import Path
import sys

device = sys.argv[1]
torch_version = version("torch")
receipt = Path(sys.prefix) / "share" / "email-memory-store" / "accelerator.json"
receipt.parent.mkdir(parents=True, exist_ok=True)
receipt.write_text(
    json.dumps(
        {
            "schema_version": 1,
            "backend": device,
            "device": device,
            "torch_version": torch_version,
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY

printf 'email-memory-store environment ready: %s\n' "$PROJECT_ENVIRONMENT"
printf 'python: %s\n' "$("$python_bin" --version 2>&1)"
printf 'accelerator: %s\n' "$ACCELERATOR"

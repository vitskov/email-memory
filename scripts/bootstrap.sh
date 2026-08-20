#!/bin/bash -p
# Reproduce the locked public package environment without reading private data.
set -euo pipefail
unset BASH_ENV ENV
REQUESTED_UV_PATH="${UV_BIN:-}"
for variable in "${!UV_@}"; do unset "$variable"; done
for variable in "${!GIT_@}"; do unset "$variable"; done
for variable in "${!PIP_@}"; do unset "$variable"; done
for variable in "${!PYTHON@}"; do unset "$variable"; done
unset VIRTUAL_ENV CONDA_PREFIX CONDA_DEFAULT_ENV
PATH='/usr/bin:/bin:/usr/sbin:/sbin'
UV_NO_CONFIG=1
GIT_CONFIG_NOSYSTEM=1
GIT_CONFIG_GLOBAL=/dev/null
GIT_TERMINAL_PROMPT=0
GIT_ASKPASS=/bin/false
GIT_CONFIG_COUNT=3
GIT_CONFIG_KEY_0=core.hooksPath
GIT_CONFIG_VALUE_0=/dev/null
GIT_CONFIG_KEY_1=core.fsmonitor
GIT_CONFIG_VALUE_1=false
GIT_CONFIG_KEY_2=credential.helper
GIT_CONFIG_VALUE_2=
PYTHONNOUSERSITE=1
export PATH UV_NO_CONFIG GIT_CONFIG_NOSYSTEM GIT_CONFIG_GLOBAL \
  GIT_TERMINAL_PROMPT GIT_ASKPASS GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 \
  GIT_CONFIG_VALUE_0 GIT_CONFIG_KEY_1 GIT_CONFIG_VALUE_1 GIT_CONFIG_KEY_2 \
  GIT_CONFIG_VALUE_2 PYTHONNOUSERSITE
CLEAN_ENV=(/usr/bin/env -u BASH_ENV -u ENV -u BASHOPTS -u SHELLOPTS)

ROOT_DIR="$(cd "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
MODE=runtime
PROJECT_ENVIRONMENT="$ROOT_DIR/.venv"
ACCELERATOR=auto

canonical_home() {
  local uid
  uid="$(/usr/bin/id -u)"
  if [[ "$(/usr/bin/uname -s)" == 'Darwin' ]]; then
    /usr/bin/dscacheutil -q user -a uid "$uid" \
      | /usr/bin/awk '$1 == "dir:" { print $2; exit }'
  else
    /usr/bin/getent passwd "$uid" | /usr/bin/cut -d: -f6
  fi
}

path_chain_is_trusted() {
  local directory=$1 current_uid mode owner
  current_uid="$(/usr/bin/id -u)"
  while :; do
    [[ -d "$directory" && ! -L "$directory" ]] || return 1
    owner="$(/usr/bin/stat -c '%u' "$directory" 2>/dev/null || \
      /usr/bin/stat -f '%u' "$directory")"
    mode="$(/usr/bin/stat -c '%a' "$directory" 2>/dev/null || \
      /usr/bin/stat -f '%Lp' "$directory")"
    [[ "$owner" == "$current_uid" || "$owner" == '0' ]] || return 1
    if (( (8#$mode & 022) != 0 )); then
      (( owner == 0 && (8#$mode & 01000) != 0 )) || return 1
    fi
    [[ "$directory" == '/' ]] && return 0
    directory="$(/usr/bin/dirname -- "$directory")"
  done
}

resolve_trusted_uv() {
  local candidate resolved directory owner mode links current_uid user_home
  current_uid="$(/usr/bin/id -u)"
  if [[ -n "$REQUESTED_UV_PATH" ]]; then
    [[ "$REQUESTED_UV_PATH" == /* ]] || return 1
    candidate=$REQUESTED_UV_PATH
  else
    user_home="$(canonical_home)"
    [[ "$user_home" == /* && -d "$user_home" ]] || return 1
    for candidate in \
      "$user_home/.local/bin/uv" \
      /usr/local/bin/uv \
      /usr/bin/uv \
      /opt/homebrew/bin/uv; do
      [[ -x "$candidate" ]] && break
    done
  fi
  [[ -n "${candidate:-}" && -f "$candidate" && -x "$candidate" && ! -L "$candidate" ]] \
    || return 1
  directory="$(cd "$(/usr/bin/dirname -- "$candidate")" && pwd -P)" || return 1
  resolved="$directory/$(/usr/bin/basename "$candidate")"
  [[ "$resolved" == "$candidate" ]] || return 1
  path_chain_is_trusted "$directory" || return 1
  owner="$(/usr/bin/stat -c '%u' "$candidate" 2>/dev/null || \
    /usr/bin/stat -f '%u' "$candidate")"
  mode="$(/usr/bin/stat -c '%a' "$candidate" 2>/dev/null || \
    /usr/bin/stat -f '%Lp' "$candidate")"
  links="$(/usr/bin/stat -c '%h' "$candidate" 2>/dev/null || \
    /usr/bin/stat -f '%l' "$candidate")"
  [[ "$owner" == "$current_uid" || "$owner" == '0' ]] || return 1
  (( (8#$mode & 022) == 0 )) || return 1
  [[ "$links" == '1' ]] || return 1
  printf '%s\n' "$candidate"
}

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

if ! UV_BIN="$(resolve_trusted_uv)"; then
  echo "a trusted absolute uv executable is required" >&2
  exit 1
fi

cd "$ROOT_DIR"
export UV_PROJECT_ENVIRONMENT="$PROJECT_ENVIRONMENT"

"${CLEAN_ENV[@]}" "$UV_BIN" python install 3.14

sync_args=(sync --locked --python 3.14)
if [[ "$MODE" == dev ]]; then
  sync_args+=(--extra dev)
else
  sync_args+=(--no-editable)
fi
"${CLEAN_ENV[@]}" "$UV_BIN" "${sync_args[@]}"

python_bin="$PROJECT_ENVIRONMENT/bin/python"
cli_bin="$PROJECT_ENVIRONMENT/bin/email-memory-store"
mcp_bin="$PROJECT_ENVIRONMENT/bin/email-memory-store-mcp"

if [[ "$ACCELERATOR" == "cuda" ]]; then
  torch_version=$(
    "${CLEAN_ENV[@]}" "$python_bin" -c \
      'from importlib.metadata import version; print(version("torch").partition("+")[0])'
  )
  if ! "${CLEAN_ENV[@]}" "$UV_BIN" pip install \
    --python "$python_bin" \
    --torch-backend auto \
    --reinstall \
    "torch==$torch_version"; then
    echo "CUDA package selection failed; restoring the locked CPU environment" >&2
    "${CLEAN_ENV[@]}" "$UV_BIN" "${sync_args[@]}"
    if [[ "$REQUESTED_ACCELERATOR" != "auto" ]]; then
      exit 1
    fi
    ACCELERATOR=cpu
  fi
fi

if ! "${CLEAN_ENV[@]}" "$python_bin" - "$ACCELERATOR" <<'PY'
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
      "${CLEAN_ENV[@]}" "$UV_BIN" "${sync_args[@]}"
    fi
    echo "$ACCELERATOR was selected, but PyTorch cannot use it" >&2
    exit 1
  fi
  echo "$ACCELERATOR is unavailable to PyTorch; using CPU" >&2
  if [[ "$ACCELERATOR" == "cuda" ]]; then
    "${CLEAN_ENV[@]}" "$UV_BIN" "${sync_args[@]}"
  fi
  ACCELERATOR=cpu
fi

"${CLEAN_ENV[@]}" "$UV_BIN" pip check --python "$python_bin"
"${CLEAN_ENV[@]}" "$python_bin" -c 'import email_memory_store'
"${CLEAN_ENV[@]}" "$cli_bin" --help >/dev/null
"${CLEAN_ENV[@]}" "$mcp_bin" --help >/dev/null

"${CLEAN_ENV[@]}" "$python_bin" - "$ACCELERATOR" <<'PY'
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
printf 'python: %s\n' "$("${CLEAN_ENV[@]}" "$python_bin" --version 2>&1)"
printf 'accelerator: %s\n' "$ACCELERATOR"

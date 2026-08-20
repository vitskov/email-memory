#!/bin/bash -p
# Stage and verify an immutable email-memory Python environment.
set -euo pipefail
unset BASH_ENV ENV
CLEAN_ENV=(/usr/bin/env -u BASH_ENV -u ENV -u BASHOPTS -u SHELLOPTS)
umask 077
PATH='/usr/bin:/bin'
export PATH

SCRIPT_DIR="$(cd "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PUBLIC_CHECKOUT="${EMAIL_MEMORY_STORE_PUBLIC_CHECKOUT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
CANONICAL_HOME="$(/usr/bin/getent passwd "$(/usr/bin/id -u)" | /usr/bin/cut -d: -f6)"
[[ "$CANONICAL_HOME" == /* && -d "$CANONICAL_HOME" ]] || {
  printf '%s\n' 'canonical user home is unavailable' >&2
  exit 1
}
HOME="$CANONICAL_HOME"
XDG_CONFIG_HOME="$HOME/.config"
XDG_DATA_HOME="$HOME/.local/share"
XDG_STATE_HOME="$HOME/.local/state"
export HOME XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME
DATA_HOME="${XDG_DATA_HOME:-${HOME}/.local/share}"
DEPLOYMENT_ROOT="${EMAIL_MEMORY_STORE_DEPLOYMENT_ROOT:-${DATA_HOME}/email-memory-store}"
REQUESTED_UV_PATH="${UV_BIN:-}"
for variable in "${!GIT_@}"; do unset "$variable"; done
for variable in "${!UV_@}"; do unset "$variable"; done
for variable in "${!PIP_@}"; do unset "$variable"; done
unset VIRTUAL_ENV CONDA_PREFIX CONDA_DEFAULT_ENV
GIT_CONFIG_NOSYSTEM=1
GIT_CONFIG_GLOBAL=/dev/null
GIT_TERMINAL_PROMPT=0
GIT_ASKPASS=/bin/false
UV_NO_CONFIG=1
export GIT_CONFIG_NOSYSTEM GIT_CONFIG_GLOBAL GIT_TERMINAL_PROMPT GIT_ASKPASS UV_NO_CONFIG
GIT=(/usr/bin/git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c credential.helper=)
for variable in "${!PYTHON@}"; do unset "$variable"; done
ALLOW_DIRTY=0
RELEASE_ID=""
ACCELERATOR=auto

usage() {
  cat <<'EOF'
usage: provision_email_memory_environment.sh [options]

Options:
  --public-checkout PATH  Clean public Git checkout (default: parent of this script)
  --deployment-root PATH Override the XDG deployment root
  --release-id ID        Override the generated release identifier
  --accelerator MODE     Select auto, cpu, cuda, or mps (default: auto)
  --no-activate          Deprecated no-op; provisioning is always staging-only
  --allow-dirty          Permit deployment from modified source trees
  -h, --help             Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --public-checkout)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      PUBLIC_CHECKOUT=$2
      shift 2
      ;;
    --deployment-root)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      DEPLOYMENT_ROOT=$2
      shift 2
      ;;
    --release-id)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      RELEASE_ID=$2
      shift 2
      ;;
    --accelerator)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      ACCELERATOR=$2
      shift 2
      ;;
    --no-activate)
      shift
      ;;
    --allow-dirty)
      ALLOW_DIRTY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$ACCELERATOR" in
  auto|cpu|cuda|mps) ;;
  *)
    printf 'unsupported accelerator: %s\n' "$ACCELERATOR" >&2
    exit 2
    ;;
esac

path_chain_is_trusted() {
  local directory=$1 current_uid mode owner
  [[ "$directory" == /* && "$(/usr/bin/readlink -m -- "$directory")" == "$directory" ]] || return 1
  current_uid="$(/usr/bin/id -u)"
  while :; do
    [[ -d "$directory" && ! -L "$directory" ]] || return 1
    mode="$(/usr/bin/stat -c '%a' -- "$directory")"
    owner="$(/usr/bin/stat -c '%u' -- "$directory")"
    [[ "$owner" == "$current_uid" || "$owner" == '0' ]] || return 1
    if (( (8#$mode & 022) != 0 )); then
      (( owner == 0 && (8#$mode & 01000) != 0 )) || return 1
    fi
    [[ "$directory" == '/' ]] && return 0
    directory="$(/usr/bin/dirname -- "$directory")"
  done
}

resolve_trusted_uv() {
  local candidate resolved mode owner current_uid
  current_uid="$(/usr/bin/id -u)"
  if [[ -n "$REQUESTED_UV_PATH" ]]; then
    [[ "$REQUESTED_UV_PATH" == /* ]] || {
      printf '%s\n' 'UV_BIN must be an absolute path' >&2
      return 1
    }
    candidate=$REQUESTED_UV_PATH
  else
    for candidate in "${HOME}/.local/bin/uv" /usr/local/bin/uv /usr/bin/uv; do
      [[ -x "$candidate" ]] && break
    done
  fi
  [[ -n "${candidate:-}" && -x "$candidate" && ! -L "$candidate" ]] || return 1
  resolved="$(/usr/bin/readlink -f -- "$candidate")" || return 1
  [[ "$resolved" == "$candidate" && -f "$resolved" && -x "$resolved" && \
     "$(/usr/bin/stat -c '%h' -- "$resolved")" == '1' ]] || return 1
  owner="$(/usr/bin/stat -c '%u' -- "$resolved")"
  mode="$(/usr/bin/stat -c '%a' -- "$resolved")"
  [[ "$owner" == "$current_uid" || "$owner" == '0' ]] || return 1
  (( (8#$mode & 022) == 0 )) || return 1
  path_chain_is_trusted "$(/usr/bin/dirname -- "$resolved")" || return 1
  printf '%s\n' "$resolved"
}

PUBLIC_CHECKOUT="$(cd "$PUBLIC_CHECKOUT" && pwd -P)"
if ! path_chain_is_trusted "$PUBLIC_CHECKOUT"; then
  printf '%s\n' 'public checkout ancestry is not trusted' >&2
  exit 1
fi
current_uid="$(/usr/bin/id -u)"
if /usr/bin/find "$PUBLIC_CHECKOUT" \
  \( -type l -o -perm /022 -o -type f -links +1 \
     -o \( ! -user "$current_uid" ! -user 0 \) \) -print -quit | /usr/bin/grep -q .; then
  printf '%s\n' 'public checkout content is not trusted' >&2
  exit 1
fi

for required_file in \
  pyproject.toml \
  uv.lock \
  scripts/bootstrap.sh \
  src/email_memory_store/deployment/scripts/email_memory_store_deploy_launcher.sh; do
  [[ -f "$PUBLIC_CHECKOUT/$required_file" && ! -L "$PUBLIC_CHECKOUT/$required_file" && \
     -O "$PUBLIC_CHECKOUT/$required_file" ]] || {
    printf 'public checkout is missing %s\n' "$required_file" >&2
    exit 1
  }
  required_mode="$(/usr/bin/stat -c '%a' -- "$PUBLIC_CHECKOUT/$required_file")"
  if (( (8#$required_mode & 022) != 0 )); then
    printf 'public checkout file is writable by an untrusted principal: %s\n' \
      "$required_file" >&2
    exit 1
  fi
done
if ! UV_BIN="$(resolve_trusted_uv)"; then
  printf '%s\n' 'a trusted uv executable is required to provision the email-memory environment' >&2
  exit 1
fi

if [[ "$ALLOW_DIRTY" == '0' ]]; then
  [[ -z "$("${GIT[@]}" -C "$PUBLIC_CHECKOUT" status --porcelain --untracked-files=normal)" ]] || {
    printf '%s\n' 'public checkout has uncommitted files; commit them before deployment' >&2
    exit 1
  }
fi

PUBLIC_REVISION="$("${GIT[@]}" -C "$PUBLIC_CHECKOUT" rev-parse --short=12 HEAD)"
if [[ -z "$RELEASE_ID" ]]; then
  RELEASE_ID="${PUBLIC_REVISION}-py314-${ACCELERATOR}"
fi
if [[ ! "$RELEASE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  printf '%s\n' 'release identifier may contain only letters, digits, dot, underscore, and hyphen' >&2
  exit 2
fi

/usr/bin/mkdir -p "$DEPLOYMENT_ROOT"
DEPLOYMENT_ROOT="$(cd "$DEPLOYMENT_ROOT" && pwd -P)"
ENVIRONMENTS_DIR="$DEPLOYMENT_ROOT/envs"
RELEASE_ROOT="$ENVIRONMENTS_DIR/$RELEASE_ID"
RELEASE_ENV="$RELEASE_ROOT/venv"
PYTHON_INSTALL_DIR="$RELEASE_ROOT/python"
/usr/bin/mkdir -p "$ENVIRONMENTS_DIR"
/usr/bin/chmod 700 "$DEPLOYMENT_ROOT" "$ENVIRONMENTS_DIR"

[[ ! -e "$RELEASE_ROOT" && ! -L "$RELEASE_ROOT" ]] || {
  printf 'release already exists: %s\n' "$RELEASE_ID" >&2
  exit 1
}
ARTIFACTS_DIR="$(/usr/bin/mktemp -d "$DEPLOYMENT_ROOT/.artifacts.XXXXXX")"
RELEASE_COMPLETE=0
cleanup() {
  /usr/bin/rm -rf -- "$ARTIFACTS_DIR"
  if [[ "$RELEASE_COMPLETE" == '0' ]]; then
    /usr/bin/rm -rf -- "$RELEASE_ROOT"
  fi
}
trap cleanup EXIT

# Python environment variables are untrusted process inputs. They must not
# influence dependency installation or allow ambient modules to satisfy probes.
PYTHONNOUSERSITE=1
UV_MANAGED_PYTHON=1
UV_PYTHON_INSTALL_DIR="$PYTHON_INSTALL_DIR"
UV_LINK_MODE=copy
export PYTHONNOUSERSITE UV_MANAGED_PYTHON UV_PYTHON_INSTALL_DIR UV_LINK_MODE UV_NO_CONFIG

/usr/bin/mkdir -p "$RELEASE_ROOT"
/usr/bin/chmod 700 "$RELEASE_ROOT"
UV_BIN="$UV_BIN" "${CLEAN_ENV[@]}" /bin/bash -p "$PUBLIC_CHECKOUT/scripts/bootstrap.sh" \
  --environment "$RELEASE_ENV" \
  --accelerator "$ACCELERATOR"

# The venv may symlink to its base interpreter, but both must remain inside the
# same versioned release container. No Python executable or standard library
# may resolve to uv's shared installation outside this trust boundary.
RESOLVED_PYTHON="$(/usr/bin/readlink -f -- "$RELEASE_ENV/bin/python")"
case "$RESOLVED_PYTHON" in
  "$PYTHON_INSTALL_DIR"/*) ;;
  *)
    printf '%s\n' 'provisioned Python resolves outside its release-local base' >&2
    exit 1
    ;;
esac
[[ -f "$RELEASE_ENV/pyvenv.cfg" && ! -L "$RELEASE_ENV/pyvenv.cfg" ]] || {
  printf '%s\n' 'provisioned environment has no trusted pyvenv.cfg' >&2
  exit 1
}
[[ -O "$RELEASE_ENV/pyvenv.cfg" && \
   "$(/usr/bin/stat -c '%h' -- "$RELEASE_ENV/pyvenv.cfg")" == '1' ]] || {
  printf '%s\n' 'provisioned environment has an untrusted pyvenv.cfg' >&2
  exit 1
}
/usr/bin/chmod 600 "$RELEASE_ENV/pyvenv.cfg"
PYTHON_HOME="$(/usr/bin/sed -n 's/^home = //p' "$RELEASE_ENV/pyvenv.cfg" | /usr/bin/head -n 1)"
RESOLVED_PYTHON_HOME="$(/usr/bin/readlink -f -- "$PYTHON_HOME")"
case "$RESOLVED_PYTHON_HOME" in
  "$PYTHON_INSTALL_DIR"/*) ;;
  *)
    printf '%s\n' 'pyvenv.cfg resolves outside its release-local Python base' >&2
    exit 1
    ;;
esac
"${CLEAN_ENV[@]}" "$UV_BIN" build --wheel --no-sources --out-dir "$ARTIFACTS_DIR/public" "$PUBLIC_CHECKOUT"
PUBLIC_WHEEL="$(/usr/bin/find "$ARTIFACTS_DIR/public" -maxdepth 1 -type f -name '*.whl' -print -quit)"
[[ -n "$PUBLIC_WHEEL" ]] || {
  printf '%s\n' 'package build did not produce the public wheel' >&2
  exit 1
}
"${CLEAN_ENV[@]}" "$UV_BIN" pip install \
  --python "$RELEASE_ENV/bin/python" \
  --reinstall \
  --no-deps \
  "$PUBLIC_WHEEL"
"${CLEAN_ENV[@]}" "$UV_BIN" pip check --python "$RELEASE_ENV/bin/python"

"${CLEAN_ENV[@]}" "$RELEASE_ENV/bin/python" -c \
  'import sys; import email_memory_store; raise SystemExit(0 if sys.version_info >= (3, 14) else "Python 3.14 or newer is required")'
"${CLEAN_ENV[@]}" "$RELEASE_ENV/bin/python" - "$RELEASE_ROOT" <<'PY'
from pathlib import Path
import sys
import sysconfig

release = Path(sys.argv[1]).resolve()
paths = (
    Path(sys.prefix),
    Path(sys.base_prefix),
    Path(sys._base_executable),
    Path(sysconfig.get_path("stdlib")),
    Path(sysconfig.get_path("platstdlib")),
)
if any(not path.resolve().is_relative_to(release) for path in paths):
    raise SystemExit("Python runtime resolves outside the release container")
PY
"${CLEAN_ENV[@]}" "$RELEASE_ENV/bin/email-memory-store" --help >/dev/null
"${CLEAN_ENV[@]}" "$RELEASE_ENV/bin/email-memory-store-mcp" --help >/dev/null
/usr/bin/install -d -m 700 -- "$RELEASE_ROOT/bin"
/usr/bin/install -m 700 -- \
  "$PUBLIC_CHECKOUT/src/email_memory_store/deployment/scripts/email_memory_store_deploy_launcher.sh" \
  "$RELEASE_ROOT/bin/email-memory-store-deploy"
/bin/bash -p -n "$RELEASE_ROOT/bin/email-memory-store-deploy"

if /usr/bin/find "$RELEASE_ROOT" -type f -links +1 -print -quit | /usr/bin/grep -q .; then
  printf '%s\n' 'release contains hard-linked files outside its trust boundary' >&2
  exit 1
fi
/usr/bin/chmod -R go-w "$RELEASE_ROOT"
if /usr/bin/find "$RELEASE_ROOT" ! -user "$(/usr/bin/id -u)" -print -quit | /usr/bin/grep -q . \
  || /usr/bin/find "$RELEASE_ROOT" \( -type f -o -type d \) -perm /022 -print -quit | /usr/bin/grep -q .; then
  printf '%s\n' 'release contains files outside the current-user trust boundary' >&2
  exit 1
fi

printf '%s\n' \
  "public_revision=$PUBLIC_REVISION" \
  "python=3.14" \
  "accelerator_request=$ACCELERATOR" \
  > "$RELEASE_ROOT/.email-memory-release"
/usr/bin/chmod 600 "$RELEASE_ROOT/.email-memory-release"
RELEASE_COMPLETE=1

printf 'staged and verified email-memory release: %s\n' "$RELEASE_ID"
printf 'release: %s\n' "$RELEASE_ROOT"
printf 'environment: %s\n' "$RELEASE_ENV"

#!/bin/bash -p
# Hardened entry point for an already-provisioned release.
set -euo pipefail
unset BASH_ENV ENV
CLEAN_ENV=(/usr/bin/env -u BASH_ENV -u ENV -u BASHOPTS -u SHELLOPTS)
for variable in "${!PYTHON@}"; do unset "$variable"; done
umask 077
PATH='/usr/bin:/bin'
export PATH

fail() {
  printf '%s\n' 'email-memory deployment launcher is not trusted' >&2
  exit 1
}

SCRIPT_PATH="$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")" || fail
BIN_DIR="$(/usr/bin/dirname -- "$SCRIPT_PATH")"
RELEASE_ROOT="$(/usr/bin/dirname -- "$BIN_DIR")"
VENV_DIR="$RELEASE_ROOT/venv"
PYTHON_REQUEST="$VENV_DIR/bin/python"
PYTHON_PATH="$(/usr/bin/readlink -f -- "$PYTHON_REQUEST")" || fail
PYVENV_CONFIG="$VENV_DIR/pyvenv.cfg"
RELEASE_MARKER="$RELEASE_ROOT/.email-memory-release"
READINESS_RECEIPT="$RELEASE_ROOT/.deployment-readiness.json"
CURRENT_UID="$(/usr/bin/id -u)"

trusted_directory() {
  local directory=$1 owner mode
  [[ -d "$directory" && ! -L "$directory" ]] || return 1
  owner="$(/usr/bin/stat -c '%u' -- "$directory")"
  mode="$(/usr/bin/stat -c '%a' -- "$directory")"
  [[ "$owner" == "$CURRENT_UID" && $((8#$mode & 022)) == 0 ]]
}

trusted_file() {
  local file=$1 required_mode=${2:-} owner mode links
  [[ -f "$file" && ! -L "$file" ]] || return 1
  owner="$(/usr/bin/stat -c '%u' -- "$file")"
  mode="$(/usr/bin/stat -c '%a' -- "$file")"
  links="$(/usr/bin/stat -c '%h' -- "$file")"
  [[ "$owner" == "$CURRENT_UID" && "$links" == '1' && $((8#$mode & 022)) == 0 ]] || return 1
  [[ -z "$required_mode" || "$mode" == "$required_mode" ]]
}

trusted_chain() {
  local directory=$1 owner mode
  while :; do
    [[ -d "$directory" && ! -L "$directory" ]] || return 1
    owner="$(/usr/bin/stat -c '%u' -- "$directory")"
    mode="$(/usr/bin/stat -c '%a' -- "$directory")"
    [[ "$owner" == "$CURRENT_UID" || "$owner" == '0' ]] || return 1
    if (( (8#$mode & 022) != 0 )); then
      (( owner == 0 && (8#$mode & 01000) != 0 )) || return 1
    fi
    [[ "$directory" == '/' ]] && return 0
    directory="$(/usr/bin/dirname -- "$directory")"
  done
}

[[ "$SCRIPT_PATH" == "$RELEASE_ROOT/bin/email-memory-store-deploy" ]] || fail
trusted_directory "$RELEASE_ROOT" || fail
trusted_directory "$BIN_DIR" || fail
trusted_directory "$VENV_DIR" || fail
trusted_directory "$VENV_DIR/bin" || fail
trusted_chain "$RELEASE_ROOT" || fail
trusted_file "$SCRIPT_PATH" 700 || fail
trusted_file "$PYTHON_PATH" || fail
trusted_file "$PYVENV_CONFIG" 600 || fail
trusted_file "$RELEASE_MARKER" 600 || fail
trusted_file "$READINESS_RECEIPT" 600 || fail
[[ -x "$PYTHON_PATH" && "$PYTHON_PATH" == "$RELEASE_ROOT"/* ]] || fail

PYTHON_HOME="$(/usr/bin/sed -n 's/^home = //p' "$PYVENV_CONFIG" | /usr/bin/head -n 1)"
[[ -n "$PYTHON_HOME" ]] || fail
RESOLVED_PYTHON_HOME="$(/usr/bin/readlink -f -- "$PYTHON_HOME")" || fail
[[ "$RESOLVED_PYTHON_HOME" == "$RELEASE_ROOT/python"/* ]] || fail

for variable in "${!GIT_@}"; do unset "$variable"; done
for variable in "${!UV_@}"; do unset "$variable"; done
for variable in "${!PIP_@}"; do unset "$variable"; done
unset VIRTUAL_ENV CONDA_PREFIX CONDA_DEFAULT_ENV
PYTHONNOUSERSITE=1
export PYTHONNOUSERSITE
"${CLEAN_ENV[@]}" "$PYTHON_REQUEST" -I - "$RELEASE_ROOT" <<'PY' || fail
from pathlib import Path
import sys
import sysconfig

release = Path(sys.argv[1]).resolve(strict=True)
paths = (
    Path(sys.prefix),
    Path(sys.base_prefix),
    Path(sys._base_executable),
    Path(sysconfig.get_path("stdlib")),
    Path(sysconfig.get_path("platstdlib")),
)
if any(not path.resolve(strict=True).is_relative_to(release) for path in paths):
    raise SystemExit(1)
PY
exec "${CLEAN_ENV[@]}" "$PYTHON_REQUEST" -I -m email_memory_store.deployment.cli "$@"

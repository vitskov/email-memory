#!/bin/bash -p
# Source-tree convenience launcher for the transactional deployment coordinator.
set -euo pipefail
unset BASH_ENV ENV
CLEAN_ENV=(/usr/bin/env -u BASH_ENV -u ENV -u BASHOPTS -u SHELLOPTS)
umask 077
PATH='/usr/bin:/bin'
export PATH

if [[ ${1:-} == '-h' || ${1:-} == '--help' ]]; then
  printf '%s\n' \
    'usage: deploy.sh [bootstrap options]' \
    '' \
    'Stage from a clean public Git checkout, validate it, and activate it transactionally.'
  exit 0
fi

SCRIPT_DIR="$(cd "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PUBLIC_CHECKOUT="$(cd "$SCRIPT_DIR/.." && pwd)"
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
for variable in "${!PYTHON@}"; do unset "$variable"; done

path_chain_is_trusted() {
  local directory=$1 current_uid mode owner
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
    [[ "$REQUESTED_UV_PATH" == /* ]] || return 1
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
if ! UV_BIN="$(resolve_trusted_uv)"; then
  printf '%s\n' 'a trusted uv executable is required for deployment' >&2
  exit 1
fi

PYTHONNOUSERSITE=1
export PYTHONNOUSERSITE
export PYTHONPATH="$PUBLIC_CHECKOUT/src"
exec "${CLEAN_ENV[@]}" "$UV_BIN" run --no-project --python 3.14 python \
  -m email_memory_store.deployment.cli bootstrap \
  --public-checkout "$PUBLIC_CHECKOUT" "$@"

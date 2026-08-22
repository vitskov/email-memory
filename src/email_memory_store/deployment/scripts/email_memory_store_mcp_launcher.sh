#!/bin/bash -p
# Local stdio launcher for the email-memory MCP service.
set -euo pipefail
unset BASH_ENV ENV
for variable in "${!PYTHON@}"; do unset "$variable"; done
for variable in "${!HIMALAYA_@}"; do unset "$variable"; done
for variable in "${!HERMES_@}"; do unset "$variable"; done
CLEAN_ENV=(/usr/bin/env -u BASH_ENV -u ENV -u BASHOPTS -u SHELLOPTS)
PATH='/usr/bin:/bin'
export PATH

SCRIPT_PATH="$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(/usr/bin/dirname -- "$SCRIPT_PATH")" && pwd)"
ENVIRONMENT_HELPER="$SCRIPT_DIR/email_memory_environment.sh"
if [[ -z "$SCRIPT_PATH" || ! -f "$SCRIPT_PATH" || -L "$SCRIPT_PATH" || \
      ! -O "$SCRIPT_PATH" || ! -d "$SCRIPT_DIR" || -L "$SCRIPT_DIR" || \
      ! -O "$SCRIPT_DIR" || ! -f "$ENVIRONMENT_HELPER" || \
      -L "$ENVIRONMENT_HELPER" || ! -O "$ENVIRONMENT_HELPER" ]]; then
  printf '%s\n' 'email-memory MCP launcher installation files are invalid' >&2
  exit 1
fi
trusted_directory="$SCRIPT_DIR"
current_uid="$(/usr/bin/id -u)"
while :; do
  if [[ ! -d "$trusted_directory" || -L "$trusted_directory" ]]; then
    printf '%s\n' 'email-memory MCP launcher installation files are invalid' >&2
    exit 1
  fi
  trusted_owner="$(/usr/bin/stat -c '%u' -- "$trusted_directory")"
  trusted_mode="$(/usr/bin/stat -c '%a' -- "$trusted_directory")"
  if [[ "$trusted_owner" != "$current_uid" && "$trusted_owner" != '0' ]]; then
    printf '%s\n' 'email-memory MCP launcher installation files are invalid' >&2
    exit 1
  fi
  if (( (8#$trusted_mode & 022) != 0 )) && \
     ! (( trusted_owner == 0 && (8#$trusted_mode & 01000) != 0 )); then
    printf '%s\n' 'email-memory MCP launcher installation files are invalid' >&2
    exit 1
  fi
  [[ "$trusted_directory" == '/' ]] && break
  trusted_directory="$(/usr/bin/dirname -- "$trusted_directory")"
done
if [[ "$(/usr/bin/stat -c '%a' -- "$SCRIPT_DIR")" != '700' || \
      "$(/usr/bin/stat -c '%a' -- "$SCRIPT_PATH")" != '700' || \
      "$(/usr/bin/stat -c '%a' -- "$ENVIRONMENT_HELPER")" != '600' ]]; then
  printf '%s\n' 'email-memory MCP launcher installation files are invalid' >&2
  exit 1
fi
# shellcheck source=email_memory_environment.sh
unset EMAIL_MEMORY_TEST_MODE EMAIL_MEMORY_STORE_ENVIRONMENT \
  EMAIL_MEMORY_STORE_COMMAND EMAIL_MEMORY_STORE_MCP_COMMAND \
  EMAIL_MEMORY_STORE_CONTROL_MCP_COMMAND \
  EMAIL_MEMORY_OPERATIONAL_PYTHON
source "$ENVIRONMENT_HELPER"
# The package-owned helper has no connector-specific configuration contract.
# Repeat the scrub so even a stale helper cannot reintroduce ambient overrides.
for variable in "${!HIMALAYA_@}"; do unset "$variable"; done
for variable in "${!HERMES_@}"; do unset "$variable"; done
MODE='retrieval'
if (($#)); then
  if [[ "$#" == 2 && "$1" == '--mode' && "$2" == 'control' ]]; then
    MODE='control'
  else
    printf '%s\n' 'email-memory MCP launcher arguments are invalid' >&2
    exit 2
  fi
fi
if [[ "$MODE" == 'control' ]]; then
  MCP_BIN="$EMAIL_MEMORY_STORE_CONTROL_MCP_COMMAND"
else
  MCP_BIN="$EMAIL_MEMORY_STORE_MCP_COMMAND"
fi
CONFIG_HOME="${XDG_CONFIG_HOME:-${HOME}/.config}"
RUNTIME_CONFIG_DIR="${CONFIG_HOME}/email-memory-store"
RUNTIME_CONFIG="${RUNTIME_CONFIG_DIR}/runtime.toml"

if [[ ! -x "$MCP_BIN" ]]; then
  echo "the selected email-memory MCP is not installed in the active isolated environment" >&2
  echo "Run provision_email_memory_environment.sh" >&2
  exit 1
fi

if [[ ! -d "$RUNTIME_CONFIG_DIR" || -L "$RUNTIME_CONFIG_DIR" || \
      ! -f "$RUNTIME_CONFIG" || -L "$RUNTIME_CONFIG" || ! -r "$RUNTIME_CONFIG" ]]; then
  echo "email-memory-store runtime manifest is missing or unreadable" >&2
  echo "Regenerate it with: email-memory-store setup-private" >&2
  exit 1
fi

if [[ "$(/usr/bin/stat -c '%u:%a' -- "$RUNTIME_CONFIG_DIR")" != "$(/usr/bin/id -u):700" || \
      "$(/usr/bin/stat -c '%u:%a' -- "$RUNTIME_CONFIG")" != "$(/usr/bin/id -u):600" ]]; then
  echo "email-memory-store runtime manifest permissions are not owner-only" >&2
  echo "Regenerate it with: email-memory-store setup-private" >&2
  exit 1
fi

EMAIL_MEMORY_STORE_RUNTIME_CONFIG="$RUNTIME_CONFIG"
export EMAIL_MEMORY_STORE_RUNTIME_CONFIG
exec "${CLEAN_ENV[@]}" "$MCP_BIN"

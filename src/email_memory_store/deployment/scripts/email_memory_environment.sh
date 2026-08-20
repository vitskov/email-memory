#!/bin/bash -p
# Resolve the active, isolated email-memory deployment without activating it.

unset BASH_ENV ENV
for variable in "${!PYTHON@}"; do unset "$variable"; done

PATH='/usr/bin:/bin'
export PATH

if [[ "${EMAIL_MEMORY_TEST_MODE:-0}" == '1' ]]; then
  EMAIL_MEMORY_DATA_HOME="${XDG_DATA_HOME:-${HOME}/.local/share}"
  EMAIL_MEMORY_STORE_ENVIRONMENT="${EMAIL_MEMORY_STORE_ENVIRONMENT:-${EMAIL_MEMORY_DATA_HOME}/email-memory-store/current}"
  EMAIL_MEMORY_STORE_COMMAND="${EMAIL_MEMORY_STORE_COMMAND:-${EMAIL_MEMORY_STORE_ENVIRONMENT}/bin/email-memory-store}"
  EMAIL_MEMORY_STORE_MCP_COMMAND="${EMAIL_MEMORY_STORE_MCP_COMMAND:-${EMAIL_MEMORY_STORE_ENVIRONMENT}/bin/email-memory-store-mcp}"
  EMAIL_MEMORY_OPERATIONAL_PYTHON="${EMAIL_MEMORY_OPERATIONAL_PYTHON:-${EMAIL_MEMORY_STORE_ENVIRONMENT}/bin/python}"
else
  canonical_home="$(/usr/bin/getent passwd "$(/usr/bin/id -u)" | /usr/bin/cut -d: -f6)"
  if [[ "$canonical_home" != /* || ! -d "$canonical_home" ]]; then
    printf '%s\n' 'canonical user home is unavailable' >&2
    exit 1
  fi
  HOME="$canonical_home"
  XDG_CONFIG_HOME="$HOME/.config"
  XDG_DATA_HOME="$HOME/.local/share"
  XDG_STATE_HOME="$HOME/.local/state"
  EMAIL_MEMORY_DATA_HOME="$XDG_DATA_HOME"
  export HOME XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME
  EMAIL_MEMORY_STORE_RELEASE="${EMAIL_MEMORY_DATA_HOME}/email-memory-store/current"
  EMAIL_MEMORY_STORE_ENVIRONMENT="${EMAIL_MEMORY_STORE_RELEASE}/venv"
  EMAIL_MEMORY_STORE_COMMAND="${EMAIL_MEMORY_STORE_ENVIRONMENT}/bin/email-memory-store"
  EMAIL_MEMORY_STORE_MCP_COMMAND="${EMAIL_MEMORY_STORE_ENVIRONMENT}/bin/email-memory-store-mcp"
  EMAIL_MEMORY_OPERATIONAL_PYTHON="${EMAIL_MEMORY_STORE_ENVIRONMENT}/bin/python"

  resolved_release=''
  if ! resolved_release="$(/usr/bin/readlink -f -- "$EMAIL_MEMORY_STORE_RELEASE")"; then
    resolved_release=''
  fi
  if [[ -z "$resolved_release" || ! -d "$resolved_release" || ! -O "$resolved_release" ]]; then
    printf '%s\n' 'email-memory release must resolve to a current-user-owned directory' >&2
    exit 1
  fi
  release_mode="$(/usr/bin/stat -c '%a' -- "$resolved_release")"
  if (( (8#$release_mode & 022) != 0 )); then
    printf '%s\n' 'email-memory release must not be group- or world-writable' >&2
    exit 1
  fi
  resolved_environment="$resolved_release/venv"
  if [[ ! -d "$resolved_environment" || -L "$resolved_environment" || ! -O "$resolved_environment" ]]; then
    printf '%s\n' 'email-memory venv must be a current-user-owned release directory' >&2
    exit 1
  fi
  environment_mode="$(/usr/bin/stat -c '%a' -- "$resolved_environment")"
  if (( (8#$environment_mode & 022) != 0 )); then
    printf '%s\n' 'email-memory venv must not be group- or world-writable' >&2
    exit 1
  fi
  resolved_bin_dir="$resolved_environment/bin"
  if [[ ! -d "$resolved_bin_dir" || ! -O "$resolved_bin_dir" ]]; then
    printf '%s\n' 'email-memory bin directory must be current-user-owned' >&2
    exit 1
  fi
  bin_mode="$(/usr/bin/stat -c '%a' -- "$resolved_bin_dir")"
  if (( (8#$bin_mode & 022) != 0 )); then
    printf '%s\n' 'email-memory bin directory must not be group- or world-writable' >&2
    exit 1
  fi
  for required_binary in \
    "$EMAIL_MEMORY_OPERATIONAL_PYTHON" \
    "$EMAIL_MEMORY_STORE_COMMAND" \
    "$EMAIL_MEMORY_STORE_MCP_COMMAND"; do
    resolved_binary=''
    if ! resolved_binary="$(/usr/bin/readlink -f -- "$required_binary")"; then
      resolved_binary=''
    fi
    if [[ -z "$resolved_binary" || ! -f "$resolved_binary" || ! -x "$resolved_binary" || ! -O "$resolved_binary" ]]; then
      printf '%s\n' 'email-memory environment contains an invalid required executable' >&2
      exit 1
    fi
    case "$resolved_binary" in
      "$resolved_release"/*) ;;
      *)
        printf '%s\n' 'email-memory executable resolves outside the active release' >&2
        exit 1
        ;;
    esac
    binary_mode="$(/usr/bin/stat -c '%a' -- "$resolved_binary")"
    if (( (8#$binary_mode & 022) != 0 )); then
      printf '%s\n' 'email-memory executables must not be group- or world-writable' >&2
      exit 1
    fi
  done
  pyvenv_config="$resolved_environment/pyvenv.cfg"
  if [[ ! -f "$pyvenv_config" || -L "$pyvenv_config" || ! -O "$pyvenv_config" ]]; then
    printf '%s\n' 'email-memory venv configuration is invalid' >&2
    exit 1
  fi
  pyvenv_mode="$(/usr/bin/stat -c '%a' -- "$pyvenv_config")"
  if (( (8#$pyvenv_mode & 022) != 0 )); then
    printf '%s\n' 'email-memory venv configuration must not be group- or world-writable' >&2
    exit 1
  fi
  python_home="$(/usr/bin/sed -n 's/^home = //p' "$pyvenv_config" | /usr/bin/head -n 1)"
  resolved_python_home=''
  if [[ -n "$python_home" ]]; then
    resolved_python_home="$(/usr/bin/readlink -f -- "$python_home")"
  fi
  case "$resolved_python_home" in
    "$resolved_release"/python/*) ;;
    *)
      printf '%s\n' 'email-memory Python base resolves outside the active release' >&2
      exit 1
      ;;
  esac

fi

PYTHONNOUSERSITE=1
export PYTHONNOUSERSITE
export EMAIL_MEMORY_STORE_ENVIRONMENT
export EMAIL_MEMORY_STORE_RELEASE
export EMAIL_MEMORY_STORE_COMMAND
export EMAIL_MEMORY_STORE_MCP_COMMAND
export EMAIL_MEMORY_OPERATIONAL_PYTHON

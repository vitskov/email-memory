#!/bin/bash -p
# Atomically install the email-memory MCP launcher and its runtime helper.
set -euo pipefail
unset BASH_ENV ENV
for variable in "${!PYTHON@}"; do unset "$variable"; done
PATH='/usr/bin:/bin'
export PATH

SCRIPT_DIR="$(cd "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_LAUNCHER="$SCRIPT_DIR/email_memory_store_mcp_launcher.sh"
SOURCE_ENVIRONMENT="$SCRIPT_DIR/email_memory_environment.sh"
DATA_HOME="${XDG_DATA_HOME:-${HOME}/.local/share}"
BIN_DIR="${HOME}/.local/bin"
INSTALL_ROOT="$DATA_HOME/email-memory-store/mcp-launcher"
INSTALL_NAME='email_memory_store_mcp_hermes.sh'

usage() {
  cat <<'EOF'
usage: install_email_memory_mcp_launcher.sh [options]

Options:
  --bin-dir PATH       User executable directory (default: ~/.local/bin)
  --install-root PATH  Private versioned launcher root
  --name NAME          Installed executable name
  -h, --help           Show this help
EOF
}

while (($#)); do
  case "$1" in
    --bin-dir)
      BIN_DIR="${2:?--bin-dir requires a path}"
      shift 2
      ;;
    --install-root)
      INSTALL_ROOT="${2:?--install-root requires a path}"
      shift 2
      ;;
    --name)
      INSTALL_NAME="${2:?--name requires a name}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$BIN_DIR" in
  /*) ;;
  *) printf '%s\n' '--bin-dir must be absolute' >&2; exit 2 ;;
esac
case "$INSTALL_ROOT" in
  /*) ;;
  *) printf '%s\n' '--install-root must be absolute' >&2; exit 2 ;;
esac
case "$INSTALL_NAME" in
  ''|*/*|.|..) printf '%s\n' '--name must be a single file name' >&2; exit 2 ;;
esac

for source_file in "$SOURCE_LAUNCHER" "$SOURCE_ENVIRONMENT"; do
  if [[ ! -f "$source_file" || -L "$source_file" || ! -O "$source_file" ]]; then
    printf '%s\n' 'launcher installation source is invalid' >&2
    exit 1
  fi
done

path_chain_is_trusted() {
  local directory="$1"
  local current_uid
  local mode
  local owner
  [[ "$directory" == /* && "$(/usr/bin/readlink -m -- "$directory")" == "$directory" ]] || return 1
  current_uid="$(/usr/bin/id -u)"
  while :; do
    [[ -d "$directory" && ! -L "$directory" ]] || return 1
    mode="$(/usr/bin/stat -c '%a' -- "$directory")"
    owner="$(/usr/bin/stat -c '%u' -- "$directory")"
    [[ "$owner" == "$current_uid" || "$owner" == '0' ]] || return 1
    if (( (8#$mode & 022) != 0 )); then
      # A root-owned sticky directory (normally /tmp) protects entries owned
      # by other users even though the directory is shared.
      (( owner == 0 && (8#$mode & 01000) != 0 )) || return 1
    fi
    [[ "$directory" == '/' ]] && return 0
    directory="$(/usr/bin/dirname -- "$directory")"
  done
}

deepest_existing_directory() {
  local candidate="$1"
  while [[ ! -e "$candidate" && ! -L "$candidate" ]]; do
    candidate="$(/usr/bin/dirname -- "$candidate")"
  done
  [[ -d "$candidate" && ! -L "$candidate" ]] || return 1
  printf '%s\n' "$candidate"
}

if ! INSTALL_ANCESTOR="$(deepest_existing_directory "$INSTALL_ROOT")"; then
  printf '%s\n' 'launcher installation root is invalid' >&2
  exit 1
fi
if ! BIN_ANCESTOR="$(deepest_existing_directory "$BIN_DIR")"; then
  printf '%s\n' 'user executable directory is invalid' >&2
  exit 1
fi
if ! path_chain_is_trusted "$INSTALL_ANCESTOR"; then
  printf '%s\n' 'launcher installation root is invalid' >&2
  exit 1
fi
if ! path_chain_is_trusted "$BIN_ANCESTOR"; then
  printf '%s\n' 'user executable directory is invalid' >&2
  exit 1
fi

if [[ -L "$INSTALL_ROOT" ]]; then
  printf '%s\n' 'launcher installation root is invalid' >&2
  exit 1
fi
/usr/bin/install -d -m 700 -- "$INSTALL_ROOT" "$INSTALL_ROOT/releases"
INSTALL_PARENT="$(/usr/bin/dirname -- "$INSTALL_ROOT")"
if [[ -L "$INSTALL_ROOT" || ! -O "$INSTALL_ROOT" || \
      -L "$INSTALL_ROOT/releases" || ! -O "$INSTALL_ROOT/releases" || \
      ! -d "$INSTALL_PARENT" || -L "$INSTALL_PARENT" || ! -O "$INSTALL_PARENT" ]]; then
  printf '%s\n' 'launcher installation root is invalid' >&2
  exit 1
fi
/usr/bin/chmod 700 -- "$INSTALL_ROOT" "$INSTALL_ROOT/releases"
install_parent_mode="$(/usr/bin/stat -c '%a' -- "$INSTALL_PARENT")"
if (( (8#$install_parent_mode & 022) != 0 )) || \
   ! path_chain_is_trusted "$INSTALL_ROOT" || \
   ! path_chain_is_trusted "$INSTALL_ROOT/releases"; then
  printf '%s\n' 'launcher installation root is invalid' >&2
  exit 1
fi

STAGING="$(/usr/bin/mktemp -d -- "$INSTALL_ROOT/.install.XXXXXXXX")"
CURRENT_TMP=''
LINK_TMP=''
cleanup() {
  if [[ -n "${STAGING:-}" && -d "$STAGING" ]]; then
    /usr/bin/rm -rf -- "$STAGING"
  fi
  if [[ -n "${CURRENT_TMP:-}" && -L "$CURRENT_TMP" ]]; then
    /usr/bin/rm -f -- "$CURRENT_TMP"
  fi
  if [[ -n "${LINK_TMP:-}" && -L "$LINK_TMP" ]]; then
    /usr/bin/rm -f -- "$LINK_TMP"
  fi
}
trap cleanup EXIT
/usr/bin/chmod 700 -- "$STAGING"
/usr/bin/install -m 700 -- "$SOURCE_LAUNCHER" "$STAGING/email_memory_store_mcp_launcher.sh"
/usr/bin/install -m 600 -- "$SOURCE_ENVIRONMENT" "$STAGING/email_memory_environment.sh"
/usr/bin/bash -p -n -- "$STAGING/email_memory_store_mcp_launcher.sh"
/usr/bin/bash -p -n -- "$STAGING/email_memory_environment.sh"

bundle_digest() {
  {
    /usr/bin/sha256sum -- "$1" | /usr/bin/cut -d ' ' -f 1
    /usr/bin/sha256sum -- "$2" | /usr/bin/cut -d ' ' -f 1
  } | /usr/bin/sha256sum | /usr/bin/cut -d ' ' -f 1
}

release_is_valid() {
  local release_dir="$1"
  local launcher="$release_dir/email_memory_store_mcp_launcher.sh"
  local environment="$release_dir/email_memory_environment.sh"
  [[ -d "$release_dir" && ! -L "$release_dir" && -O "$release_dir" && \
     -f "$launcher" && ! -L "$launcher" && -O "$launcher" && \
     -f "$environment" && ! -L "$environment" && -O "$environment" && \
     "$(/usr/bin/stat -c '%a' -- "$release_dir")" == '700' && \
     "$(/usr/bin/stat -c '%a' -- "$launcher")" == '700' && \
     "$(/usr/bin/stat -c '%a' -- "$environment")" == '600' ]]
}

symlink_is_current_user_owned() {
  local link="$1"
  [[ -L "$link" && "$(/usr/bin/stat -c '%u' -- "$link")" == "$(/usr/bin/id -u)" ]]
}

DIGEST="$(bundle_digest \
  "$STAGING/email_memory_store_mcp_launcher.sh" \
  "$STAGING/email_memory_environment.sh")"
RELEASE_DIR="$INSTALL_ROOT/releases/$DIGEST"
if [[ -e "$RELEASE_DIR" || -L "$RELEASE_DIR" ]]; then
  if ! release_is_valid "$RELEASE_DIR" || \
     [[ \
        "$DIGEST" != "$(bundle_digest \
          "$RELEASE_DIR/email_memory_store_mcp_launcher.sh" \
          "$RELEASE_DIR/email_memory_environment.sh")" ]]; then
    printf '%s\n' 'existing launcher release does not match its identity' >&2
    exit 1
  fi
else
  /usr/bin/mv -T -- "$STAGING" "$RELEASE_DIR"
  STAGING=''
fi
if ! release_is_valid "$RELEASE_DIR"; then
  printf '%s\n' 'installed launcher release failed validation' >&2
  exit 1
fi

if [[ ! -e "$BIN_DIR" && ! -L "$BIN_DIR" ]]; then
  /usr/bin/install -d -m 700 -- "$BIN_DIR"
fi
if ! path_chain_is_trusted "$BIN_DIR"; then
  printf '%s\n' 'user executable directory is invalid' >&2
  exit 1
fi
STABLE_LINK="$BIN_DIR/$INSTALL_NAME"
CURRENT_LINK="$INSTALL_ROOT/current"
if [[ ! -w "$BIN_DIR" || ! -x "$BIN_DIR" || \
      ( -d "$STABLE_LINK" && ! -L "$STABLE_LINK" ) ]]; then
  printf '%s\n' 'user executable destination is not publishable' >&2
  exit 1
fi
if [[ ( -e "$CURRENT_LINK" || -L "$CURRENT_LINK" ) && ! -L "$CURRENT_LINK" ]]; then
  printf '%s\n' 'current launcher destination is not publishable' >&2
  exit 1
fi

LINK_TMP="$BIN_DIR/.${INSTALL_NAME}.$$"
/usr/bin/ln -s -- "$INSTALL_ROOT/current/email_memory_store_mcp_launcher.sh" "$LINK_TMP"
CURRENT_TMP="$INSTALL_ROOT/.current.$$"
/usr/bin/ln -s -- "releases/$DIGEST" "$CURRENT_TMP"
/usr/bin/mv -Tf -- "$CURRENT_TMP" "$INSTALL_ROOT/current"
CURRENT_TMP=''
/usr/bin/mv -Tf -- "$LINK_TMP" "$STABLE_LINK"
LINK_TMP=''

if ! symlink_is_current_user_owned "$CURRENT_LINK" || \
   ! symlink_is_current_user_owned "$STABLE_LINK" || \
   [[ "$(/usr/bin/readlink -- "$STABLE_LINK")" != \
      "$INSTALL_ROOT/current/email_memory_store_mcp_launcher.sh" ]] || \
   [[ "$(/usr/bin/readlink -f -- "$CURRENT_LINK")" != "$RELEASE_DIR" ]] || \
   [[ "$(/usr/bin/readlink -f -- "$STABLE_LINK")" != \
      "$RELEASE_DIR/email_memory_store_mcp_launcher.sh" ]] || \
   ! release_is_valid "$RELEASE_DIR"; then
  printf '%s\n' 'published launcher links failed validation' >&2
  exit 1
fi

/usr/bin/sync -f "$INSTALL_ROOT"
/usr/bin/sync -f "$BIN_DIR"

printf '%s\n' "$BIN_DIR/$INSTALL_NAME"

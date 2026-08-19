#!/usr/bin/env bash
# Browse the email memory store in read-only snapshot mode.
# Safe to run while ingestion or extraction is in progress.
ROOT="${EMS_ROOT:-${XDG_STATE_HOME:-$HOME/.local/state}/email-memory-store}"
source "$HOME/myenv/bin/activate"
exec email-memory-store --root "$ROOT" browse --snapshot

#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
STRICT_DEPENDENCY_CHECK="${STRICT_DEPENDENCY_CHECK:-0}"
RUN_COMPILEALL="${RUN_COMPILEALL:-1}"
RUN_DEPENDENCY_CHECK="${RUN_DEPENDENCY_CHECK:-1}"
RUN_RUFF="${RUN_RUFF:-auto}"
RUN_MYPY="${RUN_MYPY:-auto}"
RUN_PYTEST="${RUN_PYTEST:-1}"
REQUIRE_RUFF="${REQUIRE_RUFF:-0}"
REQUIRE_MYPY="${REQUIRE_MYPY:-0}"
RUFF_TARGETS="${RUFF_TARGETS:-src scripts}"
RUFF_EXTRA_ARGS="${RUFF_EXTRA_ARGS:-}"
MYPY_TARGETS="${MYPY_TARGETS:-src}"
MYPY_EXTRA_ARGS="${MYPY_EXTRA_ARGS:-}"

if [[ "$RUN_COMPILEALL" == "1" ]]; then
  echo "==> Syntax gate"
  "$PYTHON_BIN" -m compileall src tests scripts
else
  echo "==> Syntax gate skipped"
fi

if [[ "$RUN_DEPENDENCY_CHECK" == "1" ]]; then
  echo "==> Dependency gate"
  if command -v uv >/dev/null 2>&1; then
    dependency_check=(uv pip check --python "$PYTHON_BIN")
  else
    dependency_check=("$PYTHON_BIN" -m pip check)
  fi
  if "${dependency_check[@]}"; then
    :
  elif [[ "$STRICT_DEPENDENCY_CHECK" == "1" ]]; then
    echo "Dependency gate failed under strict mode" >&2
    exit 1
  else
    echo "Dependency gate failed in a shared environment; continuing because STRICT_DEPENDENCY_CHECK=0" >&2
  fi
else
  echo "==> Dependency gate skipped"
fi

if [[ "$RUN_RUFF" == "1" ]] || { [[ "$RUN_RUFF" == "auto" ]] && command -v ruff >/dev/null 2>&1; }; then
  if ! command -v ruff >/dev/null 2>&1; then
    echo "Ruff is required but not installed" >&2
    exit 1
  fi
  echo "==> Ruff"
  if [[ -z "${RUFF_TARGETS// }" ]]; then
    echo "No Ruff targets selected; skipping"
  else
    read -r -a ruff_args <<< "$RUFF_TARGETS"
    if [[ -n "${RUFF_EXTRA_ARGS// }" ]]; then
      read -r -a ruff_extra_args <<< "$RUFF_EXTRA_ARGS"
      ruff check "${ruff_extra_args[@]}" "${ruff_args[@]}"
    else
      ruff check "${ruff_args[@]}"
    fi
  fi
elif [[ "$REQUIRE_RUFF" == "1" ]]; then
  echo "Ruff is required but RUN_RUFF is not enabled" >&2
  exit 1
else
  echo "==> Ruff skipped (install ruff to enable local linting)"
fi

if [[ "$RUN_MYPY" == "1" ]] || { [[ "$RUN_MYPY" == "auto" ]] && command -v mypy >/dev/null 2>&1; }; then
  if ! command -v mypy >/dev/null 2>&1; then
    echo "Mypy is required but not installed" >&2
    exit 1
  fi
  echo "==> Mypy"
  if [[ -z "${MYPY_TARGETS// }" ]]; then
    echo "No Mypy targets selected; skipping"
  else
    read -r -a mypy_args <<< "$MYPY_TARGETS"
    if [[ -n "${MYPY_EXTRA_ARGS// }" ]]; then
      read -r -a mypy_extra_args <<< "$MYPY_EXTRA_ARGS"
      mypy --config-file pyproject.toml "${mypy_extra_args[@]}" "${mypy_args[@]}"
    else
      mypy --config-file pyproject.toml "${mypy_args[@]}"
    fi
  fi
elif [[ "$REQUIRE_MYPY" == "1" ]]; then
  echo "Mypy is required but RUN_MYPY is not enabled" >&2
  exit 1
else
  echo "==> Mypy skipped (install mypy to enable local type checking)"
fi

if [[ "$RUN_PYTEST" == "1" ]]; then
  echo "==> Pytest"
  "$PYTHON_BIN" -m pytest -q
else
  echo "==> Pytest skipped"
fi

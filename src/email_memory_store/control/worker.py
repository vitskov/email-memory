"""Detached worker entry point for Email Memory control jobs."""

from __future__ import annotations

import argparse

from .jobs import run_worker


def main() -> int:
    parser = argparse.ArgumentParser(prog="email-memory-store-control-worker")
    parser.add_argument("job_id")
    args = parser.parse_args()
    try:
        return run_worker(args.job_id)
    except OSError, RuntimeError, ValueError:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

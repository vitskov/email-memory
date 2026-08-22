"""Package-owned execution boundary for fixed Email Memory control operations."""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys

from email_memory_store.deployment.cli import BootstrapError


ACTIONS = ("maintenance", "retry_failed_bodies", "reconcile")


@contextmanager
def _operation_lock(runtime_root: Path) -> Iterator[None]:
    """Acquire the same owner-only inode used by scheduled maintenance."""
    lock_path = runtime_root / "nightly_maintenance.lock"
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as error:
        raise BootstrapError("maintenance lock is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
        ):
            raise BootstrapError("maintenance lock is not secure")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def _load_operation_bundle(environ: Mapping[str, str]) -> dict[str, str]:
    from email_memory_store.local_config import load_bundle

    return load_bundle("ingestion", environ=environ)


def _run_store_operation(action: str, bundle: Mapping[str, str]) -> None:
    """Run one fixed operation with private values held only in process memory."""
    from email_memory_store import cli as store_cli
    from email_memory_store.runtime import resolve_runtime_settings

    if action not in {"retry_failed_bodies", "reconcile"}:
        raise BootstrapError("control operation is unsupported")
    runtime_config = bundle["EMAIL_MEMORY_STORE_RUNTIME_CONFIG"]
    command = (
        [
            "--runtime-config",
            runtime_config,
            "retry-failed-bodies",
            "--account",
            "local-attachment",
        ]
        if action == "retry_failed_bodies"
        else [
            "--runtime-config",
            runtime_config,
            "reconcile-ingestion-cursors",
            "--apply",
        ]
    )
    args = store_cli.build_parser().parse_args(command)
    runtime = resolve_runtime_settings(
        runtime_root=args.root,
        work_root=args.work_root,
        fact_store_db=args.fact_store_db,
        runtime_config=args.runtime_config,
        environ={},
    )
    args.root = str(runtime.runtime_root)
    args.work_root = str(runtime.work_root) if runtime.work_root else None
    args.fact_store_db = str(runtime.fact_store_db) if runtime.fact_store_db else None
    args.main_db = str(runtime.main_db)
    args.entity_db = str(runtime.entity_db)
    args.vector_store = str(runtime.vector_store)
    args.work_db = str(runtime.work_db) if runtime.work_db else None
    args.mail_client_executable = runtime.mail_client_executable
    if action == "retry_failed_bodies":
        args.account = bundle["ACCOUNT_NAME"]
        args._use_verified_default_account = True
    args.handler(args)


def run(action: str) -> int:
    if action not in ACTIONS:
        raise BootstrapError("control operation is unsupported")
    if action == "maintenance":
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "email_memory_store.deployment.cli",
                "nightly",
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            shell=False,
            env={
                "HOME": pwd.getpwuid(os.geteuid()).pw_dir,
                "PATH": "/usr/bin:/bin",
                "PYTHONNOUSERSITE": "1",
            },
        )
        return int(completed.returncode)
    canonical_home = pwd.getpwuid(os.geteuid()).pw_dir
    env = {
        "HOME": canonical_home,
        "XDG_CONFIG_HOME": f"{canonical_home}/.config",
        "XDG_DATA_HOME": f"{canonical_home}/.local/share",
        "XDG_STATE_HOME": f"{canonical_home}/.local/state",
        "PATH": "/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
    }
    bundle = _load_operation_bundle(env)
    try:
        with _operation_lock(Path(bundle["EMAIL_MEMORY_ROOT"])):
            _run_store_operation(action, bundle)
    except BlockingIOError:
        return 75
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="email-memory-store-control-operation")
    parser.add_argument("action", choices=ACTIONS)
    args = parser.parse_args(argv)
    try:
        return run(args.action)
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

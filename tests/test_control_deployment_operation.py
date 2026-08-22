from __future__ import annotations

import os
from pathlib import Path

import pytest

from email_memory_store.control import operation


def test_operation_parser_accepts_only_fixed_actions() -> None:
    with pytest.raises(SystemExit):
        operation.main(["arbitrary-command"])


def test_operation_uses_the_existing_owner_only_nightly_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "nightly_maintenance.lock"
    with operation._operation_lock(tmp_path):
        assert lock_path.stat().st_mode & 0o777 == 0o600
        with pytest.raises(BlockingIOError):
            with operation._operation_lock(tmp_path):
                pass


def test_retry_operation_keeps_private_account_out_of_process_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    runtime_config = tmp_path / "runtime.toml"
    private_account = "private-account-label"
    monkeypatch.setattr(
        operation,
        "_load_operation_bundle",
        lambda environ: {
            "EMAIL_MEMORY_ROOT": str(root),
            "EMAIL_MEMORY_STORE_RUNTIME_CONFIG": str(runtime_config),
            "ACCOUNT_NAME": private_account,
        },
    )
    monkeypatch.setattr(operation, "_operation_lock", lambda _root: _NullLock())
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        operation,
        "_run_store_operation",
        lambda action, bundle: captured.update(action=action, bundle=bundle),
    )

    assert operation.run("retry_failed_bodies") == 0
    assert captured["action"] == "retry_failed_bodies"
    assert captured["bundle"]["ACCOUNT_NAME"] == private_account
    assert private_account not in " ".join(os.sys.argv)


class _NullLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> None:
        return None


def test_maintenance_runs_as_a_supervised_child_and_returns_its_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Completed:
        returncode = 75

    def fake_run(command: list[str], **kwargs: object) -> _Completed:
        captured.update(command=command, kwargs=kwargs)
        return _Completed()

    monkeypatch.setattr(operation.subprocess, "run", fake_run)

    assert operation.run("maintenance") == 75
    assert captured["command"] == [
        operation.sys.executable,
        "-I",
        "-m",
        "email_memory_store.deployment.cli",
        "nightly",
    ]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["stdin"] is operation.subprocess.DEVNULL
    assert captured["kwargs"]["stdout"] is operation.subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] is operation.subprocess.DEVNULL


def test_operation_main_redacts_ordinary_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        operation,
        "run",
        lambda _action: (_ for _ in ()).throw(RuntimeError("private detail")),
    )
    assert operation.main(["reconcile"]) == 1


def test_operation_child_import_ignores_hostile_cwd_and_python_environment(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "shadow-imported"
    package = tmp_path / "email_memory_store"
    package.mkdir()
    (package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    startup = tmp_path / "startup.py"
    startup.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )

    completed = operation.subprocess.run(
        [
            operation.sys.executable,
            "-I",
            "-m",
            "email_memory_store.control.operation",
            "--help",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONHOME": str(tmp_path / "fake-python-home"),
            "PYTHONPATH": str(tmp_path),
            "PYTHONSTARTUP": str(startup),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "email-memory-store-control-operation" in completed.stdout
    assert not marker.exists()

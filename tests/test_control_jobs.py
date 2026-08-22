from __future__ import annotations

import json
import os
from pathlib import Path
import threading

import pytest

from email_memory_store.control import jobs


def _environment(tmp_path: Path) -> dict[str, str]:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    return {
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(state),
    }


class _Process:
    pid = os.getpid()


def _enable(environment: dict[str, str]) -> None:
    assert jobs.set_enabled(True, environ=environment) == {
        "enabled": True,
        "paths_redacted": True,
    }


def test_start_is_idempotent_and_conflicting_actions_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)
    _enable(environment)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(command: list[str], **kwargs: object) -> _Process:
        calls.append((command, kwargs))
        return _Process()

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(jobs, "_process_identity", lambda _pid: "test-process")

    first = jobs.start_job("maintenance", environ=environment)
    repeated = jobs.start_job("maintenance", environ=environment)
    conflict = jobs.start_job("reconcile", environ=environment)

    assert first["accepted"] is True
    assert repeated == first | {"idempotent": True}
    assert conflict == {
        "accepted": False,
        "reason": "operation_in_progress",
        "active_job": {
            "job_id": first["job_id"],
            "action": "maintenance",
            "state": "queued",
        },
    }
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:4] == [
        jobs.sys.executable,
        "-I",
        "-m",
        "email_memory_store.control.worker",
    ]
    assert command[4] == first["job_id"]
    assert kwargs["shell"] is False
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is jobs.subprocess.DEVNULL
    assert kwargs["stdout"] is jobs.subprocess.DEVNULL
    assert kwargs["stderr"] is jobs.subprocess.DEVNULL
    assert "EMAIL_MEMORY" not in json.dumps(kwargs["env"])


def test_job_records_are_owner_only_and_status_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)
    _enable(environment)
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *_a, **_kw: _Process())
    monkeypatch.setattr(jobs, "_process_identity", lambda _pid: "test-process")

    started = jobs.start_job("retry_failed_bodies", environ=environment)
    record_path = jobs.state_directory(environment) / f"{started['job_id']}.json"

    assert record_path.stat().st_mode & 0o777 == 0o600
    assert record_path.parent.stat().st_mode & 0o777 == 0o700
    assert jobs.job_status(str(started["job_id"]), environ=environment) == {
        "job_id": started["job_id"],
        "action": "retry_failed_bodies",
        "state": "queued",
        "created_at": pytest.approx(started["created_at"]),
        "updated_at": pytest.approx(started["created_at"]),
        "result": None,
        "paths_redacted": True,
    }
    raw = record_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in raw


@pytest.mark.parametrize("job_id", ["", "../escape", "a" * 31, "g" * 32])
def test_job_status_rejects_untrusted_identifiers(tmp_path: Path, job_id: str) -> None:
    with pytest.raises(ValueError, match="invalid job identifier"):
        jobs.job_status(job_id, environ=_environment(tmp_path))


def test_worker_records_only_redacted_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)
    _enable(environment)
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *_a, **_kw: _Process())
    monkeypatch.setattr(jobs, "_process_identity", lambda _pid: "test-process")
    started = jobs.start_job("reconcile", environ=environment)

    class _Completed:
        returncode = 75

    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> _Completed:
        captured.update(command=command, kwargs=kwargs)
        return _Completed()

    monkeypatch.setattr(jobs.subprocess, "run", fake_run)
    assert jobs.run_worker(str(started["job_id"]), environ=environment) == 75

    status = jobs.job_status(str(started["job_id"]), environ=environment)
    assert status["state"] == "failed"
    assert status["result"] == "maintenance_busy"
    assert captured["command"] == [
        jobs.sys.executable,
        "-I",
        "-m",
        "email_memory_store.control.operation",
        "reconcile",
    ]
    kwargs = captured["kwargs"]
    assert kwargs["shell"] is False
    assert kwargs["stdout"] is jobs.subprocess.DEVNULL
    assert kwargs["stderr"] is jobs.subprocess.DEVNULL


def test_control_is_disabled_until_owner_only_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(
        jobs.subprocess,
        "Popen",
        lambda *_args, **_kwargs: calls.append(object()) or _Process(),
    )

    assert jobs.is_enabled(environ=environment) is False
    assert jobs.start_job("maintenance", environ=environment) == {
        "accepted": False,
        "reason": "control_disabled",
    }
    assert calls == []

    jobs.set_enabled(True, environ=environment)
    activation = jobs.state_directory(environment) / jobs.ACTIVATION_FILE
    assert activation.stat().st_mode & 0o777 == 0o600
    assert jobs.is_enabled(environ=environment) is True
    jobs.set_enabled(False, environ=environment)
    assert jobs.is_enabled(environ=environment) is False


def test_system_status_reports_only_generic_activation_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)

    class _Completed:
        returncode = 0

    monkeypatch.setattr(jobs.subprocess, "run", lambda *_a, **_kw: _Completed())
    disabled = jobs.system_status(environ=environment)
    assert disabled == {
        "control": "disabled",
        "deployment": "ready",
        "active_job": None,
        "paths_redacted": True,
    }
    _enable(environment)
    enabled = jobs.system_status(environ=environment)
    assert enabled["control"] == "enabled"
    assert str(tmp_path) not in json.dumps(enabled)


def test_invalid_activation_state_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)
    directory = jobs.state_directory(environment)
    activation = directory / jobs.ACTIVATION_FILE
    activation.write_text('{"schema_version":1,"enabled":"yes"}', encoding="utf-8")
    activation.chmod(0o600)
    calls: list[object] = []
    monkeypatch.setattr(
        jobs.subprocess,
        "Popen",
        lambda *_args, **_kwargs: calls.append(object()) or _Process(),
    )

    with pytest.raises(RuntimeError, match="activation state is invalid"):
        jobs.start_job("maintenance", environ=environment)
    assert calls == []


def test_terminal_history_is_bounded_without_pruning_active_job(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    directory = jobs.state_directory(environment)
    for index in range(jobs.MAX_TERMINAL_RECORDS + 5):
        job_id = f"{index:032x}"
        jobs._write_record(
            directory,
            {
                "schema_version": 1,
                "job_id": job_id,
                "action": "reconcile",
                "state": "succeeded",
                "created_at": float(index),
                "updated_at": float(index),
                "result": "operation_completed",
                "worker_pid": None,
                "worker_identity": None,
            },
        )
    active_id = "f" * 32
    jobs._write_record(
        directory,
        {
            "schema_version": 1,
            "job_id": active_id,
            "action": "maintenance",
            "state": "running",
            "created_at": -1.0,
            "updated_at": -1.0,
            "result": None,
            "worker_pid": os.getpid(),
            "worker_identity": "active",
        },
    )

    jobs._prune_terminal_records(directory)

    records = jobs._records(directory)
    assert sum(record["state"] == "succeeded" for record in records) == 100
    assert any(record["job_id"] == active_id for record in records)


def test_record_reader_rejects_symlinks_without_following_them(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    _enable(environment)
    directory = jobs.state_directory(environment)
    job_id = "a" * 32
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    (directory / f"{job_id}.json").symlink_to(target)

    with pytest.raises(RuntimeError, match="control job record is invalid"):
        jobs.job_status(job_id, environ=environment)


def test_zombie_identity_is_rejected_and_interrupted_worker_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    zombie = "123 (worker with spaces) " + " ".join(["Z", *(["0"] * 18), "777"])
    assert jobs._parse_linux_process_identity(123, zombie) is None

    running = "123 (worker with spaces) " + " ".join(["S", *(["0"] * 18), "777"])
    assert jobs._parse_linux_process_identity(123, running) == "linux:123:777"

    environment = _environment(tmp_path)
    _enable(environment)
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *_a, **_kw: _Process())
    monkeypatch.setattr(jobs, "_process_identity", lambda _pid: "worker-live")
    started = jobs.start_job("reconcile", environ=environment)
    monkeypatch.setattr(jobs, "_process_identity", lambda _pid: None)

    status = jobs.job_status(str(started["job_id"]), environ=environment)
    assert status["state"] == "failed"
    assert status["result"] == "worker_interrupted"


def test_detached_worker_survives_only_mcp_parent_termination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)
    _enable(environment)
    captured: dict[str, object] = {}

    def fake_popen(command: list[str], **kwargs: object) -> _Process:
        captured.update(command=command, **kwargs)
        return _Process()

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(jobs, "_process_identity", lambda _pid: "worker-live")
    assert jobs.start_job("maintenance", environ=environment)["accepted"] is True
    assert captured["start_new_session"] is True
    assert captured["close_fds"] is True
    assert captured["stdin"] is jobs.subprocess.DEVNULL
    assert captured["stdout"] is jobs.subprocess.DEVNULL
    assert captured["stderr"] is jobs.subprocess.DEVNULL


def test_identity_lookup_failure_cannot_cross_worker_startup_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)
    _enable(environment)
    worker_results: list[int] = []
    worker_threads: list[threading.Thread] = []
    operation_calls: list[object] = []

    def fake_popen(command: list[str], **_kwargs: object) -> _Process:
        thread = threading.Thread(
            target=lambda: worker_results.append(
                jobs.run_worker(command[-1], environ=environment)
            )
        )
        worker_threads.append(thread)
        thread.start()
        return _Process()

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        jobs.subprocess,
        "run",
        lambda *_args, **_kwargs: operation_calls.append(object()),
    )
    monkeypatch.setattr(jobs, "_process_identity", lambda _pid: None)

    started = jobs.start_job("reconcile", environ=environment)
    for thread in worker_threads:
        thread.join(timeout=2)

    assert started["accepted"] is False
    assert started["reason"] == "worker_start_failed"
    assert worker_results == [2]
    assert operation_calls == []
    status = jobs.job_status(str(started["job_id"]), environ=environment)
    assert status["state"] == "failed"
    assert status["result"] == "worker_start_failed"


def test_worker_cannot_race_parent_identity_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)
    _enable(environment)
    worker_results: list[int] = []
    worker_threads: list[threading.Thread] = []

    class _Completed:
        returncode = 0

    def fake_popen(command: list[str], **_kwargs: object) -> _Process:
        thread = threading.Thread(
            target=lambda: worker_results.append(
                jobs.run_worker(command[-1], environ=environment)
            )
        )
        worker_threads.append(thread)
        thread.start()
        return _Process()

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(jobs.subprocess, "run", lambda *_a, **_kw: _Completed())
    monkeypatch.setattr(jobs, "_process_identity", lambda _pid: "durable-identity")

    started = jobs.start_job("reconcile", environ=environment)
    for thread in worker_threads:
        thread.join(timeout=2)

    assert started["accepted"] is True
    assert worker_results == [0]
    status = jobs.job_status(str(started["job_id"]), environ=environment)
    assert status["state"] == "succeeded"
    assert status["result"] == "operation_completed"


def test_worker_rejects_queued_record_without_durable_parent_handshake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)
    directory = jobs.state_directory(environment)
    job_id = "b" * 32
    jobs._write_record(
        directory,
        {
            "schema_version": 1,
            "job_id": job_id,
            "action": "reconcile",
            "state": "queued",
            "created_at": 1.0,
            "updated_at": 1.0,
            "result": None,
            "worker_pid": None,
            "worker_identity": None,
        },
    )
    operation_calls: list[object] = []
    monkeypatch.setattr(jobs, "_process_identity", lambda _pid: "child-identity")
    monkeypatch.setattr(
        jobs.subprocess,
        "run",
        lambda *_args, **_kwargs: operation_calls.append(object()),
    )

    assert jobs.run_worker(job_id, environ=environment) == 2
    assert operation_calls == []
    status = jobs.job_status(job_id, environ=environment)
    assert status["state"] == "failed"
    assert status["result"] == "worker_start_unverified"


def test_worker_child_import_ignores_hostile_cwd_and_python_environment(
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

    completed = jobs.subprocess.run(
        [
            jobs.sys.executable,
            "-I",
            "-m",
            "email_memory_store.control.worker",
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
    assert "email-memory-store-control-worker" in completed.stdout
    assert not marker.exists()

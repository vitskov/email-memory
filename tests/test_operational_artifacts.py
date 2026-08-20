from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat

import pytest

from email_memory_store.operational_artifacts import (
    append_event,
    harden_artifacts,
    main,
    prune_files,
    render_events,
    secure_artifacts,
    write_private_text,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_private_text_write_is_atomic_and_owner_only(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    directory = parent / "operations"
    path = directory / "status.txt"

    write_private_text(path, "first\n")
    write_private_text(path, "replacement\n")

    assert path.read_text(encoding="utf-8") == "replacement\n"
    assert _mode(parent) == 0o700
    assert _mode(directory) == 0o700
    assert _mode(path) == 0o600
    assert list(directory.glob(".status.txt.*")) == []


def test_private_text_can_validate_parent_permissions_without_repair(tmp_path: Path) -> None:
    directory = tmp_path / "operations"
    directory.mkdir(mode=0o755)

    with pytest.raises(PermissionError, match="mode 0700"):
        write_private_text(
            directory / "status.txt",
            "private\n",
            repair_parent_permissions=False,
        )

    assert _mode(directory) == 0o755
    assert not (directory / "status.txt").exists()


def test_secure_creates_and_repairs_explicit_artifacts(tmp_path: Path) -> None:
    directory = tmp_path / "operations"
    directory.mkdir(mode=0o755)
    existing = directory / "events.jsonl"
    existing.write_text("", encoding="utf-8")
    existing.chmod(0o644)

    secure_artifacts(directory, [Path("events.jsonl"), Path("report.txt")])

    assert _mode(directory) == 0o700
    assert _mode(existing) == 0o600
    assert _mode(directory / "report.txt") == 0o600


def test_append_persists_only_allowlisted_safe_fields(tmp_path: Path) -> None:
    path = tmp_path / "private" / "events.jsonl"
    append_event(
        path,
        event_code="run_failed",
        run_id="nightly-20260819T010000Z",
        severity="error",
        stage="export",
        exit_code=23,
        count=4,
        elapsed_seconds=61,
        retryable=True,
        recorded_at=datetime(2026, 8, 19, 5, tzinfo=timezone.utc),
    )

    event = json.loads(path.read_text(encoding="utf-8"))
    assert event == {
        "count": 4,
        "elapsed_seconds": 61,
        "event_code": "run_failed",
        "exit_code": 23,
        "recorded_at": "2026-08-19T05:00:00Z",
        "retryable": True,
        "run_id": "nightly-20260819T010000Z",
        "schema_version": 1,
        "severity": "error",
        "stage": "export",
    }
    assert _mode(path.parent) == 0o700
    assert _mode(path) == 0o600


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "person@example.test"),
        ("run_id", "secret-api-token"),
        ("stage", "/home/person/private"),
        ("event_code", "failed: password=hunter2"),
    ],
)
def test_private_looking_token_values_are_rejected_without_persistence(
    tmp_path: Path, field: str, value: str
) -> None:
    path = tmp_path / "events.jsonl"
    arguments = {"event_code": "run_failed", "run_id": "safe-run", field: value}

    with pytest.raises(ValueError, match="non-sensitive token"):
        append_event(path, **arguments)

    assert not path.exists()
    assert value not in "".join(p.read_text(errors="ignore") for p in tmp_path.rglob("*") if p.is_file())


def test_append_refuses_symlink_without_modifying_target(tmp_path: Path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_text("original\n", encoding="utf-8")
    link = tmp_path / "events.jsonl"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="symbolic links"):
        append_event(link, event_code="run_started", run_id="safe-run")

    assert target.read_text(encoding="utf-8") == "original\n"


def test_append_refuses_hardlink_without_modifying_target(tmp_path: Path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_text("original\n", encoding="utf-8")
    link = tmp_path / "events.jsonl"
    os.link(target, link)

    with pytest.raises(ValueError, match="exactly one hard link"):
        append_event(link, event_code="run_started", run_id="safe-run")

    assert target.read_text(encoding="utf-8") == "original\n"


def test_harden_preflights_symlinks_before_changing_any_file(tmp_path: Path) -> None:
    directory = tmp_path / "operations"
    directory.mkdir()
    regular = directory / "a.log"
    regular.write_text("safe", encoding="utf-8")
    regular.chmod(0o644)
    target = tmp_path / "elsewhere.log"
    target.write_text("private", encoding="utf-8")
    (directory / "z.log").symlink_to(target)

    with pytest.raises(ValueError, match="symbolic links"):
        harden_artifacts(directory, ["*.log"])

    assert _mode(regular) == 0o644
    assert _mode(target) != 0o600


def test_render_validated_events_as_generic_human_text(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    report = tmp_path / "report.txt"
    append_event(
        events,
        event_code="retry_scheduled",
        run_id="weekly-32",
        severity="warning",
        stage="delivery",
        count=2,
        retryable=True,
        recorded_at=datetime(2026, 8, 19, 5, tzinfo=timezone.utc),
    )

    assert render_events(events, report) == 1
    assert report.read_text(encoding="utf-8") == (
        "[2026-08-19T05:00:00Z] WARNING: retry scheduled; "
        "run=weekly-32 stage=delivery count=2 retryable=true\n"
    )
    assert _mode(report) == 0o600


def test_render_rejects_arbitrary_fields_without_replacing_output(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "recorded_at": "2026-08-19T05:00:00Z",
                "event_code": "run_failed",
                "run_id": "safe-run",
                "severity": "error",
                "message": "seeded private string",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "report.txt"
    output.write_text("existing\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported fields"):
        render_events(events, output)

    assert output.read_text(encoding="utf-8") == "existing\n"


def test_prune_removes_only_old_files_with_companion_marker(tmp_path: Path) -> None:
    directory = tmp_path / "batches"
    directory.mkdir()
    old_sent = directory / "week-30.txt"
    old_marker = directory / "week-30.txt.sent"
    old_unsent = directory / "week-31.txt"
    recent_sent = directory / "week-32.txt"
    recent_marker = directory / "week-32.txt.sent"
    for path in (old_sent, old_marker, old_unsent, recent_sent, recent_marker):
        path.write_text("artifact", encoding="utf-8")
    old_timestamp = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()
    recent_timestamp = datetime(2026, 8, 18, tzinfo=timezone.utc).timestamp()
    for path in (old_sent, old_marker, old_unsent):
        os.utime(path, (old_timestamp, old_timestamp))
    for path in (recent_sent, recent_marker):
        os.utime(path, (recent_timestamp, recent_timestamp))

    removed = prune_files(
        directory,
        ["week-*.txt"],
        older_than_days=14,
        companion_suffix=".sent",
        now=datetime(2026, 8, 19, tzinfo=timezone.utc),
    )

    assert removed == [old_sent, old_marker]
    assert not old_sent.exists()
    assert not old_marker.exists()
    assert old_unsent.exists()
    assert recent_sent.exists()
    assert recent_marker.exists()


def test_prune_refuses_symlinked_companion_without_deleting_primary(tmp_path: Path) -> None:
    directory = tmp_path / "batches"
    directory.mkdir()
    primary = directory / "week-30.txt"
    primary.write_text("artifact", encoding="utf-8")
    timestamp = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()
    os.utime(primary, (timestamp, timestamp))
    target = tmp_path / "marker-target"
    target.write_text("marker", encoding="utf-8")
    (directory / "week-30.txt.sent").symlink_to(target)

    with pytest.raises(ValueError, match="symbolic links"):
        prune_files(
            directory,
            ["week-*.txt"],
            14,
            ".sent",
            now=datetime(2026, 8, 19, tzinfo=timezone.utc),
        )

    assert primary.exists()
    assert target.read_text(encoding="utf-8") == "marker"


def test_module_cli_supports_append_render_harden_and_prune(tmp_path: Path) -> None:
    directory = tmp_path / "operations"
    events = directory / "events.jsonl"
    report = directory / "report.txt"

    assert main(["secure", "--directory", str(directory), "--file", "events.jsonl"]) == 0
    assert main(
        [
            "append",
            "--path",
            str(events),
            "--event-code",
            "run_succeeded",
            "--run-id",
            "manual-1",
            "--retryable",
            "false",
        ]
    ) == 0
    assert main(["render", "--input", str(events), "--output", str(report)]) == 0
    report.chmod(0o644)
    assert main(["harden", "--directory", str(directory), "--pattern", "*.txt"]) == 0
    assert _mode(report) == 0o600
    assert main(["prune", "--directory", str(directory), "--pattern", "*.old", "--days", "0"]) == 0

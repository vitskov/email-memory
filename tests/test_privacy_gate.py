from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "privacy_gate.py"


def _load_privacy_gate():
    spec = importlib.util.spec_from_file_location("privacy_gate", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


privacy_gate = _load_privacy_gate()


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("owner@" + "organization.local", "non-synthetic-email"),
        ("/home/" + "operator/private/config.json", "absolute-local-home-path"),
        ("telegram" + ":private-destination", "telegram-routing-target"),
        ("api_key=" + "sensitive-value-123", "credential-assignment"),
        ("ghp_" + "A" * 36, "known-secret-token"),
        ("-----BEGIN " + "PRIVATE KEY-----", "private-key-material"),
        ("Authorization: Bearer " + "A" * 24, "bearer-credential"),
    ],
)
def test_scan_text_rejects_generic_private_signals(text: str, rule: str) -> None:
    findings = privacy_gate.scan_text(text, "candidate.txt")

    assert rule in {finding.rule for finding in findings}
    assert all(text not in finding.render() for finding in findings)


def test_scan_text_accepts_reserved_examples_and_placeholders() -> None:
    text = "\n".join(
        [
            "owner@example.test",
            "maintainer@example.com",
            "/home/person/private/config.json",
            "password=redacted",
        ]
    )

    assert privacy_gate.scan_text(text, "candidate.txt") == []


def test_scan_path_rejects_private_runtime_names(tmp_path: Path) -> None:
    runtime_file = tmp_path / "runtime" / "state.duckdb"
    runtime_file.parent.mkdir()
    runtime_file.write_bytes(b"binary\x00state")

    findings = privacy_gate.scan_path(tmp_path)

    rules = {finding.rule for finding in findings}
    assert "private-runtime-directory" in rules
    assert "private-runtime-file" in rules


@pytest.mark.parametrize("kind", ["wheel", "tar"])
def test_scan_path_inspects_package_archive_members(tmp_path: Path, kind: str) -> None:
    private_text = "contact@" + "organization.local"
    if kind == "wheel":
        archive_path = tmp_path / "candidate.whl"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("package/config.txt", private_text)
    else:
        archive_path = tmp_path / "candidate.tar.gz"
        payload = tmp_path / "config.txt"
        payload.write_text(private_text, encoding="utf-8")
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(payload, arcname="package/config.txt")

    findings = privacy_gate.scan_path(archive_path)

    assert "non-synthetic-email" in {finding.rule for finding in findings}


def test_cli_git_history_scans_deleted_reachable_blobs(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Example User"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "user@example.test"], cwd=tmp_path, check=True
    )
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("person@" + "organization.local", encoding="utf-8")
    subprocess.run(["git", "add", "candidate.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "add candidate"], cwd=tmp_path, check=True)
    candidate.unlink()
    subprocess.run(["git", "add", "-u"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "remove candidate"], cwd=tmp_path, check=True)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--tracked", "--git-history"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "non-synthetic-email" in result.stderr
    assert "organization.local" not in result.stderr

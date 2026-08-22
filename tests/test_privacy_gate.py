from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shlex
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


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["/usr/bin/git", *arguments], cwd=cwd, check=True)


def _git_output(*arguments: str, cwd: Path) -> str:
    return subprocess.run(
        ["/usr/bin/git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repository(path: Path) -> None:
    path.mkdir()
    _git("init", "-q", cwd=path)
    _git("config", "user.name", "Example User", cwd=path)
    _git("config", "user.email", "user@example.test", cwd=path)


def _commit_file(repository: Path, name: str, contents: str) -> None:
    (repository / name).write_text(contents, encoding="utf-8")
    _git("add", name, cwd=repository)
    _git("commit", "-qm", f"add {name}", cwd=repository)


def _write_local_denylist(tmp_path: Path, text: str = "tenant42.test\n") -> Path:
    config_dir = tmp_path / "private-config"
    config_dir.mkdir(mode=0o700)
    config_dir.chmod(0o700)
    denylist = config_dir / "public-export-denylist.txt"
    denylist.write_text(text, encoding="utf-8")
    denylist.chmod(0o600)
    return denylist


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


def test_local_denylist_matches_literal_identifier_case_insensitively(
    tmp_path: Path,
) -> None:
    denylist_path = _write_local_denylist(
        tmp_path, "# deployment identifiers\n\ntenant42.test\n"
    )
    local_denylist = privacy_gate.load_local_denylist(denylist_path)

    findings = privacy_gate.scan_text(
        "TENANT42.TEST xtenant42.test tenant42.testx",
        "candidate.txt",
        local_denylist=local_denylist,
    )

    assert [finding.rule for finding in findings] == ["local-denylist-identifier"]
    assert all("tenant42.test" not in finding.render().lower() for finding in findings)


@pytest.mark.parametrize(
    ("contents", "mode", "expected_error"),
    [
        ("tenant42.test\n", 0o644, "mode must be 0600"),
        ("# comments only\n\n", 0o600, "at least one rule"),
    ],
)
def test_local_denylist_rejects_unsafe_or_empty_files(
    tmp_path: Path, contents: str, mode: int, expected_error: str
) -> None:
    denylist_path = _write_local_denylist(tmp_path, contents)
    denylist_path.chmod(mode)

    with pytest.raises(privacy_gate.LocalDenylistError, match=expected_error):
        privacy_gate.load_local_denylist(denylist_path)


def test_local_denylist_rejects_symlink_and_non_owner_only_parent(
    tmp_path: Path,
) -> None:
    denylist_path = _write_local_denylist(tmp_path)
    symlink = tmp_path / "denylist-link"
    symlink.symlink_to(denylist_path)

    with pytest.raises(privacy_gate.LocalDenylistError, match="non-symlink"):
        privacy_gate.load_local_denylist(symlink)

    denylist_path.parent.chmod(0o755)
    with pytest.raises(
        privacy_gate.LocalDenylistError, match="parent mode must be 0700"
    ):
        privacy_gate.load_local_denylist(denylist_path)


def test_local_denylist_requires_current_user_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    denylist_path = _write_local_denylist(tmp_path)
    monkeypatch.setattr(
        privacy_gate.os, "getuid", lambda: denylist_path.stat().st_uid + 1
    )

    with pytest.raises(privacy_gate.LocalDenylistError, match="current user"):
        privacy_gate.load_local_denylist(denylist_path)


def test_scan_path_rejects_private_runtime_names(tmp_path: Path) -> None:
    runtime_file = tmp_path / "runtime" / "state.duckdb"
    runtime_file.parent.mkdir()
    runtime_file.write_bytes(b"binary\x00state")

    findings = privacy_gate.scan_path(tmp_path)

    rules = {finding.rule for finding in findings}
    assert "private-runtime-directory" in rules
    assert "private-runtime-file" in rules


@pytest.mark.parametrize("prefix", [b"binary\x00", b"binary\xff"], ids=["nul", "invalid-utf8"])
def test_scan_path_detects_local_identifier_after_binary_prefix(
    tmp_path: Path, prefix: bytes
) -> None:
    private_identifier = "raw-binary-tenant.test"
    local_denylist = privacy_gate.load_local_denylist(
        _write_local_denylist(tmp_path, f"{private_identifier}\n")
    )
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(prefix + private_identifier.encode("utf-8"))

    findings = privacy_gate.scan_path(candidate, local_denylist=local_denylist)

    assert "local-denylist-identifier" in {finding.rule for finding in findings}
    assert all(private_identifier not in finding.render() for finding in findings)


def test_scan_bytes_accepts_clean_binary_content() -> None:
    data = b"\x89PNG\r\n\x1a\n\x00\xff\x10synthetic-binary-data owner@example.test"

    assert privacy_gate.scan_bytes(data, "candidate.bin") == []


@pytest.mark.parametrize(
    ("data", "rule"),
    [
        (b"binary\x00private@" + b"organization.local", "non-synthetic-email"),
        (b"binary\xffghp_" + b"A" * 36, "known-secret-token"),
    ],
)
def test_scan_bytes_applies_generic_rules_after_binary_prefix(
    data: bytes, rule: str
) -> None:
    findings = privacy_gate.scan_bytes(data, "candidate.bin")

    assert rule in {finding.rule for finding in findings}
    assert all(data.decode("utf-8", errors="replace") not in finding.render() for finding in findings)


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


def test_scan_path_applies_local_denylist_inside_archive(tmp_path: Path) -> None:
    local_denylist = privacy_gate.load_local_denylist(_write_local_denylist(tmp_path))
    archive_path = tmp_path / "candidate.whl"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("package/TENANT42.TEST.txt", "TENANT42.TEST")

    findings = privacy_gate.scan_path(archive_path, local_denylist=local_denylist)

    assert "local-denylist-identifier" in {finding.rule for finding in findings}
    assert all("tenant42.test" not in finding.render().lower() for finding in findings)


@pytest.mark.parametrize("prefix", [b"binary\x00", b"binary\xff"], ids=["nul", "invalid-utf8"])
def test_scan_archive_detects_local_identifier_after_binary_prefix(
    tmp_path: Path, prefix: bytes
) -> None:
    private_identifier = "archive-binary-tenant.test"
    local_denylist = privacy_gate.load_local_denylist(
        _write_local_denylist(tmp_path, f"{private_identifier}\n")
    )
    archive_path = tmp_path / "candidate.whl"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "package/candidate.bin", prefix + private_identifier.encode("utf-8")
        )

    findings = privacy_gate.scan_path(archive_path, local_denylist=local_denylist)

    assert "local-denylist-identifier" in {finding.rule for finding in findings}
    assert all(private_identifier not in finding.render() for finding in findings)


def test_cli_git_history_scans_deleted_reachable_blobs(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Example User"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "user@example.test"], cwd=tmp_path, check=True
    )
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("person@" + "organization.local", encoding="utf-8")
    subprocess.run(["git", "add", "candidate.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "add candidate"], cwd=tmp_path, check=True)
    candidate.unlink()
    subprocess.run(["git", "add", "-u"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "remove candidate"], cwd=tmp_path, check=True
    )

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


def test_cli_local_denylist_scans_tracked_tree_and_history_without_disclosure(
    tmp_path: Path,
) -> None:
    denylist_path = _write_local_denylist(tmp_path)
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Example User"], cwd=repository, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "user@example.test"],
        cwd=repository,
        check=True,
    )
    historical = repository / "historical.txt"
    historical.write_text("tenant42.test", encoding="utf-8")
    subprocess.run(["git", "add", "historical.txt"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "add historical"], cwd=repository, check=True
    )
    historical.unlink()
    tracked = repository / "tracked-TENANT42.TEST.txt"
    tracked.write_text("TENANT42.TEST", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "replace candidate"], cwd=repository, check=True
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--tracked",
            "--git-history",
            "--local-denylist",
            str(denylist_path),
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "local-denylist-identifier" in result.stderr
    assert "[local-identifier]" in result.stderr
    assert "tenant42.test" not in result.stderr.lower()


def test_cli_git_scan_ignores_path_git_replacement(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _init_repository(repository)
    _commit_file(repository, "candidate.txt", "owner@example.test")
    marker = tmp_path / "fake-git-ran"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        f"#!/bin/sh\n: > {shlex.quote(str(marker))}\nexit 99\n", encoding="utf-8"
    )
    fake_git.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = str(fake_bin)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--tracked", "--git-history"],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert not marker.exists()


def test_cli_git_scan_ignores_inherited_git_dir_without_disclosure(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _init_repository(repository)
    _commit_file(repository, "candidate.txt", "owner@example.test")
    redirected = tmp_path / "redirected"
    _init_repository(redirected)
    private_identifier = "redirected-tenant.test"
    _commit_file(redirected, "candidate.txt", private_identifier)
    denylist_path = _write_local_denylist(tmp_path, f"{private_identifier}\n")
    environment = os.environ.copy()
    environment["GIT_DIR"] = str(redirected / ".git")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--tracked",
            "--git-history",
            "--local-denylist",
            str(denylist_path),
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert private_identifier not in result.stdout
    assert private_identifier not in result.stderr


def test_cli_git_scan_disables_hostile_fsmonitor_and_global_config(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _init_repository(repository)
    _commit_file(repository, "candidate.txt", "owner@example.test")
    marker = tmp_path / "fsmonitor-ran"
    hostile_fsmonitor = tmp_path / "hostile-fsmonitor"
    hostile_fsmonitor.write_text(
        f"#!/bin/sh\n: > {shlex.quote(str(marker))}\nprintf '2\\n'\n",
        encoding="utf-8",
    )
    hostile_fsmonitor.chmod(0o755)
    _git("config", "core.fsmonitor", str(hostile_fsmonitor), cwd=repository)
    hostile_global = tmp_path / "hostile.gitconfig"
    hostile_global.write_text(
        f"[core]\n\tfsmonitor = {hostile_fsmonitor}\n", encoding="utf-8"
    )
    _git("ls-files", cwd=repository)
    assert marker.exists()
    marker.unlink()
    environment = os.environ.copy()
    environment["GIT_CONFIG_GLOBAL"] = str(hostile_global)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--tracked"],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert not marker.exists()


def test_cli_git_history_ignores_replacement_objects_without_disclosure(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _init_repository(repository)
    private_identifier = "original-replaced-commit.test"
    _commit_file(repository, "candidate.txt", "owner@example.test")
    _git("commit", "--amend", "-qm", private_identifier, cwd=repository)
    original_commit = _git_output("rev-parse", "HEAD", cwd=repository)
    tree = _git_output("rev-parse", "HEAD^{tree}", cwd=repository)
    replacement_commit = _git_output(
        "commit-tree", tree, "-m", "benign replacement", cwd=repository
    )
    _git("replace", original_commit, replacement_commit, cwd=repository)
    denylist_path = _write_local_denylist(tmp_path, f"{private_identifier}\n")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--git-history",
            "--local-denylist",
            str(denylist_path),
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "local-denylist-identifier" in result.stderr
    assert private_identifier not in result.stderr


def test_cli_git_history_ignores_legacy_grafts_without_disclosure(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _init_repository(repository)
    private_identifier = "original-grafted-commit.test"
    _commit_file(repository, "first.txt", "owner@example.test")
    _git("commit", "--amend", "-qm", private_identifier, cwd=repository)
    _commit_file(repository, "second.txt", "maintainer@example.test")
    head = _git_output("rev-parse", "HEAD", cwd=repository)
    graft_file = repository / ".git" / "info" / "grafts"
    graft_file.write_text(f"{head}\n", encoding="ascii")
    denylist_path = _write_local_denylist(tmp_path, f"{private_identifier}\n")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--git-history",
            "--local-denylist",
            str(denylist_path),
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "local-denylist-identifier" in result.stderr
    assert private_identifier not in result.stderr


def test_cli_git_history_rejects_shallow_repository(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    _init_repository(origin)
    _commit_file(origin, "first.txt", "owner@example.test")
    _commit_file(origin, "second.txt", "maintainer@example.test")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["/usr/bin/git", "clone", "-q", "--depth", "1", origin.as_uri(), str(shallow)],
        check=True,
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--git-history"],
        cwd=shallow,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "requires a complete non-shallow clone" in result.stderr


def test_cli_git_history_scans_denylisted_commit_metadata_without_disclosure(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _init_repository(repository)
    (repository / "candidate.txt").write_text("owner@example.test", encoding="utf-8")
    _git("add", "candidate.txt", cwd=repository)
    private_values = (
        "author-metadata.test",
        "committer-metadata.test",
        "message-metadata.test",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": private_values[0],
            "GIT_AUTHOR_EMAIL": "author@example.test",
            "GIT_COMMITTER_NAME": private_values[1],
            "GIT_COMMITTER_EMAIL": "committer@example.test",
        }
    )
    subprocess.run(
        ["/usr/bin/git", "commit", "-qm", private_values[2]],
        cwd=repository,
        env=environment,
        check=True,
    )
    denylist_path = _write_local_denylist(tmp_path, "\n".join(private_values) + "\n")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--git-history",
            "--local-denylist",
            str(denylist_path),
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stderr.count("local-denylist-identifier") == 3
    assert "candidate.txt" not in result.stderr
    assert all(value not in result.stderr for value in private_values)


def test_git_object_metadata_scan_survives_non_utf8_bytes(tmp_path: Path) -> None:
    private_identifier = "metadata-after-invalid-byte.test"
    local_denylist = privacy_gate.load_local_denylist(
        _write_local_denylist(tmp_path, f"{private_identifier}\n")
    )

    findings = privacy_gate._scan_git_object_metadata(
        b"encoding ISO-8859-1\n\xff\n" + private_identifier.encode("ascii"),
        "git:commit:000000000000",
        local_denylist=local_denylist,
    )

    assert [finding.rule for finding in findings] == ["local-denylist-identifier"]
    assert private_identifier not in findings[0].render()


def test_cli_git_history_scans_annotated_tag_metadata_without_disclosure(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _init_repository(repository)
    _commit_file(repository, "candidate.txt", "owner@example.test")
    private_values = (
        "private-release.test",
        "tagger-metadata.test",
        "tag-message-metadata.test",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_COMMITTER_NAME": private_values[1],
            "GIT_COMMITTER_EMAIL": "tagger@example.test",
        }
    )
    subprocess.run(
        ["/usr/bin/git", "tag", "-a", private_values[0], "-m", private_values[2]],
        cwd=repository,
        env=environment,
        check=True,
    )
    denylist_path = _write_local_denylist(tmp_path, "\n".join(private_values) + "\n")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--git-history",
            "--local-denylist",
            str(denylist_path),
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stderr.count("local-denylist-identifier") == 3
    assert all(value not in result.stderr for value in private_values)


def test_cli_git_history_applies_generic_rules_to_commit_metadata(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _init_repository(repository)
    (repository / "candidate.txt").write_text("owner@example.test", encoding="utf-8")
    _git("add", "candidate.txt", cwd=repository)
    private_email = "metadata@" + "organization.local"
    private_token = "ghp_" + "A" * 36
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Example Author",
            "GIT_AUTHOR_EMAIL": private_email,
            "GIT_COMMITTER_NAME": "Example Committer",
            "GIT_COMMITTER_EMAIL": "committer@example.test",
        }
    )
    subprocess.run(
        ["/usr/bin/git", "commit", "-qm", private_token],
        cwd=repository,
        env=environment,
        check=True,
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--git-history"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "non-synthetic-email" in result.stderr
    assert "known-secret-token" in result.stderr
    assert private_email not in result.stderr
    assert private_token not in result.stderr


def test_cli_git_history_scans_tree_paths_without_disclosure(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _init_repository(repository)
    private_path = Path("private") / "candidate.duckdb"
    (repository / private_path).parent.mkdir()
    (repository / private_path).write_bytes(b"synthetic\x00fixture")
    _git("add", private_path.as_posix(), cwd=repository)
    _git("commit", "-qm", "add synthetic fixture", cwd=repository)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--git-history"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "private-runtime-directory" in result.stderr
    assert "private-runtime-file" in result.stderr
    assert private_path.as_posix() not in result.stderr


@pytest.mark.parametrize("prefix", [b"binary\x00", b"binary\xff"], ids=["nul", "invalid-utf8"])
def test_cli_git_history_detects_local_identifier_after_binary_prefix(
    tmp_path: Path, prefix: bytes
) -> None:
    repository = tmp_path / "repository"
    _init_repository(repository)
    private_identifier = "blob-binary-tenant.test"
    (repository / "candidate.bin").write_bytes(
        prefix + private_identifier.encode("utf-8")
    )
    _git("add", "candidate.bin", cwd=repository)
    _git("commit", "-qm", "add synthetic binary fixture", cwd=repository)
    denylist_path = _write_local_denylist(tmp_path, f"{private_identifier}\n")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--git-history",
            "--local-denylist",
            str(denylist_path),
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "local-denylist-identifier" in result.stderr
    assert private_identifier not in result.stderr


def test_privacy_workflow_scans_pr_head_instead_of_synthetic_merge() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert (
        "repository: ${{ github.event.pull_request.head.repo.full_name || github.repository }}"
        in workflow
    )
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in workflow

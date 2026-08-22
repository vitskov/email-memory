from __future__ import annotations

import tomllib
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_python_and_uv_contract_is_pinned() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["requires-python"] == ">=3.14"
    assert project["tool"]["uv"]["required-version"] == ">=0.12.5"
    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.14"


def test_lock_and_console_entry_points_are_committed_contracts() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))

    assert lock["requires-python"] == ">=3.14"
    assert project["project"]["scripts"] == {
        "email-memory-store": "email_memory_store.cli:main",
        "email-memory-store-mcp": "email_memory_store.retrieval.mcp_server:main",
        "email-memory-store-control-mcp": (
            "email_memory_store.control.mcp_server:main"
        ),
        "email-memory-store-hermes-addon": (
            "email_memory_store.hermes_addon.installer:main"
        ),
    }
    assert "mcp>=1.0.0,<2" in project["project"]["dependencies"]


def test_bootstrap_exposes_accelerator_selection() -> None:
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "bootstrap.sh"), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--accelerator MODE" in result.stdout
    assert "auto, cpu, cuda, or mps" in result.stdout

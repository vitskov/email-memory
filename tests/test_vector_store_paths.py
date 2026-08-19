from __future__ import annotations

import importlib.util
from pathlib import Path


def _default_persist_path():
    """Load the path helper without sharing test doubles in ``sys.modules``."""
    module_path = Path(__file__).parents[1] / "src" / "email_memory_store" / "retrieval" / "vector_store.py"
    spec = importlib.util.spec_from_file_location("vector_store_path_test_module", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.default_persist_path


def test_default_persist_path_uses_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    assert _default_persist_path()() == tmp_path / "state" / "email-memory-store" / "chroma"


def test_default_persist_path_uses_generic_state_fallback(monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    assert _default_persist_path()() == Path.home() / ".local" / "state" / "email-memory-store" / "chroma"

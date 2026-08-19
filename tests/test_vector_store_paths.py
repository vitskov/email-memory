from __future__ import annotations

import importlib.util
from pathlib import Path


def _vector_store_module():
    """Load the module without sharing test doubles in ``sys.modules``."""
    module_path = Path(__file__).parents[1] / "src" / "email_memory_store" / "retrieval" / "vector_store.py"
    spec = importlib.util.spec_from_file_location("vector_store_path_test_module", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _default_persist_path():
    return _vector_store_module().default_persist_path


def test_default_persist_path_uses_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    assert _default_persist_path()() == tmp_path / "state" / "email-memory-store" / "chroma"


def test_default_persist_path_uses_generic_state_fallback(monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    assert _default_persist_path()() == Path.home() / ".local" / "state" / "email-memory-store" / "chroma"


def test_existing_collection_counts_does_not_create_missing_collections():
    module = _vector_store_module()

    class FakeCollection:
        def __init__(self, name: str, count: int) -> None:
            self.name = name
            self._count = count

        def count(self) -> int:
            return self._count

    class FakeClient:
        def list_collections(self):
            return [
                FakeCollection("holographic_facts", 3),
                FakeCollection("unrelated", 9),
            ]

        def get_collection(self, _name):
            raise AssertionError("collection objects should be used without reopening them")

    store = module.VectorStore.__new__(module.VectorStore)
    store._client = FakeClient()

    assert store.existing_collection_counts() == {"holographic_facts": 3}

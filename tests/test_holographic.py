from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers to inject a fake MemoryStore into the lazy-import machinery without
# requiring a local private provider.
# ---------------------------------------------------------------------------

def _make_fake_memory_store_cls():
    """Return a MagicMock that behaves like the MemoryStore class."""
    instance = MagicMock()
    instance.add_fact.return_value = 42
    instance.remove_fact.return_value = True
    instance.update_fact.return_value = True

    cls = MagicMock(return_value=instance)
    return cls, instance


def _patch_lazy_import(monkeypatch):
    """Patch email_memory_store.holographic._get_memory_store_class to return a fake."""
    import email_memory_store.holographic as holo_mod

    fake_cls, fake_instance = _make_fake_memory_store_cls()

    # Reset module-level cache so each test starts fresh.
    monkeypatch.setattr(holo_mod, "_MemoryStore", None)
    monkeypatch.setattr(holo_mod, "_get_memory_store_class", lambda: fake_cls)

    return fake_cls, fake_instance


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWriteFact:
    def test_calls_add_fact_with_correct_args(self, monkeypatch):
        from email_memory_store.holographic import HolographicMemoryWriter

        fake_cls, fake_instance = _patch_lazy_import(monkeypatch)

        writer = HolographicMemoryWriter(db_path="/tmp/test_memory.db")
        result = writer.write_fact("Alice likes cats", category="interests", tags="animals")

        fake_instance.add_fact.assert_called_once_with(
            "Alice likes cats", category="interests", tags="animals"
        )
        assert result == 42

    def test_returns_fact_id(self, monkeypatch):
        from email_memory_store.holographic import HolographicMemoryWriter

        fake_cls, fake_instance = _patch_lazy_import(monkeypatch)
        fake_instance.add_fact.return_value = 99

        writer = HolographicMemoryWriter(db_path="/tmp/test_memory.db")
        assert writer.write_fact("some fact") == 99

    def test_default_category_and_tags(self, monkeypatch):
        from email_memory_store.holographic import HolographicMemoryWriter

        fake_cls, fake_instance = _patch_lazy_import(monkeypatch)

        writer = HolographicMemoryWriter(db_path="/tmp/test_memory.db")
        writer.write_fact("bare fact")

        fake_instance.add_fact.assert_called_once_with(
            "bare fact", category="general", tags=""
        )


class TestRemoveFact:
    def test_calls_remove_fact_and_returns_result(self, monkeypatch):
        from email_memory_store.holographic import HolographicMemoryWriter

        fake_cls, fake_instance = _patch_lazy_import(monkeypatch)
        fake_instance.remove_fact.return_value = True

        writer = HolographicMemoryWriter(db_path="/tmp/test_memory.db")
        result = writer.remove_fact(7)

        fake_instance.remove_fact.assert_called_once_with(7)
        assert result is True

    def test_returns_false_when_not_found(self, monkeypatch):
        from email_memory_store.holographic import HolographicMemoryWriter

        fake_cls, fake_instance = _patch_lazy_import(monkeypatch)
        fake_instance.remove_fact.return_value = False

        writer = HolographicMemoryWriter(db_path="/tmp/test_memory.db")
        assert writer.remove_fact(999) is False


class TestUpdateFact:
    def test_calls_update_fact_with_correct_args(self, monkeypatch):
        from email_memory_store.holographic import HolographicMemoryWriter

        fake_cls, fake_instance = _patch_lazy_import(monkeypatch)

        writer = HolographicMemoryWriter(db_path="/tmp/test_memory.db")
        result = writer.update_fact(
            3, content="new content", tags="new-tag", category="work"
        )

        fake_instance.update_fact.assert_called_once_with(
            3, content="new content", tags="new-tag", category="work"
        )
        assert result is True

    def test_partial_update_passes_none_for_missing_args(self, monkeypatch):
        from email_memory_store.holographic import HolographicMemoryWriter

        fake_cls, fake_instance = _patch_lazy_import(monkeypatch)

        writer = HolographicMemoryWriter(db_path="/tmp/test_memory.db")
        writer.update_fact(5, tags="only-tags")

        fake_instance.update_fact.assert_called_once_with(
            5, content=None, tags="only-tags", category=None
        )


class TestContextManager:
    def test_close_called_on_exit(self, monkeypatch):
        from email_memory_store.holographic import HolographicMemoryWriter

        fake_cls, fake_instance = _patch_lazy_import(monkeypatch)

        with HolographicMemoryWriter(db_path="/tmp/test_memory.db") as writer:
            writer.write_fact("a fact")  # force store creation

        fake_instance.close.assert_called_once()

    def test_returns_self_on_enter(self, monkeypatch):
        from email_memory_store.holographic import HolographicMemoryWriter

        fake_cls, fake_instance = _patch_lazy_import(monkeypatch)

        writer = HolographicMemoryWriter(db_path="/tmp/test_memory.db")
        with writer as w:
            assert w is writer

    def test_close_is_idempotent(self, monkeypatch):
        from email_memory_store.holographic import HolographicMemoryWriter

        fake_cls, fake_instance = _patch_lazy_import(monkeypatch)

        writer = HolographicMemoryWriter(db_path="/tmp/test_memory.db")
        writer.write_fact("a fact")  # open the store
        writer.close()
        writer.close()  # second close should not raise

        # The underlying store close is called exactly once (second close is no-op
        # because self._store is set to None after first close).
        fake_instance.close.assert_called_once()


class TestDefaultHolographicDbPath:
    def test_returns_xdg_state_path(self, monkeypatch, tmp_path):
        from email_memory_store.holographic import default_holographic_db_path

        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        result = default_holographic_db_path()
        assert isinstance(result, Path)
        assert result == tmp_path / "state" / "email-memory-store" / "fact-store.db"

    def test_falls_back_to_generic_user_state_path(self, monkeypatch):
        from email_memory_store.holographic import default_holographic_db_path

        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        result = default_holographic_db_path()
        assert result.is_absolute()
        assert result == Path.home() / ".local" / "state" / "email-memory-store" / "fact-store.db"


class TestFactStoreProvider:
    def test_requires_an_explicit_local_provider(self, monkeypatch):
        import email_memory_store.holographic as holo_mod

        monkeypatch.setattr(holo_mod, "_MemoryStore", None)
        monkeypatch.delenv(holo_mod.FACT_STORE_PROVIDER_ENV, raising=False)

        with pytest.raises(RuntimeError, match="No local fact-store provider"):
            holo_mod._get_memory_store_class()

    def test_loads_the_explicitly_configured_provider(self, monkeypatch):
        import email_memory_store.holographic as holo_mod

        class LocalMemoryStore:
            pass

        module = ModuleType("test_local_fact_store")
        module.LocalMemoryStore = LocalMemoryStore
        monkeypatch.setitem(sys.modules, module.__name__, module)
        monkeypatch.setattr(holo_mod, "_MemoryStore", None)
        monkeypatch.setenv(
            holo_mod.FACT_STORE_PROVIDER_ENV,
            f"{module.__name__}:LocalMemoryStore",
        )

        assert holo_mod._get_memory_store_class() is LocalMemoryStore

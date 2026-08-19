from __future__ import annotations

import importlib
import os
from pathlib import Path

# The private provider is intentionally not a package dependency. A local
# deployment supplies it only when fact-store integration is enabled.
FACT_STORE_PROVIDER_ENV = "EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER"
_MemoryStore = None


def _state_home(*, environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    state_home = env.get("XDG_STATE_HOME")
    return Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"


def _get_memory_store_class():
    """Load the explicitly configured local fact-store provider on first use."""
    global _MemoryStore
    if _MemoryStore is None:
        provider = os.environ.get(FACT_STORE_PROVIDER_ENV)
        if not provider:
            raise RuntimeError(
                "No local fact-store provider is configured. Install the private "
                f"provider and set {FACT_STORE_PROVIDER_ENV} to 'module:Class'."
            )
        module_name, separator, class_name = provider.partition(":")
        if not separator or not module_name or not class_name:
            raise RuntimeError(
                f"{FACT_STORE_PROVIDER_ENV} must have the form 'module:Class'."
            )
        try:
            module = importlib.import_module(module_name)
            memory_store_class = getattr(module, class_name)
        except (AttributeError, ImportError) as exc:
            raise RuntimeError(
                f"Unable to load local fact-store provider {provider!r}."
            ) from exc
        if not callable(memory_store_class):
            raise RuntimeError(
                f"Configured fact-store provider {provider!r} is not callable."
            )
        _MemoryStore = memory_store_class
    return _MemoryStore


def default_holographic_db_path(*, environ: dict[str, str] | None = None) -> Path:
    """Return the generic XDG state location for a local fact-store database."""
    return _state_home(environ=environ) / "email-memory-store" / "fact-store.db"


class HolographicMemoryWriter:
    """Thin adapter that wraps MemoryStore for use from the email-memory-store package.

    Import of MemoryStore is deferred until first use so that the package can be
    installed and imported without a private provider on the Python path.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_path = default_holographic_db_path()
        self._db_path = Path(db_path).expanduser()
        self._store = None  # lazy

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_store(self):
        if self._store is None:
            cls = _get_memory_store_class()
            self._store = cls(db_path=self._db_path)
        return self._store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write_fact(self, content: str, category: str = "general", tags: str = "") -> int:
        """Insert a fact into holographic memory and return its fact_id."""
        return self._get_store().add_fact(content, category=category, tags=tags)

    def remove_fact(self, fact_id: int) -> bool:
        """Delete a fact by id. Returns True if it existed."""
        return self._get_store().remove_fact(fact_id)

    def update_fact(
        self,
        fact_id: int,
        content: str | None = None,
        tags: str | None = None,
        category: str | None = None,
    ) -> bool:
        """Partially update a fact. Returns True if the row existed."""
        return self._get_store().update_fact(
            fact_id,
            content=content,
            tags=tags,
            category=category,
        )

    def close(self) -> None:
        """Close the underlying database connection if it was opened."""
        if self._store is not None:
            self._store.close()
            self._store = None

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "HolographicMemoryWriter":
        return self

    def __exit__(self, *_) -> None:
        self.close()

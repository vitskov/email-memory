"""Public package facade with dependency-free initialization.

Operational modules such as the deployment coordinator must be able to start
before the project environment has been provisioned.  Keep the traditional
top-level imports available, but resolve them only when callers request them.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import EmailMemoryPaths
    from .holographic import HolographicMemoryWriter, default_holographic_db_path
    from .store import EmailMemoryStore

__all__ = [
    "EmailMemoryPaths",
    "EmailMemoryStore",
    "HolographicMemoryWriter",
    "default_holographic_db_path",
]

_PUBLIC_ATTRIBUTES = {
    "EmailMemoryPaths": (".config", "EmailMemoryPaths"),
    "EmailMemoryStore": (".store", "EmailMemoryStore"),
    "HolographicMemoryWriter": (".holographic", "HolographicMemoryWriter"),
    "default_holographic_db_path": (
        ".holographic",
        "default_holographic_db_path",
    ),
}


def __getattr__(name: str) -> Any:
    """Resolve legacy top-level exports without eager optional dependencies."""
    try:
        module_name, attribute_name = _PUBLIC_ATTRIBUTES[name]
    except KeyError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

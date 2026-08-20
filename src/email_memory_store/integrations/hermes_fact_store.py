"""Adapter for an explicitly configured external Hermes fact store."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import stat
import sys
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any, Literal, TypedDict


FACT_STORE_ROOT_ENV = "EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT"
_FACT_STORE_MODULE = "plugins.memory.holographic.store"
_FACT_STORE_CLASS = "MemoryStore"
_INVALID_ROOT_ERROR = "The configured fact-store module root is not secure."
_IMPORT_ERROR = "Unable to load the configured fact-store provider."


class FactStoreProbe(TypedDict):
    """Redacted readiness result for deployment checks."""

    status: Literal["disabled", "ready", "failed"]


def _path_component_is_secure(component: os.stat_result, *, current_uid: int) -> bool:
    return component.st_uid in {0, current_uid} and not component.st_mode & (
        stat.S_IWGRP | stat.S_IWOTH
    )


def _directory_component_is_secure(
    component: os.stat_result, *, current_uid: int
) -> bool:
    if not stat.S_ISDIR(component.st_mode) or component.st_uid not in {0, current_uid}:
        return False
    broadly_writable = component.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    if not broadly_writable:
        return True
    return component.st_uid == 0 and bool(component.st_mode & stat.S_ISVTX)


def _secure_regular_file(path: Path, *, current_uid: int) -> None:
    try:
        component = path.lstat()
    except OSError:
        raise RuntimeError(_INVALID_ROOT_ERROR) from None
    if (
        not stat.S_ISREG(component.st_mode)
        or not _path_component_is_secure(component, current_uid=current_uid)
        or component.st_nlink != 1
    ):
        raise RuntimeError(_INVALID_ROOT_ERROR)


def _optional_secure_regular_file(path: Path, *, current_uid: int) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise RuntimeError(_INVALID_ROOT_ERROR) from None
    _secure_regular_file(path, current_uid=current_uid)
    return True


def _validate_source_cache(source_file: Path, *, current_uid: int) -> None:
    """Validate bytecode candidates that Python could use for one source file."""
    _optional_secure_regular_file(
        source_file.with_suffix(".pyc"), current_uid=current_uid
    )
    cache_directory = source_file.parent / "__pycache__"
    try:
        cache_metadata = cache_directory.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise RuntimeError(_INVALID_ROOT_ERROR) from None
    if not _directory_component_is_secure(cache_metadata, current_uid=current_uid):
        raise RuntimeError(_INVALID_ROOT_ERROR)

    cache_prefix = f"{source_file.stem}."
    try:
        candidates = tuple(cache_directory.iterdir())
    except OSError:
        raise RuntimeError(_INVALID_ROOT_ERROR) from None
    for candidate in candidates:
        if candidate.name.startswith(cache_prefix) and candidate.suffix == ".pyc":
            _secure_regular_file(candidate, current_uid=current_uid)


def _validate_provider_root(configured_root: str) -> Path:
    candidate = Path(configured_root).expanduser()
    if not candidate.is_absolute():
        raise RuntimeError(_INVALID_ROOT_ERROR)

    try:
        current_uid = os.geteuid()
        current = Path(candidate.anchor)
        if not _directory_component_is_secure(current.lstat(), current_uid=current_uid):
            raise RuntimeError(_INVALID_ROOT_ERROR)
        for part in candidate.parts[1:]:
            if part == "..":
                raise RuntimeError(_INVALID_ROOT_ERROR)
            current /= part
            component = current.lstat()
            if not _directory_component_is_secure(component, current_uid=current_uid):
                raise RuntimeError(_INVALID_ROOT_ERROR)

        provider_root = candidate.resolve(strict=True)
        provider_stat = provider_root.stat()
    except OSError, RuntimeError:
        raise RuntimeError(_INVALID_ROOT_ERROR) from None

    if provider_root != candidate or not stat.S_ISDIR(provider_stat.st_mode):
        raise RuntimeError(_INVALID_ROOT_ERROR)
    if provider_stat.st_uid != current_uid:
        raise RuntimeError(_INVALID_ROOT_ERROR)
    return provider_root


def _validate_provider_module(provider_root: Path) -> Path:
    """Validate all importable package components before any provider code runs."""
    current_uid = os.geteuid()
    current = provider_root
    module_parts = _FACT_STORE_MODULE.split(".")
    for package_name in module_parts[:-1]:
        current /= package_name
        try:
            component = current.lstat()
        except OSError:
            raise RuntimeError(_INVALID_ROOT_ERROR) from None
        if not _directory_component_is_secure(component, current_uid=current_uid):
            raise RuntimeError(_INVALID_ROOT_ERROR)
        _optional_secure_regular_file(current / "__init__.py", current_uid=current_uid)
        _validate_source_cache(current / "__init__.py", current_uid=current_uid)

    module_name = module_parts[-1]
    package_candidate = current / module_name
    try:
        package_metadata = package_candidate.lstat()
    except FileNotFoundError:
        package_metadata = None
    except OSError:
        raise RuntimeError(_INVALID_ROOT_ERROR) from None

    if package_metadata is not None:
        if not _directory_component_is_secure(
            package_metadata, current_uid=current_uid
        ):
            raise RuntimeError(_INVALID_ROOT_ERROR)
        package_initializer = package_candidate / "__init__.py"
        if _optional_secure_regular_file(package_initializer, current_uid=current_uid):
            _validate_source_cache(package_initializer, current_uid=current_uid)
            return package_initializer

    module_file = current / f"{module_name}.py"
    _secure_regular_file(module_file, current_uid=current_uid)
    _validate_source_cache(module_file, current_uid=current_uid)
    return module_file


def _module_is_from_root(
    module: Any, provider_root: Path, expected_module_file: Path
) -> bool:
    module_file = getattr(module, "__file__", None)
    if not isinstance(module_file, str):
        return False
    try:
        resolved_module = Path(module_file).resolve(strict=True)
        resolved_module.relative_to(provider_root)
        if resolved_module != expected_module_file.resolve(strict=True):
            return False
    except OSError, ValueError:
        return False
    return True


def _module_location_is_from_root(module: Any, provider_root: Path) -> bool:
    module_file = getattr(module, "__file__", None)
    if isinstance(module_file, str):
        try:
            Path(module_file).resolve(strict=True).relative_to(provider_root)
        except OSError, ValueError:
            return False
        return True

    module_paths = getattr(module, "__path__", None)
    if module_paths is None:
        return False
    try:
        resolved_paths = [Path(path).resolve(strict=True) for path in module_paths]
        return bool(resolved_paths) and all(
            path.is_relative_to(provider_root) for path in resolved_paths
        )
    except OSError, TypeError:
        return False


def _loaded_provider_modules_are_from_root(provider_root: Path) -> bool:
    parts = _FACT_STORE_MODULE.split(".")
    for end in range(1, len(parts) + 1):
        loaded = sys.modules.get(".".join(parts[:end]))
        if loaded is not None and not _module_location_is_from_root(
            loaded, provider_root
        ):
            return False
    return True


@contextmanager
def _temporary_import_root(provider_root: Path) -> Iterator[None]:
    root_text = str(provider_root)
    original_index: int | None = None
    original_dont_write_bytecode = sys.dont_write_bytecode
    try:
        original_index = sys.path.index(root_text)
        sys.path.pop(original_index)
    except ValueError:
        pass
    sys.path.insert(0, root_text)
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = original_dont_write_bytecode
        try:
            sys.path.remove(root_text)
        except ValueError:
            pass
        if original_index is not None:
            sys.path.insert(min(original_index, len(sys.path)), root_text)


def _load_fact_store_class() -> Any:
    configured_root = os.environ.get(FACT_STORE_ROOT_ENV)
    if not configured_root:
        raise RuntimeError("The external fact-store provider is disabled.")

    provider_root = _validate_provider_root(configured_root)
    expected_module_file = _validate_provider_module(provider_root)
    try:
        if not _loaded_provider_modules_are_from_root(provider_root):
            raise ImportError
        with _temporary_import_root(provider_root):
            module = importlib.import_module(_FACT_STORE_MODULE)
            if not _loaded_provider_modules_are_from_root(
                provider_root
            ) or not _module_is_from_root(module, provider_root, expected_module_file):
                raise ImportError
            memory_store_class = getattr(module, _FACT_STORE_CLASS)
    except Exception:
        raise RuntimeError(_IMPORT_ERROR) from None
    if not callable(memory_store_class):
        raise RuntimeError(_IMPORT_ERROR)
    return memory_store_class


def probe_fact_store() -> FactStoreProbe:
    """Report redacted optional-provider state without constructing a store."""
    if not os.environ.get(FACT_STORE_ROOT_ENV):
        return {"status": "disabled"}
    try:
        _load_fact_store_class()
    except RuntimeError:
        return {"status": "failed"}
    return {"status": "ready"}


class MemoryStore:
    """Instantiate the configured external store behind the public provider path."""

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        return _load_fact_store_class()(*args, **kwargs)

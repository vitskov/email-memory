from __future__ import annotations

from collections.abc import Iterator
import importlib.util
from pathlib import Path
import py_compile
import shutil
from types import ModuleType, SimpleNamespace
import sys
import tempfile

import pytest

from email_memory_store.integrations import hermes_fact_store
from email_memory_store.integrations.hermes_fact_store import (
    FACT_STORE_ROOT_ENV,
    MemoryStore,
    probe_fact_store,
)


@pytest.fixture
def secure_root() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix=".email-memory-facts-", dir=Path.home()))
    root.chmod(0o700)
    try:
        yield root
    finally:
        _forget_provider_modules()
        shutil.rmtree(root)


def _write_fake_provider(root: Path) -> None:
    provider = root / "plugins" / "memory" / "holographic"
    provider.mkdir(parents=True)
    for directory in (root / "plugins", root / "plugins" / "memory", provider):
        directory.chmod(0o700)
    (provider / "store.py").write_text(
        "class MemoryStore:\n"
        "    def __init__(self, *, db_path):\n"
        "        self.db_path = db_path\n",
        encoding="utf-8",
    )
    (provider / "store.py").chmod(0o600)


def _forget_provider_modules() -> None:
    for name in tuple(sys.modules):
        if name == "plugins" or name.startswith("plugins."):
            sys.modules.pop(name)


def test_memory_store_loads_from_secure_explicit_root(
    secure_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fake_provider(secure_root)
    _forget_provider_modules()
    monkeypatch.setenv(FACT_STORE_ROOT_ENV, str(secure_root))

    store = MemoryStore(db_path="test.db")

    assert store.db_path == "test.db"
    assert probe_fact_store() == {"status": "ready"}
    assert str(secure_root) not in sys.path


def test_probe_distinguishes_disabled_from_failed(
    secure_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(FACT_STORE_ROOT_ENV, raising=False)
    assert probe_fact_store() == {"status": "disabled"}

    monkeypatch.setenv(FACT_STORE_ROOT_ENV, str(secure_root))
    assert probe_fact_store() == {"status": "failed"}
    assert str(secure_root) not in sys.path


@pytest.mark.parametrize("mode", [0o720, 0o702])
def test_rejects_group_or_world_writable_path_components(
    secure_root: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    writable_parent = secure_root / "writable"
    provider_root = writable_parent / "provider"
    provider_root.mkdir(parents=True)
    writable_parent.chmod(mode)
    monkeypatch.setenv(FACT_STORE_ROOT_ENV, str(provider_root))

    with pytest.raises(RuntimeError, match="not secure"):
        MemoryStore(db_path="test.db")


def test_rejects_symlink_path_components(
    secure_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_root = secure_root / "provider"
    provider_root.mkdir()
    linked_root = secure_root / "linked"
    linked_root.symlink_to(provider_root, target_is_directory=True)
    monkeypatch.setenv(FACT_STORE_ROOT_ENV, str(linked_root))

    with pytest.raises(RuntimeError, match="not secure"):
        MemoryStore(db_path="test.db")


def test_rejects_symlinked_module_file_outside_provider_root(
    secure_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fake_provider(secure_root)
    module_file = secure_root / "plugins" / "memory" / "holographic" / "store.py"
    module_file.unlink()
    side_effect = secure_root.parent / f"{secure_root.name}-side-effect"
    outside_module = secure_root.parent / f"{secure_root.name}-outside.py"
    outside_module.write_text(
        f"from pathlib import Path\nPath({str(side_effect)!r}).touch()\n",
        encoding="utf-8",
    )
    outside_module.chmod(0o600)
    module_file.symlink_to(outside_module)
    monkeypatch.setenv(FACT_STORE_ROOT_ENV, str(secure_root))

    try:
        with pytest.raises(RuntimeError, match="not secure"):
            MemoryStore(db_path="test.db")
        assert not side_effect.exists()
    finally:
        outside_module.unlink(missing_ok=True)
        side_effect.unlink(missing_ok=True)


def test_rejects_writable_provider_code_before_package_side_effect(
    secure_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fake_provider(secure_root)
    side_effect = secure_root / "side-effect"
    package_initializer = secure_root / "plugins" / "__init__.py"
    package_initializer.write_text(
        f"from pathlib import Path\nPath({str(side_effect)!r}).touch()\n",
        encoding="utf-8",
    )
    package_initializer.chmod(0o600)
    module_file = secure_root / "plugins" / "memory" / "holographic" / "store.py"
    module_file.chmod(0o666)
    monkeypatch.setenv(FACT_STORE_ROOT_ENV, str(secure_root))

    with pytest.raises(RuntimeError, match="not secure"):
        MemoryStore(db_path="test.db")

    assert not side_effect.exists()


def test_rejects_group_writable_bytecode_before_package_side_effect(
    secure_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fake_provider(secure_root)
    side_effect = secure_root / "side-effect"
    package_initializer = secure_root / "plugins" / "__init__.py"
    package_initializer.write_text(
        f"from pathlib import Path\nPath({str(side_effect)!r}).touch()\n",
        encoding="utf-8",
    )
    package_initializer.chmod(0o600)
    module_file = secure_root / "plugins" / "memory" / "holographic" / "store.py"
    cache_file = Path(importlib.util.cache_from_source(str(module_file)))
    py_compile.compile(str(module_file), cfile=str(cache_file), doraise=True)
    cache_file.parent.chmod(0o700)
    cache_file.chmod(0o660)
    monkeypatch.setenv(FACT_STORE_ROOT_ENV, str(secure_root))

    with pytest.raises(RuntimeError, match="not secure"):
        MemoryStore(db_path="test.db")

    assert not side_effect.exists()


def test_secure_bytecode_cache_is_accepted(
    secure_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fake_provider(secure_root)
    module_file = secure_root / "plugins" / "memory" / "holographic" / "store.py"
    cache_file = Path(importlib.util.cache_from_source(str(module_file)))
    py_compile.compile(str(module_file), cfile=str(cache_file), doraise=True)
    cache_file.parent.chmod(0o700)
    cache_file.chmod(0o600)
    monkeypatch.setenv(FACT_STORE_ROOT_ENV, str(secure_root))

    store = MemoryStore(db_path="cached.db")

    assert store.db_path == "cached.db"


def test_rejects_hardlinked_provider_source_and_bytecode(
    secure_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fake_provider(secure_root)
    module_file = secure_root / "plugins" / "memory" / "holographic" / "store.py"
    source_alias = secure_root / "source-alias.py"
    source_alias.hardlink_to(module_file)
    monkeypatch.setenv(FACT_STORE_ROOT_ENV, str(secure_root))

    with pytest.raises(RuntimeError, match="not secure"):
        MemoryStore(db_path="test.db")

    source_alias.unlink()
    cache_file = Path(importlib.util.cache_from_source(str(module_file)))
    py_compile.compile(str(module_file), cfile=str(cache_file), doraise=True)
    cache_file.parent.chmod(0o700)
    cache_file.chmod(0o600)
    cache_alias = secure_root / "cache-alias.pyc"
    cache_alias.hardlink_to(cache_file)

    with pytest.raises(RuntimeError, match="not secure"):
        MemoryStore(db_path="test.db")


def test_rejects_symlinked_package_initializer(
    secure_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fake_provider(secure_root)
    initializer = secure_root / "plugins" / "memory" / "__init__.py"
    outside_initializer = secure_root / "outside-init.py"
    outside_initializer.write_text("raise AssertionError('must not run')\n")
    outside_initializer.chmod(0o600)
    initializer.symlink_to(outside_initializer)
    monkeypatch.setenv(FACT_STORE_ROOT_ENV, str(secure_root))

    with pytest.raises(RuntimeError, match="not secure"):
        MemoryStore(db_path="test.db")


def test_rejects_cached_out_of_root_package_before_provider_side_effect(
    secure_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fake_provider(secure_root)
    side_effect = secure_root / "side-effect"
    memory_initializer = secure_root / "plugins" / "memory" / "__init__.py"
    memory_initializer.write_text(
        f"from pathlib import Path\nPath({str(side_effect)!r}).touch()\n",
        encoding="utf-8",
    )
    memory_initializer.chmod(0o600)
    outside_package = secure_root.parent / f"{secure_root.name}-outside-package"
    outside_package.mkdir(mode=0o700)
    cached_plugins = ModuleType("plugins")
    cached_plugins.__path__ = [str(outside_package)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "plugins", cached_plugins)
    monkeypatch.setenv(FACT_STORE_ROOT_ENV, str(secure_root))

    try:
        with pytest.raises(RuntimeError, match="Unable to load"):
            MemoryStore(db_path="test.db")
        assert not side_effect.exists()
    finally:
        outside_package.rmdir()


def test_rejects_relative_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FACT_STORE_ROOT_ENV, "relative/provider")

    with pytest.raises(RuntimeError, match="not secure"):
        MemoryStore(db_path="test.db")


def test_rejects_root_not_owned_by_current_user(
    secure_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FACT_STORE_ROOT_ENV, str(secure_root))
    monkeypatch.setattr(
        hermes_fact_store.os, "geteuid", lambda: secure_root.stat().st_uid + 1
    )

    with pytest.raises(RuntimeError, match="not secure"):
        MemoryStore(db_path="test.db")


def test_rejects_path_component_owned_by_another_user(
    secure_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_root = secure_root / "foreign" / "provider"
    provider_root.mkdir(parents=True)
    foreign_component = provider_root.parent
    real_lstat = Path.lstat

    def fake_lstat(path: Path):
        result = real_lstat(path)
        if path == foreign_component:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_uid=secure_root.stat().st_uid + 1,
            )
        return result

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    monkeypatch.setenv(FACT_STORE_ROOT_ENV, str(provider_root))

    with pytest.raises(RuntimeError, match="not secure"):
        MemoryStore(db_path="test.db")


def test_import_failure_does_not_expose_configured_root(
    secure_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fake_provider(secure_root)
    provider = secure_root / "plugins" / "memory" / "holographic"
    (provider / "store.py").write_text("not valid Python !!!\n", encoding="utf-8")
    monkeypatch.setenv(FACT_STORE_ROOT_ENV, str(secure_root))

    with pytest.raises(RuntimeError) as caught:
        MemoryStore(db_path="test.db")

    assert str(secure_root) not in str(caught.value)
    assert "plugins.memory" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert str(secure_root) not in sys.path

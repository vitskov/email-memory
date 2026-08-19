from __future__ import annotations

from importlib.abc import Traversable
from importlib.resources import files
from pathlib import Path
from typing import Iterable


_PACKAGE_ROOT = files('email_memory_store.promotion')
_DEFAULT_SOUL_RESOURCE = _PACKAGE_ROOT.joinpath('souls/default.md')
_RULEBOOK_RESOURCE = _PACKAGE_ROOT.joinpath('rulebooks/MEMORY_PROMOTION_RULEBOOK.md')


def read_packaged_default_soul_text() -> str:
    return _DEFAULT_SOUL_RESOURCE.read_text(encoding='utf-8').strip()


def read_packaged_rulebook_text() -> str:
    return _RULEBOOK_RESOURCE.read_text(encoding='utf-8').strip()


def _is_asset_file(relative_parts: tuple[str, ...]) -> bool:
    if not relative_parts:
        return False
    if '__pycache__' in relative_parts:
        return False
    name = relative_parts[-1]
    return not (name.endswith('.py') or name.endswith('.pyc'))


def _iter_packaged_asset_files(
    node: Traversable | None = None,
    relative_parts: tuple[str, ...] = (),
) -> Iterable[tuple[tuple[str, ...], Traversable]]:
    node = _PACKAGE_ROOT if node is None else node
    if node.is_file():
        if _is_asset_file(relative_parts):
            yield relative_parts, node
        return
    for child in node.iterdir():
        yield from _iter_packaged_asset_files(child, relative_parts + (child.name,))


def _seed(destination: str | Path, content: str, *, force: bool = False) -> Path:
    path = Path(destination).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if force or not path.exists():
        path.write_text(content.rstrip() + '\n', encoding='utf-8')
    return path


def seed_runtime_promotion_assets(*, runtime_root: str | Path, force: bool = False) -> dict[str, object]:
    runtime_root_path = Path(runtime_root).expanduser()
    runtime_root_path.mkdir(parents=True, exist_ok=True)
    seeded_paths: list[str] = []
    default_soul_path = runtime_root_path / 'souls' / 'default.md'
    rulebook_path = runtime_root_path / 'rulebooks' / 'MEMORY_PROMOTION_RULEBOOK.md'
    for relative_parts, resource in _iter_packaged_asset_files():
        destination = runtime_root_path.joinpath(*relative_parts)
        _seed(destination, resource.read_text(encoding='utf-8'), force=force)
        seeded_paths.append(str(destination))
    seeded_paths.sort()
    return {
        'runtime_root': runtime_root_path,
        'soul_path': default_soul_path,
        'rulebook_path': rulebook_path,
        'seeded_paths': seeded_paths,
    }

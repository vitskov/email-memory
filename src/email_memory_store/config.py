from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .promotion.assets import seed_runtime_promotion_assets


@dataclass(slots=True)
class EmailMemoryPaths:
    root: Path
    db_path: Path
    entity_db_path: Path
    config_dir: Path
    promotion_config_dir: Path
    promotion_souls_dir: Path
    promotion_rulebooks_dir: Path
    promotion_templates_dir: Path
    default_promotion_soul_path: Path
    promotion_rulebook_path: Path
    batch_review_template_path: Path
    raw_dir: Path
    cache_dir: Path
    reports_dir: Path
    work_root: Path | None
    work_db_path: Path | None

    @classmethod
    def from_root(
        cls,
        root: str | Path,
        work_root: str | Path | None = None,
        *,
        db_path: str | Path | None = None,
        entity_db_path: str | Path | None = None,
        work_db_path: str | Path | None = None,
    ) -> "EmailMemoryPaths":
        root_path = Path(root).expanduser()
        work_root_path = Path(work_root).expanduser() if work_root else None
        return cls(
            root=root_path,
            db_path=Path(db_path).expanduser() if db_path else root_path / 'email_memory.duckdb',
            entity_db_path=(
                Path(entity_db_path).expanduser()
                if entity_db_path else root_path / 'entity_memory.duckdb'
            ),
            config_dir=root_path / 'config',
            promotion_config_dir=root_path / 'config' / 'promotion',
            promotion_souls_dir=root_path / 'config' / 'promotion' / 'souls',
            promotion_rulebooks_dir=root_path / 'config' / 'promotion' / 'rulebooks',
            promotion_templates_dir=root_path / 'config' / 'promotion' / 'templates',
            default_promotion_soul_path=root_path / 'config' / 'promotion' / 'souls' / 'default.md',
            promotion_rulebook_path=root_path / 'config' / 'promotion' / 'rulebooks' / 'MEMORY_PROMOTION_RULEBOOK.md',
            batch_review_template_path=root_path / 'config' / 'promotion' / 'templates' / 'batch_review_prompt.md',
            raw_dir=root_path / 'raw',
            cache_dir=root_path / 'cache',
            reports_dir=root_path / 'reports',
            work_root=work_root_path,
            work_db_path=(
                Path(work_db_path).expanduser()
                if work_db_path else (work_root_path / 'email_memory.work.duckdb') if work_root_path else None
            ),
        )

    def ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.entity_db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.work_db_path:
            self.work_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.promotion_config_dir.mkdir(parents=True, exist_ok=True)
        self.promotion_souls_dir.mkdir(parents=True, exist_ok=True)
        self.promotion_rulebooks_dir.mkdir(parents=True, exist_ok=True)
        self.promotion_templates_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        if self.work_root:
            self.work_root.mkdir(parents=True, exist_ok=True)
        seed_runtime_promotion_assets(runtime_root=self.promotion_config_dir)

"""Loads lineage.yaml, the builder's settings for a given SQL repo."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class LineageConfig:
    repo_root: Path
    dialect: str | None = None
    sql_paths: list[str] = field(default_factory=lambda: ["."])
    dag_paths: list[str] = field(default_factory=list)
    config_paths: list[str] = field(default_factory=list)
    template_searchpath: list[str] = field(default_factory=list)
    schema_files: list[str] = field(default_factory=list)   # INFORMATION_SCHEMA.COLUMNS exports
    schema_bigquery: list[str] = field(default_factory=list)  # read live: project.region-xx / project.dataset
    exclude: list[str] = field(default_factory=list)
    drop_catalog: bool = True
    variables: dict = field(default_factory=dict)
    config_file: Path | None = None   # the lineage.yaml that was loaded (None: defaults)

    @classmethod
    def load(cls, repo_root: str | Path, config_file: str | Path | None = None) -> "LineageConfig":
        root = Path(repo_root).resolve()
        candidates = [Path(config_file)] if config_file else [root / "lineage.yaml", root / "lineage.yml"]
        path = next((p for p in candidates if p.exists()), None)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {} if path else {}
        known = {f for f in cls.__dataclass_fields__ if f not in ("repo_root", "config_file")}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown keys in {path}: {sorted(unknown)}")
        return cls(repo_root=root, config_file=path, **data)

    def resolve(self, paths: list[str]) -> list[Path]:
        return [(self.repo_root / p).resolve() for p in paths]

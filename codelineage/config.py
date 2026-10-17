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
    exclude: list[str] = field(default_factory=list)
    drop_catalog: bool = True
    variables: dict = field(default_factory=dict)

    @classmethod
    def load(cls, repo_root: str | Path, config_file: str | Path | None = None) -> "LineageConfig":
        root = Path(repo_root).resolve()
        path = Path(config_file) if config_file else root / "lineage.yaml"
        data = {}
        if path.exists():
            data = yaml.safe_load(path.read_text()) or {}
        known = {f for f in cls.__dataclass_fields__ if f != "repo_root"}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown keys in {path}: {sorted(unknown)}")
        return cls(repo_root=root, **data)

    def resolve(self, paths: list[str]) -> list[Path]:
        return [(self.repo_root / p).resolve() for p in paths]

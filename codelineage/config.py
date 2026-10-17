"""Loads lineage.yaml, the builder's settings for a given SQL repo."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(ValueError):
    """A problem in lineage.yaml, worded for the person editing it."""


def _read_yaml(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        # context_mark is where the broken construct starts (the real culprit); problem_mark is where
        # the parser gave up, often the next line
        mark = getattr(e, "context_mark", None) or getattr(e, "problem_mark", None)
        line_no = mark.line + 1 if mark else None
        lines = text.splitlines()
        bad = lines[line_no - 1] if line_no and line_no <= len(lines) else ""
        hint = ""
        if re.match(r"^\s*[A-Za-z_]+:\S", bad):
            hint = " Add a space after the colon (e.g. `exclude: [...]`)."
        elif "\t" in bad:
            hint = " Use spaces, not tabs."
        where = f" line {line_no}: {bad.strip()}" if line_no else ""
        raise ConfigError(f"{path.name} is not valid YAML at{where}.{hint}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must be a list of `setting: value` lines")
    return data


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
        data = _read_yaml(path) if path else {}
        known = {f for f in cls.__dataclass_fields__ if f not in ("repo_root", "config_file")}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"{path.name}: unknown setting(s) {', '.join(sorted(unknown))}. "
                              f"Valid settings: {', '.join(sorted(known))}")
        for key in ("sql_paths", "dag_paths", "config_paths", "template_searchpath", "exclude",
                    "schema_files", "schema_bigquery"):
            if isinstance(data.get(key), str):          # `sql_paths: bigquery` -> [bigquery]
                data[key] = [data[key]]
            elif key in data and data[key] is None:
                data[key] = []
        return cls(repo_root=root, config_file=path, **data)

    def resolve(self, paths: list[str]) -> list[Path]:
        return [(self.repo_root / p).resolve() for p in paths]

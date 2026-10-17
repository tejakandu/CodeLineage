"""Crawls a SQL repo (SQL files, Airflow DAGs, config files) and builds the lineage graph."""
from __future__ import annotations

import fnmatch
import json
import logging
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .config import LineageConfig
from .dag_parser import SqlTask, parse_dag_file
from .graph import LineageGraph
from .sql_parser import StatementLineage, parse_sql
from .templating import SqlRenderer, load_config_values

log = logging.getLogger(__name__)


def _excluded(path: Path, root: Path, patterns: list[str]) -> bool:
    rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    return any(fnmatch.fnmatch(rel, p) for p in patterns)


def _statement_line(raw: str, target: str) -> int | None:
    short = re.escape(target.split(".")[-1])
    pat = re.compile(rf"\b(insert|create|merge|replace)\b[^;]*?{short}", re.I | re.S)
    m = pat.search(raw)
    return raw.count("\n", 0, m.start()) + 1 if m else None


def _build_schema(statements: list[StatementLineage], extra: dict) -> dict:
    """sqlglot schema ({db: {table: {col: type}}}) from what earlier statements produce."""
    schema: dict = json.loads(json.dumps(extra)) if extra else {}
    for s in statements:
        parts = s.target_table.split(".")
        if len(parts) != 2 or not s.columns:
            continue
        schema.setdefault(parts[0], {}).setdefault(parts[1], {c: "UNKNOWN" for c in s.columns})
    return schema


def build_graph(cfg: LineageConfig) -> LineageGraph:
    root = cfg.repo_root
    searchpath = cfg.resolve(cfg.template_searchpath) + cfg.resolve(cfg.sql_paths) + [root]
    values = {**load_config_values(cfg.resolve(cfg.config_paths)), **cfg.variables}
    renderer = SqlRenderer(searchpath, values)

    # 1. DAGs -> which task runs which SQL
    tasks: list[SqlTask] = []
    for d in cfg.resolve(cfg.dag_paths):
        for f in sorted(d.rglob("*.py")) if d.is_dir() else [d]:
            if not _excluded(f, root, cfg.exclude):
                tasks.extend(parse_dag_file(f))

    def resolve_template(ref: str) -> Path | None:
        for base in searchpath:
            p = (base / ref).resolve()
            if p.is_file():
                return p
        # DAGs often use absolute deploy paths (/opt/airflow/sql/x.sql): match on the tail.
        parts = Path(ref).parts
        for i in range(1, len(parts)):
            for base in searchpath:
                p = (base / Path(*parts[i:])).resolve()
                if p.is_file():
                    return p
        return None

    tasks_by_file: dict[Path, list[SqlTask]] = {}
    units: list[tuple[str, str, list[SqlTask]]] = []  # (display file, raw sql, tasks)
    unresolved = []
    for t in tasks:
        if t.sql_file:
            p = resolve_template(t.sql_file)
            if p:
                tasks_by_file.setdefault(p, []).append(t)
            else:
                unresolved.append(f"{t.dag_id}.{t.task_id}: SQL file not found: {t.sql_file}")
        elif t.inline_sql:
            rel = Path(t.dag_file).resolve().relative_to(root)
            units.append((f"{rel}::{t.task_id}", t.inline_sql, [t]))

    # 2. SQL files
    seen = set()
    for d in cfg.resolve(cfg.sql_paths):
        for f in sorted(d.rglob("*.sql")) if d.is_dir() else [d]:
            if f in seen or _excluded(f, root, cfg.exclude):
                continue
            seen.add(f)
            units.append((str(f.relative_to(root)), f.read_text(errors="replace"), tasks_by_file.get(f, [])))
    for f, ts in tasks_by_file.items():  # referenced by DAGs but outside sql_paths
        if f not in seen:
            units.append((str(f.relative_to(root)) if f.is_relative_to(root) else str(f), f.read_text(errors="replace"), ts))

    rendered = [(name, raw, renderer.render(raw, ts[0].params if ts else None), ts) for name, raw, ts in units]

    extra_schema = {}
    # 3. Two passes: the first learns each table's output columns so the second can expand SELECT *.
    first = [s for _, _, sql, _ in rendered for s in parse_sql(sql, cfg.dialect, None, cfg.drop_catalog)]
    schema = _build_schema(first, extra_schema)

    tables: dict[str, dict] = {}
    edges: list[dict] = []
    warnings: list[str] = list(unresolved)
    for name, raw, sql, ts in rendered:
        statements = parse_sql(sql, cfg.dialect, schema, cfg.drop_catalog)
        if not statements and re.search(r"\b(insert|create|merge)\b", sql, re.I):
            warnings.append(f"{name}: no lineage extracted (unsupported or unparseable SQL)")
        for s in statements:
            line = _statement_line(raw, s.target_table)
            runs = [{"dag_id": t.dag_id, "task_id": t.task_id, "schedule": t.schedule,
                     "upstream_tasks": t.upstream_tasks} for t in ts] or [{}]
            info = tables.setdefault(s.target_table, {"columns": [], "built_by": []})
            info["columns"] = sorted(set(info["columns"]) | set(s.columns))
            for run in runs:
                info["built_by"].append({"file": name, "line": line, "statement_type": s.statement_type,
                                         "source_tables": s.source_tables, "filters": s.filters, **run})
                for e in s.edges:
                    edges.append({**asdict(e), "file": name, "line": line,
                                  "statement_type": s.statement_type,
                                  "dag_id": run.get("dag_id"), "task_id": run.get("task_id")})
            warnings.extend(f"{name}: {w}" for w in s.warnings)

    return LineageGraph({
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo_root": str(root),
        "dialect": cfg.dialect,
        "tables": tables,
        "edges": edges,
        "warnings": warnings,
    })

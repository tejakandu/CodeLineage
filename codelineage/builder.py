"""Crawls a SQL repo (SQL files, Airflow DAGs, config files) and builds the lineage graph."""
from __future__ import annotations

import fnmatch
import json
import subprocess
import logging
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .config import LineageConfig
from .dag_parser import SqlTask, parse_dag_file
from .graph import LineageGraph
from .schema import load_bigquery_schema, load_information_schema, to_sqlglot_schema
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


def _inferred_columns(statements: list[StatementLineage]) -> dict[str, dict]:
    """Columns each built table produces, as learned from the SQL itself."""
    out: dict[str, dict] = {}
    for s in statements:
        if s.columns:
            out.setdefault(s.target_table, {}).update({c: {} for c in s.columns})
    return out


def _git_commit(root: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _bigquery_schema(cfg: LineageConfig, warnings: list[str], bq_client=None) -> dict:
    """INFORMATION_SCHEMA from BigQuery, cached in .lineage/ so builds still work offline."""
    cache = cfg.repo_root / ".lineage" / "information_schema.json"
    try:
        data = load_bigquery_schema(cfg.schema_bigquery, cfg.drop_catalog, bq_client)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data))
        return data
    except Exception as e:  # noqa: BLE001
        if cache.exists():
            warnings.append(f"BigQuery INFORMATION_SCHEMA unavailable ({e}); using cached copy from {cache.name}")
            return json.loads(cache.read_text())
        warnings.append(f"BigQuery INFORMATION_SCHEMA unavailable and no cache: {e}")
        return {}


def build_graph(cfg: LineageConfig, bq_client=None) -> LineageGraph:
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

    # 3. Schema: the warehouse INFORMATION_SCHEMA (authoritative) plus columns inferred from the SQL.
    #    A first parse pass learns each built table's columns so the second can expand SELECT *.
    early_warnings: list[str] = []
    info_schema = load_information_schema(cfg.resolve(cfg.schema_files), cfg.drop_catalog)
    if cfg.schema_bigquery:
        for t, cols in _bigquery_schema(cfg, early_warnings, bq_client).items():
            for c, info in cols.items():
                info_schema.setdefault(t, {}).setdefault(c, info)
    first = [s for name, _, sql, _ in rendered for s in parse_sql(sql, cfg.dialect, None, cfg.drop_catalog, name)]
    schema = to_sqlglot_schema(info_schema, _inferred_columns(first))

    tables: dict[str, dict] = {}
    edges: list[dict] = []
    warnings: list[str] = list(unresolved) + early_warnings
    for name, raw, sql, ts in rendered:
        statements = parse_sql(sql, cfg.dialect, schema, cfg.drop_catalog, name)
        if not statements and re.search(r"\b(insert|create|merge|update)\b", sql, re.I):
            warnings.append(f"{name}: no lineage extracted (unsupported or unparseable SQL)")
        for s in statements:
            line = s.line or _statement_line(raw, s.target_table)
            runs = [{"dag_id": t.dag_id, "task_id": t.task_id, "schedule": t.schedule,
                     "upstream_tasks": t.upstream_tasks} for t in ts] or [{}]
            info = tables.setdefault(s.target_table, {"columns": [], "built_by": []})
            if s.is_temp:
                info["temp"] = True
            info["columns"] = sorted(set(info["columns"]) | set(s.columns))
            for run in runs:
                info["built_by"].append({"file": name, "line": line, "statement_type": s.statement_type,
                                         "source_tables": s.source_tables, "filters": s.filters, **run})
                for e in s.edges:
                    edges.append({**asdict(e), "file": name, "line": line,
                                  "statement_type": s.statement_type,
                                  "dag_id": run.get("dag_id"), "task_id": run.get("task_id")})
            warnings.extend(f"{name}:{line}: {w}" for w in s.warnings)

    # 4. Check the SQL against the warehouse: columns that don't exist usually mean stale SQL,
    #    a typo, or a table that is missing from the export.
    if info_schema:
        missing = sorted({(e["source_table"], e["source_column"], e["file"]) for e in edges
                          if e.get("source_table") in info_schema and not e.get("ambiguous")
                          and e["source_column"] not in info_schema[e["source_table"]]})
        warnings += [f"{f}: {t}.{c} not found in INFORMATION_SCHEMA" for t, c, f in missing]
        referenced = {e["source_table"] for e in edges if e.get("source_table")}
        unknown = sorted(t for t in referenced - set(info_schema) - set(tables) if "::" not in t)
        if unknown:
            warnings.append(f"{len(unknown)} source tables not in INFORMATION_SCHEMA: {', '.join(unknown[:20])}")

    used = {e["source_table"] for e in edges if e.get("source_table")} | {e["target_table"] for e in edges} | set(tables)
    return LineageGraph({
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo_root": str(root),
        "git_commit": _git_commit(root),
        "dialect": cfg.dialect,
        "tables": tables,
        "edges": edges,
        "schema": {t: info_schema[t] for t in sorted(used) if t in info_schema},
        "warnings": warnings,
    })

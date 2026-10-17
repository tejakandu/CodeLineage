"""Crawls a SQL repo (SQL files, Airflow DAGs, config files) and builds the lineage graph."""
from __future__ import annotations

import json
import subprocess
import time
import logging
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .config import LineageConfig
from .dag_parser import SqlTask, parse_dag_file
from sqlglot.schema import ensure_schema

from .fsutil import read_text, walk_files
from .graph import LineageGraph
from .schema import load_bigquery_schema, load_information_schema, to_sqlglot_schema
from .sql_parser import StatementLineage, parse_sql
from .templating import SqlRenderer, load_config_values

log = logging.getLogger(__name__)


def _statement_line(raw: str, target: str) -> int | None:
    short = re.escape(target.split(".")[-1])
    pat = re.compile(rf"\b(insert|create|merge|replace)\b[^;]*?{short}", re.I | re.S)
    m = pat.search(raw)
    return raw.count("\n", 0, m.start()) + 1 if m else None


def _inferred_columns(statements: list[StatementLineage]) -> dict[str, dict]:
    """Columns of tables the repo creates with CREATE ... AS SELECT. INSERT / MERGE / UPDATE only
    name some of a table's columns, so treating them as the full column list breaks lineage."""
    out: dict[str, dict] = {}
    for s in statements:
        if s.columns and s.statement_type.startswith("CREATE"):
            out.setdefault(s.target_table, {}).update({c: {} for c in s.columns})
    return out


def _git_commit(root: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _bigquery_schema(cfg: LineageConfig, warnings: list[str], bq_client=None) -> dict:
    """INFORMATION_SCHEMA from BigQuery, cached in .lineage/ so builds still work offline."""
    cache = cfg.repo_root / ".lineage" / "information_schema.json"
    try:
        data = load_bigquery_schema(cfg.schema_bigquery, cfg.drop_catalog, bq_client, cfg.bigquery_project)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data), encoding="utf-8")
        return data
    except Exception as e:  # noqa: BLE001
        if cache.exists():
            warnings.append(f"BigQuery INFORMATION_SCHEMA unavailable ({e}); using cached copy from {cache.name}")
            return json.loads(cache.read_text(encoding="utf-8"))
        warnings.append(f"BigQuery INFORMATION_SCHEMA unavailable and no cache: {e}")
        return {}


SLOW_FILE_SECONDS = 30
AMBIGUOUS = Path("<ambiguous>")
_WRITES = re.compile(r"\b(insert\s+into|merge\s+|update\s+\S+\s+set|as\s*\(?\s*(with|select)\b)", re.I)


def _writes_from_query(path: Path) -> bool:
    try:
        return bool(_WRITES.search(read_text(path)))
    except OSError:
        return False


def build_graph(cfg: LineageConfig, bq_client=None, progress=None) -> LineageGraph:
    """progress(stage, done, total, name), if given, is called as files are processed."""
    def report(stage: str, i: int, total: int, name: str) -> None:
        if progress:
            progress(stage, i, total, name)

    root = cfg.repo_root
    searchpath = cfg.resolve(cfg.template_searchpath) + cfg.resolve(cfg.sql_paths) + [root]
    values = {**load_config_values(cfg.resolve(cfg.config_paths)), **cfg.variables}
    renderer = SqlRenderer(searchpath, values)

    # 1. DAGs -> which task runs which SQL
    tasks: list[SqlTask] = []
    dag_files = 0
    io_warnings: list[str] = []
    for d in cfg.resolve(cfg.dag_paths):
        for f in walk_files(d, ".py", root, cfg.exclude, io_warnings):
            dag_files += 1
            tasks.extend(parse_dag_file(f))

    # SQL files are listed first so DAG references can also be matched by bare file name.
    sql_files: list[Path] = []
    for d in cfg.resolve(cfg.sql_paths):
        sql_files += walk_files(d, ".sql", root, cfg.exclude, io_warnings)
    by_name: dict[str, list[Path]] = {}
    for f in dict.fromkeys(sql_files):
        by_name.setdefault(f.name.lower(), []).append(f)
    ambiguous_refs: set[str] = set()

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
        # A bare file name (query_sql_path = "all_claims_view.sql"): use the one SQL file with that name.
        matches = by_name.get(Path(ref).name.lower(), [])
        if len(matches) > 1:
            # Same name in several folders (e.g. ddl_master/x.sql defines the table, dml/x.sql loads it):
            # a DAG task runs logic, so prefer the one file that writes data from a query.
            matches = [m for m in matches if _writes_from_query(m)] or matches
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            ambiguous_refs.add(f"{ref}: {len(matches)} files have this name "
                               f"({', '.join(m.relative_to(root).as_posix() for m in matches[:3])}); "
                               "add the right folder to template_searchpath")
            return AMBIGUOUS
        return None

    tasks_by_file: dict[Path, list[SqlTask]] = {}
    units: list[tuple[str, str, list[SqlTask]]] = []  # (display file, raw sql, tasks)
    unresolved = []
    for t in tasks:
        if t.sql_file:
            p = resolve_template(t.sql_file)
            if p is AMBIGUOUS:
                continue                     # reported once per file name, not as "not found"
            if p:
                tasks_by_file.setdefault(p, []).append(t)
            else:
                unresolved.append(f"{t.dag_id}.{t.task_id}: SQL file not found: {t.sql_file}")
        elif t.inline_sql:
            rel = Path(t.dag_file).resolve().relative_to(root)
            units.append((f"{rel.as_posix()}::{t.task_id}", t.inline_sql, [t]))
    unresolved += sorted(ambiguous_refs)

    # 2. SQL files
    seen = set()
    def read(f: Path) -> str | None:
        try:
            return read_text(f)
        except OSError as e:
            io_warnings.append(f"{f.as_posix()}: could not read ({e.strerror or e})")
            return None

    for f in sql_files:
        if f in seen:
            continue
        seen.add(f)
        text = read(f)
        if text is not None:
            units.append((f.relative_to(root).as_posix() if f.is_relative_to(root) else f.as_posix(), text, tasks_by_file.get(f, [])))
    for f, ts in tasks_by_file.items():  # referenced by DAGs but outside sql_paths
        if f not in seen:
            text = read(f)
            if text is not None:
                units.append((f.relative_to(root).as_posix() if f.is_relative_to(root) else f.as_posix(), text, ts))

    rendered = [(name, raw, renderer.render(raw, ts[0].params if ts else None), ts) for name, raw, ts in units]

    # 3. Schema: the warehouse INFORMATION_SCHEMA (authoritative) plus columns inferred from the SQL.
    #    A first parse pass learns each built table's columns so the second can expand SELECT *.
    early_warnings: list[str] = []
    info_schema = load_information_schema(cfg.resolve(cfg.schema_files), cfg.drop_catalog)
    if cfg.schema_bigquery:
        for t, cols in _bigquery_schema(cfg, early_warnings, bq_client).items():
            for c, info in cols.items():
                info_schema.setdefault(t, {}).setdefault(c, info)
    first, ddl = [], {}
    for i, (name, _, sql, _) in enumerate(rendered, 1):
        report("reading columns", i, len(rendered), name)
        first += parse_sql(sql, cfg.dialect, None, cfg.drop_catalog, name, columns_only=True, ddl=ddl)
    # Schema sources, best first: warehouse INFORMATION_SCHEMA, CREATE TABLE DDL in the repo, then
    # columns of tables the repo creates with CREATE ... AS SELECT.
    schema = to_sqlglot_schema(info_schema, ddl, _inferred_columns(first))
    schema = ensure_schema(schema, dialect=cfg.dialect) if schema else None   # built once, reused

    tables: dict[str, dict] = {}
    edges: list[dict] = []
    warnings: list[str] = list(unresolved) + early_warnings + io_warnings
    for i, (name, raw, sql, ts) in enumerate(rendered, 1):
        report("tracing lineage", i, len(rendered), name)
        started = time.monotonic()
        diagnostics: list[str] = []
        statements = parse_sql(sql, cfg.dialect, schema, cfg.drop_catalog, name, diagnostics)
        elapsed = time.monotonic() - started
        if elapsed > SLOW_FILE_SECONDS:
            warnings.append(f"{name}: slow file ({elapsed:.0f}s to trace); add it to exclude if it isn't needed")
        if diagnostics:
            what = "no lineage extracted" if not statements else f"{len(diagnostics)} statement(s) skipped"
            warnings.append(f"{name}: {what}: {diagnostics[0]}")
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
        "config_file": str(cfg.config_file) if cfg.config_file else None,
        "tables": tables,
        "edges": edges,
        "schema": {t: {**ddl.get(t, {}), **info_schema.get(t, {})} for t in sorted(used)
                   if t in info_schema or t in ddl},
        "warnings": warnings,
        "dag_scan": {"files": dag_files, "sql_tasks": len(tasks),
                     "linked": sum(1 for t in tasks if t.inline_sql) + sum(len(v) for v in tasks_by_file.values())},
    })

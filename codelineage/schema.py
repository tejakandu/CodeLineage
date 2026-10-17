"""Loads warehouse metadata (an export of INFORMATION_SCHEMA.COLUMNS) to make parsing exact.

With it the parser can expand ``SELECT *``, pin unqualified columns in joins to the right table,
and flag SQL that references columns that no longer exist. Column types and descriptions are also
passed to the LLM so it can explain columns in business terms.

Accepted formats (column names are case-insensitive):
  * CSV / TSV / JSON-lines / JSON list of rows with at least ``table_name`` and ``column_name``;
    optional ``table_catalog`` (or ``table_database``), ``table_schema``, ``data_type``,
    ``ordinal_position``, ``description`` (or ``comment`` / ``column_comment``).
    The BigQuery ``INFORMATION_SCHEMA.COLUMN_FIELD_PATHS`` export (``field_path``) works too.
  * JSON nested mapping ``{schema: {table: {column: type}}}``.
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

_ALIASES = {
    "catalog": ("table_catalog", "table_database", "catalog", "project_id", "database"),
    "schema": ("table_schema", "schema", "dataset", "dataset_id", "owner"),
    "table": ("table_name", "table"),
    "column": ("column_name", "field_path", "column"),
    "type": ("data_type", "type", "column_type"),
    "ordinal": ("ordinal_position", "ordinal"),
    "description": ("description", "comment", "column_comment", "remarks"),
}


def _pick(row: dict, key: str):
    for name in _ALIASES[key]:
        v = row.get(name)
        if v not in (None, ""):
            return str(v)
    return None


def _rows(path: Path) -> list[dict]:
    text = path.read_text(errors="replace")
    if path.suffix in (".csv", ".tsv"):
        delim = "\t" if path.suffix == ".tsv" else ","
        return [{k.strip().lower(): v for k, v in r.items() if k} for r in csv.DictReader(text.splitlines(), delimiter=delim)]
    if path.suffix in (".jsonl", ".ndjson"):
        return [{k.lower(): v for k, v in json.loads(line).items()} for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    if isinstance(data, list):
        return [{k.lower(): v for k, v in r.items()} for r in data]
    rows = []  # nested {schema: {table: {column: type}}}
    for db, tables in data.items():
        for table, cols in tables.items():
            for col, typ in cols.items():
                rows.append({"table_schema": db, "table_name": table, "column_name": col, "data_type": typ})
    return rows


def load_information_schema(paths: list[Path], drop_catalog: bool = True) -> dict[str, dict[str, dict]]:
    """Returns ``{table: {column: {"type", "description", "ordinal"}}}`` with lowercase names
    shaped like the graph's table names (``schema.table`` when drop_catalog)."""
    out: dict[str, dict[str, dict]] = {}
    for base in paths:
        files = [base] if base.is_file() else sorted(base.rglob("*")) if base.exists() else []
        for f in files:
            if f.suffix not in (".csv", ".tsv", ".json", ".jsonl", ".ndjson"):
                continue
            for row in _rows(f):
                table, column = _pick(row, "table"), _pick(row, "column")
                if not table or not column:
                    continue
                parts = [] if drop_catalog else [_pick(row, "catalog")]
                parts += [_pick(row, "schema"), table]
                name = ".".join(p for p in parts if p).lower()
                column = column.lower()
                if "." in column:  # nested STRUCT field path: keep only the top-level column
                    column = column.split(".")[0]
                cols = out.setdefault(name, {})
                info = cols.setdefault(column, {})
                for key in ("type", "description", "ordinal"):
                    v = _pick(row, key)
                    if v and key not in info:
                        info[key] = v
    return out


def to_sqlglot_schema(*schemas: dict[str, dict]) -> dict:
    """Nested ``{db: {table: {col: type}}}`` for sqlglot. Earlier arguments win. sqlglot needs every
    table at the same depth, so only names with the most common number of parts are kept."""
    names = {n for s in schemas for n in s}
    if not names:
        return {}
    depth = Counter(n.count(".") for n in names).most_common(1)[0][0]
    nested: dict = {}
    for s in schemas:
        for name, cols in s.items():
            if name.count(".") != depth or "::" in name or not cols:
                continue
            node = nested
            *path, leaf = name.split(".")
            for p in path:
                node = node.setdefault(p, {})
            node.setdefault(leaf, {c: "UNKNOWN" for c in cols})
    return nested

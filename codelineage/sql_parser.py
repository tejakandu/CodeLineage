"""Extracts column-level lineage from SQL statements using sqlglot.

Handles CREATE TABLE/VIEW ... AS SELECT, INSERT [INTO] ... SELECT and MERGE. CTEs and
subqueries are resolved down to physical tables; the CTE names passed through are kept as
``via`` so the explanation can mention them.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.lineage import Node, lineage
from sqlglot.optimizer.qualify import qualify

log = logging.getLogger(__name__)


@dataclass
class ColumnEdge:
    target_table: str
    target_column: str
    source_table: str | None      # None for constants / COUNT(*)
    source_column: str | None
    transformation: str           # the SQL expression producing target_column
    via: list[str] = field(default_factory=list)     # CTEs / subqueries passed through
    steps: list[str] = field(default_factory=list)   # expression at each hop inside the statement


@dataclass
class StatementLineage:
    target_table: str
    statement_type: str
    source_tables: list[str]
    columns: list[str]
    edges: list[ColumnEdge]
    filters: list[str] = field(default_factory=list)   # WHERE / JOIN ON / QUALIFY conditions
    line: int | None = None
    warnings: list[str] = field(default_factory=list)


def table_name(table: exp.Table, drop_catalog: bool = True) -> str:
    parts = [table.catalog, table.db, table.name]
    if drop_catalog:
        parts = parts[1:]
    return ".".join(p for p in parts if p).lower()


def _physical_tables(query: exp.Expr, drop_catalog: bool) -> list[str]:
    ctes = {c.alias_or_name.lower() for c in query.find_all(exp.CTE)}
    out = []
    for t in query.find_all(exp.Table):
        if not t.db and t.name.lower() in ctes:
            continue
        name = table_name(t, drop_catalog)
        if name and name not in out:
            out.append(name)
    return out


def _filters(query: exp.Expr) -> list[str]:
    out = []
    for node in query.find_all(exp.Where, exp.Join, exp.Qualify, exp.Having):
        cond = node.args.get("on") if isinstance(node, exp.Join) else node.this
        if cond is not None:
            label = {"Where": "WHERE", "Join": "JOIN ON", "Qualify": "QUALIFY", "Having": "HAVING"}[type(node).__name__]
            out.append(f"{label} {cond.sql()}")
    return out


def _alias_outputs(query: exp.Query, names: list[str]) -> exp.Query:
    """Aliases SELECT outputs positionally to the INSERT column list."""
    query = query.copy()
    selects = [query] if isinstance(query, exp.Select) else list(query.find_all(exp.Select))
    for sel in selects:
        if sel.find_ancestor(exp.CTE, exp.Subquery):
            continue
        new = []
        for i, e in enumerate(sel.expressions):
            if i < len(names) and not isinstance(e, exp.Star):
                inner = e.this if isinstance(e, exp.Alias) else e
                new.append(exp.alias_(inner, names[i], quoted=False))
            else:
                new.append(e)
        sel.set("expressions", new)
    return query


def _expr_sql(node: Node, dialect) -> str:
    e = node.expression
    return (e.this if isinstance(e, exp.Alias) else e).sql(dialect=dialect)


def _leaves(node: Node, via: list[str], steps: list[str], dialect):
    """Yields (leaf_node, via_ctes, steps) for every physical-table column under node."""
    if isinstance(node.source, exp.Table) and not node.downstream:
        yield node, via, steps
        return
    for child in node.downstream:
        hop, hop_steps = via, steps
        if child.downstream and "." in child.name:
            hop = via + [child.name.split(".")[0]]
            hop_steps = steps + [f"{hop[-1]}: {_expr_sql(child, dialect)}"]
        yield from _leaves(child, hop, hop_steps, dialect)


def _query_lineage(target: str, query: exp.Query, columns: list[str], schema: dict,
                   dialect: str | None, drop_catalog: bool, warnings: list[str]) -> list[ColumnEdge]:
    edges = []
    for col in columns:
        try:
            root = lineage(col, query, schema=schema, dialect=dialect)
        except Exception as e:  # noqa: BLE001 - sqlglot raises many types
            warnings.append(f"column {col}: {e}")
            continue
        transformation = _expr_sql(root, dialect)
        found = False
        for leaf, via, steps in _leaves(root, [], [], dialect):
            found = True
            edges.append(ColumnEdge(
                target, col.lower(), table_name(leaf.source, drop_catalog),
                leaf.name.split(".")[-1].lower(), transformation, via, steps))
        if not found:
            edges.append(ColumnEdge(target, col.lower(), None, None, transformation))
    return edges


def _output_columns(query: exp.Query, schema: dict, dialect) -> list[str]:
    if query.is_star and schema:
        try:  # expands SELECT * using the known schema
            query = qualify(query.copy(), schema=schema, dialect=dialect, validate_qualify_columns=False)
        except Exception:  # noqa: BLE001
            pass
    return [c for c in query.named_selects if c and c != "*"]


def _merge_lineage(stmt: exp.Merge, target: str, dialect, drop_catalog) -> tuple[list[ColumnEdge], list[str]]:
    target_alias = stmt.this.alias_or_name.lower()
    using = stmt.args.get("using")
    alias_map: dict[str, str] = {}
    for t in using.find_all(exp.Table):
        alias_map[t.alias_or_name.lower()] = table_name(t, drop_catalog)
    if isinstance(using, exp.Subquery) and using.alias:
        inner = using.this
        tables = _physical_tables(inner, drop_catalog)
        alias_map[using.alias.lower()] = tables[0] if len(tables) == 1 else ",".join(tables)
    alias_map.setdefault(target_alias, target)

    edges, columns = [], []

    def add(col_name: str, value: exp.Expr):
        col_name = col_name.lower()
        columns.append(col_name)
        srcs = list(value.find_all(exp.Column)) if not isinstance(value, exp.Column) else [value]
        if not srcs:
            edges.append(ColumnEdge(target, col_name, None, None, value.sql(dialect=dialect)))
        for c in srcs:
            src_table = alias_map.get(c.table.lower(), c.table.lower() or None)
            edges.append(ColumnEdge(target, col_name, src_table, c.name.lower(), value.sql(dialect=dialect)))

    for when in stmt.find_all(exp.When):
        then = when.args.get("then")
        if isinstance(then, exp.Update):
            for eq in then.expressions:
                add(eq.this.name, eq.expression)
        elif isinstance(then, exp.Insert):
            cols = then.this.expressions if isinstance(then.this, exp.Tuple) else []
            vals = then.expression.expressions if isinstance(then.expression, exp.Tuple) else []
            for c, v in zip(cols, vals):
                add(c.name, v)
    return edges, list(dict.fromkeys(columns))


def parse_sql(sql: str, dialect: str | None = None, schema: dict | None = None,
              drop_catalog: bool = True) -> list[StatementLineage]:
    schema = schema or {}
    results = []
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except Exception as e:  # noqa: BLE001
        log.warning("parse failed: %s", e)
        return []

    for stmt in statements:
        if stmt is None:
            continue
        line = None
        warnings: list[str] = []
        if isinstance(stmt, exp.Create) and isinstance(stmt.expression, exp.Query):
            target_expr = stmt.this.this if isinstance(stmt.this, exp.Schema) else stmt.this
            kind = f"CREATE {stmt.args.get('kind', 'TABLE')}"
            query = stmt.expression
            col_names = [c.name for c in stmt.this.expressions] if isinstance(stmt.this, exp.Schema) else []
        elif isinstance(stmt, exp.Insert) and isinstance(stmt.expression, exp.Query):
            target_expr = stmt.this.this if isinstance(stmt.this, exp.Schema) else stmt.this
            kind = "INSERT"
            query = stmt.expression
            col_names = [c.name for c in stmt.this.expressions] if isinstance(stmt.this, exp.Schema) else []
        elif isinstance(stmt, exp.Merge):
            target = table_name(stmt.this, drop_catalog)
            edges, cols = _merge_lineage(stmt, target, dialect, drop_catalog)
            using = stmt.args.get("using")
            results.append(StatementLineage(
                target, "MERGE", [t for t in _physical_tables(using, drop_catalog) if t != target],
                cols, edges, [f"MERGE ON {stmt.args['on'].sql(dialect=dialect)}"] if stmt.args.get("on") else []))
            continue
        else:
            continue

        if not isinstance(target_expr, exp.Table):
            continue
        target = table_name(target_expr, drop_catalog)
        if col_names:
            query = _alias_outputs(query, col_names)
            columns = col_names
        else:
            columns = _output_columns(query, schema, dialect)
        if not columns:
            warnings.append("could not determine output columns (SELECT * without a known schema?)")
        edges = _query_lineage(target, query, columns, schema, dialect, drop_catalog, warnings)
        results.append(StatementLineage(
            target, kind, _physical_tables(query, drop_catalog), [c.lower() for c in columns], edges,
            _filters(query), line, warnings))
    return results

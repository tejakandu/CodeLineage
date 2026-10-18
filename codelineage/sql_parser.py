"""Extracts column-level lineage from SQL statements using sqlglot.

Handles CREATE TABLE/VIEW ... AS SELECT, INSERT [INTO] ... SELECT and MERGE. CTEs and
subqueries are resolved down to physical tables; the CTE names passed through are kept as
``via`` so the explanation can mention them.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.lineage import Node, lineage
from sqlglot.schema import Schema, ensure_schema
from sqlglot.optimizer.qualify import qualify

log = logging.getLogger(__name__)
# sqlglot logs every statement it falls back on (procedures, IF/BEGIN blocks); we recover those ourselves.
logging.getLogger("sqlglot").setLevel(logging.ERROR)


@dataclass
class ColumnEdge:
    target_table: str
    target_column: str
    source_table: str | None      # None for constants / COUNT(*)
    source_column: str | None
    transformation: str           # the SQL expression producing target_column
    via: list[str] = field(default_factory=list)     # CTEs / subqueries passed through
    steps: list[str] = field(default_factory=list)   # expression at each hop inside the statement
    ambiguous: bool = False       # unqualified column in a join; source table is a best guess


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
    is_temp: bool = False


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


def _column_root(col: str, query: exp.Query, schema: Schema | None, dialect, warnings: list[str]):
    """Lineage of one column, used when the all-columns call fails. Falls back to no schema."""
    try:
        return lineage(col, query, schema=schema or {}, dialect=dialect, trim_selects=False)
    except Exception as e:  # noqa: BLE001 - sqlglot raises many types
        if schema is None:
            warnings.append(f"column {col}: {e}")
            return None
        # Usually a stale/partial INFORMATION_SCHEMA (a column added since the export): resolve
        # without the schema rather than lose the column's lineage.
        try:
            root = lineage(col, query, schema={}, dialect=dialect, trim_selects=False)
            warnings.append(f"column {col}: resolved without schema ({_first_line(e)}); the schema may be "
                            "missing columns (stale INFORMATION_SCHEMA or DDL)")
            return root
        except Exception as e2:  # noqa: BLE001
            warnings.append(f"column {col}: {e2}")
            return None


def _query_lineage(target: str, query: exp.Query, columns: list[str], schema: Schema | None,
                   dialect: str | None, drop_catalog: bool, warnings: list[str]) -> list[ColumnEdge]:
    edges = []
    outer_warnings, warnings = warnings, []
    # One call traces every output column with a shared cache (qualifying the query once); a
    # per-column call re-analyses the whole query each time, which is slow on wide tables.
    # trim_selects=False skips building a trimmed copy of every CTE per hop (only used for display
    # by sqlglot; the edges are identical) and is ~15x faster on CTE-heavy SQL.
    def trace_all(sch):
        return {k.lower(): v for k, v in lineage(None, query, schema=sch or {}, dialect=dialect,
                                                 trim_selects=False).items()}
    try:
        roots = trace_all(schema)
    except Exception as e:  # noqa: BLE001
        roots = {}
        if schema is not None:
            # Usually the schema lacks a column this query uses (stale INFORMATION_SCHEMA, or columns
            # only guessed from the repo). Retry the whole query without it once, instead of failing
            # into slow column-by-column tracing with a warning per column.
            try:
                roots = trace_all(None)
                warnings.append(f"resolved without schema ({_first_line(e)}); the schema may be "
                                "missing columns (stale INFORMATION_SCHEMA or DDL)")
            except Exception:  # noqa: BLE001 - fall back to column by column
                roots = {}
    for col in columns:
        root = roots.get(col.lower()) or _column_root(col, query, schema, dialect, warnings)
        if root is None:
            continue
        transformation = _expr_sql(root, dialect)
        found = False
        for leaf, via, steps in _leaves(root, [], [], dialect):
            found = True
            edges.append(ColumnEdge(
                target, col.lower(), table_name(leaf.source, drop_catalog),
                leaf.name.split(".")[-1].lower(), transformation, via, steps))
        if not found:
            refs = list(root.expression.find_all(exp.Column))
            candidates = _physical_tables(query, drop_catalog)
            if refs and candidates:
                # Columns sqlglot could not pin to a table (unqualified, join, no schema): keep the
                # chain going to every candidate table, flagged so the answer can say so.
                warnings.append(f"column {col}: unqualified reference, source table ambiguous among "
                                f"{', '.join(candidates)} (load INFORMATION_SCHEMA to resolve)")
                for r in refs:
                    for cand in candidates:
                        edges.append(ColumnEdge(target, col.lower(), cand, r.name.lower(), transformation,
                                                ambiguous=True))
            else:
                edges.append(ColumnEdge(target, col.lower(), None, None, transformation))
    outer_warnings.extend(_collapse(warnings))
    return edges


def _collapse(warnings: list[str]) -> list[str]:
    """'column a: X', 'column b: X', ... -> '2 columns (a, b): X', so one problem is one warning."""
    groups: dict[str, list[str]] = {}
    other = []
    for w in warnings:
        m = re.match(r"column (\S+): (.*)", w, re.S)
        if not m:
            other.append(w)
            continue
        msg = m.group(2).replace(f"'{m.group(1)}'", "'<col>'").replace(f" {m.group(1)}:", " <col>:")
        groups.setdefault(msg, []).append(m.group(1))
    out = list(other)
    for msg, cols in groups.items():
        if len(cols) == 1:
            out.append(f"column {cols[0]}: {msg.replace('<col>', cols[0])}")
        else:
            shown = ", ".join(cols[:5]) + (", ..." if len(cols) > 5 else "")
            out.append(f"{len(cols)} columns ({shown}): {msg}")
    return out


def _expand_stars(query: exp.Query, schema: Schema | None, dialect) -> exp.Query | None:
    """The query with * expanded from the schema, or None if a * can't be expanded."""
    if schema is None:
        return None
    try:
        q = qualify(query.copy(), schema=schema, dialect=dialect, validate_qualify_columns=False, identify=False)
    except Exception:  # noqa: BLE001
        return None
    stars = [e for sel in ([q] if isinstance(q, exp.Select) else q.find_all(exp.Select))
             if not sel.find_ancestor(exp.CTE, exp.Subquery) for e in sel.expressions
             if isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))]
    return None if stars else q


def _output_columns(query: exp.Query, schema: Schema | None, dialect) -> list[str]:
    if query.is_star and schema:
        try:  # expands SELECT * using the known schema
            query = qualify(query.copy(), schema=schema, dialect=dialect, validate_qualify_columns=False, identify=False)
        except Exception:  # noqa: BLE001
            pass
    return [c for c in query.named_selects if c and c != "*"]


def _subquery_sources(query: exp.Query, column: str, schema, dialect, drop_catalog: bool):
    """(source table, source column, steps) for one output column of a USING/FROM subquery."""
    root = None
    for sch in ((schema, None) if schema is not None else (None,)):
        try:
            root = lineage(column, query, schema=sch or {}, dialect=dialect, trim_selects=False)
            break
        except Exception:  # noqa: BLE001
            continue
    if root is None:
        return [], None
    inner = _expr_sql(root, dialect)
    return [(table_name(leaf.source, drop_catalog), leaf.name.split(".")[-1].lower(), steps)
            for leaf, _via, steps in _leaves(root, [], [], dialect)], inner


def _assignment_edges(target: str, pairs: list[tuple[str, exp.Expr]], alias_map: dict,
                      dialect, schema=None, drop_catalog: bool = True) -> tuple[list[ColumnEdge], list[str]]:
    """Edges for `col = expr` style assignments (MERGE, UPDATE). alias_map values are table names or,
    for `USING (SELECT ...) s`, the subquery itself, traced column by column to its real sources."""
    others = [v for v in dict.fromkeys(alias_map.values(), None) if v is not None and v != target]
    edges, columns = [], []

    def via(source, alias: str, col: str, sql: str, col_name: str, ambiguous: bool = False) -> None:
        if isinstance(source, exp.Query):
            found, inner = _subquery_sources(source, col, schema, dialect, drop_catalog)
            for tbl, src_col, steps in found:
                edges.append(ColumnEdge(target, col_name, tbl, src_col, sql, [alias],
                                        [f"{alias}: {inner}"] + steps, ambiguous))
            if not found:
                for tbl in _physical_tables(source, drop_catalog):   # couldn't trace: flag every input
                    edges.append(ColumnEdge(target, col_name, tbl, col, sql, [alias], ambiguous=True))
        else:
            edges.append(ColumnEdge(target, col_name, source, col, sql, ambiguous=ambiguous))

    for col_name, value in pairs:
        col_name = col_name.lower()
        columns.append(col_name)
        sql = value.sql(dialect=dialect)
        refs = [value] if isinstance(value, exp.Column) else list(value.find_all(exp.Column))
        if not refs:
            edges.append(ColumnEdge(target, col_name, None, None, sql))
        for c in refs:
            alias, col = c.table.lower(), c.name.lower()
            if alias:
                via(alias_map.get(alias, alias), alias, col, sql, col_name)
            elif len(others) == 1:
                via(others[0], next(a for a, v in alias_map.items() if v is others[0]), col, sql, col_name)
            else:
                for o in others or [target]:
                    a = next((k for k, v in alias_map.items() if v is o), "")
                    via(o, a, col, sql, col_name, ambiguous=len(others) > 1)
    return edges, list(dict.fromkeys(columns))


def _inside_subquery(node: exp.Expr, top: exp.Expr) -> bool:
    """True if node sits inside a subquery at or below `top` (other than being that subquery)."""
    p = node.parent
    while p is not None:
        if isinstance(p, exp.Subquery):
            return True
        if p is top:
            return False
        p = p.parent
    return False


def _alias_map(target_expr: exp.Table, target: str, sources: list[exp.Expr], drop_catalog: bool) -> dict:
    """alias -> table name, or alias -> subquery for `(SELECT ...) s`. Aliases inside a subquery stay
    inside it (they are resolved when the subquery is traced)."""
    alias_map: dict = {target_expr.alias_or_name.lower(): target, target_expr.name.lower(): target}
    for src in sources:
        if src is None:
            continue
        for node in src.find_all(exp.Table, exp.Subquery):
            if _inside_subquery(node, src):
                continue                                   # belongs to a subquery: not visible here
            if isinstance(node, exp.Subquery):
                if node.alias:
                    alias_map[node.alias.lower()] = node.this
            else:
                alias_map.setdefault(node.alias_or_name.lower(), table_name(node, drop_catalog))
    return alias_map


def _merge_lineage(stmt: exp.Merge, target: str, dialect, drop_catalog, schema=None):
    alias_map = _alias_map(stmt.this, target, [stmt.args.get("using")], drop_catalog)
    pairs = []
    for when in stmt.find_all(exp.When):
        then = when.args.get("then")
        if isinstance(then, exp.Update):
            pairs += [(eq.this.name, eq.expression) for eq in then.expressions]
        elif isinstance(then, exp.Insert):
            cols = then.this.expressions if isinstance(then.this, exp.Tuple) else []
            vals = then.expression.expressions if isinstance(then.expression, exp.Tuple) else []
            pairs += [(c.name, v) for c, v in zip(cols, vals)]
    return _assignment_edges(target, pairs, alias_map, dialect, schema, drop_catalog)


# ---------------------------------------------------------------- statement splitting / recovery

_DML = {"INSERT", "MERGE", "UPDATE", "CREATE"}


def _chunks(sql: str, dialect) -> list[tuple[str, int, list]]:
    """Splits a script into statements at top-level semicolons, keeping each one's start line, so a
    single unparseable statement doesn't lose the rest of the file."""
    try:
        tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(sql)
    except Exception:  # noqa: BLE001 - fall back to a naive split
        out, line = [], 1
        for part in sql.split(";"):
            out.append((part, line, []))
            line += part.count("\n")
        return out
    out, cur = [], []
    for tok in tokens + [None]:
        if tok is None or tok.token_type.name == "SEMICOLON":
            if cur:
                out.append((sql[cur[0].start:cur[-1].end + 1], cur[0].line, cur))
            cur = []
        else:
            cur.append(tok)
    return out


def _first_line(e: Exception) -> str:
    return (str(e).strip().splitlines() or [type(e).__name__])[0][:160]


def _parse_chunk(text: str, tokens: list, dialect, diagnostics: list[str] | None = None,
                 line: int = 1) -> list[tuple[exp.Expr, int]]:
    """Parses one statement. If it isn't a plain statement (procedure header, IF ... THEN, BEGIN,
    LOOP, ...), parses from the first DML keyword inside it instead. Returns (expr, line offset).
    When a statement that writes data can't be parsed, the reason goes to diagnostics."""
    error = None
    try:
        stmt = sqlglot.parse_one(text, read=dialect)
    except Exception as e:  # noqa: BLE001
        stmt, error = None, e
    if stmt is not None and not isinstance(stmt, exp.Command) and not (
            isinstance(stmt, exp.Create) and str(stmt.args.get("kind", "")).upper() in ("PROCEDURE", "FUNCTION")):
        return [(stmt, 0)]
    base = tokens[0].start if tokens else 0
    for i, tok in enumerate(tokens[1:] if tokens else [], start=1):
        if tok.token_type.name in _DML:
            if tok.token_type.name == "CREATE":
                nxt = " ".join(t.text.upper() for t in tokens[i + 1:i + 4])
                if "PROCEDURE" in nxt or "FUNCTION" in nxt:
                    continue
            try:
                inner = sqlglot.parse_one(text[tok.start - base:], read=dialect)
            except Exception as e:  # noqa: BLE001
                error = error or e
                continue
            if not isinstance(inner, exp.Command):
                return [(inner, tok.line - tokens[0].line)]
    if diagnostics is not None and (error is not None or _writes_data(tokens)):
        reason = _first_line(error) if error else f"statement type not supported ({_lead(tokens)} ...)"
        diagnostics.append(f"line {line}: {reason}")
    return []


_NOT_LINEAGE = {"FUNCTION", "PROCEDURE", "SCHEMA", "INDEX", "ROW", "POLICY", "SNAPSHOT", "MODEL", "SEARCH"}


def _writes_data(tokens: list) -> bool:
    """INSERT / MERGE / UPDATE, or CREATE ... TABLE|VIEW (not functions, procedures, schemas...)."""
    words = [t.text.upper() for t in (tokens or [])[:8]]
    if not words:
        return False
    if words[0] in ("INSERT", "MERGE", "UPDATE"):
        return True
    return words[0] == "CREATE" and not _NOT_LINEAGE & set(words) and bool({"TABLE", "VIEW"} & set(words))


def _lead(tokens: list) -> str:
    """The statement's leading keywords, without table or column names."""
    out = []
    for t in (tokens or [])[:6]:
        if t.token_type.name in ("VAR", "IDENTIFIER", "STRING", "NUMBER"):
            break
        out.append(t.text.upper())
    return " ".join(out) or "?"


def _collect_ddl(stmt: exp.Expr, dialect, drop_catalog: bool, ddl: dict) -> None:
    """CREATE TABLE t (col TYPE, ...) definitions: a schema source that needs no warehouse access."""
    if not (isinstance(stmt, exp.Create) and isinstance(stmt.this, exp.Schema)):
        return
    table = stmt.this.this
    if not isinstance(table, exp.Table):
        return
    cols = {}
    for d in stmt.this.expressions:
        if isinstance(d, exp.ColumnDef):
            kind = d.args.get("kind")
            cols[d.name.lower()] = {"type": kind.sql(dialect=dialect) if kind else "UNKNOWN"}
    if cols:
        ddl.setdefault(table_name(table, drop_catalog), {}).update(cols)


def _is_temp(stmt: exp.Expr) -> bool:
    return isinstance(stmt, exp.Create) and stmt.find(exp.TemporaryProperty) is not None


def parse_sql(sql: str, dialect: str | None = None, schema: dict | Schema | None = None,
              drop_catalog: bool = True, temp_scope: str | None = None,
              diagnostics: list[str] | None = None, columns_only: bool = False,
              ddl: dict | None = None) -> list[StatementLineage]:
    """Column lineage for every lineage-producing statement in ``sql``.

    ``temp_scope`` (usually the file path) namespaces TEMP tables as ``<scope>::<name>`` so two
    files using the same temp table name don't get linked to each other."""
    if isinstance(schema, dict):  # build sqlglot's schema object once, not once per column
        schema = ensure_schema(schema, dialect=dialect) if schema else None
    parsed = []
    for text, line, tokens in _chunks(sql, dialect):
        if not text.strip():
            continue
        for stmt, offset in _parse_chunk(text, tokens, dialect, diagnostics, line):
            # The graph and schema are lowercase; normalise identifiers so ORDERS, Orders and orders
            # match (string literals are untouched).
            for ident in stmt.find_all(exp.Identifier):
                ident.set("this", ident.this.lower())
            parsed.append((stmt, line + offset))

    temps = {table_name(s.this.this if isinstance(s.this, exp.Schema) else s.this, drop_catalog)
             for s, _ in parsed if _is_temp(s)}

    results = []
    for stmt, line in parsed:
        if ddl is not None:
            _collect_ddl(stmt, dialect, drop_catalog, ddl)
        result = _statement_lineage(stmt, dialect, schema, drop_catalog, columns_only)
        if result is None:
            continue
        result.line = line
        result.is_temp = result.target_table in temps
        results.append(result)

    if temp_scope and temps:
        scoped = lambda t: f"{temp_scope}::{t}" if t in temps else t  # noqa: E731
        for r in results:
            r.target_table = scoped(r.target_table)
            r.source_tables = [scoped(t) for t in r.source_tables]
            for e in r.edges:
                e.target_table = scoped(e.target_table)
                e.source_table = scoped(e.source_table) if e.source_table else None
    return results


def _statement_lineage(stmt: exp.Expr, dialect, schema: Schema | None, drop_catalog: bool,
                       columns_only: bool = False) -> StatementLineage | None:
    warnings: list[str] = []
    if isinstance(stmt, (exp.Create, exp.Insert)) and isinstance(stmt.expression, exp.Query):
        target_expr = stmt.this.this if isinstance(stmt.this, exp.Schema) else stmt.this
        kind = f"CREATE {stmt.args.get('kind', 'TABLE')}" if isinstance(stmt, exp.Create) else "INSERT"
        query = stmt.expression
        col_names = [c.name for c in stmt.this.expressions] if isinstance(stmt.this, exp.Schema) else []
    elif isinstance(stmt, exp.Merge):
        target = table_name(stmt.this, drop_catalog)
        edges, cols = _merge_lineage(stmt, target, dialect, drop_catalog, schema)
        using = stmt.args.get("using")
        return StatementLineage(
            target, "MERGE", [t for t in _physical_tables(using, drop_catalog) if t != target], cols, edges,
            [f"MERGE ON {stmt.args['on'].sql(dialect=dialect)}"] if stmt.args.get("on") else [])
    elif isinstance(stmt, exp.Update) and isinstance(stmt.this, exp.Table):
        target = table_name(stmt.this, drop_catalog)
        sources = [stmt.args.get("from_") or stmt.args.get("from")] + list(stmt.args.get("joins") or [])
        alias_map = _alias_map(stmt.this, target, sources, drop_catalog)
        pairs = [(eq.this.name, eq.expression) for eq in stmt.expressions if isinstance(eq, exp.EQ)]
        edges, cols = _assignment_edges(target, pairs, alias_map, dialect, schema, drop_catalog)
        src_tables = sorted({t for s in sources if s is not None for t in _physical_tables(s, drop_catalog)} - {target})
        where = stmt.args.get("where")
        return StatementLineage(target, "UPDATE", src_tables, cols, edges,
                                [f"WHERE {where.this.sql(dialect=dialect)}"] if where else [])
    else:
        return None

    if not isinstance(target_expr, exp.Table):
        return None
    target = table_name(target_expr, drop_catalog)
    if col_names:
        if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))
               for sel in ([query] if isinstance(query, exp.Select) else query.find_all(exp.Select))
               if not sel.find_ancestor(exp.CTE, exp.Subquery) for e in sel.expressions):
            # `CREATE VIEW v (a, b) AS SELECT * ...`: expand the * so names can be matched by position
            expanded = _expand_stars(query, schema, dialect)
            if expanded is None:
                warnings.append("could not determine output columns (column list over SELECT * on a "
                                "table missing from the schema)")
                return StatementLineage(target, kind, _physical_tables(query, drop_catalog),
                                        [c.lower() for c in col_names], [], _filters(query), None, warnings)
            query = expanded
        query = _alias_outputs(query, col_names)
        columns = col_names
    else:
        columns = _output_columns(query, schema, dialect)
    if not columns:
        warnings.append("could not determine output columns (SELECT * on a table missing from INFORMATION_SCHEMA?)")
    edges = [] if columns_only else _query_lineage(target, query, columns, schema, dialect, drop_catalog, warnings)
    return StatementLineage(target, kind, _physical_tables(query, drop_catalog), [c.lower() for c in columns],
                            edges, _filters(query), None, warnings)

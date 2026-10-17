"""Statically inspects Airflow DAG files (via ``ast``, never importing them) to find which
task runs which SQL, so lineage edges can say "built by dag X / task Y"."""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from .fsutil import read_text

_SQL_KEYS = {"sql", "query", "bql", "sql_query"}
_INCLUDE_RE = re.compile(r"""{%-?\s*include\s+['"]([^'"]+)['"]\s*-?%}""")
_SQL_WORD_RE = re.compile(r"\b(select|insert|merge|create|update)\b", re.I)


@dataclass
class SqlTask:
    dag_id: str
    task_id: str
    dag_file: str
    schedule: str | None = None
    sql_file: str | None = None      # template path as written in the DAG (resolved later)
    inline_sql: str | None = None
    params: dict = field(default_factory=dict)
    upstream_tasks: list[str] = field(default_factory=list)


def _const(node):
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def _kw(call: ast.Call, name: str):
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def _call_name(call: ast.Call) -> str:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""


def _find_sql_value(call: ast.Call):
    for k in call.keywords:
        if k.arg in _SQL_KEYS:
            return _const(k.value)
    conf = _kw(call, "configuration")  # BigQueryInsertJobOperator
    conf = _const(conf) if conf is not None else None
    if isinstance(conf, dict):
        q = conf.get("query")
        if isinstance(q, dict):
            return q.get("query")
    return None


def _text(node) -> str | None:
    """A string argument: a literal, or an f-string kept as written (f"load_{name}" -> "load_{name}")."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{" + ast.unparse(v.value) + "}"
                       for v in node.values)
    return None


_SQL_PATH = re.compile(r"^[\w.\-/\\{}$]+\.sql$", re.I)


def _sql_literals(node: ast.AST) -> list[str]:
    """.sql paths written anywhere inside a call or function: helpers, open(), os.path.join(...)."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "join" and \
                isinstance(n.func, ast.Attribute) and getattr(n.func.value, "attr", "") == "path":
            parts = [_text(a) for a in n.args]     # os.path.join(SQL_DIR, "mart", "x.sql") -> mart/x.sql
            tail = [x for x in parts if x]
            if tail and tail[-1].lower().endswith(".sql"):
                out.append("/".join(tail))
        v = _text(n)
        if v and _SQL_PATH.match(v.strip()) and "{" not in v:
            out.append(v.strip())
    return list(dict.fromkeys(out))


def _classify(value) -> tuple[str | None, str | None]:
    """Returns (sql_file, inline_sql) for a sql=... value."""
    if isinstance(value, list):
        value = "\n;\n".join(v for v in value if isinstance(v, str))
    if not isinstance(value, str):
        return None, None
    stripped = value.strip()
    m = _INCLUDE_RE.fullmatch(stripped)
    if m:
        return m.group(1), None
    if stripped.endswith(".sql") and "\n" not in stripped:
        return stripped, None
    if _SQL_WORD_RE.search(stripped):
        return None, value
    return None, None


def parse_dag_file(path: Path) -> list[SqlTask]:
    try:
        tree = ast.parse(read_text(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []

    dag_id, schedule, dag_params = path.stem, None, {}
    var_to_task: dict[str, str] = {}
    tasks: list[SqlTask] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) == "DAG":
            first = node.args[0] if node.args else _kw(node, "dag_id")
            dag_id = _const(first) or dag_id if first is not None else dag_id
            sched = _kw(node, "schedule") or _kw(node, "schedule_interval")
            schedule = _const(sched) if sched is not None else None
            p = _kw(node, "params")
            dag_params = _const(p) or {} if p is not None else {}

    dag_params = dag_params if isinstance(dag_params, dict) else {}
    seen: set[tuple[str, str | None]] = set()

    def add(task_id: str, value, call: ast.Call | None, scope: ast.AST) -> None:
        if value is None:                 # sql passed through a helper, open(), os.path.join(), ...
            literals = _sql_literals(scope)
            value = literals[0] if literals else None
        sql_file, inline = _classify(value)
        if not (sql_file or inline) or (task_id, sql_file or inline) in seen:
            return
        seen.add((task_id, sql_file or inline))
        p = _kw(call, "params") if call is not None else None
        params = {**dag_params, **((_const(p) or {}) if p is not None else {})}
        tasks.append(SqlTask(dag_id, task_id, str(path), schedule, sql_file, inline, params))

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and _kw(node.value, "task_id"):
            tid = _text(_kw(node.value, "task_id"))
            for t in node.targets:
                if isinstance(t, ast.Name) and tid:
                    var_to_task[t.id] = tid
        # classic operators: any call with task_id=..., wherever it appears (assigned, in lists, chains)
        if isinstance(node, ast.Call) and _kw(node, "task_id") is not None:
            tid = _text(_kw(node, "task_id"))
            if tid:
                add(tid, _find_sql_value(node), node, node)
        # TaskFlow: @task / @task.bigquery functions that run a .sql file
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                d = dec.func if isinstance(dec, ast.Call) else dec
                name = d.attr if isinstance(d, ast.Attribute) else getattr(d, "id", "")
                root = d.value.id if isinstance(d, ast.Attribute) and isinstance(d.value, ast.Name) else name
                if root == "task":
                    tid = _text(_kw(dec, "task_id")) if isinstance(dec, ast.Call) and _kw(dec, "task_id") else node.name
                    add(tid, None, dec if isinstance(dec, ast.Call) else None, node)

    # Task dependencies from `a >> b`, `[a, b] >> c`, `a << b`
    deps: dict[str, set[str]] = {}

    def names(n) -> list[str]:
        if isinstance(n, ast.Name):
            return [var_to_task.get(n.id, n.id)]
        if isinstance(n, (ast.List, ast.Tuple)):
            return [x for e in n.elts for x in names(e)]
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.RShift, ast.LShift)):
            return names(n.right) if isinstance(n.op, ast.RShift) else names(n.left)
        return []

    def leftmost(n) -> list[str]:
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.RShift, ast.LShift)):
            return leftmost(n.left) if isinstance(n.op, ast.RShift) else names(n.right)
        return names(n)

    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.RShift, ast.LShift)):
            up, down = (leftmost(node.left), names(node.right))
            if isinstance(node.op, ast.LShift):
                up, down = names(node.right), names(node.left)
            for d in down:
                deps.setdefault(d, set()).update(up)
    for t in tasks:
        t.upstream_tasks = sorted(deps.get(t.task_id, ()))
    return tasks

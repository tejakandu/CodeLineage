"""Statically inspects Airflow DAG files (via ``ast``, never importing them) to find which
task runs which SQL, so lineage edges can say "built by dag X / task Y"."""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

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
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):
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

    for node in ast.walk(tree):
        call, target = None, None
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
            target = node.targets[0].id if isinstance(node.targets[0], ast.Name) else None
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
        if call is None or _kw(call, "task_id") is None:
            continue
        task_id = _const(_kw(call, "task_id"))
        if not isinstance(task_id, str):
            continue
        if target:
            var_to_task[target] = task_id
        sql_file, inline = _classify(_find_sql_value(call))
        if not (sql_file or inline):
            continue
        p = _kw(call, "params")
        params = {**(dag_params if isinstance(dag_params, dict) else {}), **((_const(p) or {}) if p is not None else {})}
        tasks.append(SqlTask(dag_id, task_id, str(path), schedule, sql_file, inline, params))

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

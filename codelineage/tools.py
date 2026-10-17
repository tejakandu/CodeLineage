"""LLM-facing tools. The same plain functions are exposed to the Gemini API (automatic function
calling) and to Gemini Code Assist / Gemini CLI through the MCP server.

Answers come from the parsed graph, never from the model's guess, so every hop is traceable to a
file, line and Airflow task.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlencode

from .builder import build_graph
from .config import LineageConfig
from .graph import LineageGraph

DEFAULT_GRAPH = ".lineage/graph.json"


class LineageService:
    def __init__(self, graph: LineageGraph, repo_root: str | Path):
        self.graph = graph
        self.repo_root = Path(repo_root).resolve()

    @classmethod
    def from_env(cls, repo: str | None = None, graph_path: str | None = None, rebuild: bool = False) -> "LineageService":
        repo = repo or os.environ.get("CODELINEAGE_REPO", ".")
        cfg = LineageConfig.load(repo)
        path = Path(graph_path or os.environ.get("CODELINEAGE_GRAPH") or cfg.repo_root / DEFAULT_GRAPH)
        if rebuild or not path.exists():
            graph = build_graph(cfg)
            graph.save(path)
        else:
            graph = LineageGraph.load(path)
        return cls(graph, cfg.repo_root)

    # ----- helpers -----
    def _resolve(self, table: str, column: str | None = None) -> tuple[str | None, str | None, str | None]:
        """Returns (table, column, error_message)."""
        if column is None and table.count(".") >= 2:
            table, column = table.rsplit(".", 1)
        tables = self.graph.resolve_table(table)
        if not tables:
            return None, None, f"No table matching '{table}'. Use find_columns or list_tables to search."
        if len(tables) > 1 and tables[0] != table.lower():
            return None, None, f"'{table}' is ambiguous. Candidates: {', '.join(tables[:10])}. Ask the user which one."
        t = tables[0]
        if column is None:
            return t, None, None
        cols = self.graph.resolve_column(t, column)
        if not cols:
            return t, None, f"Table {t} has no column like '{column}'. Known columns: {', '.join(self.graph.columns(t))}"
        if len(cols) > 1:
            return t, None, f"'{column}' is ambiguous in {t}. Candidates: {', '.join(cols)}. Ask the user which one."
        return t, cols[0], None

    # ----- tools -----
    def find_columns(self, search: str) -> str:
        hits = self.graph.search_columns(search)
        return "\n".join(hits) if hits else f"No columns matching '{search}'."

    def list_tables(self, name_filter: str = "") -> str:
        tables = [t for t in self.graph.all_tables() if name_filter.lower() in t]
        return "\n".join(f"{t}{'  (source / not built in this repo)' if self.graph.is_source(t) else ''}"
                         for t in tables[:200]) or "No tables."

    def describe_table(self, table: str) -> str:
        t, _, err = self._resolve(table)
        if err:
            return err
        info = self.graph.tables.get(t)
        out = {"table": t, "columns": self.graph.columns(t)}
        details = self.graph.data.get("schema", {}).get(t)
        if details:
            out["column_details"] = details
        if info:
            out["built_by"] = info["built_by"]
        else:
            out["note"] = "Not created by any SQL in the repo: an external/raw source table."
        return json.dumps(out, indent=2)

    def trace_column_lineage(self, table: str, column: str, direction: str = "upstream", max_depth: int = 10) -> str:
        t, c, err = self._resolve(table, column)
        if err:
            return err
        direction = "downstream" if direction.lower().startswith("down") else "upstream"
        tree = self.graph.trace(t, c, direction, max_depth)
        built = {name: self.graph.tables[name]["built_by"] for name in _tables_in(tree) if name in self.graph.tables}
        filters = {name: sorted({f for b in bs for f in b.get("filters", [])}) for name, bs in built.items()}
        schedules = {name: sorted({f"{b['dag_id']}.{b['task_id']} ({b.get('schedule')})" for b in bs if b.get("dag_id")})
                     for name, bs in built.items()}
        explorer = os.environ.get("CODELINEAGE_EXPLORER_URL")
        link = None
        if explorer:  # deep link into the Lineage Terminal, focused on this column
            params = {"f": f"{t}.{c}", "m": "col", "d": "up" if direction == "upstream" else "down", "n": max_depth}
            link = f"{explorer.rstrip('/')}/#{urlencode(params)}"
        return "\n".join(([f"INTERACTIVE GRAPH: {link}", ""] if link else []) + [
            f"{direction.upper()} LINEAGE of {t}.{c}",
            "",
            "TREE (each ⤷ line is the SQL expression that produces the parent column, with file and Airflow task):",
            self.graph.render_tree(tree, direction),
            "",
            "FILTERS / JOINS applied when building each table:",
            json.dumps({k: v for k, v in filters.items() if v}, indent=2),
            "",
            "AIRFLOW TASKS (dag.task (schedule)):",
            json.dumps({k: v for k, v in schedules.items() if v}, indent=2),
            "",
            "MERMAID:",
            "```mermaid",
            self.graph.render_mermaid(tree, direction),
            "```",
        ])

    def trace_table_lineage(self, table: str, direction: str = "upstream", max_depth: int = 10) -> str:
        t, _, err = self._resolve(table)
        if err:
            return err
        direction = "downstream" if direction.lower().startswith("down") else "upstream"
        tree = self.graph.table_dependencies(t, direction, max_depth)
        lines = []

        def walk(n: dict, depth: int) -> None:
            runs = ", ".join(sorted({f"{b['dag_id']}.{b['task_id']}" for b in n.get("built_by", []) if b.get("dag_id")}))
            files = ", ".join(sorted({b["file"] for b in n.get("built_by", [])}))
            meta = f"  [{files}{' | ' + runs if runs else ''}]" if files else "  ◆ SOURCE"
            lines.append(f"{'  ' * depth}{'└─ ' if depth else ''}{n['table']}{meta}")
            for ch in n.get("children", []):
                walk(ch, depth + 1)

        walk(tree, 0)
        return f"{direction.upper()} TABLE LINEAGE of {t}\n" + "\n".join(lines)

    def read_sql_file(self, path: str, start_line: int = 1, end_line: int = 200) -> str:
        p = (self.repo_root / path.split("::")[0]).resolve()
        if not p.is_relative_to(self.repo_root) or not p.is_file():
            return f"File not found in repo: {path}"
        lines = p.read_text(errors="replace").splitlines()
        start, end = max(1, start_line), min(len(lines), end_line)
        return "\n".join(f"{i:5d}  {lines[i - 1]}" for i in range(start, end + 1))

    def tool_functions(self) -> list:
        """Plain functions with docstrings/type hints, as the Gemini SDK and MCP expect."""
        svc = self

        def find_columns(search: str) -> str:
            """Search every known column (as table.column) whose name contains the text. Use this when the
            user names a column but not its table, or you are unsure of exact names."""
            return svc.find_columns(search)

        def list_tables(name_filter: str = "") -> str:
            """List known tables, optionally only those whose name contains name_filter."""
            return svc.list_tables(name_filter)

        def describe_table(table: str) -> str:
            """Show a table's columns and how it is built: SQL file, statement type, source tables,
            filters/joins and the Airflow DAG/task/schedule that runs it."""
            return svc.describe_table(table)

        def trace_column_lineage(table: str, column: str, direction: str = "upstream", max_depth: int = 10) -> str:
            """Multi-level column lineage. direction='upstream' answers "where does this column come from"
            (down to raw source tables); direction='downstream' answers "what is impacted if this column
            changes". Table names may be partial (e.g. 'fct_daily_revenue')."""
            return svc.trace_column_lineage(table, column, direction, max_depth)

        def trace_table_lineage(table: str, direction: str = "upstream", max_depth: int = 10) -> str:
            """Table-level dependency tree (which tables feed this table, recursively, or which tables
            depend on it when direction='downstream')."""
            return svc.trace_table_lineage(table, direction, max_depth)

        def read_sql_file(path: str, start_line: int = 1, end_line: int = 200) -> str:
            """Read a SQL/DAG file from the repo (path relative to repo root, as shown in lineage output)
            to quote or explain the exact code."""
            return svc.read_sql_file(path, start_line, end_line)

        return [find_columns, list_tables, describe_table, trace_column_lineage, trace_table_lineage, read_sql_file]


def _tables_in(tree: dict) -> set[str]:
    out = {tree["table"]} if "table" in tree else set()
    for link in tree.get("upstream", []) + tree.get("downstream", []):
        out |= _tables_in(link["node"])
    return out


SYSTEM_PROMPT = """You are CodeLineage, a data lineage assistant for internal employees.
You answer questions such as "where does column X in table Y come from?" or "what breaks if I change
column Z?" using ONLY the lineage tools. The tools are built by parsing the company's SQL, Airflow DAG
and config files, so they are the source of truth. Never invent tables, columns or logic.

How to work:
1. Identify the table and column. If unsure of exact names, call find_columns / list_tables. If a tool
   says a name is ambiguous, ask the user to choose.
2. Call trace_column_lineage (upstream for "where from", downstream for "impact"). Use read_sql_file
   only if you need the exact code to explain a tricky transformation.
3. Answer in this structure:
   **Summary** - one or two plain-English sentences: the ultimate source(s) and what happens on the way.
   **Lineage by level** - a markdown table with columns: Level | Column | Transformation (plain English
   + the SQL expression) | Built in (file, DAG.task). Level 0 is the asked column; higher levels move
   toward the raw sources (or toward consumers for downstream).
   **Filters & joins** that affect the values (e.g. status = 'COMPLETE').
   **Source tables** - the raw tables at the end of the chain (marked SOURCE).
   **Diagram** - repeat the mermaid block from the tool output unchanged. If the tool output has an
   INTERACTIVE GRAPH link, put it first as "[Open interactive lineage](link)".
4. Column types and descriptions from the warehouse INFORMATION_SCHEMA appear in parentheses; use
   them to explain columns in business terms. Hops marked AMBIGUOUS are best guesses (unqualified
   column in a join with no schema): say so explicitly.
5. Tables named like "path/file.sql::tmp_x" are temp tables that exist only inside that script.
6. If a column has "no source column" it is derived from a constant or row count such as COUNT(*);
   say so. If the tool output shows truncation, offer to go deeper.
Keep it concise and factual."""

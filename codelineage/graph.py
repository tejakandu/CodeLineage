"""The lineage graph: storage (JSON), lookup, multi-level traversal and rendering."""
from __future__ import annotations

import difflib
import json
from collections import defaultdict
from pathlib import Path


def col_key(table: str, column: str) -> str:
    return f"{table}.{column}"


class LineageGraph:
    def __init__(self, data: dict):
        self.data = data
        self.tables: dict[str, dict] = data.get("tables", {})
        self.edges: list[dict] = data.get("edges", [])
        self._up: dict[str, list[dict]] = defaultdict(list)
        self._down: dict[str, list[dict]] = defaultdict(list)
        self._table_cols: dict[str, set[str]] = defaultdict(set)
        for e in self.edges:
            self._up[col_key(e["target_table"], e["target_column"])].append(e)
            self._table_cols[e["target_table"]].add(e["target_column"])
            if e.get("source_table"):
                self._down[col_key(e["source_table"], e["source_column"])].append(e)
                self._table_cols[e["source_table"]].add(e["source_column"])
        for t, info in self.tables.items():
            self._table_cols[t].update(info.get("columns", []))

    # ---------- persistence ----------
    @classmethod
    def load(cls, path: str | Path) -> "LineageGraph":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.data, indent=2, default=list), encoding="utf-8")

    # ---------- lookup ----------
    def all_tables(self) -> list[str]:
        return sorted(self._table_cols)

    def columns(self, table: str) -> list[str]:
        return sorted(self._table_cols.get(table, ()))

    def resolve_table(self, name: str) -> list[str]:
        """Exact match, then suffix match (``fct_daily_revenue`` -> ``mart.fct_daily_revenue``),
        then substring, then fuzzy. Returns candidates, best first."""
        name = name.strip().strip("`\"").lower()
        tables = self.all_tables()
        if name in self._table_cols:
            return [name]
        for candidates in (
            [t for t in tables if t.endswith("." + name) or name.endswith("." + t)],
            [t for t in tables if name in t],
            difflib.get_close_matches(name, tables, n=5, cutoff=0.6),
        ):
            if candidates:
                return candidates
        return []

    def resolve_column(self, table: str, column: str) -> list[str]:
        column = column.strip().strip("`\"").lower()
        cols = self.columns(table)
        if column in cols:
            return [column]
        return [c for c in cols if column in c] or difflib.get_close_matches(column, cols, n=5, cutoff=0.6)

    def search_columns(self, pattern: str, limit: int = 50) -> list[str]:
        pattern = pattern.lower()
        everything = [(t, c) for t, cols in sorted(self._table_cols.items()) for c in sorted(cols)]
        out = [col_key(t, c) for t, c in everything if pattern in c] or \
              [col_key(t, c) for t, c in everything if pattern in col_key(t, c)]
        return out[:limit]

    def column_info(self, table: str, column: str) -> dict:
        """Type/description from INFORMATION_SCHEMA, if it was loaded."""
        return self.data.get("schema", {}).get(table, {}).get(column, {})

    def is_source(self, table: str) -> bool:
        return table not in self.tables

    # ---------- traversal ----------
    def trace(self, table: str, column: str, direction: str = "upstream", max_depth: int = 10) -> dict:
        index = self._up if direction == "upstream" else self._down

        def build(t: str, c: str, depth: int, path: frozenset) -> dict:
            key = col_key(t, c)
            node = {"table": t, "column": c, "is_source_table": self.is_source(t)}
            if key in path:
                node["cycle"] = True
                return node
            if depth >= max_depth:
                node["truncated"] = bool(index.get(key))
                return node
            links = []
            for e in index.get(key, []):
                nt, nc = (e["source_table"], e["source_column"]) if direction == "upstream" else (e["target_table"], e["target_column"])
                link = {k: e.get(k) for k in ("transformation", "steps", "file", "line", "dag_id", "task_id", "statement_type", "ambiguous")}
                if direction == "downstream":
                    link["transformation"] = e["transformation"]
                link["node"] = build(nt, nc, depth + 1, path | {key}) if nt else {"constant": True}
                links.append(link)
            node["upstream" if direction == "upstream" else "downstream"] = links
            return node

        return build(table, column, 0, frozenset())

    def table_dependencies(self, table: str, direction: str = "upstream", max_depth: int = 10) -> dict:
        def neighbours(t: str) -> list[str]:
            if direction == "upstream":
                return sorted({s for b in self.tables.get(t, {}).get("built_by", []) for s in b.get("source_tables", [])})
            return sorted(name for name, info in self.tables.items()
                          if any(t in b.get("source_tables", []) for b in info.get("built_by", [])))

        def build(t: str, depth: int, path: frozenset) -> dict:
            node = {"table": t, "built_by": self.tables.get(t, {}).get("built_by", [])}
            if t in path or depth >= max_depth:
                return node
            node["children"] = [build(n, depth + 1, path | {t}) for n in neighbours(t)]
            return node

        return build(table, 0, frozenset())

    # ---------- rendering ----------
    def render_tree(self, tree: dict, direction: str = "upstream") -> str:
        key = "upstream" if direction == "upstream" else "downstream"

        def about(table: str, column: str) -> str:
            info = self.column_info(table, column)
            bits = [info["type"]] if info.get("type") else []
            if info.get("description"):
                bits.append(f'"{info["description"]}"')
            return f"  ({' · '.join(bits)})" if bits else ""

        lines = [f"{tree['table']}.{tree['column']}{about(tree['table'], tree['column'])}"]

        def where(link: dict) -> str:
            bits = []
            if link.get("file"):
                bits.append(f"{link['file']}" + (f":{link['line']}" if link.get("line") else ""))
            if link.get("dag_id"):
                bits.append(f"dag={link['dag_id']} task={link['task_id']}")
            return f"  [{' | '.join(bits)}]" if bits else ""

        def walk(node: dict, prefix: str) -> None:
            links = node.get(key, [])
            for i, link in enumerate(links):
                last = i == len(links) - 1
                branch, cont = ("└─ ", "   ") if last else ("├─ ", "│  ")
                child = link["node"]
                if child.get("constant"):
                    label = "(no source column: constant / COUNT(*) / generated)"
                else:
                    label = f"{child['table']}.{child['column']}"
                    if child.get("is_source_table") and direction == "upstream":
                        label += "  ◆ SOURCE"
                    label += about(child["table"], child["column"])
                    if link.get("ambiguous"):
                        label += "  ⚠ AMBIGUOUS (unqualified column; source table not certain)"
                    if child.get("cycle"):
                        label += "  (cycle)"
                    if child.get("truncated"):
                        label += "  … (max depth)"
                lines.append(f"{prefix}{branch}{label}")
                lines.append(f"{prefix}{cont}   ⤷ {link['transformation']}{where(link)}")
                for s in link.get("steps") or []:
                    lines.append(f"{prefix}{cont}     via {s}")
                if not child.get("constant"):
                    walk(child, prefix + cont)

        walk(tree, "")
        return "\n".join(lines)

    @staticmethod
    def render_mermaid(tree: dict, direction: str = "upstream") -> str:
        key = "upstream" if direction == "upstream" else "downstream"
        ids: dict[str, str] = {}
        out = ["flowchart LR"]
        seen = set()

        def nid(label: str) -> str:
            if label not in ids:
                ids[label] = f"n{len(ids)}"
                out.append(f'  {ids[label]}["{label}"]')
            return ids[label]

        def walk(node: dict) -> None:
            me = nid(f"{node['table']}.{node['column']}")
            for link in node.get(key, []):
                child = link["node"]
                other = nid("constant" if child.get("constant") else f"{child['table']}.{child['column']}")
                src, dst = (other, me) if direction == "upstream" else (me, other)
                label = (link.get("transformation") or "").replace('"', "'")[:60]
                edge = f'  {src} -->|"{label}"| {dst}'
                if edge not in seen:
                    seen.add(edge)
                    out.append(edge)
                if not child.get("constant"):
                    walk(child)

        walk(tree)
        return "\n".join(out)

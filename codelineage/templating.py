"""Renders Jinja/Airflow templated SQL into plain SQL that a parser can read.

Unknown variables render as their own dotted name (``{{ params.project }}`` -> ``project``) so
identifiers stay readable instead of breaking the parse.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import jinja2
import yaml


class _NamedUndefined(jinja2.ChainableUndefined):
    def __str__(self) -> str:
        return self._undefined_name or "undefined"

    __html__ = __str__


def load_config_values(paths: list[Path]) -> dict:
    """Merges every YAML/JSON file under ``paths`` into one dict (later files win)."""
    values: dict = {}
    for base in paths:
        files = [base] if base.is_file() else sorted(base.rglob("*")) if base.exists() else []
        for f in files:
            try:
                if f.suffix in (".yaml", ".yml"):
                    data = yaml.safe_load(f.read_text(encoding="utf-8"))
                elif f.suffix == ".json":
                    data = json.loads(f.read_text(encoding="utf-8"))
                else:
                    continue
            except Exception:
                continue
            if isinstance(data, dict):
                values.update(data)
    return values


class SqlRenderer:
    def __init__(self, searchpath: list[Path], values: dict):
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader([str(p) for p in searchpath]),
            undefined=_NamedUndefined,
            autoescape=False,
        )
        self.env.filters.setdefault("ds_format", lambda v, *a: v)
        self.values = values

    def context(self, extra_params: dict | None = None) -> dict:
        params = {**self.values, **(extra_params or {})}
        return {
            **params,
            "params": params,
            "var": {"value": params, "json": params},
            "macros": {},
        }

    def render(self, sql: str, extra_params: dict | None = None) -> str:
        ctx = self.context(extra_params)
        try:
            sql = self.env.from_string(sql).render(**ctx)
        except jinja2.TemplateError:
            pass  # keep the raw text; the parser may still cope with it
        return _DOLLAR_VAR.sub(lambda m: _lookup(ctx, m.group(1)), sql)


_DOLLAR_VAR = re.compile(r"\$\{\s*([A-Za-z_][\w.]*)\s*\}")   # ${project}, ${env.dataset} (shell/Terraform style)


def _lookup(ctx: dict, dotted: str) -> str:
    node = ctx.get("params", ctx)
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return dotted.split(".")[-1]
        node = node[part]
    return str(node)

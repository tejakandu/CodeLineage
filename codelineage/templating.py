"""Renders Jinja/Airflow templated SQL into plain SQL that a parser can read.

Unknown variables render as their own dotted name (``{{ params.project }}`` -> ``project``) so
identifiers stay readable instead of breaking the parse.
"""
from __future__ import annotations

import json
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
        try:
            return self.env.from_string(sql).render(**self.context(extra_params))
        except jinja2.TemplateError:
            # Fall back to the raw text; the parser may still cope with it.
            return sql

"""The Lineage Terminal: an interactive, self-contained HTML explorer for the lineage graph.

Two ways to use it:
  * `codelineage explore --out lineage.html`  one static file (graph + SQL sources embedded), no
    server and no CDN, so it can be emailed, attached to a ticket or opened from a share.
  * `codelineage web`                         served, adding Gemini chat, trending and live SQL.
"""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from . import __version__
from .graph import LineageGraph

MAX_FILE_BYTES = 512_000


def referenced_files(graph: LineageGraph) -> set[str]:
    files = {e["file"] for e in graph.edges}
    files |= {b["file"] for t in graph.tables.values() for b in t.get("built_by", [])}
    return {f.split("::")[0] for f in files if f}


def read_repo_file(repo_root: Path, path: str) -> str | None:
    p = (repo_root / path).resolve()
    if not p.is_relative_to(repo_root.resolve()) or not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
        return None
    return p.read_text(errors="replace")


def render_explorer(graph: LineageGraph, repo_root: str | Path, *, mode: str = "static",
                    include_sql: bool = True, chat: bool = False, trending: dict | None = None) -> str:
    repo_root = Path(repo_root)
    files = {}
    if include_sql and mode == "static":
        for f in sorted(referenced_files(graph)):
            text = read_repo_file(repo_root, f)
            if text is not None:
                files[f] = text
    boot = {"graph": graph.data, "files": files, "mode": mode, "chat": chat, "version": __version__}
    if trending is not None:
        boot["trending"] = trending
    # "</" would end the <script> element early; "<\/" is the same string to JavaScript.
    payload = json.dumps(boot, default=list).replace("</", "<\\/")
    template = resources.files("codelineage").joinpath("static/explorer.html").read_text()
    return template.replace("/*__BOOT__*/null", payload, 1)

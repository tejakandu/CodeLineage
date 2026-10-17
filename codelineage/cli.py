"""Command line entry point: build the graph, trace lineage, or chat with Gemini."""
from __future__ import annotations

import argparse
import sys

from .builder import build_graph
from .config import LineageConfig
from .tools import DEFAULT_GRAPH, LineageService


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="codelineage")
    ap.add_argument("--repo", default=".", help="path to the SQL repo (containing lineage.yaml)")
    ap.add_argument("--graph", help=f"graph JSON path (default <repo>/{DEFAULT_GRAPH})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("build", help="parse the repo and write the lineage graph")
    t = sub.add_parser("trace", help="print multi-level lineage for a column")
    t.add_argument("table")
    t.add_argument("column")
    t.add_argument("--down", action="store_true", help="downstream impact instead of upstream sources")
    t.add_argument("--depth", type=int, default=10)
    s = sub.add_parser("search", help="find columns by name")
    s.add_argument("text")
    c = sub.add_parser("chat", help="ask questions in natural language (Gemini)")
    c.add_argument("question", nargs="*", help="one-shot question; omit for interactive mode")
    c.add_argument("--model")
    w = sub.add_parser("web", help="browser chat window (Gemini)")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--model")
    args = ap.parse_args(argv)

    if args.cmd == "build":
        cfg = LineageConfig.load(args.repo)
        graph = build_graph(cfg)
        path = args.graph or cfg.repo_root / DEFAULT_GRAPH
        graph.save(path)
        print(f"Wrote {path}: {len(graph.tables)} built tables, {len(graph.all_tables())} total, "
              f"{len(graph.edges)} column edges, {len(graph.data['warnings'])} warnings")
        for w_ in graph.data["warnings"][:50]:
            print("  warning:", w_)
        return 0

    svc = LineageService.from_env(args.repo, args.graph)
    if args.cmd == "trace":
        print(svc.trace_column_lineage(args.table, args.column, "downstream" if args.down else "upstream", args.depth))
    elif args.cmd == "search":
        print(svc.find_columns(args.text))
    elif args.cmd == "chat":
        from .gemini_chat import LineageChat
        chat = LineageChat(svc, args.model)
        if args.question:
            print(chat.ask(" ".join(args.question)))
            return 0
        print(f"CodeLineage chat ({chat.model}). Ctrl-D to exit.")
        while True:
            try:
                q = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                return 0
            if q:
                print(chat.ask(q))
    elif args.cmd == "web":
        from .web import serve
        serve(svc, args.host, args.port, args.model)
    return 0


if __name__ == "__main__":
    sys.exit(main())

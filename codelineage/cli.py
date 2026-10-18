"""Command line entry point: build the graph, trace lineage, or chat with Gemini."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .builder import build_graph
from .config import ConfigError, LineageConfig
from .tools import DEFAULT_GRAPH, LineageService


_WARNING_KINDS = [
    ("no lineage extracted", "files with no parseable lineage (wrong dialect? unsupported SQL?)"),
    ("statement(s) skipped", "files where some statements could not be parsed"),
    ("SQL file not found", "DAG SQL references not found (check template_searchpath)"),
    ("ambiguous", "ambiguous unqualified columns (include your DDL folder, or add schema_bigquery / schema_files)"),
    ("could not determine output columns", "SELECT * on tables missing from the schema"),
    ("schema may be missing columns", "queries using columns the schema lacks (resolved anyway)"),
    ("INFORMATION_SCHEMA", "schema mismatches (stale SQL or schema)"),
    ("could not read", "unreadable files"),
    ("skipped folder", "unreadable folders"),
    ("slow file", "slow files (consider excluding)"),
    ("prod lineage", "links BigQuery recorded in prod that no SQL in this repo explains"),
]


def _progress_printer():
    """Progress on stderr: an updating line in a terminal, a line every 10% when redirected."""
    tty = sys.stderr.isatty()
    state = {"last": -1, "started": time.monotonic()}

    def show(stage: str, done: int, total: int, name: str) -> None:
        pct = done * 100 // max(total, 1)
        if tty:
            elapsed = time.monotonic() - state["started"]
            line = f"  {stage}: {done}/{total} ({pct}%) {elapsed:5.0f}s  {name[-60:]}"
            print("\r" + line.ljust(110)[:110], end="" if done < total else "\n", file=sys.stderr, flush=True)
        elif pct // 10 != state["last"] or done == total:
            state["last"] = pct // 10
            print(f"  {stage}: {done}/{total} ({pct}%)", file=sys.stderr, flush=True)
        if done == total:
            state["last"], state["started"] = -1, time.monotonic()
    return show


def group_warnings(warnings: list[str]) -> list[tuple[str, list[str]]]:
    groups: dict[str, list[str]] = {}
    for w in warnings:
        kind = next((label for key, label in _WARNING_KINDS if key in w), "other")
        groups.setdefault(kind, []).append(w)
    return sorted(groups.items(), key=lambda kv: -len(kv[1]))


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2


def _main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):  # Windows code pages can't print the lineage tree's └ ◆ ⤷
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(prog="codelineage")
    ap.add_argument("--repo", default=".", help="path to the SQL repo (containing lineage.yaml)")
    ap.add_argument("--graph", help=f"graph JSON path (default <repo>/{DEFAULT_GRAPH})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    bp = sub.add_parser("build", help="parse the repo and write the lineage graph")
    bp.add_argument("--refresh-schema", action="store_true", help="re-read INFORMATION_SCHEMA from BigQuery now")
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
    x = sub.add_parser("explore", help="write the interactive Lineage Terminal as one HTML file")
    x.add_argument("--out", default="lineage.html")
    x.add_argument("--no-sql", action="store_true", help="don't embed SQL source files")
    pb = sub.add_parser("publish", help="upload graph, Lineage Terminal and SQL files to gs://bucket/prefix")
    pb.add_argument("uri", help="gs://bucket/prefix")
    pb.add_argument("--build", action="store_true", help="rebuild the graph from the repo first")
    pb.add_argument("--no-sql", action="store_true", help="don't upload SQL files")
    pl = sub.add_parser("prod-lineage", help="export the lineage BigQuery recorded in prod (Data Lineage API)")
    pl.add_argument("--project", required=True, help="prod project that runs the jobs")
    pl.add_argument("--location", required=True, help="where the data lives, e.g. us or northamerica-northeast1")
    pl.add_argument("--tables", nargs="+", help="dataset.table names in prod (default: every table the graph "
                                                "builds, minus prod_dataset_suffix)")
    pl.add_argument("--depth", type=int, default=3, help="hops to follow upstream (default 3)")
    pl.add_argument("--down", action="store_true", help="follow downstream instead of upstream")
    pl.add_argument("--out", help="file or gs://bucket/path.json (default <repo>/.lineage/prod_lineage.json)")
    pl.add_argument("--quota-project", help="project billed for the API calls (try the prod project if you get "
                                            "SERVICE_DISABLED or a quota-project error)")
    b = sub.add_parser("bq-init", help="create the BigQuery table for prompt/answer logging")
    b.add_argument("table", help="project.dataset.table")
    b.add_argument("--retention-days", type=int, default=400, help="partition expiry (0 = keep forever)")
    args = ap.parse_args(argv)

    if args.cmd == "bq-init":
        from .telemetry import create_table
        t = create_table(args.table, args.retention_days or None)
        print(f"Ready: {t.full_table_id}. Set CODELINEAGE_BQ_TABLE={args.table} to start logging.")
        return 0

    if args.cmd == "prod-lineage":
        return _prod_lineage(args)

    if args.cmd == "build":
        cfg = LineageConfig.load(args.repo)
        if cfg.config_file is None:
            others = sorted(p.name for p in cfg.repo_root.glob("lineage*") if p.is_file())
            print(f"NOTE: no lineage.yaml in {cfg.repo_root}; using defaults (whole repo, no SQL dialect)."
                  + (f" Found {', '.join(others)}: rename it to lineage.yaml?" if others else
                     " Copy examples/sample_repo/lineage.yaml there and edit it."), file=sys.stderr)
        elif not cfg.dialect:
            print(f"NOTE: no 'dialect' in {cfg.config_file.name}; set it (e.g. dialect: bigquery) or "
                  "dialect-specific SQL won't parse.", file=sys.stderr)
        graph = build_graph(cfg, progress=_progress_printer(), refresh_schema=args.refresh_schema)
        path = Path(args.graph or cfg.repo_root / DEFAULT_GRAPH)
        graph.save(path)
        warnings = graph.data["warnings"]
        print(f"Wrote {path}: {len(graph.tables)} built tables, {len(graph.all_tables())} total, "
              f"{len(graph.edges)} column edges, {len(warnings)} warnings")
        src = graph.data.get("schema_source")
        if src:
            print(f"Schema: {len(graph.data.get('schema', {}))} tables from {src['source']}"
                  + (f" (fetched {src['fetched_at'][:16].replace('T', ' ')} UTC; --refresh-schema to update)"
                     if src.get("source", "").startswith("cache") else ""))
        prod = graph.data.get("prod_lineage")
        if prod:
            links = prod["links"]
            print(f"Prod lineage: {len(links)} table links from {prod['source']} (fetched "
                  f"{str(prod.get('fetched_at'))[:16].replace('T', ' ')} UTC), "
                  f"{sum(not l['in_code'] for l in links)} not explained by the SQL")
        scan = graph.data.get("dag_scan") or {}
        if cfg.dag_paths:
            print(f"DAGs: {scan.get('files', 0)} files scanned, {scan.get('sql_tasks', 0)} SQL tasks found, "
                  f"{scan.get('linked', 0)} linked to SQL files")
            if scan.get("files") and not scan.get("linked"):
                print("  NOTE: no DAG task could be linked to SQL. Check dag_paths / template_searchpath, or "
                      "tell us how your DAGs reference SQL (e.g. sql=..., read_sql(...), open(...)).",
                      file=sys.stderr)
        if warnings:
            report = path.with_name("warnings.txt")
            report.write_text("\n".join(warnings) + "\n", encoding="utf-8")
            for kind, items in group_warnings(warnings):
                print(f"\n  {len(items):>5}  {kind}")
                for w_ in items[:3]:
                    print(f"         {w_}")
            print(f"\nAll warnings: {report}")
        return 0

    if args.cmd == "publish":
        from .gcs import is_gcs, publish
        if not is_gcs(args.uri):
            ap.error("publish needs a gs://bucket/prefix URI")
        svc = LineageService.from_env(args.repo, args.graph, rebuild=args.build)
        if svc.repo_root is None:
            ap.error("publish needs the SQL repo checked out locally (--repo)")
        m = publish(svc.graph, svc.repo_root, args.uri, include_sql=not args.no_sql)
        print(f"Published {m['tables']} tables / {m['edges']} edges / {m['files']} files "
              f"(commit {str(m['git_commit'])[:8]}) to {args.uri}\n"
              f"Users set CODELINEAGE_GRAPH={m['uri']}")
        return 0

    svc = LineageService.from_env(args.repo, args.graph)
    if args.cmd == "trace":
        print(svc.trace_column_lineage(args.table, args.column, "downstream" if args.down else "upstream", args.depth))
    elif args.cmd == "search":
        print(svc.find_columns(args.text))
    elif args.cmd == "chat":
        from .gemini_chat import LineageChat
        from .telemetry import InteractionLogger, default_user
        logger = InteractionLogger.from_env()
        chat = LineageChat(svc, args.model, logger=logger, surface="cli", user_id=default_user())
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
    elif args.cmd == "explore":
        from .explorer import render_explorer
        from .telemetry import trending
        trend = trending(svc.graph) if os.environ.get("CODELINEAGE_LOG_FILE") else None
        Path(args.out).write_text(render_explorer(svc.graph, svc.repo_root, include_sql=not args.no_sql,
                                                  trending=trend, read_file=svc.read_file), encoding="utf-8")
        print(f"Wrote {args.out}: open it in any browser (or VS Code: Simple Browser / Live Preview).")
    elif args.cmd == "web":
        from .web import serve
        serve(svc, args.host, args.port, args.model)
    return 0


def _prod_lineage(args) -> int:
    from . import prod_lineage

    cfg = LineageConfig.load(args.repo)
    tables = args.tables
    if not tables:
        path = Path(args.graph or cfg.repo_root / DEFAULT_GRAPH)
        if not path.exists():
            print("Error: no graph yet. Run `build` first, or pass --tables dataset.table ...", file=sys.stderr)
            return 2
        from .graph import LineageGraph
        suffix = cfg.prod_dataset_suffix
        tables = []
        for t in LineageGraph.load(path).tables:
            if "::" in t or "." not in t:
                continue
            ds, name = t.rsplit(".", 1)
            tables.append(f"{ds[:-len(suffix)] if suffix and ds.endswith(suffix) else ds}.{name}")
    out = args.out or str(cfg.repo_root / ".lineage" / "prod_lineage.json")
    try:
        data = prod_lineage.fetch(args.project, args.location, sorted(set(tables)), args.depth,
                                  "downstream" if args.down else "upstream",
                                  quota_project=args.quota_project, progress=_progress_printer_simple())
    except ImportError:
        print("Error: run  pip install google-cloud-datacatalog-lineage  in this venv first.", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        hint = ("enable the Data Lineage API, or add --quota-project <prod-project>"
                if "SERVICE_DISABLED" in msg or "quota project" in msg.lower() else
                "you need roles/datalineage.viewer on the prod project" if "403" in msg or "PERMISSION" in msg else
                "check --project / --location and your gcloud login")
        print(f"Error: {msg.splitlines()[0][:300]}\n  Hint: {hint}", file=sys.stderr)
        return 1
    prod_lineage.save(data, out)
    print(f"Wrote {out}: {len(data['links'])} table links from {len(data['tables'])} starting tables.")
    for w in data["warnings"]:
        print(f"  NOTE: {w}", file=sys.stderr)
    if not data["links"]:
        print("  No links: no job wrote these tables in the last 30 days, the names differ from prod "
              "(see prod_dataset_suffix), or --location is wrong.")
    print("Next: add  prod_lineage: " + (args.out or ".lineage/prod_lineage.json")
          + "  to lineage.yaml and run build.")
    return 0


def _progress_printer_simple():
    show = _progress_printer()
    return lambda done, total, name: show("searching prod lineage", done, total, name)


if __name__ == "__main__":
    sys.exit(main())

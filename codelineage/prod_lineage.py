"""Lineage that BigQuery recorded in production (Data Lineage API, Knowledge Catalog).

The SQL repo says what *should* happen; BigQuery's own lineage records what *did* run in the last
30 days, including loads that live outside the repo. `codelineage prod-lineage` exports those
table-to-table links to a JSON file (local or gs://); `build` merges them into the graph when
lineage.yaml sets `prod_lineage:`, marking each link as also found in the SQL or prod-only.

Needs roles/datalineage.viewer on the prod project and the Data Lineage API enabled there.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .gcs import GcsStore, is_gcs


def _table(fqn: str) -> str:
    """'bigquery:proj.ds.t' -> 'proj.ds.t'; other systems keep their full name."""
    return fqn[len("bigquery:"):] if fqn.startswith("bigquery:") else fqn


def _time(ts) -> str | None:
    try:
        return ts.isoformat(timespec="seconds") if ts else None
    except (AttributeError, TypeError):
        return None


def fetch(project: str, location: str, tables: list[str], depth: int = 3, direction: str = "upstream",
          client=None, quota_project: str | None = None, progress=None) -> dict:
    """Walks BigQuery's recorded lineage from `tables` (``ds.t`` or ``proj.ds.t``) up to `depth` hops.
    progress(done, total, name), if given, is called per table searched."""
    from google.cloud import datacatalog_lineage_v1 as lineage

    if client is None:
        opts = {"quota_project_id": quota_project} if quota_project else None
        client = lineage.LineageClient(client_options=opts)
    parent = f"projects/{project}/locations/{location}"
    start = [t if t.count(".") >= 2 else f"{project}.{t}" for t in tables]
    level = {t: 0 for t in start}
    queue, links, done = list(start), {}, 0
    while queue:
        name = queue.pop(0)
        done += 1
        if progress:
            progress(done, done + len(queue), name)
        ref = lineage.EntityReference(fully_qualified_name=f"bigquery:{name}")
        req = (lineage.SearchLinksRequest(parent=parent, target=ref) if direction == "upstream"
               else lineage.SearchLinksRequest(parent=parent, source=ref))
        for link in client.search_links(request=req):
            src, tgt = _table(link.source.fully_qualified_name), _table(link.target.fully_qualified_name)
            links.setdefault(link.name, {"source": src, "target": tgt, "first_seen": _time(link.start_time),
                                         "last_seen": _time(link.end_time), "processes": [],
                                         # column-level links name the columns (when BigQuery has them)
                                         "source_field": ".".join(getattr(link.source, "field", ())) or None,
                                         "target_field": ".".join(getattr(link.target, "field", ())) or None})
            nxt = src if direction == "upstream" else tgt
            if nxt not in level and level[name] + 1 < depth and "." in nxt and ":" not in nxt:
                level[nxt] = level[name] + 1
                queue.append(nxt)

    # Which job/pipeline made each link (best effort: needs extra permissions on some projects).
    warnings, names, processes = [], list(links), {}
    try:
        for i in range(0, len(names), 100):
            req = lineage.BatchSearchLinkProcessesRequest(parent=parent, links=names[i:i + 100])
            for pl in client.batch_search_link_processes(request=req):
                if pl.process not in processes:
                    proc = client.get_process(name=pl.process)
                    processes[pl.process] = {"name": proc.display_name or pl.process.rsplit("/", 1)[-1],
                                             "attributes": {k: str(v) for k, v in dict(proc.attributes).items()}}
                for info in pl.links:
                    if info.link in links:
                        links[info.link]["processes"].append({**processes[pl.process],
                                                              "last_run": _time(info.end_time)})
    except Exception as e:  # noqa: BLE001
        warnings.append(f"could not read the processes behind the links: {e}")
    return {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "project": project,
            "location": location, "direction": direction, "tables": start,
            "links": list(links.values()), "warnings": warnings}


def save(data: dict, dest: str | Path) -> str:
    text = json.dumps(data, indent=2)
    if is_gcs(dest):
        store, name = GcsStore.for_object(str(dest))
        store.upload_text(name, text, "application/json")
        return str(dest)
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_text(text, encoding="utf-8")
    return str(dest)


def load(src: str | Path) -> dict:
    if is_gcs(src):
        store, name = GcsStore.for_object(str(src))
        src = store.download_cached(name)
    return json.loads(Path(src).read_text(encoding="utf-8"))


def merge(data: dict, known_tables: set[str], code_pairs: set[tuple[str, str]], drop_catalog: bool = True,
          dataset_suffix: str = "") -> dict:
    """Maps prod table names onto the graph's names and marks which links the SQL also has.

    With drop_catalog the project is dropped (``proj.ds.t`` -> ``ds.t``). `dataset_suffix` is for a
    graph built with another environment's names: prod ``ds.t`` matches graph ``ds<suffix>.t``."""
    def name(full: str) -> str:
        n = full.lower()
        if drop_catalog and n.count(".") >= 2:
            n = n.split(".", 1)[1]
        if dataset_suffix and "." in n:
            ds, t = n.rsplit(".", 1)
            if f"{ds}{dataset_suffix}.{t}" in known_tables:
                return f"{ds}{dataset_suffix}.{t}"
        return n

    by_pair: dict[tuple[str, str], dict] = {}
    for link in data.get("links", []):
        s, t = name(link["source"]), name(link["target"])
        out = by_pair.get((s, t))
        if out is None:
            out = by_pair[(s, t)] = {"source_table": s, "target_table": t, "prod_source": link["source"],
                                     "prod_target": link["target"], "last_seen": link.get("last_seen"),
                                     "processes": [], "columns": [], "in_code": (s, t) in code_pairs}
        if (link.get("last_seen") or "") > (out["last_seen"] or ""):
            out["last_seen"] = link["last_seen"]
        for p in link.get("processes", []):
            if len(out["processes"]) < 5 and p["name"] not in {x["name"] for x in out["processes"]}:
                out["processes"].append(p)
        if link.get("source_field") or link.get("target_field"):
            out["columns"].append([(link.get("source_field") or "").lower(), (link.get("target_field") or "").lower()])
    links = list(by_pair.values())
    return {"fetched_at": data.get("fetched_at"), "project": data.get("project"),
            "location": data.get("location"), "links": links}

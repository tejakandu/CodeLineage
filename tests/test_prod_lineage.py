"""Prod lineage from BigQuery's Data Lineage API: export (with a fake API) and merge into the graph."""
import json
import shutil
from types import SimpleNamespace as NS

import pytest

from codelineage import prod_lineage
from codelineage.builder import build_graph
from codelineage.config import LineageConfig
from codelineage.tools import LineageService

from test_lineage import SAMPLE


def _ref(fqn, field=()):
    return NS(fully_qualified_name=fqn, field=list(field))


class FakeLineage:
    """Upstream links by target table, like SearchLinks with target=."""
    def __init__(self, links):
        self.links, self.searched = links, []

    def search_links(self, request):
        fqn = request.target.fully_qualified_name
        self.searched.append(fqn)
        return [NS(name=f"l/{i}", source=_ref(s, sf), target=_ref(t, tf), start_time=None, end_time=None)
                for i, (s, t, sf, tf) in enumerate(self.links) if t == fqn]

    def batch_search_link_processes(self, request):
        return [NS(process="projects/p/locations/l/processes/job1",
                   links=[NS(link=n, end_time=None) for n in request.links])]

    def get_process(self, name):
        return NS(display_name="daily_claim_load", attributes={"bigquery_job_id": "bqjob_1"})


def test_fetch_walks_upstream_and_names_processes():
    pytest.importorskip("google.cloud.datacatalog_lineage_v1")
    fake = FakeLineage([
        ("bigquery:prod.claims_internal.claim_base", "bigquery:prod.claims.claim", (), ()),
        ("bigquery:prod.claims.claim", "bigquery:prod.claims.claim", (), ()),
        ("bigquery:prod.raw.claim_src", "bigquery:prod.claims_internal.claim_base", ("id",), ("claim_id",)),
    ])
    data = prod_lineage.fetch("prod", "us", ["claims.claim"], depth=3, client=fake)
    assert fake.searched == ["bigquery:prod.claims.claim", "bigquery:prod.claims_internal.claim_base",
                             "bigquery:prod.raw.claim_src"]
    assert {(l["source"], l["target"]) for l in data["links"]} == {
        ("prod.claims_internal.claim_base", "prod.claims.claim"), ("prod.claims.claim", "prod.claims.claim"),
        ("prod.raw.claim_src", "prod.claims_internal.claim_base")}
    assert all(l["processes"][0]["name"] == "daily_claim_load" for l in data["links"])
    col = next(l for l in data["links"] if l["source_field"])
    assert (col["source_field"], col["target_field"]) == ("id", "claim_id")
    # depth limits the walk
    assert len(prod_lineage.fetch("prod", "us", ["claims.claim"], depth=1, client=FakeLineage(fake.links))["links"]) == 2


def test_merge_maps_names_and_suffix():
    data = {"links": [{"source": "prod.claims.contact", "target": "prod.claims.claim"},
                      {"source": "prod.claims.user", "target": "prod.claims.claim"},
                      {"source": "prod.claims.claim", "target": "prod.claims.claim"}]}
    known = {"claims_qa.claim", "claims_qa.contact"}
    out = prod_lineage.merge(data, known, {("claims_qa.contact", "claims_qa.claim"), ("claims_qa.claim", "claims_qa.claim")},
                             dataset_suffix="_qa")
    got = {(l["source_table"], l["target_table"]): l["in_code"] for l in out["links"]}
    # user isn't in the graph under either name, so it keeps its prod name
    assert got == {("claims_qa.contact", "claims_qa.claim"): True, ("claims.user", "claims_qa.claim"): False,
                   ("claims_qa.claim", "claims_qa.claim"): True}


def test_build_merges_prod_lineage(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(SAMPLE, repo, ignore=shutil.ignore_patterns(".lineage"))
    prod_lineage.save({"fetched_at": "2026-10-18T00:00:00+00:00", "project": "acme", "links": [
        # through the script's temp table in the SQL: still counts as explained by the code
        {"source": "acme.staging.int_orders_usd", "target": "acme.mart.dim_customer_ltv",
         "processes": [{"name": "ltv_job"}]},
        {"source": "acme.raw.orders", "target": "acme.staging.stg_orders"},
        # loaded outside the repo
        {"source": "acme.vendor.fx_feed", "target": "acme.ref.fx_rates_daily", "processes": [{"name": "fx_load"}]},
    ]}, repo / "prod.json")
    cfg = LineageConfig.load(repo)
    cfg.prod_lineage = "prod.json"
    g = build_graph(cfg)
    links = {(l["source_table"], l["target_table"]): l["in_code"] for l in g.data["prod_lineage"]["links"]}
    assert links == {("staging.int_orders_usd", "mart.dim_customer_ltv"): True,
                     ("raw.orders", "staging.stg_orders"): True, ("vendor.fx_feed", "ref.fx_rates_daily"): False}
    assert [w for w in g.data["warnings"] if w.startswith("prod lineage")] == [
        "prod lineage: vendor.fx_feed -> ref.fx_rates_daily ran in prod (fx_load) but no SQL in this repo does it"]
    # Gemini sees it through describe_table, and the prod-only table is a known name
    svc = LineageService(g, repo)
    info = json.loads(svc.describe_table("ref.fx_rates_daily"))
    assert info["prod_lineage"]["upstream"][0]["from"] == "vendor.fx_feed"
    assert "vendor.fx_feed" in g.all_tables()
    # a missing file is a warning, not a crash
    cfg.prod_lineage = "missing.json"
    assert any("could not read missing.json" in w for w in build_graph(cfg).data["warnings"])


def test_cli_exports_tables_from_the_graph(tmp_path, monkeypatch, capsys):
    from codelineage.cli import main
    repo = tmp_path / "repo"
    shutil.copytree(SAMPLE, repo, ignore=shutil.ignore_patterns(".lineage"))
    assert main(["--repo", str(repo), "build"]) == 0
    (repo / "lineage.yaml").write_text((repo / "lineage.yaml").read_text(encoding="utf-8")
                                       + "\nprod_dataset_suffix: ing\n", encoding="utf-8")
    seen = {}

    def fake_fetch(project, location, tables, depth, direction, **kw):
        seen.update(project=project, tables=tables, direction=direction)
        return {"links": [], "tables": tables, "warnings": []}
    monkeypatch.setattr(prod_lineage, "fetch", fake_fetch)
    assert main(["--repo", str(repo), "prod-lineage", "--project", "acme", "--location", "us"]) == 0
    assert seen["project"] == "acme" and seen["direction"] == "upstream"
    assert "stag.stg_orders" in seen["tables"] and "mart.fct_daily_revenue" in seen["tables"]
    assert not any("::" in t for t in seen["tables"])
    assert (repo / ".lineage" / "prod_lineage.json").exists()
    assert "prod_lineage: .lineage/prod_lineage.json" in capsys.readouterr().out

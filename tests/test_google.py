"""GCS publishing, BigQuery INFORMATION_SCHEMA and Google identity, with in-memory fakes."""
import json
import shutil

import pytest

from codelineage.builder import build_graph
from codelineage.config import LineageConfig
from codelineage.gcs import GcsStore, publish
from codelineage.schema import load_bigquery_schema
from codelineage.tools import LineageService

from test_lineage import SAMPLE


class FakeBlob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    @property
    def generation(self):
        return self.store[self.name][1]

    def upload_from_string(self, data, content_type=None):
        gen = self.store.get(self.name, (None, 0))[1] + 1
        self.store[self.name] = (data, gen)

    def download_as_text(self):
        return self.store[self.name][0]

    def exists(self):
        return self.name in self.store


class FakeBucket:
    def __init__(self, store):
        self.store = store

    def blob(self, name):
        return FakeBlob(self.store, name)

    def get_blob(self, name):
        return FakeBlob(self.store, name) if name in self.store else None


class FakeGcs:
    def __init__(self):
        self.objects = {}

    def bucket(self, name):
        return FakeBucket(self.objects.setdefault(name, {}))


@pytest.fixture
def gcs(monkeypatch, tmp_path):
    fake = FakeGcs()
    monkeypatch.setenv("CODELINEAGE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("CODELINEAGE_USER", "publisher@corp.com")
    from google.cloud import storage
    monkeypatch.setattr(storage, "Client", lambda *a, **k: fake)
    return fake


def test_publish_then_use_without_a_repo_clone(gcs, tmp_path, monkeypatch):
    graph = build_graph(LineageConfig.load(SAMPLE))
    m = publish(graph, SAMPLE, "gs://lineage-bkt/prod", client=gcs)
    objs = gcs.objects["lineage-bkt"]
    assert {"prod/graph.json", "prod/lineage.html", "prod/manifest.json", "prod/files/sql/mart/fct_daily_revenue.sql"} <= set(objs)
    assert m["uri"] == "gs://lineage-bkt/prod/graph.json" and m["published_by"] == "publisher@corp.com"
    assert json.loads(objs["prod/manifest.json"][0])["git_commit"] == graph.data["git_commit"]

    # an employee with no clone: empty working dir, graph from GCS
    empty = tmp_path / "empty"
    empty.mkdir()
    svc = LineageService.from_env(str(empty), "gs://lineage-bkt/prod/graph.json")
    assert svc.repo_root is None
    assert "raw.orders.gross_amount" in svc.trace_column_lineage("fct_daily_revenue", "net_revenue")
    assert "SUM(net_amount_usd)" in svc.read_sql_file("sql/mart/fct_daily_revenue.sql")
    assert svc.read_sql_file("../../secrets.txt").startswith("File not found")


def test_cached_graph_redownloads_only_when_changed(gcs, tmp_path):
    store = GcsStore("gs://b/p", client=gcs)
    store.upload_text("graph.json", '{"tables": {}, "edges": []}')
    path = store.download_cached("graph.json")
    path.write_text("locally modified", encoding="utf-8")              # proves the next call doesn't re-download
    assert store.download_cached("graph.json").read_text(encoding="utf-8") == "locally modified"
    store.upload_text("graph.json", '{"tables": {"a": {}}, "edges": []}')   # new generation
    assert '"a"' in store.download_cached("graph.json").read_text(encoding="utf-8")


def test_cached_graph_used_when_gcs_unreachable(gcs):
    store = GcsStore("gs://b/p", client=gcs)
    store.upload_text("graph.json", "{}")
    store.download_cached("graph.json")

    class Down:
        def bucket(self, name):
            raise ConnectionError("no network")
    assert GcsStore("gs://b/p", client=Down()).download_cached("graph.json").read_text(encoding="utf-8") == "{}"


class FakeRow(dict):
    def items(self):
        return super().items()


class FakeBq:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.sql = rows or [], fail, []

    def query(self, sql, **kw):
        if self.fail:
            raise ConnectionError("bq down")
        self.sql.append(sql)
        return type("Job", (), {"result": lambda _s: [FakeRow(r) for r in self.rows]})()


BQ_ROWS = [
    {"table_catalog": "acme-prod", "table_schema": "raw", "table_name": "orders", "column_name": "Gross_Amount",
     "data_type": "NUMERIC", "description": "Order amount before discounts"},
    {"table_catalog": "acme-prod", "table_schema": "raw", "table_name": "payments", "column_name": "card_number",
     "data_type": "STRING", "description": None},
]


def test_bigquery_information_schema_query_and_rows():
    bq = FakeBq(BQ_ROWS)
    out = load_bigquery_schema(["acme-prod.region-us"], client=bq)
    assert out["raw.orders"]["gross_amount"] == {"type": "NUMERIC", "description": "Order amount before discounts"}
    assert "`acme-prod`.`region-us`.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS" in bq.sql[0]
    with pytest.raises(ValueError):
        load_bigquery_schema(["acme-prod.raw`; DROP TABLE x"], client=bq)


def test_build_uses_bigquery_schema_and_caches_it(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(SAMPLE, repo, ignore=shutil.ignore_patterns(".lineage"))
    cfg = LineageConfig.load(repo)
    cfg.schema_files, cfg.schema_bigquery = [], ["acme-prod.region-us"]
    g = build_graph(cfg, bq_client=FakeBq(BQ_ROWS))
    assert g.column_info("raw.orders", "gross_amount")["description"] == "Order amount before discounts"
    assert (repo / ".lineage" / "information_schema.json").exists()
    # BigQuery down: falls back to the cache, with a warning
    g2 = build_graph(cfg, bq_client=FakeBq(fail=True))
    assert g2.column_info("raw.orders", "gross_amount")["type"] == "NUMERIC"
    assert any("using cached copy" in w for w in g2.data["warnings"])


def test_user_identity_precedence(monkeypatch):
    from codelineage import telemetry
    monkeypatch.setenv("CODELINEAGE_USER", "override@corp.com")
    assert telemetry.default_user() == "override@corp.com"
    monkeypatch.delenv("CODELINEAGE_USER")
    monkeypatch.setattr(telemetry, "google_account", lambda: "alice@corp.com")
    assert telemetry.default_user() == "alice@corp.com"

from pathlib import Path

import pytest

from codelineage.builder import build_graph
from codelineage.config import LineageConfig
from codelineage.dag_parser import parse_dag_file
from codelineage.schema import load_information_schema, to_sqlglot_schema
from codelineage.sql_parser import parse_sql
from codelineage.tools import LineageService

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample_repo"


@pytest.fixture(scope="module")
def graph():
    return build_graph(LineageConfig.load(SAMPLE))


@pytest.fixture(scope="module")
def service(graph):
    return LineageService(graph, SAMPLE)


def sources(node):
    """All terminal (table, column) pairs under an upstream trace."""
    links = node.get("upstream", [])
    if not links:
        return {(node["table"], node["column"])}
    out = set()
    for link in links:
        if not link["node"].get("constant"):
            out |= sources(link["node"])
    return out


def test_insert_positional_columns_and_cte_resolution():
    sql = """
    INSERT INTO s.t (a, total)
    WITH x AS (SELECT id, amount * 2 AS amt FROM raw.src)
    SELECT id, SUM(amt) FROM x GROUP BY id
    """
    (st,) = parse_sql(sql, "bigquery")
    assert st.target_table == "s.t"
    edges = {(e.target_column, e.source_table, e.source_column) for e in st.edges}
    assert edges == {("a", "raw.src", "id"), ("total", "raw.src", "amount")}
    total = next(e for e in st.edges if e.target_column == "total")
    assert total.via == ["x"]
    assert "amount * 2" in total.steps[0]


def test_merge():
    sql = """MERGE ds.dim t USING raw.src s ON t.id = s.id
             WHEN MATCHED THEN UPDATE SET t.name = UPPER(s.name)
             WHEN NOT MATCHED THEN INSERT (id, name) VALUES (s.id, s.name)"""
    (st,) = parse_sql(sql, "bigquery")
    assert st.statement_type == "MERGE"
    assert ("name", "raw.src", "name") in {(e.target_column, e.source_table, e.source_column) for e in st.edges}


def test_select_star_expanded_from_known_schema():
    schema = {"s": {"x": {"a": "INT", "b": "INT"}}}
    (st,) = parse_sql("CREATE VIEW s.v AS SELECT * FROM s.x", "bigquery", schema)
    assert sorted(st.columns) == ["a", "b"]


def test_dag_parser_finds_sql_tasks_and_dependencies():
    tasks = {t.task_id: t for t in parse_dag_file(SAMPLE / "dags" / "daily_sales_dag.py")}
    assert tasks["stg_orders"].sql_file == "staging/stg_orders.sql"
    assert tasks["fct_daily_revenue"].dag_id == "daily_sales"
    assert tasks["fct_daily_revenue"].upstream_tasks == ["int_orders_usd", "stg_customers"]
    inline = {t.task_id: t for t in parse_dag_file(SAMPLE / "dags" / "reporting_dag.py")}
    assert inline["customer_snapshot"].inline_sql


def test_jinja_params_rendered_and_catalog_dropped(graph):
    assert "staging.stg_orders" in graph.tables
    assert "raw.orders" in graph.all_tables()
    # the only warning is a real finding: a table used in SQL but missing from the schema export
    assert graph.data["warnings"] == ["1 source tables not in INFORMATION_SCHEMA: raw.customer_flags"]


def test_multi_level_upstream(graph):
    tree = graph.trace("mart.rpt_region_kpis", "total_revenue")
    assert sources(tree) == {
        ("ref.fx_rates_daily", "usd_rate"),
        ("raw.orders", "gross_amount"),
        ("raw.orders", "discount_amount"),
    }
    built = graph.tables["staging.int_orders_usd"]["built_by"][0]
    assert (built["dag_id"], built["task_id"]) == ("daily_sales", "int_orders_usd")


def test_downstream_impact(graph):
    tree = graph.trace("raw.orders", "discount_amount", "downstream")
    text = graph.render_tree(tree, "downstream")
    assert "mart.rpt_region_kpis.avg_order_value" in text


def test_service_fuzzy_resolution_and_errors(service):
    out = service.trace_column_lineage("fct_daily_revenue", "net_revenue")
    assert "raw.orders.gross_amount" in out and "```mermaid" in out
    assert "no column like" in service.trace_column_lineage("fct_daily_revenue", "zzz")
    assert service.read_sql_file("../../etc/passwd").startswith("File not found")


def edge_set(statements):
    return {(e.target_table, e.target_column, e.source_table, e.source_column)
            for s in statements for e in s.edges}


def test_script_with_procedure_update_and_bad_statement():
    sql = """
    DECLARE d DATE DEFAULT CURRENT_DATE();
    THIS IS NOT SQL AT ALL;
    CREATE OR REPLACE PROCEDURE ds.p()
    BEGIN
      IF TRUE THEN
        INSERT INTO mart.a (x) SELECT y FROM raw.b;
      END IF;
      UPDATE mart.a t SET x = s.z FROM raw.c s WHERE t.id = s.id;
    END;
    """
    statements = parse_sql(sql, "bigquery")
    assert edge_set(statements) == {("mart.a", "x", "raw.b", "y"), ("mart.a", "x", "raw.c", "z")}
    assert [s.line for s in statements] == [7, 9]


def test_temp_tables_are_scoped_per_file():
    sql = "CREATE TEMP TABLE tmp AS SELECT a FROM raw.x; INSERT INTO mart.y (a) SELECT a FROM tmp;"
    one = parse_sql(sql, "bigquery", temp_scope="one.sql")
    assert ("mart.y", "a", "one.sql::tmp", "a") in edge_set(one)
    assert one[0].is_temp and one[0].target_table == "one.sql::tmp"


def test_unqualified_join_columns_flagged_without_schema_and_resolved_with_it():
    sql = "CREATE TABLE m.t AS SELECT ORDER_ID, Region FROM RAW.Orders o JOIN raw.customers c ON o.cid = c.cid"
    (bare,) = parse_sql(sql, "bigquery")
    assert all(e.ambiguous for e in bare.edges) and bare.warnings
    schema = {"raw": {"orders": {"order_id": "UNKNOWN", "cid": "UNKNOWN"},
                      "customers": {"cid": "UNKNOWN", "region": "UNKNOWN"}}}
    (resolved,) = parse_sql(sql, "bigquery", schema)
    assert edge_set([resolved]) == {("m.t", "order_id", "raw.orders", "order_id"),
                                    ("m.t", "region", "raw.customers", "region")}
    assert not any(e.ambiguous for e in resolved.edges)


def test_information_schema_formats(tmp_path):
    (tmp_path / "cols.csv").write_text(
        "TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,DATA_TYPE,DESCRIPTION\n"
        "p,Raw,Orders,Order_ID,STRING,Order key\n", encoding="utf-8")
    (tmp_path / "more.json").write_text('{"ref": {"fx": {"usd_rate": "NUMERIC"}}}', encoding="utf-8")
    info = load_information_schema([tmp_path])
    assert info["raw.orders"]["order_id"] == {"type": "STRING", "description": "Order key"}
    assert info["ref.fx"]["usd_rate"]["type"] == "NUMERIC"
    assert to_sqlglot_schema(info) == {"raw": {"orders": {"order_id": "UNKNOWN"}}, "ref": {"fx": {"usd_rate": "UNKNOWN"}}}


def test_sample_repo_needs_schema_for_payments(graph):
    no_schema = LineageConfig.load(SAMPLE)
    no_schema.schema_files = []
    bare = build_graph(no_schema)
    assert any("ambiguous" in w for w in bare.data["warnings"])
    assert bare.columns("staging.v_payments_masked") == []   # SELECT * unknown without schema
    # with INFORMATION_SCHEMA: resolved, and SELECT * EXCEPT drops the PCI column
    assert graph.columns("staging.v_payments_masked") == ["amount_cents", "method_id", "order_id", "payment_id"]
    stg = {(e["target_column"], e["source_table"], e["source_column"]) for e in graph.edges
           if e["target_table"] == "staging.stg_payments"}
    assert ("method_name", "raw.payment_methods", "method_name") in stg
    assert graph.column_info("raw.payments", "amount_cents")["description"] == "Captured amount in USD cents"


def test_complex_script_traces_through_temp_table_and_procedure(graph):
    tree = graph.trace("mart.dim_customer_ltv", "tier")
    assert sources(tree) == {("raw.orders", "gross_amount"), ("raw.orders", "discount_amount"),
                             ("ref.fx_rates_daily", "usd_rate"), ("raw.customer_flags", "vip_tier")}
    assert "sql/mart/dim_customer_ltv.sql::tmp_customer_orders" in graph.tables


def test_stale_schema_does_not_lose_lineage():
    # the schema export predates the discount_amount column
    schema = {"raw": {"orders": {"order_id": "UNKNOWN", "gross_amount": "UNKNOWN"}}}
    (st,) = parse_sql("CREATE TABLE s.t AS SELECT o.order_id, o.gross_amount - o.discount_amount AS net FROM raw.orders o",
                      "bigquery", schema)
    assert ("s.t", "net", "raw.orders", "discount_amount") in edge_set([st])
    assert any("INFORMATION_SCHEMA may be stale" in w for w in st.warnings)


def test_cli_never_relies_on_the_platform_encoding(tmp_path):
    """Windows defaults to cp1252; every file read/write must name its encoding (regression)."""
    import shutil
    import subprocess
    import sys
    repo = tmp_path / "repo"
    shutil.copytree(SAMPLE, repo, ignore=shutil.ignore_patterns(".lineage"))
    base = [sys.executable, "-X", "warn_default_encoding", "-W", "error::EncodingWarning", "-m", "codelineage", "--repo", str(repo)]
    for args in (["build"], ["trace", "rpt_region_kpis", "total_revenue"], ["explore", "--out", str(tmp_path / "x.html")]):
        r = subprocess.run(base + args, capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, r.stderr[-2000:]


def test_graph_paths_use_forward_slashes(graph):
    """Paths are stored POSIX-style so a graph built on Windows works elsewhere (and in GCS)."""
    files = {e["file"] for e in graph.edges} | {b["file"] for t in graph.tables.values() for b in t["built_by"]}
    assert files and not any("\\" in f for f in files)
    assert "dags/reporting_dag.py::customer_snapshot" in files

from pathlib import Path

import pytest

from codelineage.builder import build_graph
from codelineage.config import LineageConfig
from codelineage.dag_parser import parse_dag_file
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
    assert not graph.data["warnings"]


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

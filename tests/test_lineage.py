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
    stale = [w for w in st.warnings if "schema may be missing columns" in w]
    assert len(stale) == 1                       # one warning per statement, not one per column


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


def test_windows_extended_length_paths():
    from codelineage.fsutil import extended, plain
    assert extended(r"C:\repo\a.sql", windows=True) == "\\\\?\\C:\\repo\\a.sql"
    assert extended(r"\\server\share\a.sql", windows=True) == "\\\\?\\UNC\\server\\share\\a.sql"
    assert extended("/repo/a.sql", windows=False) == "/repo/a.sql"
    for p in (r"C:\repo\a.sql", r"\\server\share\a.sql"):
        assert plain(extended(p, windows=True)) == p


def test_deep_paths_excluded_folders_and_unreadable_files(tmp_path, monkeypatch):
    import codelineage.builder as builder
    repo = tmp_path / "repo"
    deep = repo / "sql" / "ddl_archive" / ("x" * 60) / ("y" * 60) / ("z" * 60) / ("w" * 60)
    deep.mkdir(parents=True)
    (deep / "old.sql").write_text("CREATE TABLE a.old AS SELECT id FROM raw.t", encoding="utf-8")
    live = repo / "sql" / "live"
    live.mkdir(parents=True)
    (live / "ok.sql").write_text("CREATE TABLE a.ok AS SELECT id FROM raw.t", encoding="utf-8")
    (live / "broken.sql").write_text("CREATE TABLE a.broken AS SELECT id FROM raw.t", encoding="utf-8")
    (repo / "lineage.yaml").write_text("dialect: bigquery\nsql_paths: [sql]\n", encoding="utf-8")

    cfg = LineageConfig.load(repo)
    assert "a.old" in build_graph(cfg).tables          # >260-char path still read

    real = builder.read_text
    monkeypatch.setattr(builder, "read_text", lambda f: (_ for _ in ()).throw(OSError(2, "Filename too long"))
                        if f.name == "broken.sql" else real(f))
    cfg.exclude = ["sql/ddl_archive/*"]
    g = build_graph(cfg)
    assert set(g.tables) == {"a.ok"}                   # archive skipped, broken file skipped
    assert any("broken.sql: could not read (Filename too long)" in w for w in g.data["warnings"])


def test_parse_failures_report_their_reason():
    diags = []
    assert parse_sql("CREATE TABLE a.b AS SELECT FROM WHERE ,,", "bigquery", diagnostics=diags) == []
    assert diags and diags[0].startswith("line 1:")
    diags = []
    parse_sql("SELECT COUNT(*) FROM `p.d.t`", "bigquery", diagnostics=diags)
    assert diags == []                                   # read-only queries are not failures


def test_missing_lineage_yaml_is_reported(tmp_path, capsys):
    from codelineage.cli import main
    (tmp_path / "lineage.html").write_text("dialect: bigquery\n", encoding="utf-8")
    main(["--repo", str(tmp_path), "build"])
    err = capsys.readouterr().err
    assert "no lineage.yaml" in err and "lineage.html" in err
    (tmp_path / "lineage.yml").write_text("dialect: bigquery\n", encoding="utf-8")
    assert LineageConfig.load(tmp_path).dialect == "bigquery"   # .yml accepted too


def test_lineage_yaml_mistakes_get_plain_messages(tmp_path):
    import pytest
    from codelineage.config import ConfigError
    cfg = tmp_path / "lineage.yaml"
    cfg.write_text('dialect: bigquery\nexclude:["a/*","b/*"]\ndrop_catalog: true\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r'line 2: exclude:\["a/\*".*space after the colon'):
        LineageConfig.load(tmp_path)
    cfg.write_text("dialect: bigquery\nsql_path: [sql]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown setting.*sql_path.*Valid settings"):
        LineageConfig.load(tmp_path)
    cfg.write_text("dialect: bigquery\nsql_paths: sql\nexclude:\n", encoding="utf-8")
    loaded = LineageConfig.load(tmp_path)
    assert loaded.sql_paths == ["sql"] and loaded.exclude == []


def test_cli_prints_config_errors_without_traceback(tmp_path, capsys):
    from codelineage.cli import main
    (tmp_path / "lineage.yaml").write_text("exclude:[x]\n", encoding="utf-8")
    assert main(["--repo", str(tmp_path), "build"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("Error: lineage.yaml is not valid YAML at line 1") and "Traceback" not in err


def test_build_reports_progress_for_both_passes():
    calls = []
    build_graph(LineageConfig.load(SAMPLE), progress=lambda stage, done, total, name: calls.append((stage, done, total)))
    stages = {c[0] for c in calls}
    assert stages == {"reading columns", "tracing lineage"}
    for stage in stages:
        done = [c for c in calls if c[0] == stage]
        assert done[-1][1] == done[-1][2] and [d[1] for d in done] == list(range(1, len(done) + 1))


def test_wide_query_with_schema_gap_stays_fast_and_quiet():
    import time
    cols = [f"c{i}" for i in range(150)]
    schema = {"curated": {"claims": {c: "UNKNOWN" for c in cols[:100]}}}     # lacks reference_code
    sql = (f"CREATE OR REPLACE VIEW mart.v AS WITH base AS (SELECT c.*, c.reference_code AS ref "
           f"FROM curated.claims c) SELECT {', '.join(f'b.{c}' for c in cols[:100])}, b.ref FROM base b")
    t = time.monotonic()
    (st,) = parse_sql(sql, "bigquery", schema)
    assert time.monotonic() - t < 1.0
    assert len(st.edges) == 101 and len(st.warnings) == 1


def test_only_data_writing_statements_are_reported():
    diags = []
    parse_sql("CREATE TEMP FUNCTION f(x INT64) AS (x + 1); CREATE SCHEMA IF NOT EXISTS ds;", "bigquery",
              diagnostics=diags)
    assert diags == []


def test_ddl_files_are_a_schema_source(tmp_path):
    (tmp_path / "ddl").mkdir()
    (tmp_path / "dml").mkdir()
    (tmp_path / "ddl" / "t.sql").write_text(
        "CREATE TABLE IF NOT EXISTS `p.raw.orders` (order_id STRING, cid STRING, amount NUMERIC);\n"
        "CREATE TABLE IF NOT EXISTS `p.raw.customers` (cid STRING, region STRING);", encoding="utf-8")
    (tmp_path / "dml" / "m.sql").write_text(
        "INSERT INTO mart.t (order_id, region, amount) SELECT order_id, region, amount "
        "FROM `p.raw.orders` o JOIN `p.raw.customers` c ON o.cid = c.cid", encoding="utf-8")
    (tmp_path / "lineage.yaml").write_text("dialect: bigquery\nsql_paths: [ddl, dml]\n", encoding="utf-8")
    g = build_graph(LineageConfig.load(tmp_path))
    edges = {(e["target_column"], e["source_table"], e["source_column"], e["ambiguous"]) for e in g.edges}
    assert edges == {("order_id", "raw.orders", "order_id", False), ("region", "raw.customers", "region", False),
                     ("amount", "raw.orders", "amount", False)}
    assert g.column_info("raw.orders", "amount")["type"] == "NUMERIC"
    assert not any("ambiguous" in w for w in g.data["warnings"])


def test_view_column_list_over_select_star():
    schema = {"ds": {"src": {"start_dt": "DATE", "end_dt": "DATE"}}}
    sql = "CREATE OR REPLACE VIEW ds.v (business_start_dt, business_end_dt) AS SELECT * FROM ds.src"
    (st,) = parse_sql(sql, "bigquery", schema)
    assert edge_set([st]) == {("ds.v", "business_start_dt", "ds.src", "start_dt"),
                              ("ds.v", "business_end_dt", "ds.src", "end_dt")}
    assert not st.warnings
    (bare,) = parse_sql(sql, "bigquery")                  # no schema: one clear warning, not one per column
    assert bare.edges == [] and len(bare.warnings) == 1 and "SELECT *" in bare.warnings[0]


def test_dollar_placeholders_are_rendered():
    from codelineage.templating import SqlRenderer
    r = SqlRenderer([], {"project": "acme-prod", "env": {"ds": "curated"}})
    out = r.render("CREATE OR REPLACE VIEW ${project}.${env.ds}.v AS SELECT a FROM ${unknown_var}.t")
    assert out == "CREATE OR REPLACE VIEW acme-prod.curated.v AS SELECT a FROM unknown_var.t"
    (st,) = parse_sql(out, "bigquery")
    assert st.target_table == "curated.v"


def test_repeated_column_failures_become_one_warning():
    from codelineage.sql_parser import _collapse
    ws = [f"column c{i}: Cannot find column 'c{i}' in query." for i in range(8)] + ["something else"]
    out = _collapse(ws)
    assert len(out) == 2 and out[1].startswith("8 columns (c0, c1, c2, c3, c4, ...): Cannot find column '<col>'")


def test_dag_patterns_beyond_literal_sql_argument(tmp_path):
    dag = tmp_path / "patterns_dag.py"
    dag.write_text('''
import os
from airflow import DAG
from airflow.decorators import task
SQL_DIR = "/opt/airflow/sql"

def read_sql(name):
    return open(os.path.join(SQL_DIR, name)).read()

with DAG("claims_daily", schedule="0 2 * * *") as dag:
    a = BigQueryInsertJobOperator(task_id="load_a",
        configuration={"query": {"query": read_sql("mart/a.sql"), "useLegacySql": False}})
    b = BigQueryExecuteQueryOperator(task_id="load_b", sql=open(os.path.join(SQL_DIR, "mart", "b.sql")).read())
    tasks = [SQLExecuteQueryOperator(task_id=f"load_{n}", sql=f"{n}.sql") for n in ["c", "d"]]

    @task(task_id="run_e")
    def run_e():
        client.query(open("/opt/airflow/sql/mart/e.sql").read())

    a >> b
''', encoding="utf-8")
    from codelineage.dag_parser import parse_dag_file
    found = {t.task_id: t.sql_file for t in parse_dag_file(dag)}
    assert found == {"load_a": "mart/a.sql", "load_b": "mart/b.sql", "run_e": "/opt/airflow/sql/mart/e.sql"}
    assert all(t.dag_id == "claims_daily" and t.schedule == "0 2 * * *" for t in parse_dag_file(dag))


def test_taskflow_override_with_sql_path_variable_and_bare_file_name(tmp_path):
    """The real repo's DAG shape: a shared @task function called via .override(task_id=...), with the
    SQL file in a module-level variable holding only the file name."""
    (tmp_path / "dags").mkdir()
    (tmp_path / "bigquery" / "dml").mkdir(parents=True)
    (tmp_path / "bigquery" / "dml" / "all_claims_view.sql").write_text(
        "CREATE OR REPLACE TABLE ds.all_claims_view AS SELECT c.claim_id FROM raw.claims c", encoding="utf-8")
    for name in ("dup_a", "dup_b"):
        (tmp_path / "bigquery" / name).mkdir()
        (tmp_path / "bigquery" / name / "dup.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "dags" / "tr__audit_data__all_claims_view_refresh.py").write_text('''
from airflow.decorators import dag
from common.tasks import execute_sql_fn

query_sql_path = "all_claims_view.sql"
other_sql: str = "dup.sql"

with DAG(dag_id="tr__audit_data__all_claims_view_refresh", schedule="0 5 * * *") as d:
    wait = ExternalTaskSensor(task_id="wait_for_reference_data_dag", external_task_id=None)
    task_1 = execute_sql_fn.override(task_id="refresh_acv")(predefined_args, query_sql_path)
    task_2 = execute_sql_fn.override(task_id="refresh_dup")(predefined_args, other_sql)
    wait >> task_1
''', encoding="utf-8")
    (tmp_path / "lineage.yaml").write_text("dialect: bigquery\nsql_paths: [bigquery]\ndag_paths: [dags]\n",
                                          encoding="utf-8")
    g = build_graph(LineageConfig.load(tmp_path))
    (bb,) = g.tables["ds.all_claims_view"]["built_by"]
    assert (bb["dag_id"], bb["task_id"], bb["schedule"]) == ("tr__audit_data__all_claims_view_refresh", "refresh_acv", "0 5 * * *")
    assert bb["upstream_tasks"] == ["wait_for_reference_data_dag"]
    assert g.data["dag_scan"] == {"files": 1, "sql_tasks": 2, "linked": 1}
    assert any("dup.sql: 2 files have this name" in w for w in g.data["warnings"])


def test_bigquery_schema_job_project(monkeypatch):
    from google.cloud import bigquery
    from codelineage.schema import load_bigquery_schema
    seen = {}

    class Client:
        def __init__(self, project=None):
            seen["project"] = project

        def query(self, sql, location=None):
            seen["sql"], seen["location"] = sql, location
            return type("J", (), {"result": lambda s: []})()
    monkeypatch.setattr(bigquery, "Client", Client)
    load_bigquery_schema(["dp-qa-derived-e0f9.region-northamerica-northeast1"], project="dp-qa-derived-e0f9")
    assert seen["project"] == "dp-qa-derived-e0f9"
    assert "`dp-qa-derived-e0f9`.`region-northamerica-northeast1`.INFORMATION_SCHEMA" in seen["sql"]
    assert seen["location"] == "northamerica-northeast1"
    load_bigquery_schema(["dp-qa-derived-e0f9.product_common_claim_rt_qa_sit3"])
    assert seen["location"] is None


def test_duplicate_file_names_prefer_the_one_with_logic(tmp_path):
    (tmp_path / "dags").mkdir()
    for folder, sql in (("bigquery/ddl_master/derived", "CREATE TABLE IF NOT EXISTS ds.activity (id STRING, amt NUMERIC)"),
                        ("bigquery/dml", "INSERT INTO ds.activity (id, amt) SELECT id, amt FROM raw.activity_src")):
        (tmp_path / folder).mkdir(parents=True)
        (tmp_path / folder / "activity.sql").write_text(sql, encoding="utf-8")
    for folder in ("bigquery/a", "bigquery/b"):                  # both have logic: truly ambiguous
        (tmp_path / folder).mkdir(parents=True)
        (tmp_path / folder / "both.sql").write_text("INSERT INTO ds.x (id) SELECT id FROM raw.y", encoding="utf-8")
    (tmp_path / "dags" / "d.py").write_text('''
p1 = "activity.sql"
p2 = "both.sql"
with DAG(dag_id="act") as d:
    t1 = execute_sql_fn.override(task_id="load_activity")(args, p1)
    t2 = execute_sql_fn.override(task_id="load_both")(args, p2)
''', encoding="utf-8")
    (tmp_path / "lineage.yaml").write_text("dialect: bigquery\nsql_paths: [bigquery]\ndag_paths: [dags]\n", encoding="utf-8")
    g = build_graph(LineageConfig.load(tmp_path))
    runs = {(b["file"], b.get("task_id")) for b in g.tables["ds.activity"]["built_by"]}
    assert ("bigquery/dml/activity.sql", "load_activity") in runs
    w = g.data["warnings"]
    assert any("both.sql: 2 files have this name" in x for x in w)
    assert not any("SQL file not found" in x for x in w)          # duplicates are not "not found"

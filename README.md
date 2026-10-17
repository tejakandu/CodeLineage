# CodeLineage

Ask in plain English *"where does `net_revenue` in `fct_daily_revenue` come from?"* and get a
**multi-level, column-level lineage** answer, traced through your SQL files, Airflow DAGs and config
files, explained by **Gemini**.

Built for this toolset: **VS Code + Gemini Code Assist + Gemini models** (Gemini API or Vertex AI).
No other LLM or SaaS lineage product is needed.

```
mart.rpt_region_kpis.total_revenue
└─ mart.fct_daily_revenue.net_revenue
      ⤷ SUM(fct_daily_revenue.net_revenue)  [sql/mart/rpt_region_kpis.sql:1 | dag=reporting task=rpt_region_kpis]
   └─ staging.int_orders_usd.net_amount_usd
         ⤷ SUM(orders_enriched.net_amount_usd)  [sql/mart/fct_daily_revenue.sql:1 | dag=daily_sales task=fct_daily_revenue]
           via orders_enriched: o.net_amount_usd
      ├─ ref.fx_rates_daily.usd_rate  ◆ SOURCE
      │     ⤷ ROUND(so.net_amount_local * fx.usd_rate, 2)  [sql/intermediate/int_orders_usd.sql:2 | dag=daily_sales task=int_orders_usd]
      └─ staging.stg_orders.net_amount_local
            ⤷ ROUND(so.net_amount_local * fx.usd_rate, 2)  [sql/intermediate/int_orders_usd.sql:2 | dag=daily_sales task=int_orders_usd]
         ├─ raw.orders.gross_amount  ◆ SOURCE
         └─ raw.orders.discount_amount  ◆ SOURCE
```

## Why it's built this way

Pasting hundreds of SQL files into an LLM and asking it to trace lineage gives answers that sound
right but often aren't: it misses CTE hops, mixes up aliases, and makes up tables. So the work is
split in two:

| Layer | Who does it | What it does |
|---|---|---|
| **1. Parse (deterministic)** | `sqlglot` + Python `ast` | Renders Jinja/Airflow templates using your config files, parses every SQL statement (CTAS, INSERT…SELECT, MERGE, views), resolves CTEs/subqueries/aliases down to physical columns, and maps SQL files to DAG tasks. Writes `.lineage/graph.json`. |
| **2. Graph tools** | `codelineage.tools` | `trace_column_lineage` (upstream sources / downstream impact, N levels), `trace_table_lineage`, `describe_table`, `find_columns`, `read_sql_file`. |
| **3. Explain (LLM)** | Gemini | Works out which table/column the user means, calls the tools, and explains each level in business language with the filters, joins, DAG and schedule. |

Every hop in an answer points back to a file, line and Airflow task, so users can check it.

```
 SQL repo ──► builder ──► graph.json ──► tools ──┬──► MCP server ──► Gemini Code Assist (VS Code chat)
 (sql, dags,   (sqlglot,                         ├──► Gemini CLI
  config)       jinja, ast)                      └──► Gemini API/Vertex chat ──► CLI  /  web chat window
```

## Quick start (sample repo)

```bash
pip install -e ".[dev]"
python -m codelineage --repo examples/sample_repo build
python -m codelineage --repo examples/sample_repo trace rpt_region_kpis avg_order_value
python -m codelineage --repo examples/sample_repo trace raw.orders discount_amount --down   # impact
pytest
```

## Three ways for employees to ask questions

### A. Gemini Code Assist chat in VS Code (recommended)

Gemini Code Assist **agent mode** can call MCP servers configured in `.gemini/settings.json`
(project) or `~/.gemini/settings.json` (user). This is the same config format Gemini CLI uses.

1. `pip install` this package into the Python environment VS Code uses.
2. Copy `examples/sample_repo/.gemini/settings.json` and `GEMINI.md` into the root of your SQL repo.
   Set `CODELINEAGE_REPO` to an absolute path if VS Code isn't opened at the repo root.
3. Open the Gemini Code Assist chat, switch on **Agent** mode, and ask:
   *"Where does avg_order_value in rpt_region_kpis come from? Show all levels."*

Gemini calls `trace_column_lineage` and answers with a level-by-level table and a mermaid diagram.
If your admin has turned off agent mode or MCP, use B or C. They use the same tools.

### B. Terminal chat (Gemini API or Vertex AI)

```bash
# Vertex AI (usual inside a company)
export GOOGLE_GENAI_USE_VERTEXAI=true GOOGLE_CLOUD_PROJECT=my-proj GOOGLE_CLOUD_LOCATION=us-central1
gcloud auth application-default login
# or: export GOOGLE_API_KEY=...

export CODELINEAGE_MODEL=gemini-2.5-flash   # any Gemini model with function calling (pro for harder SQL)
python -m codelineage --repo /path/to/sql-repo chat
python -m codelineage --repo /path/to/sql-repo chat "what breaks if I drop raw.orders.discount_amount?"
```

### C. Browser chat window

```bash
python -m codelineage --repo /path/to/sql-repo web --port 8765   # http://127.0.0.1:8765
```

A single page with chat history and rendered mermaid diagrams. To share it, run it on an internal VM
or Cloud Run behind IAP. It binds to localhost by default and has no auth of its own.

## Pointing it at your repo

Add a `lineage.yaml` at the root of your SQL repo (see `examples/sample_repo/lineage.yaml`):

```yaml
dialect: bigquery            # snowflake, postgres, redshift, spark, databricks, tsql, oracle, ...
sql_paths: [sql]
dag_paths: [dags]
config_paths: [config]       # YAML/JSON values available to Jinja as params.x, var.value.x, x
template_searchpath: [sql]   # where DAG sql="x.sql" / {% include %} paths are resolved
exclude: ["sql/scratch/*"]
drop_catalog: true           # project.dataset.table == dataset.table
variables: { ds: "2024-01-01" }
```

Then run `codelineage build`. Read the warnings it prints: they list files it couldn't parse and DAG
SQL references it couldn't resolve. Fix those by adding variables or paths.

**Keeping it fresh:** run `codelineage build` in CI on merge to main (or nightly), and publish
`.lineage/graph.json` somewhere everyone can read it (commit it, or put it in GCS and set
`CODELINEAGE_GRAPH`). The MCP server and chat only read the graph, so answers come back fast.

## What it handles

- `CREATE [OR REPLACE] TABLE/VIEW … AS SELECT`, `INSERT INTO t (cols) SELECT …` (matched by
  position), `MERGE … WHEN MATCHED UPDATE / WHEN NOT MATCHED INSERT`
- CTEs, nested subqueries, aliases, `UNION`, window functions, `SELECT *` (expanded from tables
  built earlier in the repo)
- Jinja / Airflow templating (`{{ params.x }}`, `{{ var.value.x }}`, `{{ ds }}`, `{% include %}`)
- Airflow DAGs read statically, never imported: `sql=` / `query=` / `BigQueryInsertJobOperator`
  `configuration.query.query`, file references or inline SQL, DAG id, schedule, task dependencies
- Upstream ("where from") and downstream ("impact") traces, filters/joins per hop, and columns with
  no source (`COUNT(*)`, constants) flagged as such

**Not covered (yet):** SQL assembled at runtime in Python (f-strings), stored procedures and dynamic
`EXECUTE IMMEDIATE`, and transforms that happen outside SQL (Spark/pandas). These show up in the
build warnings or appear as tables with no upstream. Ideas for next steps: a `schema_files` option
fed from `INFORMATION_SCHEMA` for better `SELECT *` handling, dbt `manifest.json` ingestion, and
column descriptions from a data catalog.

## Layout

```
codelineage/
  config.py        lineage.yaml loader
  templating.py    Jinja/Airflow rendering with config values
  dag_parser.py    Airflow DAG → SQL task mapping (ast)
  sql_parser.py    sqlglot column-level lineage per statement
  builder.py       crawl repo → graph.json
  graph.py         graph storage, fuzzy lookup, multi-level trace, text/mermaid rendering
  tools.py         LLM tools + system prompt
  gemini_chat.py   Gemini (google-genai) chat with automatic function calling
  mcp_server.py    MCP server for Gemini Code Assist / Gemini CLI
  web.py           browser chat window
examples/sample_repo/   SQL + DAGs + config used by the tests
```

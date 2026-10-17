# CodeLineage architecture

## Big picture

```
            OFFLINE BUILD (CI / nightly, seconds–minutes)                    ONLINE (per question, ~ms + LLM)
┌───────────────────────────────────────────────────────────────┐     ┌──────────────────────────────────────┐
│ SQL repo                                                      │     │  graph.json (read-only, in memory)   │
│  sql/**.sql ─┐                                                │     │        │                             │
│  dags/**.py ─┼─► 1 Discover ─► 2 Render ─► 3 Split ─► 4 Parse │     │  tools.py  (6 functions)             │
│  config/*.yml┘    (dag_parser)  (Jinja)    (tokenizer)  (AST) │     │   ├─ MCP server ─► Gemini Code Assist│
│                                                   │           │     │   ├─ Gemini API/Vertex ─► CLI chat   │
│ INFORMATION_SCHEMA export ─► 5 Schema ────────────┤           │     │   └─ Gemini API/Vertex ─► web chat   │
│                                                   ▼           │     │                                      │
│                         6 Column lineage (sqlglot) ─► 7 Graph ┼────►│  Gemini: understands the question,   │
│                         8 Validate against schema   graph.json│     │  calls tools, explains each level    │
└───────────────────────────────────────────────────────────────┘     └──────────────────────────────────────┘
```

**The LLM never parses SQL.** Parsing is deterministic and done ahead of time; Gemini only
interprets the question and explains facts returned by tools. This is what makes answers on complex
SQL trustworthy and repeatable, and keeps token cost tiny (a trace is a few KB, not the whole repo).

## Build pipeline (`codelineage build`)

| Step | Module | What happens |
|---|---|---|
| 1 Discover | `dag_parser.py`, `builder.py` | DAG files are read with Python `ast` (never imported, so no Airflow install or side effects). Each operator with `sql=`/`query=`/`configuration.query.query` becomes a *SqlTask*: dag_id, task_id, schedule, params, task dependencies, and either a SQL file reference (resolved via `template_searchpath`, also matching absolute deploy paths like `/opt/airflow/sql/x.sql` by suffix) or inline SQL. All `*.sql` under `sql_paths` are collected too, DAG-referenced or not. |
| 2 Render | `templating.py` | Jinja renders `{{ params.x }}`, `{{ var.value.x }}`, `{{ ds }}`, `{% include %}`, using values from `config_paths` YAML/JSON + DAG `params` + `variables`. Unknown variables render as their own name instead of failing, so the SQL stays parseable. |
| 3 Split | `sql_parser._chunks` | The dialect's tokenizer splits the script at top-level `;` (semicolons inside strings/comments are ignored), keeping each statement's start line. One bad statement can't sink the file. |
| 4 Parse + recover | `sql_parser._parse_chunk` | Each statement is parsed by sqlglot into an AST. If it's not a plain statement (procedure header, `BEGIN`, `IF … THEN`, `LOOP`), the parser restarts at the first `INSERT/MERGE/UPDATE/CREATE` token inside it, which is how DML inside stored procedures and scripting blocks is recovered. Identifiers are lowercased so `ORDERS`/`Orders`/`orders` match. |
| 5 Schema | `schema.py` | The INFORMATION_SCHEMA export (authoritative) is merged with columns inferred from the SQL itself (a first parse pass learns what each built table outputs). |
| 6 Column lineage | `sql_parser._statement_lineage` | For `CREATE … AS SELECT`/`INSERT … SELECT`: sqlglot qualifies every column against the schema, expands `*` / `* EXCEPT`, then walks the scope tree per output column through CTEs, subqueries, UNIONs, UNNEST, window functions and scalar subqueries down to physical `table.column` leaves. The expression at each hop is kept (`steps`). `INSERT (cols)` maps by position. `MERGE`/`UPDATE … FROM` map each `SET col = expr` through the alias map. TEMP tables are renamed `<file>::<name>` so same-named temp tables in different files never connect. |
| 7 Graph | `builder.py` → `graph.json` | Stored, see below. |
| 8 Validate | `builder.py` | Warnings for: unparseable files, unresolved DAG SQL references, ambiguous unqualified columns, `SELECT *` on unknown tables, columns referenced in SQL but absent from INFORMATION_SCHEMA (stale SQL / typos), source tables missing from the export. |

## What is stored, and how

We don't store the code or embeddings of it. We store the **extracted lineage graph**, with
pointers (file + line) back into the repo. `read_sql_file` reads the actual code on demand when the
LLM needs to quote it.

`.lineage/graph.json`:

```jsonc
{
  "built_at": "...", "repo_root": "...", "dialect": "bigquery",
  "tables": {                       // one entry per table BUILT by the repo
    "mart.fct_daily_revenue": {
      "columns": ["net_revenue", "order_count", "order_date", "region"],
      "temp": false,
      "built_by": [{                // one per statement that writes it (can be several)
        "file": "sql/mart/fct_daily_revenue.sql", "line": 1, "statement_type": "CREATE TABLE",
        "source_tables": ["staging.int_orders_usd", "staging.stg_customers"],
        "filters": ["JOIN ON o.customer_id = c.customer_id"],
        "dag_id": "daily_sales", "task_id": "fct_daily_revenue", "schedule": "0 3 * * *",
        "upstream_tasks": ["int_orders_usd", "stg_customers"]
      }]
    }
  },
  "edges": [{                       // one per (target column ← source column) hop
    "target_table": "mart.fct_daily_revenue", "target_column": "net_revenue",
    "source_table": "staging.int_orders_usd", "source_column": "net_amount_usd",
    "transformation": "SUM(orders_enriched.net_amount_usd)",
    "via": ["orders_enriched"], "steps": ["orders_enriched: o.net_amount_usd"],
    "ambiguous": false,
    "file": "sql/mart/fct_daily_revenue.sql", "line": 1, "statement_type": "CREATE TABLE",
    "dag_id": "daily_sales", "task_id": "fct_daily_revenue"
  }],
  "schema": { "raw.orders": { "gross_amount": {"type": "NUMERIC", "description": "...", "ordinal": "5"} } },
  "warnings": ["..."]
}
```

It's an **edge list** (a property graph in its simplest form). On load, `graph.py` builds two
in-memory indexes (`target → edges` for upstream and `source → edges` for downstream), so a
multi-level trace is a recursive walk with cycle detection and a depth limit, taking milliseconds.
Tables that appear only as sources are the raw/external inputs (◆ SOURCE).

**Scaling.** JSON in memory is fine to roughly a few hundred thousand edges (≈ 5–10k SQL files).
Beyond that, or if you want SQL access to lineage, the same two record types map directly to tables:

| Option | When |
|---|---|
| JSON file (current) | Up to a few thousand SQL files; simplest; commit it or put it in GCS. |
| SQLite / DuckDB (`edges`, `tables`, `schema` tables, indexed on target/source) | Larger repos; incremental rebuilds keyed by file hash. |
| BigQuery dataset + recursive CTE | Share lineage with BI tools / Looker; join with job logs and data catalog. |

## Complex SQL: what's covered

| Construct | Status |
|---|---|
| Multi-statement scripts, `DECLARE`/`SET` variables | ✅ split per statement, line-accurate |
| TEMP tables used across statements | ✅ traced through, scoped per file |
| Stored procedures / `BEGIN…END` / `IF…THEN` / loops | ✅ DML inside is recovered |
| Nested CTEs, subqueries (FROM, scalar, correlated) | ✅ |
| UNION / UNION ALL, CASE, window functions, QUALIFY | ✅ |
| UNNEST / LATERAL / arrays | ✅ to the array column |
| `SELECT *`, `* EXCEPT(...)`, `t.*` | ✅ with INFORMATION_SCHEMA (or upstream table built in repo) |
| Unqualified columns in joins | ✅ with INFORMATION_SCHEMA; otherwise flagged ⚠ AMBIGUOUS, never dropped |
| `INSERT (cols) SELECT` positional, `MERGE`, `UPDATE … FROM` | ✅ |
| Jinja / Airflow templating, `{% include %}` | ✅ |
| Mixed-case identifiers | ✅ normalised |
| `EXECUTE IMMEDIATE` / SQL built in Python f-strings | ❌ not statically knowable; shows up as missing lineage |
| Non-SQL transforms (Spark, pandas) | ❌ out of scope |

Dialects come from sqlglot: BigQuery, Snowflake, Postgres, Redshift, Spark/Databricks, Hive, T-SQL,
Oracle, Teradata, Trino/Presto, MySQL, DuckDB, and others.

## Providing INFORMATION_SCHEMA

Export `INFORMATION_SCHEMA.COLUMNS` for the schemas the repo reads, as CSV/TSV/JSON/JSONL, and list
the file(s) or folder in `schema_files`. Column names are matched case-insensitively; required:
`table_name`, `column_name`; recommended: `table_schema`, `data_type`, `description`.

```sql
-- BigQuery (per region; COLUMN_FIELD_PATHS adds descriptions)
SELECT table_catalog, table_schema, table_name, column_name, ordinal_position, data_type, description
FROM `region-us`.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS JOIN `region-us`.INFORMATION_SCHEMA.COLUMNS
  USING (table_catalog, table_schema, table_name, column_name);

-- Snowflake / Postgres / Redshift / SQL Server
SELECT table_catalog, table_schema, table_name, column_name, ordinal_position, data_type, comment
FROM information_schema.columns;
```

Refresh it on the same schedule as the graph build.

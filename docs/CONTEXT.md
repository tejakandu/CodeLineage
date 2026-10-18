# Project context (handoff notes)

Notes for picking this work up in a later session. Company names, project IDs and data are
deliberately replaced with placeholders: this repo must never contain them.

## Goal

An internal data lineage tool. A user asks in a chat window "where does this column come from?"
and gets a multi-level lineage answer plus an interactive graph.

## Hard constraints

- **Tools allowed at work:** VS Code, Gemini Code Assist, Gemini models. No Claude at work.
- **GCP services allowed:** Gemini (Vertex AI), BigQuery, GCS only. No Cloud Run, Cloud Build,
  IAP, load balancing, Cloud Logging or build triggers. The web UI runs on localhost only.
- **Code flow is one-way:** code is written here and pushed to this personal repo, and the user
  pulls it onto the company laptop. Nothing is ever pushed from the company laptop. So:
  - never commit company config, project names, SQL or data;
  - all instructions must be pull-only;
  - screenshots should have company names blurred.
- **Two GCP projects:**
  - `<LLM_PROJECT>` runs Gemini (`GOOGLE_CLOUD_PROJECT`);
  - `<BQ_PROJECT>` holds the tables (`bigquery_project:` in `lineage.yaml`). BigQuery jobs must
    run there, or you get a 403 `bigquery.jobs.create`.
- **Input:** a Bitbucket SQL repo with BigQuery SQL, YAML/JSON config and Airflow DAGs. It is on
  Windows, with paths over 260 characters.

## Architecture (see `docs/ARCHITECTURE.md`)

| Module | Role |
|---|---|
| `codelineage/sql_parser.py` | sqlglot (BigQuery) lineage. Batch `lineage(None, ...)`, `trim_selects=False`, one prebuilt `ensure_schema`. MERGE/UPDATE assignments, traced through `USING (subquery)`. DDL collection. |
| `codelineage/dag_parser.py` | Static `ast` parse of Airflow DAGs: classic operators, TaskFlow `.override(task_id=...)`, module string constants, `os.path.join`/`open`, `>>` dependencies. |
| `codelineage/templating.py` | Jinja/Airflow rendering and `${var}` placeholders. |
| `codelineage/builder.py` | `build_graph`. Resolves DAG SQL paths (search path, tail match, basename match). Two passes: columns_only, then full. Schema priority: INFORMATION_SCHEMA, then repo DDL, then CTAS-inferred. BigQuery schema cache in `.lineage/information_schema.json` (`schema_cache_hours`, default 24; `build --refresh-schema` forces a fresh read). Only datasets/tables named in the SQL are fetched and kept. |
| `codelineage/schema.py` | INFORMATION_SCHEMA from files, or live from `COLUMN_FIELD_PATHS` (region scopes pass `location=`). |
| `codelineage/graph.py` | `LineageGraph`: trace, tree/mermaid rendering, column info. Tables include schema-only columns. |
| `codelineage/tools.py`, `gemini_chat.py` | Gemini via google-genai on Vertex AI with automatic function calling. Grounding rules in the prompt, an `ungrounded_names` check, and a retry with tools off when an answer has no text. Default model `gemini-3.8-flash` (`gemini-2.5-*` retires 2026-10-20). |
| `codelineage/mcp_server.py` | MCP tools for Gemini Code Assist agent mode (`.gemini/settings.json`). |
| `codelineage/web.py`, `static/explorer.html` | "Lineage Terminal": vanilla JS/SVG layered DAG explorer plus chat, on 127.0.0.1. `A` = all columns; columns without lineage are dimmed and explain why. |
| `codelineage/telemetry.py` | Logs prompts and answers to BigQuery (`bq-init`) and/or JSONL. |
| `codelineage/prod_lineage.py` | Optional: exports BigQuery's own recorded lineage (Data Lineage API, Knowledge Catalog) with `prod-lineage`; `build` merges it (`prod_lineage:`, `prod_dataset_suffix:`) and flags links no SQL explains. |
| `codelineage/gcs.py` | `publish` the graph to GCS; cached reads of `gs://` graphs. |
| `codelineage/fsutil.py` | UTF-8 everywhere, Windows `\\?\` long paths, POSIX paths in the graph. |

CLI commands: `build`, `trace`, `search`, `chat`, `web`, `explore`, `publish`, `bq-init`, `prod-lineage`.
Tests: `pytest`, all passing. They must also pass with
`python -X warn_default_encoding -W error::EncodingWarning -m pytest`.

## Lessons learned

- Windows: always specify the encoding explicitly, and handle long paths. Users may save the
  config under the wrong name or make YAML typos, so `ConfigError` points at the bad line.
- Performance: never rebuild the sqlglot schema per column, and avoid deep copies. On a 9,000+
  column repo the build went from 834 s to about 1.5 min.
- An empty sqlglot `MappingSchema` is truthy, so use `None` for "no schema".
- Gemini will invent DAGs and files unless it is grounded. Keep the grounding rules.
- Gate commits on pytest's own exit code, not on a piped `tail`.

## Status on the real repo (last report)

- Warnings went from 4,359 to 119, edges from about 25k to 39k, and 206 DAG tasks are linked.
- **Open:**
  - About 104 ambiguous-column groups. These should clear once the live INFORMATION_SCHEMA read
    works from `<BQ_PROJECT>`.
  - 9 unparseable files: two "Invalid expression" errors at line 8, column 19 of a pair of
    reference-data SQL files, and one `CREATE VIEW` metrics file. Waiting for the user to share
    the code shapes, with names changed.
  - 6 schema mismatches in one KPI table.
  - DAGs that reference SQL in other repos stay unresolved (expected).
- **Latest fixes, waiting for the user to confirm:**
  - BigQuery `DECLARE` script variables used in queries were reported as ambiguous columns. They
    are now treated as variables.
  - Unqualified join columns are narrowed to the candidate tables whose schema has them.
  - INFORMATION_SCHEMA is fetched only for datasets the SQL mentions, and only referenced tables
    are kept. A region-wide fetch was 65k tables and 347 s, and it slowed parsing.
  - The explorer centres on the focused column in tall tables and shows a "NO LINEAGE FOUND"
    notice on the canvas.
- **Earlier fixes:** MERGE `USING (subquery)` tracing; all schema columns shown; the 24 h schema
  cache.

## Working branch

`claude/data-lineage-llm-tool-xc73zo` (PR #1 was merged earlier; open a new PR only on request).

## Prod lineage (Knowledge Catalog)

The user found BigQuery's built-in lineage in prod and confirmed `search_links` works with their
account (6 upstream links for the claim table). Its limits: 30 days of history, no column-level
lineage for multi-statement scripts (most of the repo's SQL uses DECLARE), and no file, DAG or logic.
So it complements the parser rather than replacing it. The code exports table links (and column
pairs when the API returns `field`) and shows prod-only links dashed in the explorer. The QA graph
names differ from prod by a dataset suffix, which `prod_dataset_suffix` maps.


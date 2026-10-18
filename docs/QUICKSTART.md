# Quickstart on your laptop

You don't change any Python code. Everything is configuration in four places:

| What | Where | You set |
|---|---|---|
| How to read your SQL repo | `lineage.yaml` in the root of **your SQL repo** | dialect, folders, schema source |
| Google project, model, GCS, logging | environment variables (or a `.env`-style script) | project, location, bucket, table |
| Gemini Code Assist tools | `.gemini/settings.json` in your SQL repo (or `~/.gemini/settings.json`) | path to Python, env vars |
| Gemini prompt (optional) | `GEMINI.md` in your SQL repo | how answers are worded |

## 0. Get the latest code

```bash
cd CodeLineage
git fetch origin
git checkout claude/data-lineage-llm-tool-xc73zo     # until it is merged into main
git pull
```

## 1. Install (once)

Python 3.10 or newer. If your company uses an internal PyPI mirror, `pip` must point at it
(`pip config set global.index-url https://<your-mirror>/simple`).

**Windows (PowerShell)**
```powershell
cd C:\path\to\CodeLineage
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[google]"
```

**macOS / Linux**
```bash
cd ~/path/to/CodeLineage
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[google]"
```

Check it works on the sample repo (no Google access needed):
```bash
python -m codelineage --repo examples/sample_repo build
python -m codelineage --repo examples/sample_repo trace rpt_region_kpis total_revenue
pytest      # optional: pip install pytest first
```

## 2. Sign in to Google (once)

```bash
gcloud auth application-default login
```

Your account needs `roles/aiplatform.user` on the project (Gemini). For the other roles see
`docs/DEPLOYMENT.md`.

## 3. Create `lineage.yaml` in YOUR SQL repo

Copy `examples/sample_repo/lineage.yaml` to the root of your SQL repo and edit it:

```yaml
dialect: bigquery                 # bigquery | snowflake | postgres | redshift | spark | tsql | ...
sql_paths: [sql]                  # folders with .sql files (relative to the SQL repo root)
dag_paths: [dags]                 # Airflow DAG folders; [] if none
config_paths: [config]            # YAML/JSON used by Jinja ({{ params.x }}, {{ var.value.x }})
template_searchpath: [sql]        # where DAG sql="x.sql" / {% include %} paths are resolved
exclude: ["sql/scratch/*"]        # optional
drop_catalog: true                # treat project.dataset.table == dataset.table
schema_bigquery: [my-project.region-us]   # read INFORMATION_SCHEMA live (or project.dataset)
bigquery_project: my-data-project       # project that runs that query (needs BigQuery Job User there)
# schema_files: [metadata/columns.csv]    # ...or an exported INFORMATION_SCHEMA.COLUMNS file
variables:                        # values for template variables not in config files
  ds: "2024-01-01"
```

Mapping to your repo:
- `sql_paths` / `dag_paths` / `config_paths`: the folder names in your repo.
- `template_searchpath`: whatever your DAGs use as `template_searchpath`. Absolute deploy paths in
  DAGs (like `/opt/airflow/sql/x.sql`) are matched by their tail automatically.
- `variables`: anything Jinja needs that isn't in your config files (`ds`, `params.env`, ...).

Build and read the warnings. They tell you what to fix in `lineage.yaml`:
```bash
python -m codelineage --repo C:\path\to\sql-repo build          # Windows
python -m codelineage --repo ~/path/to/sql-repo build             # macOS / Linux
```

| Warning | Fix |
|---|---|
| `SQL file not found: x.sql` | add the folder to `template_searchpath` |
| `unqualified reference, source table ambiguous` | add `schema_bigquery` (or `schema_files`) |
| `could not determine output columns (SELECT * ...)` | same: the schema is missing that table |
| `no lineage extracted` | unsupported or dynamic SQL; check the file, wrong `dialect`? |
| `... not found in INFORMATION_SCHEMA` | stale SQL, or the schema scope misses that dataset |

Then try it:
```bash
python -m codelineage --repo <sql-repo> search revenue
python -m codelineage --repo <sql-repo> trace <table> <column>
python -m codelineage --repo <sql-repo> explore --out lineage.html   # open in a browser
```

## 4. Set environment variables

**Windows (PowerShell)**, for the current window (use `setx NAME value` to keep them):
```powershell
$env:GOOGLE_GENAI_USE_VERTEXAI = "true"
$env:GOOGLE_CLOUD_PROJECT      = "my-project"
$env:GOOGLE_CLOUD_LOCATION     = "global"
$env:CODELINEAGE_MODEL         = "gemini-3.8-flash"                          # default anyway
$env:CODELINEAGE_BQ_TABLE      = "my-project.codelineage.interactions"       # optional logging
```

**macOS / Linux**
```bash
export GOOGLE_GENAI_USE_VERTEXAI=true
export GOOGLE_CLOUD_PROJECT=my-project
export GOOGLE_CLOUD_LOCATION=global
export CODELINEAGE_MODEL=gemini-3.8-flash
export CODELINEAGE_BQ_TABLE=my-project.codelineage.interactions
```

| Variable | Meaning |
|---|---|
| `GOOGLE_GENAI_USE_VERTEXAI` | `true`: use Gemini on Vertex AI with your Google login |
| `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` | project with Vertex AI enabled; `global` or your region |
| `CODELINEAGE_MODEL` | Gemini model (see `docs/DEPLOYMENT.md`) |
| `CODELINEAGE_REPO` | path to your SQL repo (instead of `--repo`) |
| `CODELINEAGE_GRAPH` | graph to use: local path or `gs://bucket/prefix/graph.json` |
| `CODELINEAGE_BQ_TABLE` | BigQuery table for usage logs (create with `codelineage bq-init`) |
| `CODELINEAGE_LOG_FILE` | local JSONL log instead of / as well as BigQuery |
| `CODELINEAGE_EXPLORER_URL` | `http://127.0.0.1:8765`: Gemini answers link into the Lineage Terminal |
| `CODELINEAGE_USER` | override the user name in logs (default: your Google account) |

## 5. Run it

```bash
# Terminal chat with Gemini
python -m codelineage --repo <sql-repo> chat
python -m codelineage --repo <sql-repo> chat "where does net_revenue in fct_daily_revenue come from?"

# Lineage Terminal (graph + Gemini chat) on http://127.0.0.1:8765
python -m codelineage --repo <sql-repo> web
#   in VS Code: Ctrl/Cmd+Shift+P -> "Simple Browser: Show" -> http://127.0.0.1:8765
```

## 6. Gemini Code Assist in VS Code

Create `.gemini/settings.json` in your SQL repo (or `~/.gemini/settings.json` for all projects).
Use the **full path to the venv's Python**, because VS Code may not see your activated venv:

**Windows**
```json
{
  "mcpServers": {
    "codelineage": {
      "command": "C:\\path\\to\\CodeLineage\\.venv\\Scripts\\python.exe",
      "args": ["-m", "codelineage.mcp_server"],
      "env": {
        "CODELINEAGE_REPO": "C:\\path\\to\\sql-repo",
        "CODELINEAGE_EXPLORER_URL": "http://127.0.0.1:8765",
        "CODELINEAGE_BQ_TABLE": "my-project.codelineage.interactions"
      },
      "timeout": 60000
    }
  }
}
```

**macOS / Linux**: `"command": "/Users/me/CodeLineage/.venv/bin/python"` and
`"CODELINEAGE_REPO": "/Users/me/sql-repo"`.

Copy `GEMINI.md` from this repo into your SQL repo root. Reload VS Code, open Gemini Code Assist
chat, switch on **Agent** mode, and ask: *"Where does <column> in <table> come from? Show all levels."*

If the tools don't appear: your admin may have disabled agent mode or MCP; use step 5 instead.

## 7. Share with your team (optional, needs a GCS bucket)

```bash
python -m codelineage --repo <sql-repo> publish gs://my-lineage-bucket/prod --build
```

Teammates then don't need the SQL repo. They set
`CODELINEAGE_GRAPH=gs://my-lineage-bucket/prod/graph.json` (in their env or in the `env` block of
`.gemini/settings.json`) instead of `CODELINEAGE_REPO`. Run `publish` again after SQL changes are
merged.

## 8. Compare with what ran in prod (optional)

BigQuery records lineage for jobs that actually ran (last 30 days), including loads that live outside
your SQL repo. CodeLineage can pull those table links and show them next to the code-derived ones.
Needs `roles/datalineage.viewer` on the prod project.

```bash
pip install -e ".[google]"          # adds google-cloud-datacatalog-lineage
python -m codelineage --repo <sql-repo> prod-lineage --project <prod-project> --location <region>
#   default: starts from every table the graph builds; or pass --tables dataset.table ...
#   --depth 3 (hops upstream), --out gs://bucket/path.json to share, --quota-project <prod-project>
```

Then in `lineage.yaml`:
```yaml
prod_lineage: .lineage/prod_lineage.json   # or the gs:// path
prod_dataset_suffix: ""                    # e.g. _qa if your graph uses QA dataset names and prod doesn't
```
and `build` again. The build prints `Prod lineage: N table links ..., M not explained by the SQL`; each
unexplained link is a warning. In the Lineage Terminal, TABLE view draws prod-only links dashed cyan,
and the DETAIL of a table (or of a column with no lineage) lists what prod saw writing it.

## Troubleshooting

| Problem | Fix |
|---|---|
| `DefaultCredentialsError` | run `gcloud auth application-default login` |
| `403 aiplatform...` | you need `roles/aiplatform.user` on `GOOGLE_CLOUD_PROJECT` |
| `404 model not found` | model not in that location: try `GOOGLE_CLOUD_LOCATION=global` or `gemini-3.5-flash` |
| `pip` can't download | set your company's PyPI mirror (step 1) |
| SSL errors behind a proxy | `pip config set global.cert <company-ca.pem>`; for Google calls set `REQUESTS_CA_BUNDLE` to the same file |
| Code Assist doesn't list the tools | full path to `python.exe` in settings.json; agent mode on; reload VS Code |
| `prod-lineage`: `SERVICE_DISABLED` / quota project | add `--quota-project <prod-project>` |
| `prod-lineage`: `403` | you need `roles/datalineage.viewer` on the prod project |
| `prod-lineage`: 0 links | wrong `--location`, names differ from prod (`prod_dataset_suffix`), or no job in 30 days |
| Old graph after SQL changes | `build` again (or `publish --build` for the shared copy) |
| `git clone`: `Filename too long` / `checkout failed` | Windows path limit. In the clone: `git config core.longpaths true` then `git restore --source=HEAD :/`. CodeLineage reads long paths itself; add folders you don't need (e.g. `bigquery/ddl_archive/*`) to `exclude` |

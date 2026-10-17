# Setup with Gemini, BigQuery and Cloud Storage

CodeLineage needs only three Google Cloud services. Nothing is hosted: each employee runs the tool
on their own machine, in VS Code, signed in with their own Google account.

| Service | Used for |
|---|---|
| **Vertex AI (Gemini)** | The LLM in terminal chat and the Lineage Terminal's chat. Gemini Code Assist uses its own Gemini backend |
| **BigQuery** | Reading INFORMATION_SCHEMA at build time; the usage log table; the TRENDING panel |
| **Cloud Storage** | The shared, published lineage graph, Lineage Terminal page and SQL files |

There is no database server, no web hosting and no API keys.

```
 maintainer's machine                        GCS bucket                     every employee's VS Code
 ─────────────────────                       ──────────                     ───────────────────────
 SQL repo checkout                           gs://…/lineage/                Gemini Code Assist ─► MCP server ─┐
   codelineage build ──► BigQuery            ├ graph.json  ◄── download ─── Lineage Terminal (localhost) ──┤─► Vertex AI Gemini
   (INFORMATION_SCHEMA)                      ├ lineage.html     (cached)    terminal chat ─────────────────┘
   codelineage publish ─────────────────────►├ files/…                      usage logs ─────────────────────► BigQuery
                                             └ manifest.json
```

## Google sign-in

Everyone authenticates with their own Google account:

```bash
gcloud auth application-default login
```

Those credentials call Gemini, BigQuery and Cloud Storage, and the account's email is what the usage
log records as `user_id`. Access is controlled entirely by IAM on your project, dataset and bucket.
The Lineage Terminal only listens on `localhost` and refuses any other address, because it has no
sign-in of its own.

## IAM roles

| Who | Role | On | For |
|---|---|---|---|
| Employees (Google group) | `roles/aiplatform.user` | project | Gemini |
| Employees | `roles/storage.objectViewer` | lineage bucket | Reading the published graph |
| Employees (if logging) | `roles/bigquery.dataEditor` | log dataset | Writing usage rows |
| Employees (if logging) | `roles/bigquery.jobUser` | project | TRENDING panel query |
| Maintainers | `roles/storage.objectAdmin` | lineage bucket | `codelineage publish` |
| Maintainers | `roles/bigquery.metadataViewer` + `roles/bigquery.jobUser` | warehouse datasets / project | Reading INFORMATION_SCHEMA |

## One-time setup (maintainer)

```bash
pip install "codelineage[google] @ git+https://github.com/tejakandu/CodeLineage"
gcloud auth application-default login
gcloud storage buckets create gs://acme-lineage --location=EU --uniform-bucket-level-access
codelineage bq-init acme-prod.codelineage.interactions        # optional usage log table
```

In the SQL repo's `lineage.yaml`, read the schema straight from BigQuery:

```yaml
schema_bigquery: [acme-prod.region-eu]     # or acme-prod.raw, acme-prod.staging, ...
```

The build caches the result in `.lineage/information_schema.json`, so builds still work when
BigQuery can't be reached. If the schema is stale (a column added since), lineage is still resolved
and the build warns about it.

## Publishing (after SQL changes are merged)

```bash
git pull
codelineage --repo . publish gs://acme-lineage/prod --build
```

This rebuilds the graph and uploads `graph.json`, `lineage.html`, the referenced SQL files and a
`manifest.json` (time, git commit, publisher). `graph.json` is uploaded last, so readers never see a
graph whose files are missing. With no CI service available, a maintainer runs this after merges.
If your Git host has its own CI runner, the same command works there with a service account that
holds the maintainer roles.

## Every employee

```bash
pip install "codelineage[google] @ git+https://github.com/tejakandu/CodeLineage"
gcloud auth application-default login
```

Add to `~/.gemini/settings.json` (or the project's `.gemini/settings.json`) for Gemini Code Assist:

```json
{
  "mcpServers": {
    "codelineage": {
      "command": "python",
      "args": ["-m", "codelineage.mcp_server"],
      "env": {
        "CODELINEAGE_GRAPH": "gs://acme-lineage/prod/graph.json",
        "CODELINEAGE_BQ_TABLE": "acme-prod.codelineage.interactions",
        "CODELINEAGE_EXPLORER_URL": "http://127.0.0.1:8765",
        "GOOGLE_GENAI_USE_VERTEXAI": "true",
        "GOOGLE_CLOUD_PROJECT": "acme-prod",
        "GOOGLE_CLOUD_LOCATION": "global"
      }
    }
  }
}
```

No clone of the SQL repo is needed. The graph is downloaded once and re-downloaded only when a new
version is published, and SQL files are read from the bucket on demand. To open the Lineage
Terminal with Gemini chat:

```bash
export CODELINEAGE_GRAPH=gs://acme-lineage/prod/graph.json
codelineage web          # then in VS Code: "Simple Browser: Show" → http://127.0.0.1:8765
```

For a read-only view without installing anything, download
`gs://acme-lineage/prod/lineage.html` (Cloud console or `gcloud storage cp`) and open it in a
browser: it embeds the graph and the SQL.

## Which Gemini model

Set `CODELINEAGE_MODEL`; the default is **`gemini-3.8-flash`**.

| Model | Use it for | Lifecycle (Vertex AI) |
|---|---|---|
| `gemini-3.8-flash` (default) | All lineage chat. The work is tool calling plus explaining results, which Flash does well, fast and cheaply | Released Sep 2, 2026; available until at least Sep 2, 2027 |
| `gemini-3.5-flash` | Fallback if 3.8 Flash isn't enabled in your region or org yet | Available until at least May 19, 2027 |
| `gemini-4-argon` | Newest model (Oct 13, 2026). Worth an A/B test on hard questions once your eval set exists | Available until at least Oct 13, 2027 |
| `gemini-2.5-flash` / `gemini-2.5-pro` | Don't use | Retire **Oct 20, 2026** |

The model does not parse SQL (the parser does), so a bigger model mostly buys nicer explanations.
Check model availability for your `GOOGLE_CLOUD_LOCATION`; newer models are often served from
`global` first. In Gemini Code Assist, the model is chosen by Code Assist, not by this setting.

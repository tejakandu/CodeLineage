# Logging prompts and answers to BigQuery

## Setup

```bash
pip install -e ".[bigquery]"
codelineage bq-init my-proj.codelineage.interactions          # or run the DDL below
export CODELINEAGE_BQ_TABLE=my-proj.codelineage.interactions   # chat, web and MCP all pick this up
# optional: also/instead write JSON lines locally
export CODELINEAGE_LOG_FILE=.lineage/interactions.jsonl
```

The identity writing rows needs `roles/bigquery.dataEditor` on the dataset. That's the web
server's service account, or each user's ADC credentials for CLI/MCP. Writes are batched on a
background thread; if BigQuery is down the chat keeps working and a warning is logged.

## What gets captured, by surface

| Surface | Question | Answer | Tool calls | User |
|---|---|---|---|---|
| `web` chat window | ✅ | ✅ | ✅ | IAP email (`X-Goog-Authenticated-User-Email`) when behind IAP |
| `cli` chat | ✅ | ✅ | ✅ | `CODELINEAGE_USER` or OS user |
| `mcp` (Gemini Code Assist / Gemini CLI) | ❌ | ❌ | ✅ | `CODELINEAGE_USER` or OS user |

Through Gemini Code Assist, Google's client owns the conversation: the MCP server is only called for
tools, so it never sees the prompt or the final answer. You still learn what people look up (every
table/column traced, misses, latency). For the prompts themselves on that path, ask your Google
Cloud admin about Gemini for Google Cloud's prompt/response logging to Cloud Logging, and route it
to BigQuery with a log sink. Otherwise point users who need analysis at the web chat.

## Table

```sql
CREATE TABLE IF NOT EXISTS `my-proj.codelineage.interactions` (
  event_id         STRING    NOT NULL OPTIONS(description="Unique id of this row"),
  event_type       STRING    NOT NULL OPTIONS(description="interaction | tool_call | feedback"),
  ts               TIMESTAMP NOT NULL OPTIONS(description="When the event finished (UTC)"),
  interaction_id   STRING    OPTIONS(description="Question/answer turn; feedback rows point at it"),
  session_id       STRING,
  surface          STRING    OPTIONS(description="cli | web | mcp"),
  user_id          STRING,
  model            STRING,
  question         STRING,
  answer           STRING,
  tool_calls       ARRAY<STRUCT<name STRING, args STRING, result STRING, result_chars INT64,
                                found BOOL, error STRING, latency_ms INT64>>,
  traced_columns   ARRAY<STRING>,
  latency_ms       INT64,
  prompt_tokens    INT64,
  output_tokens    INT64,
  total_tokens     INT64,
  error            STRING,
  feedback_rating  INT64     OPTIONS(description="1 = helpful, -1 = not helpful"),
  feedback_comment STRING,
  graph_built_at   TIMESTAMP OPTIONS(description="Version of the lineage graph that answered"),
  app_version      STRING
)
PARTITION BY DATE(ts)
CLUSTER BY event_type, surface, user_id
OPTIONS (partition_expiration_days = 400, description = "CodeLineage questions, answers, tool calls and feedback");
```

Notes: token counts are from the final model call of a turn (the SDK reports usage per call).
`tool_calls.result` keeps the first 20k characters of what the model was shown, which is what you
need to tell a parser gap from a model mistake. Questions may contain sensitive text: restrict
dataset access and keep the partition expiry.

## Analysis queries

```sql
-- Usage: daily questions, users, failure rate
SELECT DATE(ts) day, surface, COUNT(*) questions, COUNT(DISTINCT user_id) users,
       COUNTIF(error IS NOT NULL) failed, APPROX_QUANTILES(latency_ms, 100)[OFFSET(50)] p50_ms,
       APPROX_QUANTILES(latency_ms, 100)[OFFSET(95)] p95_ms, SUM(total_tokens) tokens
FROM `my-proj.codelineage.interactions`
WHERE event_type = 'interaction' AND ts > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY 1, 2 ORDER BY 1 DESC;

-- Answer quality: thumbs-down with the question, answer and what the tools returned
SELECT i.ts, i.user_id, i.question, i.answer, f.feedback_comment, i.tool_calls
FROM `my-proj.codelineage.interactions` i
JOIN `my-proj.codelineage.interactions` f
  ON f.event_type = 'feedback' AND f.interaction_id = i.interaction_id AND f.feedback_rating < 0
WHERE i.event_type = 'interaction'
ORDER BY i.ts DESC;

-- Names people use that don't resolve: feed a glossary / decide whether semantic search is worth it
SELECT JSON_VALUE(c.args, '$.search') AS term, JSON_VALUE(c.args, '$.table') AS table_arg,
       JSON_VALUE(c.args, '$.column') AS column_arg, COUNT(*) misses
FROM `my-proj.codelineage.interactions`, UNNEST(tool_calls) c
WHERE c.found = FALSE
GROUP BY 1, 2, 3 ORDER BY misses DESC LIMIT 100;

-- Questions where the model never managed to trace anything
SELECT ts, user_id, question, answer
FROM `my-proj.codelineage.interactions`
WHERE event_type = 'interaction' AND ARRAY_LENGTH(traced_columns) = 0
ORDER BY ts DESC;

-- Most-asked-about columns (all surfaces, including Gemini Code Assist via MCP)
SELECT col, COUNT(*) lookups, COUNT(DISTINCT user_id) users
FROM `my-proj.codelineage.interactions`, UNNEST(traced_columns) col
GROUP BY col ORDER BY lookups DESC LIMIT 50;

-- Tool health
SELECT c.name, COUNT(*) calls, COUNTIF(NOT c.found) not_found, COUNTIF(c.error IS NOT NULL) errors,
       APPROX_QUANTILES(c.latency_ms, 100)[OFFSET(95)] p95_ms
FROM `my-proj.codelineage.interactions`, UNNEST(tool_calls) c
GROUP BY 1 ORDER BY calls DESC;
```

"""Logs questions, answers, tool calls and feedback for usage analysis.

Sinks (either or both, chosen by env vars):
  CODELINEAGE_BQ_TABLE   project.dataset.table   streaming inserts into BigQuery
  CODELINEAGE_LOG_FILE   path.jsonl              local JSON lines (handy for dev, or to batch-load)

Rows are written by a background thread so the chat never waits on BigQuery, and a logging failure
never breaks a chat. Create the table with `codelineage bq-init` (schema below).
"""
from __future__ import annotations

import atexit
import functools
import getpass
import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

MAX_TEXT = 200_000          # BigQuery streaming rows are limited to 10 MB; answers are far smaller
MAX_TOOL_RESULT = 20_000    # enough to see what the model was shown when debugging a bad answer

# (name, type, mode, description) - also rendered as DDL in docs/TELEMETRY.md
SCHEMA = [
    ("event_id", "STRING", "REQUIRED", "Unique id of this row"),
    ("event_type", "STRING", "REQUIRED", "interaction | tool_call | feedback"),
    ("ts", "TIMESTAMP", "REQUIRED", "When the event finished (UTC)"),
    ("interaction_id", "STRING", "NULLABLE", "Question/answer turn; feedback rows point at it"),
    ("session_id", "STRING", "NULLABLE", "Chat session (several turns)"),
    ("surface", "STRING", "NULLABLE", "cli | web | mcp"),
    ("user_id", "STRING", "NULLABLE", "IAP email on web; CODELINEAGE_USER or OS user elsewhere"),
    ("model", "STRING", "NULLABLE", "Gemini model id"),
    ("question", "STRING", "NULLABLE", "User prompt"),
    ("answer", "STRING", "NULLABLE", "Model output"),
    ("tool_calls", "RECORD", "REPEATED", "Tools the model called during the turn"),
    ("traced_columns", "STRING", "REPEATED", "table.column values passed to lineage tools"),
    ("latency_ms", "INT64", "NULLABLE", "End-to-end latency"),
    ("prompt_tokens", "INT64", "NULLABLE", "Prompt tokens (final model call)"),
    ("output_tokens", "INT64", "NULLABLE", "Output tokens (final model call)"),
    ("total_tokens", "INT64", "NULLABLE", "Total tokens (final model call)"),
    ("error", "STRING", "NULLABLE", "Exception text if the turn failed"),
    ("feedback_rating", "INT64", "NULLABLE", "1 = helpful, -1 = not helpful"),
    ("feedback_comment", "STRING", "NULLABLE", "Free-text feedback"),
    ("graph_built_at", "TIMESTAMP", "NULLABLE", "Version of the lineage graph that answered"),
    ("app_version", "STRING", "NULLABLE", "CodeLineage version"),
]
TOOL_CALL_FIELDS = [
    ("name", "STRING", "NULLABLE", "Tool name"),
    ("args", "STRING", "NULLABLE", "Arguments as JSON"),
    ("result", "STRING", "NULLABLE", f"Tool output (first {MAX_TOOL_RESULT} chars)"),
    ("result_chars", "INT64", "NULLABLE", "Full output length"),
    ("found", "BOOL", "NULLABLE", "False when the tool could not resolve the name / found nothing"),
    ("error", "STRING", "NULLABLE", "Exception text"),
    ("latency_ms", "INT64", "NULLABLE", "Tool latency"),
]

# Tool outputs that mean "nothing found" (see tools.py), used for the `found` flag.
_NOT_FOUND = ("No table matching", "No columns matching", "has no column like", "is ambiguous", "File not found")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_user() -> str | None:
    try:
        return os.environ.get("CODELINEAGE_USER") or getpass.getuser()
    except Exception:  # noqa: BLE001
        return None


def _clip(value, limit: int = MAX_TEXT):
    return value[:limit] if isinstance(value, str) else value


class InteractionLogger:
    def __init__(self, bq_table: str | None = None, log_file: str | None = None, bq_client=None):
        self.bq_table = bq_table
        self.log_file = Path(log_file) if log_file else None
        self._client = bq_client
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="codelineage-telemetry", daemon=True)
        self._thread.start()
        atexit.register(self.flush)

    @classmethod
    def from_env(cls) -> "InteractionLogger | None":
        table, path = os.environ.get("CODELINEAGE_BQ_TABLE"), os.environ.get("CODELINEAGE_LOG_FILE")
        return cls(table, path) if (table or path) else None

    @property
    def client(self):
        if self._client is None:
            from google.cloud import bigquery
            self._client = bigquery.Client()
        return self._client

    def log(self, event_type: str, **fields) -> str:
        row = {"event_id": uuid.uuid4().hex, "event_type": event_type, "ts": now(),
               "app_version": __version__, **fields}
        for key in ("question", "answer", "feedback_comment", "error"):
            if key in row:
                row[key] = _clip(row[key])
        self._queue.put(row)
        return row["event_id"]

    def flush(self, timeout: float = 10.0) -> None:
        """Waits (bounded) for queued rows to be written, e.g. before a one-shot CLI exits."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.05)

    def _run(self) -> None:
        while True:
            rows = [self._queue.get()]
            while len(rows) < 500:  # batch whatever else is waiting
                try:
                    rows.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self._write(rows)
            except Exception as e:  # noqa: BLE001 - never let telemetry break the app
                log.warning("telemetry write failed (%d rows): %s", len(rows), e)
            finally:
                for _ in rows:
                    self._queue.task_done()

    def _write(self, rows: list[dict]) -> None:
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a") as f:
                for r in rows:
                    f.write(json.dumps(r, default=str) + "\n")
        if self.bq_table:
            errors = self.client.insert_rows_json(self.bq_table, rows, row_ids=[r["event_id"] for r in rows])
            if errors:
                log.warning("BigQuery rejected rows: %s", errors[:3])


class ToolRecorder:
    """Wraps tool functions so each call is recorded (signature and docstring are preserved, which
    the Gemini SDK and MCP use to build the tool schema)."""

    def __init__(self):
        self.calls: list[dict] = []

    def reset(self) -> list[dict]:
        calls, self.calls = self.calls, []
        return calls

    def wrap(self, fn, on_call=None):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            start, result, error = time.monotonic(), None, None
            try:
                result = fn(*args, **kwargs)
                return result
            except Exception as e:  # noqa: BLE001
                error = f"{type(e).__name__}: {e}"
                raise
            finally:
                text = result if isinstance(result, str) else ("" if result is None else json.dumps(result, default=str))
                call = {
                    "name": fn.__name__,
                    "args": json.dumps(kwargs or dict(enumerate(args)), default=str),
                    "result": text[:MAX_TOOL_RESULT],
                    "result_chars": len(text),
                    "found": error is None and not any(m in text[:300] for m in _NOT_FOUND),
                    "error": error,
                    "latency_ms": int((time.monotonic() - start) * 1000),
                }
                if on_call:
                    on_call(call)   # streamed out; don't also accumulate (long-running MCP server)
                else:
                    self.calls.append(call)
        return wrapper


def traced_columns(calls: list[dict]) -> list[str]:
    out = []
    for c in calls:
        if c["name"] not in ("trace_column_lineage", "trace_table_lineage", "describe_table"):
            continue
        try:
            a = json.loads(c["args"])
        except ValueError:
            continue
        name = ".".join(str(a[k]) for k in ("table", "column") if a.get(k))
        if name and name not in out:
            out.append(name)
    return out


def bq_schema():
    from google.cloud import bigquery

    def field(name, typ, mode, desc, sub=None):
        return bigquery.SchemaField(name, typ, mode=mode, description=desc, fields=sub or ())

    sub = [field(*f) for f in TOOL_CALL_FIELDS]
    return [field(*f, sub if f[0] == "tool_calls" else None) for f in SCHEMA]


def create_table(table_id: str, partition_expiration_days: int | None = 400, client=None):
    """Creates the log table: partitioned by day on ts (cheap date-bounded queries, retention via
    partition expiry) and clustered by event_type, surface, user_id."""
    from google.cloud import bigquery

    client = client or bigquery.Client()
    table = bigquery.Table(table_id, schema=bq_schema())
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY, field="ts",
        expiration_ms=partition_expiration_days * 86_400_000 if partition_expiration_days else None)
    table.clustering_fields = ["event_type", "surface", "user_id"]
    table.description = "CodeLineage questions, answers, tool calls and feedback"
    return client.create_table(table, exists_ok=True)


def trending(graph, days: int = 14, limit: int = 15, log_file: str | None = None,
             bq_table: str | None = None, bq_client=None) -> dict:
    """Most-looked-up columns over the last `days`, with a daily series for sparklines.

    Reads the BigQuery log table if configured, else the JSONL log. Partial names users typed
    (e.g. ``fct_daily_revenue.net_revenue``) are resolved to full graph names."""
    from collections import Counter, defaultdict
    from datetime import date, timedelta

    log_file = log_file or os.environ.get("CODELINEAGE_LOG_FILE")
    bq_table = bq_table or os.environ.get("CODELINEAGE_BQ_TABLE")
    start = date.today() - timedelta(days=days - 1)
    hits: list[tuple[str, str, str | None]] = []  # (raw name, day, user)
    source = None
    if bq_table:
        try:
            from google.cloud import bigquery
            client = bq_client or bigquery.Client()
            sql = f"""SELECT col, CAST(DATE(ts) AS STRING) day, user_id
                      FROM `{bq_table}`, UNNEST(traced_columns) col
                      WHERE DATE(ts) >= @start"""
            job = client.query(sql, job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("start", "DATE", start)]))
            hits = [(r["col"], r["day"], r["user_id"]) for r in job.result()]
            source = "bigquery"
        except Exception as e:  # noqa: BLE001
            log.warning("trending from BigQuery failed: %s", e)
    if source is None and log_file and Path(log_file).exists():
        for line in Path(log_file).read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            day = str(row.get("ts", ""))[:10]
            if day >= start.isoformat():
                hits += [(c, day, row.get("user_id")) for c in row.get("traced_columns") or []]
        source = "log file"
    if source is None:
        return {"items": [], "note": "No usage log configured (CODELINEAGE_LOG_FILE or CODELINEAGE_BQ_TABLE)."}

    def resolve(name: str) -> str | None:
        table, _, column = name.rpartition(".")
        tables = graph.resolve_table(table or name)
        if not tables:
            return None
        if not column or not table:
            return tables[0]
        cols = graph.resolve_column(tables[0], column)
        return f"{tables[0]}.{cols[0]}" if cols else None

    days_list = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    counts, users, series = Counter(), defaultdict(set), defaultdict(Counter)
    cache: dict[str, str | None] = {}
    for raw, day, user in hits:
        full = cache.setdefault(raw, resolve(raw))
        if not full:
            continue
        counts[full] += 1
        series[full][day] += 1
        if user:
            users[full].add(user)
    items = [{"column": c, "lookups": n, "users": len(users[c]), "series": [series[c][d] for d in days_list]}
             for c, n in counts.most_common(limit)]
    return {"items": items, "window": f"last {days} days · {source}"}

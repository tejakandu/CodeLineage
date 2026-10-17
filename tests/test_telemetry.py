import json
from types import SimpleNamespace

from codelineage.builder import build_graph
from codelineage.config import LineageConfig
from codelineage.gemini_chat import LineageChat
from codelineage.telemetry import SCHEMA, TOOL_CALL_FIELDS, InteractionLogger, ToolRecorder
from codelineage.tools import LineageService

from test_lineage import SAMPLE


class FakeBigQuery:
    def __init__(self):
        self.rows = []

    def insert_rows_json(self, table, rows, row_ids=None):
        self.rows += rows
        return []


class FakeChat:
    """Mimics automatic function calling: calls tools, then answers."""

    def __init__(self, tools):
        self.tools = {t.__name__: t for t in tools}

    def send_message(self, question):
        if "boom" in question:
            raise RuntimeError("quota exceeded")
        self.tools["find_columns"](search="nothing_like_this")
        out = self.tools["trace_column_lineage"](table="fct_daily_revenue", column="net_revenue")
        return SimpleNamespace(text=f"Answer based on {len(out)} chars",
                               usage_metadata=SimpleNamespace(prompt_token_count=1200, candidates_token_count=300,
                                                              total_token_count=1500))


class FakeGenai:
    def __init__(self):
        self.chats = SimpleNamespace(create=lambda model, config: FakeChat(config.tools))


def make_chat(logger):
    service = LineageService(build_graph(LineageConfig.load(SAMPLE)), SAMPLE)
    return LineageChat(service, "gemini-test", logger=logger, surface="web", user_id="alice@corp.com",
                       session_id="s1", client=FakeGenai())


def test_interaction_and_feedback_rows_reach_bigquery(tmp_path):
    bq = FakeBigQuery()
    logger = InteractionLogger("p.d.t", str(tmp_path / "log.jsonl"), bq_client=bq)
    chat = make_chat(logger)
    answer = chat.ask("where does net_revenue come from?")
    chat.feedback(chat.last_interaction_id, -1, "missed the fx join")
    logger.flush()

    interaction, feedback = bq.rows
    assert interaction["event_type"] == "interaction" and interaction["answer"] == answer
    assert interaction["question"] == "where does net_revenue come from?"
    assert (interaction["user_id"], interaction["surface"], interaction["model"]) == ("alice@corp.com", "web", "gemini-test")
    assert [c["name"] for c in interaction["tool_calls"]] == ["find_columns", "trace_column_lineage"]
    assert [c["found"] for c in interaction["tool_calls"]] == [False, True]
    assert interaction["traced_columns"] == ["fct_daily_revenue.net_revenue"]
    assert interaction["total_tokens"] == 1500 and interaction["graph_built_at"]
    assert feedback["interaction_id"] == interaction["interaction_id"] and feedback["feedback_rating"] == -1
    assert len((tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_failed_turn_is_logged_with_error():
    bq = FakeBigQuery()
    logger = InteractionLogger("p.d.t", bq_client=bq)
    chat = make_chat(logger)
    try:
        chat.ask("boom")
    except RuntimeError:
        pass
    logger.flush()
    assert bq.rows[0]["error"] == "RuntimeError: quota exceeded" and bq.rows[0]["answer"] is None


def test_rows_only_use_schema_fields():
    """BigQuery streaming inserts reject unknown fields."""
    bq = FakeBigQuery()
    logger = InteractionLogger("p.d.t", bq_client=bq)
    chat = make_chat(logger)
    chat.ask("q")
    chat.feedback(chat.last_interaction_id, 1)
    logger.flush()
    top, sub = {f[0] for f in SCHEMA}, {f[0] for f in TOOL_CALL_FIELDS}
    for row in bq.rows:
        assert set(row) <= top, set(row) - top
        for call in row.get("tool_calls", []):
            assert set(call) <= sub
    json.dumps(bq.rows)


def test_bigquery_failure_never_breaks_chat():
    class Broken:
        def insert_rows_json(self, *a, **k):
            raise ConnectionError("bq down")

    logger = InteractionLogger("p.d.t", bq_client=Broken())
    assert make_chat(logger).ask("q").startswith("Answer")
    logger.flush()


def test_recorder_preserves_tool_signature():
    from google.genai import types

    def trace(table: str, column: str, direction: str = "upstream") -> str:
        """Doc."""
        return "x"

    decl = types.FunctionDeclaration.from_callable_with_api_option(callable=ToolRecorder().wrap(trace))
    assert decl.name == "trace" and decl.parameters.required == ["table", "column"]

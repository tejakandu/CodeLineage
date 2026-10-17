import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

import codelineage.gemini_chat as gemini_chat
from codelineage import web
from codelineage.builder import build_graph
from codelineage.config import LineageConfig
from codelineage.explorer import render_explorer
from codelineage.telemetry import ToolRecorder
from codelineage.tools import LineageService

from test_lineage import SAMPLE


class FakeChat:
    """Stands in for LineageChat: calls the real trace tool, returns markdown."""

    def __init__(self, service, model=None, **kw):
        self.recorder = ToolRecorder()
        self.trace = self.recorder.wrap(service.tool_functions()[3])
        self.last_interaction_id, self.last_tool_calls, self.feedbacks = None, [], []

    def ask(self, q):
        self.trace(table="rpt_region_kpis", column="total_revenue", direction="upstream")
        self.last_tool_calls, self.last_interaction_id = self.recorder.reset(), "i1"
        return "**Summary**: comes from raw.orders\n\n| Level | Column |\n|---|---|\n| 0 | total_revenue |"

    def feedback(self, *a):
        self.feedbacks.append(a)


@pytest.fixture(scope="module")
def service():
    return LineageService(build_graph(LineageConfig.load(SAMPLE)), SAMPLE)


@pytest.fixture(scope="module")
def server(service):
    orig = gemini_chat.LineageChat
    gemini_chat.LineageChat = FakeChat
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    threading.Thread(target=web.serve, args=(service, "127.0.0.1", port), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/")
            break
        except OSError:
            time.sleep(0.05)
    yield base
    gemini_chat.LineageChat = orig


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.read().decode()


def post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def test_static_explorer_embeds_graph_and_sql(service):
    html = render_explorer(service.graph, SAMPLE)
    assert "/*__BOOT__*/" not in html and "</script>" in html
    boot = json.loads(html.split("window.BOOT = ", 1)[1].split(";</script>", 1)[0])
    assert boot["mode"] == "static" and "mart.fct_daily_revenue" in boot["graph"]["tables"]
    assert "sql/mart/fct_daily_revenue.sql" in boot["files"]
    assert boot["graph"]["schema"]["raw.orders"]["gross_amount"]["type"] == "NUMERIC"


def test_server_page_files_and_guards(server):
    status, page = get(server + "/")
    assert status == 200 and '"mode": "server"' in page and '"chat": true' in page
    status, sql = get(server + "/api/file?path=sql/mart/fct_daily_revenue.sql")
    assert "fct_daily_revenue" in sql
    for bad in ("../../etc/passwd", "lineage.yaml", "config/env_prod.yaml"):
        with pytest.raises(urllib.error.HTTPError):
            get(server + "/api/file?path=" + bad)


def test_chat_returns_traced_column_for_the_graph(server):
    out = post(server + "/api/chat", {"session": "s", "question": "where does total_revenue come from"})
    assert out["traced"] == ["rpt_region_kpis.total_revenue"] and out["traced_direction"] == "upstream"
    assert out["interaction_id"] == "i1" and "Summary" in out["answer"]
    assert post(server + "/api/feedback", {"session": "s", "interaction_id": "i1", "rating": -1}) == {}


def test_trending_from_log_file(service, tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from codelineage.telemetry import trending
    now = datetime.now(timezone.utc).isoformat()
    log = tmp_path / "l.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in [
        {"ts": now, "user_id": "a", "traced_columns": ["fct_daily_revenue.net_revenue"]},
        {"ts": now, "user_id": "b", "traced_columns": ["mart.fct_daily_revenue.net_revenue", "raw.orders.status"]},
        {"ts": "2001-01-01T00:00:00", "user_id": "c", "traced_columns": ["raw.orders.status"]},
    ]))
    monkeypatch.delenv("CODELINEAGE_BQ_TABLE", raising=False)
    t = trending(service.graph, days=7, log_file=str(log))
    top = t["items"][0]
    assert top["column"] == "mart.fct_daily_revenue.net_revenue" and top["lookups"] == 2 and top["users"] == 2
    assert len(top["series"]) == 7 and top["series"][-1] == 2
    assert {i["column"]: i["lookups"] for i in t["items"]}["raw.orders.status"] == 1   # old row excluded

from types import SimpleNamespace

from codelineage.builder import build_graph
from codelineage.config import LineageConfig
from codelineage.gemini_chat import LineageChat
from codelineage.tools import LineageService, ungrounded_names

from test_lineage import SAMPLE


def service():
    return LineageService(build_graph(LineageConfig.load(SAMPLE)), SAMPLE)


def test_made_up_names_are_flagged_real_ones_pass():
    answer = (
        "| 0 | `mart.fct_daily_revenue.net_revenue` | `SUM(o.net_amount_usd)` | `sql/mart/fct_daily_revenue.sql` |\n"
        "| 1 | `staging.int_orders_usd.net_amount_usd` | via `o.net_amount_usd` | `dags/daily_sales_dag.py` |\n"
        "| 2 | `fivetran.cc_exposure.coverageid` | made up | `dags/claims_daily.py` |\n"
        "```mermaid\nflowchart LR\n  a[`nonexistent.inside_fence`]\n```"
    )
    assert ungrounded_names(answer, service()) == ["fivetran.cc_exposure.coverageid", "dags/claims_daily.py"]


class Chat:
    def __init__(self, replies):
        self.replies, self.configs = list(replies), []

    def send_message(self, message, config=None):
        self.configs.append(config)
        return self.replies.pop(0)


def reply(text=None, function_call=False):
    parts = ([SimpleNamespace(text=text)] if text else []) + \
            ([SimpleNamespace(text=None, function_call={"name": "x"})] if function_call else [])
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))], usage_metadata=None)


def make(replies):
    chat = Chat(replies)
    client = SimpleNamespace(chats=SimpleNamespace(create=lambda model, config: chat))
    return LineageChat(service(), "gemini-test", client=client), chat


def test_answer_without_text_asks_again_with_tools_off():
    lc, chat = make([reply(function_call=True), reply("Comes from `raw.orders.gross_amount`.")])
    assert lc.ask("where from?") == "Comes from `raw.orders.gross_amount`."
    mode = chat.configs[1].tool_config.function_calling_config.mode
    assert str(mode).endswith("NONE") and chat.configs[1].tools is None


def test_ungrounded_answer_gets_a_warning():
    lc, _ = make([reply("Built by `dags/invented_dag.py` from `raw.orders.gross_amount`.")])
    out = lc.ask("q")
    assert "Not found in the lineage graph" in out and "`dags/invented_dag.py`" in out
    assert "raw.orders.gross_amount`, " not in out.split("verify")[-1]

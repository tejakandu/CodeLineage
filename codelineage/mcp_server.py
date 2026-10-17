"""MCP server exposing the lineage tools to Gemini Code Assist (agent mode) and Gemini CLI.

Configure in .gemini/settings.json (project) or ~/.gemini/settings.json (user):
  {"mcpServers": {"codelineage": {"command": "python", "args": ["-m", "codelineage.mcp_server"],
                                  "env": {"CODELINEAGE_REPO": "/path/to/sql-repo"}}}}
"""
from __future__ import annotations

import uuid

from .telemetry import InteractionLogger, ToolRecorder, default_user, traced_columns
from .tools import SYSTEM_PROMPT, LineageService

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server


def create_server(service: LineageService | None = None, logger: InteractionLogger | None = None):
    """Gemini Code Assist owns the conversation, so this server never sees the user's prompt or the
    final answer, only the tool calls. Each call is logged as a `tool_call` row."""
    service = service or LineageService.from_env()
    logger = logger or InteractionLogger.from_env()
    server = _Server("codelineage", instructions=SYSTEM_PROMPT)
    user, session = default_user(), uuid.uuid4().hex
    built_at = service.graph.data.get("built_at")

    def on_call(call: dict) -> None:
        logger.log("tool_call", session_id=session, surface="mcp", user_id=user, tool_calls=[call],
                   traced_columns=traced_columns([call]), latency_ms=call["latency_ms"],
                   error=call["error"], graph_built_at=built_at)

    recorder = ToolRecorder()
    for fn in service.tool_functions():
        server.tool()(recorder.wrap(fn, on_call) if logger else fn)
    return server


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()

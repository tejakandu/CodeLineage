"""MCP server exposing the lineage tools to Gemini Code Assist (agent mode) and Gemini CLI.

Configure in .gemini/settings.json (project) or ~/.gemini/settings.json (user):
  {"mcpServers": {"codelineage": {"command": "python", "args": ["-m", "codelineage.mcp_server"],
                                  "env": {"CODELINEAGE_REPO": "/path/to/sql-repo"}}}}
"""
from __future__ import annotations

from .tools import SYSTEM_PROMPT, LineageService

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server


def create_server(service: LineageService | None = None):
    service = service or LineageService.from_env()
    server = _Server("codelineage", instructions=SYSTEM_PROMPT)
    for fn in service.tool_functions():
        server.tool()(fn)
    return server


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()

"""Serves the Lineage Terminal on the user's own machine, with Gemini chat, SQL and trending.

Runs on localhost only: open it in a browser, or inside VS Code with "Simple Browser: Show". Gemini,
BigQuery and GCS calls use the user's own Google credentials (Application Default Credentials).
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .explorer import referenced_files, render_explorer

from .telemetry import InteractionLogger, default_user, traced_columns, trending
from .tools import LineageService

def serve(service: LineageService, host: str = "127.0.0.1", port: int = 8765, model: str | None = None,
          logger: InteractionLogger | None = None) -> None:
    from .gemini_chat import LineageChat

    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("The Lineage Terminal has no sign-in of its own; serve it on localhost only.")
    logger = logger or InteractionLogger.from_env()
    user = default_user()
    sessions: dict[str, LineageChat] = {}
    lock = threading.Lock()
    page = render_explorer(service.graph, service.repo_root, mode="server", chat=True).encode()
    allowed_files = referenced_files(service.graph)
    trend_cache: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
            elif url.path == "/api/file":
                path = parse_qs(url.query).get("path", [""])[0]
                text = service.read_file(path) if path in allowed_files else None
                if text is None:
                    return self._send(404, b"not found", "text/plain")
                self._send(200, text.encode(), "text/plain; charset=utf-8")
            elif url.path == "/api/trending":
                if time.monotonic() - trend_cache.get("at", -1e9) > 600:  # 10 min cache
                    trend_cache.update(at=time.monotonic(), data=trending(service.graph))
                self._send(200, json.dumps(trend_cache["data"]).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):  # noqa: N802
            if self.path not in ("/api/chat", "/api/feedback"):
                return self._send(404, b"not found", "text/plain")
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                sid = str(req.get("session") or uuid.uuid4())
                with lock:
                    chat = sessions.get(sid) or sessions.setdefault(sid, LineageChat(
                        service, model, logger=logger, surface="web", user_id=user, session_id=sid))
                if self.path == "/api/feedback":
                    chat.feedback(str(req["interaction_id"]), 1 if int(req["rating"]) > 0 else -1, req.get("comment"))
                    return self._send(200, b"{}", "application/json")
                answer = chat.ask(str(req["question"]))
                calls = chat.last_tool_calls
                direction = next((json.loads(c["args"]).get("direction") for c in calls
                                  if c["name"] == "trace_column_lineage"), None)
                body = {"answer": answer, "interaction_id": chat.last_interaction_id,
                        "traced": traced_columns(calls), "traced_direction": direction}
                self._send(200, json.dumps(body).encode(), "application/json")
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")

    print(f"CodeLineage Lineage Terminal on http://{host}:{port}  (signed in to Google as {user})")
    ThreadingHTTPServer((host, port), Handler).serve_forever()

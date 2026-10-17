"""Minimal browser chat window (standard library only) for employees outside VS Code."""
from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .telemetry import InteractionLogger
from .tools import LineageService

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>CodeLineage</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1d2330}
 header{padding:12px 20px;background:#1d2330;color:#fff;font-weight:600}
 #log{max-width:960px;margin:0 auto;padding:16px 16px 120px}
 .msg{background:#fff;border-radius:8px;padding:12px 16px;margin:10px 0;box-shadow:0 1px 2px #0001;overflow-x:auto}
 .user{background:#e8f0fe}
 table{border-collapse:collapse}td,th{border:1px solid #ccd;padding:4px 8px;vertical-align:top}
 pre{background:#f0f2f5;padding:8px;overflow-x:auto}
 form{position:fixed;bottom:0;left:0;right:0;background:#fff;border-top:1px solid #dde;padding:12px;display:flex;gap:8px;justify-content:center}
 input{flex:1;max-width:860px;padding:10px;font-size:15px;border:1px solid #ccd;border-radius:6px}
 button{padding:10px 18px;border:0;border-radius:6px;background:#1a73e8;color:#fff;font-size:15px}
 .fb{margin-top:8px}.fb button{background:#eef1f5;color:#1d2330;padding:4px 10px;font-size:13px;margin-right:6px}
</style></head><body>
<header>CodeLineage — ask where your data comes from</header>
<div id="log"><div class="msg">Try: <i>Where does avg_order_value in rpt_region_kpis come from?</i></div></div>
<form id="f"><input id="q" autocomplete="off" placeholder="Where does column X in table Y come from?">
<button>Ask</button></form>
<script>
const session = crypto.randomUUID(); const log = document.getElementById('log');
if (window.mermaid) mermaid.initialize({startOnLoad:false});
function add(cls, html){const d=document.createElement('div');d.className='msg '+cls;d.innerHTML=html;log.appendChild(d);window.scrollTo(0,document.body.scrollHeight);return d;}
function esc(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
async function render(div, md){
  div.innerHTML = window.marked ? marked.parse(md) : '<pre>'+esc(md)+'</pre>';
  if (!window.mermaid) return;
  for (const code of div.querySelectorAll('code.language-mermaid')){
    try{const {svg}=await mermaid.render('m'+Math.random().toString(36).slice(2), code.textContent);
        code.parentElement.outerHTML='<div>'+svg+'</div>';}catch(e){}
  }
}
function addFeedback(div, id){
  const fb=document.createElement('div'); fb.className='fb';
  fb.innerHTML='<button data-r="1">👍 Helpful</button><button data-r="-1">👎 Wrong / not helpful</button>';
  fb.onclick = async e => { const r=e.target.dataset.r; if(!r) return;
    const comment = r<0 ? (prompt('What was wrong? (optional)')||null) : null;
    await fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session,interaction_id:id,rating:+r,comment})});
    fb.textContent='Thanks for the feedback.'; };
  div.appendChild(fb);
}
document.getElementById('f').onsubmit = async e => {
  e.preventDefault(); const q=document.getElementById('q'); const text=q.value.trim(); if(!text) return;
  q.value=''; add('user', esc(text)); const pending = add('', '<i>Tracing lineage…</i>');
  try{ const r = await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session,question:text})});
       const j = await r.json(); await render(pending, j.answer || ('Error: '+j.error));
       if (j.interaction_id) addFeedback(pending, j.interaction_id); }
  catch(err){ pending.textContent='Error: '+err; }
};
</script></body></html>"""


def _user(headers) -> str | None:
    # Identity-Aware Proxy puts the signed-in user here as "accounts.google.com:alice@corp.com".
    v = headers.get("X-Goog-Authenticated-User-Email")
    return v.split(":", 1)[-1] if v else None


def serve(service: LineageService, host: str = "127.0.0.1", port: int = 8765, model: str | None = None,
          logger: InteractionLogger | None = None) -> None:
    from .gemini_chat import LineageChat

    logger = logger or InteractionLogger.from_env()
    sessions: dict[str, LineageChat] = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
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
                        service, model, logger=logger, surface="web", user_id=_user(self.headers), session_id=sid))
                if self.path == "/api/feedback":
                    chat.feedback(str(req["interaction_id"]), 1 if int(req["rating"]) > 0 else -1, req.get("comment"))
                    return self._send(200, b"{}", "application/json")
                answer = chat.ask(str(req["question"]))
                body = {"answer": answer, "interaction_id": chat.last_interaction_id}
                self._send(200, json.dumps(body).encode(), "application/json")
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")

    print(f"CodeLineage web chat on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()

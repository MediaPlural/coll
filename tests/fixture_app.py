"""Deterministic fixture app for the behaviour lane's end-to-end test.

Serves a tiny SPA with the exact anatomy the lane claims to capture: a labelled
navigation sidebar, a main pane, a control that fires an XHR and re-renders, a
control that pushes an SPA route, a modal, and a localStorage write. No external
assets, no network, no auth — so the test asserts on behaviour, not on some
third party's uptime.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Fixture Workspace</title>
<style>
  body{margin:0;font:14px -apple-system,system-ui,sans-serif;display:flex;height:100vh}
  nav{width:220px;background:#1d1f23;color:#eee;padding:12px;box-sizing:border-box}
  nav button{display:block;width:100%;margin:6px 0;padding:8px;background:#2b2f36;
    color:#eee;border:0;border-radius:6px;text-align:left;font-size:14px;cursor:pointer}
  main{flex:1;padding:20px}
  ul{list-style:none;padding:0}
  li{padding:6px 0;border-bottom:1px solid #eee}
  dialog{border:1px solid #ccc;border-radius:8px;padding:16px}
  #toast{margin-top:10px;color:#2a7}
</style></head>
<body>
<nav aria-label="Workspace">
  <button id="inbox">Inbox</button>
  <button id="projects">Projects</button>
  <button id="search">Open search</button>
</nav>
<main>
  <h1>Workspace</h1>
  <button id="new">New item</button>
  <p id="toast"></p>
  <ul id="items"><li>First note</li><li>Second note</li></ul>
</main>
<dialog id="dlg"><form method="dialog">
  <h2>Search</h2>
  <input id="q" placeholder="Search workspace">
  <button id="run">Run search</button>
</form></dialog>
<script>
const store = localStorage;
function render(list){
  const ul = document.getElementById('items');
  ul.innerHTML = '';
  list.forEach(t => { const li = document.createElement('li'); li.textContent = t; ul.appendChild(li); });
}
document.getElementById('inbox').onclick = async () => {
  const r = await fetch('/api/inbox');
  const d = await r.json();
  render(d.items.map(i => (i.unread ? '• ' : '') + i.title));
  history.pushState({}, '', '/inbox');
  document.getElementById('toast').textContent = d.items.length + ' messages';
};
document.getElementById('projects').onclick = () => {
  history.pushState({}, '', '/projects');
  render(['Project Alpha', 'Project Beta']);
  document.getElementById('toast').textContent = 'projects';
};
document.getElementById('new').onclick = async () => {
  const r = await fetch('/api/items', {method:'POST', body:'{}'});
  const d = await r.json();
  const li = document.createElement('li'); li.textContent = d.title;
  document.getElementById('items').appendChild(li);
  const saved = JSON.parse(store.getItem('fixture.items') || '[]');
  saved.push(d.title); store.setItem('fixture.items', JSON.stringify(saved));
  document.getElementById('toast').textContent = 'created';
};
document.getElementById('search').onclick = () => { document.getElementById('dlg').showModal(); };
document.getElementById('run').onclick = async () => {
  const q = document.getElementById('q').value;
  const r = await fetch('/api/search?q=' + encodeURIComponent(q));
  const d = await r.json();
  document.getElementById('dlg').close();
  render(d.results.map(x => x.title));
};
</script></body></html>
"""

INBOX = {"items": [{"id": 1, "title": "Welcome back", "unread": True},
                   {"id": 2, "title": "Weekly digest", "unread": False}]}
SEARCH = {"query": "q", "results": [{"id": 7, "title": "Result one", "score": 0.92}]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):  # silence the test runner
        pass

    def _send(self, obj, status=200, ctype="application/json"):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/inbox", "/projects"):
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/inbox":
            self._send(INBOX)
            return
        if path == "/api/search":
            self._send(SEARCH)
            return
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        if urlparse(self.path).path == "/api/items":
            self._send({"id": 9, "title": "New item", "createdAt": "2026-01-01T00:00:00Z"}, 201)
            return
        self._send({"error": "not found"}, 404)


def serve(port: int = 0):
    """Start the fixture on `port` (0 = ephemeral). Returns (server, port)."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


if __name__ == "__main__":
    s, p = serve(8765)
    print("fixture on http://127.0.0.1:%d" % p)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        s.shutdown()

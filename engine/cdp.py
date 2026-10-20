"""Chrome DevTools Protocol client — the behaviour sensor.

Static analysis tells you what symbols exist. This tells you what actually
happens when a human touches the thing: which control fired, what the DOM did,
which requests left the process, what the app wrote to storage.

Two transports, one interface:
  - `http_targets(port)` / `http_version(port)` — the discovery endpoints
    (`/json/list`, `/json/version`) every Chromium debugger exposes.
  - `Session` — a WebSocket-bound CDP session with request/response correlation
    and an event queue.

Works identically against Chrome (web lane) and Electron (electron lane): both
are Chromium and both honour `--remote-debugging-port`. Nothing here needs a
GUI to drive; Electron targets just have to be launched with the flag (see
behavior.launch_electron) or attached when the app already exposes a port.
"""
from __future__ import annotations

import json
import time
import urllib.request

from .ws import WebSocket, WebSocketError


class CDPError(RuntimeError):
    """The debuggee answered a command with an `error` object."""


def _http_json(port: int, path: str, timeout: float = 5.0):
    url = "http://127.0.0.1:%d%s" % (int(port), path)
    req = urllib.request.Request(url, headers={"Host": "127.0.0.1:%d" % int(port)})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def http_version(port: int, timeout: float = 5.0) -> dict:
    """Browser/build identity of the debuggee (also our readiness probe)."""
    return _http_json(port, "/json/version", timeout)


def http_targets(port: int, timeout: float = 5.0) -> list:
    """All targets: pages, service workers, Electron renderers."""
    return _http_json(port, "/json/list", timeout)


def wait_for_port(port: int, deadline_s: float = 30.0) -> bool:
    """Poll until the debugger answers. Chromium takes a moment to bind."""
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            http_version(port, timeout=1.5)
            return True
        except Exception:  # noqa: BLE001 — not up yet, keep waiting
            time.sleep(0.25)
    return False


def pick_page(targets: list, url_prefix=None) -> dict:
    """Choose the page target a human would be looking at.

    Preference: exact-ish url prefix match, then any `page`, then the first
    target with a webSocketDebuggerUrl. Renderer helpers and service workers
    are never chosen — they have no UI to describe.
    """
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if url_prefix:
        for t in pages:
            if (t.get("url") or "").startswith(url_prefix):
                return t
    if pages:
        return pages[0]
    for t in targets:
        if t.get("webSocketDebuggerUrl"):
            return t
    return {}


class Session:
    """One CDP connection with id/response correlation and an event backlog."""

    def __init__(self, ws_url=None, timeout: float = 30.0, transport=None):
        # `transport` is an injection point for tests (any object with
        # send_text / recv_text / close mirrors the WebSocket contract).
        self.ws = transport or WebSocket(ws_url, timeout=timeout)
        self.timeout = timeout
        self._id = 0
        self.events = []
        self.commands = []      # every command sent — the reproduction log
        self.closed = False

    # -- core --------------------------------------------------------------
    def call(self, method: str, params=None, timeout=None):
        """Send a command, pump events until its reply lands, return `result`."""
        self._id += 1
        mid = self._id
        payload = {"id": mid, "method": method}
        if params:
            payload["params"] = params
        self.commands.append({"id": mid, "method": method,
                              "params": params or {}})
        self.ws.send_text(json.dumps(payload))
        deadline = time.time() + (timeout or self.timeout)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("CDP %s timed out after %.1fs" % (method, timeout or self.timeout))
            msg = self.ws.recv_text(timeout=min(remaining,
                                                _next_event_hint(self.events)))
            if msg is None:
                continue
            try:
                data = json.loads(msg)
            except ValueError:
                continue
            if data.get("id") == mid:
                if "error" in data:
                    raise CDPError("%s: %s" % (method, data["error"].get("message")))
                return data.get("result", {})
            if "method" in data:
                self.events.append(data)

    def pump(self, seconds: float = 0.0) -> list:
        """Read events for up to `seconds`; return them (not queued — the caller
        owns them, so nothing is processed twice)."""
        got = []
        end = time.time() + seconds
        while True:
            slice_t = max(0.0, min(0.25, end - time.time()))
            msg = self.ws.recv_text(timeout=slice_t)
            if msg is None:
                if time.time() >= end:
                    break
                continue
            try:
                data = json.loads(msg)
            except ValueError:
                continue
            if "method" in data:
                got.append(data)
            if time.time() >= end:
                break
        return got

    def drain(self) -> list:
        """Take and clear the event backlog."""
        out, self.events = self.events, []
        return out

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001 — closing a dead socket is fine
                pass

    # -- typed helpers -----------------------------------------------------
    def evaluate(self, expression: str, await_promise: bool = False,
                 timeout=None):
        """Runtime.evaluate returning the *value* (not the RemoteObject)."""
        r = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "userGesture": True,
        }, timeout=timeout)
        if r.get("exceptionDetails"):
            raise CDPError("JS: %s" % json.dumps(r["exceptionDetails"])[:400])
        return (r.get("result") or {}).get("value")


def _next_event_hint(backlog: list) -> float:
    """Short read slice while events are queued (they arrive in bursts)."""
    return 0.05 if not backlog else 0.02

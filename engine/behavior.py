"""Lane D — behavioural capture: watch the feature run, not just exist.

Input: a Chromium debuggee — either a web page we launch headless, or an
Electron app that exposes (or is launched with) `--remote-debugging-port`.

Output: a *behaviour record*, not an evidence dump:

  screens     what a human sees, snapshotted, with a PNG per snap
  states      clustered (url + visible-region signature) states S1..Sn
  transitions what control caused what change, with the DOM delta and the
              requests it fired
  controls    every visible interactive element, with the region that owns it
  requests    the API call graph (method, path, status, type, initiator)
  payloads    JSON response schemas — the observed data model
  storage     the app's own client-side keys and their shapes

Doctrine inherited from the static lanes: nothing is asserted that was not
observed, every artifact is byte-pinned by the EvidenceBank, and every
limitation is stated. What this lane adds is the second half of the sentence:
not "these classes exist" but "touching this control does that".

Two honest constraints:
  - Chrome is launched headless with a throwaway profile: the run cannot see
    whatever a logged-in session sees. Logged-out behaviour is real behaviour,
    and it is labelled as such.
  - Electron attach needs a debug port. We never relaunch a user's running app
    to get one; `launch_app=True` is opt-in and uses an isolated profile.
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time

from . import probes
from .cdp import CDPError, Session, http_version, http_targets, pick_page, wait_for_port

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]

VIEWPORT = {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False}

# Never auto-click these. A teardown must not buy, delete, publish or log out.
_DESTRUCTIVE = re.compile(
    r"\b(delete|remove|trash|destroy|sign ?out|log ?out|logout|unsubscribe|"
    r"buy|purchase|pay|checkout|upgrade|subscribe now|publish|deploy|merge|"
    r"discard|cancel (plan|subscription|account)|deactivate|erase|reset)\b", re.I)

_JSON_MIME = ("json", "javascript")  # application/json, +json, text/json
# Static payloads that happen to be JSON (lottie animations, manifests, i18n
# bundles) are assets, not API contract — capturing them pollutes the data model.
_ASSET_BODY_RE = re.compile(
    r"\.(json|js|mjs|map|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|mp4|webm)($|\?)",
    re.I)
_ASSET_BODY_PATHS = ("/img/", "/assets/", "/static/", "/fonts/", "/lottie/")
_ASSET_URL_RE = re.compile(
    r"\.(svg|png|jpe?g|gif|webp|ico|woff2?|ttf|eot|css|js|mjs|map|json|mp4|webm|"
    r"vtt|ts)($|\?)|/(s/i|s/desktop|img|assets|static|fonts|lottie|vi|vi_webp|"
    r"youtube/img)/", re.I)


def _looks_like_asset(url: str) -> bool:
    return bool(_ASSET_URL_RE.search(url or ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def chrome_binary() -> str:
    env = os.environ.get("COLL_CHROME")
    if env and os.path.exists(env):
        return env
    for c in CHROME_CANDIDATES:
        if os.path.exists(c):
            return c
    for name in ("google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return ""


# --------------------------------------------------------------------------
# launching
# --------------------------------------------------------------------------
def launch_chrome(url: str, port: int = None, headless: bool = True,
                  viewport: dict = None, profile_dir: str = None,
                  extra_args: list = None) -> dict:
    """Start Chrome (headless by default) with CDP on `port`. Returns a handle."""
    exe = chrome_binary()
    if not exe:
        return {"error": "no Chromium-family browser found (set COLL_CHROME)"}
    port = port or free_port()
    profile_dir = profile_dir or tempfile.mkdtemp(prefix="coll-profile-")
    vp = viewport or VIEWPORT
    argv = [
        exe,
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=%s" % profile_dir,
        "--window-size=%d,%d" % (vp["width"], vp["height"]),
        "--no-first-run", "--no-default-browser-check", "--disable-extensions",
        "--disable-background-networking", "--disable-sync",
        "--disable-features=Translate,MediaRouter,OptimizationHints",
        "--hide-scrollbars", "--mute-audio", "--disable-popup-blocking",
        "about:blank",
    ]
    if headless:
        argv.insert(1, "--headless=new")
    if extra_args:
        argv += list(extra_args)
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            start_new_session=True)
    ok = wait_for_port(port, 25.0)
    return {"proc": proc, "port": port, "argv": argv, "profile_dir": profile_dir,
            "ready": ok, "error": None if ok else "debugger port never opened"}


def electron_executable(app_path: str) -> str:
    """`/Applications/X.app` -> its Contents/MacOS executable."""
    app_path = app_path.rstrip("/")
    if app_path.endswith(".app"):
        macos = os.path.join(app_path, "Contents", "MacOS")
        if os.path.isdir(macos):
            for name in sorted(os.listdir(macos)):
                p = os.path.join(macos, name)
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    return p
    elif os.path.isdir(app_path):
        for name in sorted(os.listdir(app_path)):
            p = os.path.join(app_path, name)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return ""


def launch_electron(app_path: str, port: int = None, profile_dir: str = None,
                    extra_args: list = None) -> dict:
    """Launch an Electron app with CDP enabled, in an isolated profile.

    Opt-in only: this starts a second instance of the app on the user's desktop.
    A throwaway `--user-data-dir` keeps the user's real session untouched, and
    the caller kills it when the capture ends.
    """
    exe = electron_executable(app_path)
    if not exe:
        return {"error": "no executable found under %s" % app_path}
    port = port or free_port()
    profile_dir = profile_dir or tempfile.mkdtemp(prefix="coll-app-")
    argv = [exe, "--remote-debugging-port=%d" % port,
            "--user-data-dir=%s" % profile_dir,
            "--no-first-run", "--no-default-browser-check"]
    if extra_args:
        argv += list(extra_args)
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            start_new_session=True)
    ok = wait_for_port(port, 20.0)
    return {"proc": proc, "port": port, "argv": argv, "profile_dir": profile_dir,
            "ready": ok, "error": None if ok else
            "app did not expose a debugger port (Electron build may ignore the flag)"}


def attach_port(app_path: str = "", preferred=(9222, 9223, 9229)) -> dict:
    """Find an already-running debuggee: explicit env port, else common ports."""
    env = os.environ.get("COLL_CDP_PORT")
    ports = [int(env)] if env else list(preferred)
    for port in ports:
        try:
            info = http_version(port, timeout=1.0)
            return {"port": port, "ready": True,
                    "browser": info.get("Browser", ""),
                    "protocol": info.get("Protocol-Version", "")}
        except Exception:  # noqa: BLE001 — port closed is the normal case
            continue
    return {"port": None, "ready": False,
            "error": "no debugger port open. Start the app with "
                     "--remote-debugging-port=9222 (or pass --attach PORT)"}


# --------------------------------------------------------------------------
# harvesting CDP events
# --------------------------------------------------------------------------
class Harvester:
    """Accumulates network + console events into an API call graph."""

    def __init__(self, sess: Session, bank=None, body_budget: int = 12,
                 body_cap: int = 262144):
        self.sess = sess
        self.bank = bank
        self.pending = {}
        self.requests = []
        self.console = []
        self.errors = []
        self.body_budget = body_budget
        self.body_cap = body_cap
        self._bodies = []

    def process(self, events: list) -> list:
        """Fold events in; return the request records completed by this batch."""
        done = []
        for ev in events:
            m = ev.get("method", "")
            p = ev.get("params", {})
            if m == "Network.requestWillBeSent":
                self.pending[p["requestId"]] = {
                    "request_id": p["requestId"],
                    "method": p.get("request", {}).get("method"),
                    "url": p.get("request", {}).get("url"),
                    "type": p.get("type"),
                    "initiator": (p.get("initiator") or {}).get("type"),
                    "initiator_url": (((p.get("initiator") or {}).get("stack") or {})
                                      .get("callFrames") or [{}])[0].get("url", ""),
                    "started": time.time(),
                }
            elif m == "Network.responseReceived":
                rec = self.pending.get(p.get("requestId"))
                if rec is not None:
                    r = p.get("response", {})
                    rec["status"] = r.get("status")
                    rec["mime"] = (r.get("mimeType") or "").split(";")[0]
                    rec["size"] = r.get("encodedDataLength") or 0
                    rec["from_cache"] = bool(r.get("fromDiskCache") or r.get("fromServiceWorker"))
            elif m == "Network.loadingFinished":
                rec = self.pending.pop(p.get("requestId"), None)
                if rec:
                    rec["bytes"] = p.get("encodedDataLength", rec.get("size", 0))
                    rec["duration_ms"] = int((time.time() - rec.pop("started", time.time())) * 1000)
                    self.requests.append(rec)
                    done.append(rec)
                    self._maybe_body(rec)
            elif m == "Network.loadingFailed":
                rec = self.pending.pop(p.get("requestId"), None)
                if rec:
                    rec["error"] = p.get("errorText") or "failed"
                    rec["status"] = 0
                    self.requests.append(rec)
                    done.append(rec)
            elif m == "Runtime.consoleAPICalled":
                if len(self.console) < 60:
                    parts = []
                    for a in p.get("args", [])[:4]:
                        v = a.get("value", a.get("description", a.get("type")))
                        parts.append(str(v)[:200])
                    self.console.append({"type": p.get("type"),
                                         "text": " ".join(parts)[:400]})
            elif m == "Runtime.exceptionThrown":
                d = (p.get("exceptionDetails") or {})
                msg = (d.get("exception") or {}).get("description") or d.get("text") or ""
                if len(self.errors) < 30:
                    self.errors.append({"text": str(msg)[:400],
                                        "url": d.get("url", "")})
            elif m == "Log.entryAdded":
                e = p.get("entry") or {}
                if e.get("level") in ("error", "warning") and len(self.errors) < 30:
                    self.errors.append({"text": str(e.get("text"))[:400],
                                        "url": e.get("url", "")})
        return done

    def _maybe_body(self, rec: dict) -> None:
        """Pull a JSON body so the data model is observed, not invented."""
        if self.body_budget <= 0:
            return
        if rec.get("type") not in ("XHR", "Fetch"):
            return
        mime = rec.get("mime") or ""
        if not any(t in mime for t in _JSON_MIME):
            return
        url = rec.get("url") or ""
        if _ASSET_BODY_RE.search(url) or any(p in url for p in _ASSET_BODY_PATHS):
            return   # a static JSON asset, not an API payload
        try:
            body = self.sess.call("Network.getResponseBody",
                                  {"requestId": rec["request_id"]}, timeout=10)
        except (CDPError, TimeoutError, Exception):  # noqa: BLE001
            return
        raw = body.get("body") or ""
        if body.get("base64Encoded"):
            try:
                raw = base64.b64decode(raw).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                return
        if len(raw) > self.body_cap:
            raw = raw[:self.body_cap]
        self.body_budget -= 1
        rec["body_bytes"] = len(raw)
        sample = raw
        try:
            parsed = json.loads(raw)
            rec["json"] = True
            rec["schema"] = infer_schema(parsed)
            sample = json.dumps(parsed, indent=1)[: self.body_cap // 2]
            self._bodies.append({"url": rec["url"], "method": rec["method"],
                                 "schema": rec["schema"]})
        except ValueError:
            rec["json"] = False
        if self.bank is not None:
            f = self.bank.add("behavior", rec.get("url", ""),
                              "response body %s %s" % (rec.get("method"), _short(rec.get("url"))),
                              "cdp:Network.getResponseBody",
                              "Network.getResponseBody(requestId=%s)" % rec.get("request_id"),
                              raw.encode(), {"bytes": len(raw)},
                              confidence="observed",
                              limitation="single response sample; shape is not a contract")
            rec["body_artifact"] = f.artifact

    def bodies(self) -> list:
        return self._bodies

    def endpoints(self, limit: int = 120) -> list:
        """Group requests into endpoint templates (path params folded).

        Sorted so *data* endpoints survive the cap: on a media-heavy app the
        first 60 rows would otherwise be icons and font subsets.
        """
        seen = {}
        for r in self.requests:
            key = (r.get("method"), _path_template(r.get("url", "")))
            cur = seen.setdefault(key, {
                "method": r.get("method"), "path": key[1],
                "hits": 0, "statuses": set(), "host": _host(r.get("url", "")),
                "types": set(), "schema": None, "sample_url": r.get("url"),
            })
            cur["hits"] += 1
            if r.get("status"):
                cur["statuses"].add(int(r["status"]))
            if r.get("type"):
                cur["types"].add(r["type"])
            if r.get("schema") and not cur["schema"]:
                cur["schema"] = r["schema"]
        out = []
        for cur in seen.values():
            cur["statuses"] = sorted(cur["statuses"])
            cur["types"] = sorted(cur["types"])
            cur["is_asset"] = _looks_like_asset(cur.get("sample_url") or "")
            out.append(cur)
        out.sort(key=lambda e: (e["is_asset"], -e["hits"], e["path"]))
        return out[:limit]


def _short(url: str) -> str:
    p = _path_template(url or "")
    return p.replace("/", "-").strip("-")[:50] or "root"


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1) if m else ""


def _path_template(url: str) -> str:
    """Fold ids/uuids/numbers out of a path so calls group into endpoints."""
    m = re.match(r"https?://[^/]+([^?#]*)", url or "")
    path = m.group(1) if m else (url or "")
    path = re.sub(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "/{uuid}", path, flags=re.I)
    path = re.sub(r"/\d{2,}", "/{id}", path)
    path = re.sub(r"/[0-9a-f]{16,}", "/{hash}", path, flags=re.I)
    path = re.sub(r"/[A-Za-z0-9_-]{20,}(?=/|$)", "/{token}", path)
    return path or "/"


def infer_schema(obj, depth: int = 0) -> object:
    """JSON -> a compact shape tree. Arrays report their first element's shape."""
    if depth > 5:
        return "..."
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "bool"
    if isinstance(obj, (int, float)):
        return "number"
    if isinstance(obj, str):
        return "string"
    if isinstance(obj, list):
        return "array" if not obj else ["array", infer_schema(obj[0], depth + 1)]
    if isinstance(obj, dict):
        return {str(k): infer_schema(v, depth + 1) for k, v in list(obj.items())[:24]}
    return "unknown"


# --------------------------------------------------------------------------
# the session itself
# --------------------------------------------------------------------------
def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Capture:
    """A live behavioural capture. One instance == one debuggee."""

    def __init__(self, sess: Session, bank, target: str, mode: str,
                 port: int = None, settle_ms: int = 900, debuggee: dict = None):
        self.sess = sess
        self.bank = bank
        self.target = target
        self.mode = mode
        self.port = port
        self.settle_ms = settle_ms
        self.debuggee = debuggee or {}
        self.harv = Harvester(sess, bank)
        self.screens = []
        self.states = []
        self.transitions = []
        self.base_url = ""
        self.title = ""
        self.started = _now()

    # -- primitives --------------------------------------------------------
    def evaluate(self, expr: str, timeout: float = 20):
        return self.sess.evaluate(expr, timeout=timeout)

    def settle(self, quiet_s: float = 0.45, max_s: float = 6.0) -> list:
        """Read events until the page goes quiet or `max_s` lapses.

        Two sources, both drained: the backlog `Session.call` accumulated while
        it was pumping, and fresh bytes off the socket (`pump`). Draining only
        one of them silently loses most of the network graph.
        """
        got_all, last = [], time.time()
        end = time.time() + max_s
        while time.time() < end:
            batch = self.sess.drain()
            if not batch:
                batch = self.sess.pump(0.1)
            if batch:
                self.harv.process(batch)
                self._rearm_if_navigated(batch)
                got_all += batch
                last = time.time()
            elif time.time() - last >= quiet_s:
                break
        return got_all

    def _rearm_if_navigated(self, events: list) -> None:
        """A full navigation wipes the execution context — re-inject probes."""
        for ev in events:
            m = ev.get("method")
            if m == "Runtime.executionContextsCleared" or (
                    m == "Page.frameNavigated"
                    and not (ev.get("params", {}).get("frame", {}) or {}).get("parentId")):
                try:
                    self.evaluate(probes.ARM)
                except CDPError:
                    pass
                return

    def wait_ready(self, max_s: float = 20.0) -> str:
        end = time.time() + max_s
        state = ""
        while time.time() < end:
            try:
                state = self.evaluate("document.readyState") or ""
            except CDPError:
                state = ""
            if state == "complete":
                break
            time.sleep(0.2)
        self.settle()
        return state

    def screenshot(self, label: str) -> str:
        try:
            r = self.sess.call("Page.captureScreenshot",
                               {"format": "png", "captureBeyondViewport": False},
                               timeout=30)
        except (CDPError, TimeoutError):
            return ""
        raw = base64.b64decode(r.get("data", ""))
        if not raw:
            return ""
        f = self.bank.add("behavior", self.target, "screenshot %s" % label,
                          "cdp:Page.captureScreenshot",
                          "Page.captureScreenshot(format=png)",
                          raw, {"bytes": len(raw)}, confidence="observed",
                          limitation="viewport-only; offscreen content not shown",
                          ext="png")
        return f.artifact

    def snapshot(self, label: str = None, shot: bool = True) -> dict:
        snap = self.evaluate(probes.SNAPSHOT, timeout=30) or {}
        snap["region_keys"] = sorted(r["key"] for r in snap.get("regions", []))
        snap["sig"] = _state_sig(snap)
        self._note_controls(snap)
        if shot:
            snap["screenshot"] = self.screenshot(label or ("screen-%02d" % (len(self.screens) + 1)))
        sid = self._state_id(snap)
        snap["state_id"] = sid
        self.screens.append({
            "id": "K%d" % len(self.screens),
            "label": label or "",
            "state_id": sid,
            "url": snap.get("url"), "path": snap.get("path"),
            "title": snap.get("title"),
            "regions": [{"key": r["key"], "role": r["role"], "label": r["label"],
                         "bbox": r["bbox"]} for r in snap.get("regions", [])[:12]],
            "control_count": len(snap.get("controls", [])),
            "dialog_count": (snap.get("counts") or {}).get("dialogs", 0),
            "screenshot": snap.get("screenshot", ""),
        })
        return snap

    def _note_controls(self, snap: dict) -> None:
        """Accumulate the union of controls seen, keyed by (role, name, owner)."""
        seen = getattr(self, "_ctl_index", None)
        if seen is None:
            seen = self._ctl_index = {}
        for c in snap.get("controls", []):
            key = (c.get("role"), (c.get("name") or "").strip(), c.get("owner"))
            if not key[1] or key in seen:
                continue
            seen[key] = {"role": c.get("role"), "name": key[1],
                         "owner": c.get("owner"), "tag": c.get("tag"),
                         "bbox": c.get("bbox"), "href": c.get("href")}

    def _state_id(self, snap: dict) -> str:
        sig = snap.get("sig")
        for st in self.states:
            if st["sig"] == sig:
                return st["id"]
        sid = "S%d" % (len(self.states) + 1)
        self.states.append({
            "id": sid, "sig": sig,
            "path": snap.get("path"), "title": snap.get("title"),
            "regions": [r["key"] for r in snap.get("regions", [])[:12]],
            "headings": snap.get("headings", [])[:6],
            "dialogs": (snap.get("counts") or {}).get("dialogs", 0),
            "control_count": len(snap.get("controls", [])),
            "first_seen_step": len(self.transitions),
        })
        return sid

    def click_at(self, x: int, y: int) -> None:
        for t in ("mousePressed", "mouseReleased"):
            self.sess.call("Input.dispatchMouseEvent", {
                "type": t, "x": x, "y": y, "button": "left",
                "clickCount": 1, "buttons": 1 if t == "mousePressed" else 0,
            }, timeout=10)

    def send_key(self, key: str, code: str = None, modifiers: int = 0) -> None:
        for t in ("keyDown", "keyUp"):
            self.sess.call("Input.dispatchKeyEvent", {
                "type": t, "key": key, "code": code or key,
                "windowsVirtualKeyCode": _vk(key), "modifiers": modifiers,
            }, timeout=10)

    # -- bootstrap ---------------------------------------------------------
    def open(self, url=None) -> dict:
        for domain in ("Page", "Runtime", "Network", "Log", "DOM", "Accessibility"):
            try:
                self.sess.call(domain + ".enable", timeout=20)
            except (CDPError, TimeoutError):
                pass
        try:
            self.sess.call("Emulation.setDeviceMetricsOverride", VIEWPORT, timeout=10)
        except (CDPError, TimeoutError):
            pass
        if url:
            self.sess.call("Page.navigate", {"url": url}, timeout=30)
            self.wait_ready()
        self.settle()
        self.evaluate(probes.ARM)
        info = self.evaluate("({url: location.href, title: document.title, ua: navigator.userAgent})") or {}
        self.base_url = info.get("url") or url or ""
        self.title = info.get("title") or ""
        ax = self.accessibility()
        self.storage_shapes = self.evaluate(probes.STORAGE_SHAPES) or {}
        # everything loaded before the first interaction is the *page load*, not
        # the effect of a control — mark it seen so transitions only show causes
        self._seen_req = list(self.harv.requests)
        # the base screen is itself evidence: state S1 must exist even when the
        # run only observes (no steps, no exploration)
        self.base_snap = self.snapshot(label="initial")
        return {"url": self.base_url, "title": self.title,
                "user_agent": info.get("ua"), "a11y_nodes": ax.get("node_count"),
                "state": self.base_snap.get("state_id")}

    def accessibility(self) -> dict:
        """Accessibility-tree walk (proves the AX surface, not just the DOM)."""
        try:
            r = self.sess.call("Accessibility.getFullAXTree", {}, timeout=30)
        except (CDPError, TimeoutError):
            return {"node_count": 0, "roles": {}, "named": []}
        nodes = r.get("nodes", [])
        roles, named = {}, []
        for n in nodes:
            role = ((n.get("role") or {}).get("value") or "")
            name = ((n.get("name") or {}).get("value") or "")
            if area_ignored(n):
                continue
            roles[role] = roles.get(role, 0) + 1
            if name and len(named) < 80:
                named.append({"role": role, "name": name[:80],
                              "ignored": bool(n.get("ignored"))})
        if self.bank is not None:
            self.bank.add("behavior", self.target, "accessibility tree",
                          "cdp:Accessibility.getFullAXTree",
                          "Accessibility.getFullAXTree()",
                          json.dumps(nodes)[:400000].encode(),
                          {"nodes": len(nodes), "roles": dict(sorted(
                              roles.items(), key=lambda kv: -kv[1])[:10])},
                          confidence="observed",
                          limitation="AX tree is platform-normalized; not a 1:1 DOM map")
        return {"node_count": len(nodes),
                "roles": dict(sorted(roles.items(), key=lambda kv: -kv[1])[:25]),
                "named": named}

    # -- actions -----------------------------------------------------------
    def do_click_target(self, spec: dict, label: str = "") -> dict:
        before = self.snapshot(label="before-%s" % (label or "click"), shot=True)
        self.sess.drain()
        res = self.evaluate(probes.resolve_probe(spec), timeout=20) or {}
        if not res.get("found"):
            self.transitions.append({
                "n": len(self.transitions) + 1, "action": "click", "target": spec,
                "error": res.get("why", "not found"), "applied": False,
            })
            return {"ok": False, "reason": res.get("why", "not found")}
        if res.get("disabled"):
            return {"ok": False, "reason": "control disabled"}
        self.click_at(res["x"], res["y"])
        self.settle()
        return self._record(before, action="click", spec=spec,
                            element=res, label=label)

    def do_type(self, spec: dict, text: str, label: str = "") -> dict:
        before = self.snapshot(label="before-%s" % (label or "type"), shot=False)
        res = self.evaluate(probes.resolve_probe(spec), timeout=20) or {}
        if not res.get("found"):
            self.transitions.append({
                "n": len(self.transitions) + 1, "action": "type", "target": spec,
                "error": res.get("why", "not found"), "applied": False})
            return {"ok": False, "reason": res.get("why", "not found")}
        self.click_at(res["x"], res["y"])
        time.sleep(0.15)
        typed = self.evaluate(probes.type_probe(res["id"], text), timeout=20) or {}
        self.settle()
        out = self._record(before, action="type", spec=spec, element=res,
                           label=label, note="typed %r" % text[:40])
        out["typed"] = typed
        return out

    def do_key(self, key: str, modifiers: int = 0, label: str = "") -> dict:
        before = self.snapshot(label="before-%s" % (label or "key"), shot=False)
        self.send_key(key, modifiers=modifiers)
        self.settle()
        return self._record(before, action="key", spec={"key": key}, element={},
                            label=label)

    def _record(self, before: dict, action: str, spec: dict, element: dict,
                label: str = "", note: str = "") -> dict:
        delta = self.evaluate(probes.DELTA) or {}
        after = self.snapshot(label="after-%s" % (label or action), shot=True)
        fired = [r for r in self.harv.requests if r not in getattr(self, "_seen_req", [])]
        if not hasattr(self, "_seen_req"):
            self._seen_req = []
        self._seen_req.extend(fired)

        # the contract is about *data* calls; images/fonts/beacons are counted,
        # not listed, or the interaction table becomes unreadable on real apps
        data_calls = [r for r in fired if r.get("type") in ("XHR", "Fetch")
                      and not _looks_like_asset(r.get("url"))]
        other_calls = [r for r in fired if r not in data_calls]

        before_controls = {(c.get("role"), c.get("name")) for c in before.get("controls", [])}
        after_controls = {(c.get("role"), c.get("name")) for c in after.get("controls", [])}
        added = sorted(after_controls - before_controls)[:20]
        removed = sorted(before_controls - after_controls)[:20]
        before_regions = set(before.get("region_keys", []))
        after_regions = set(after.get("region_keys", []))

        rec = {
            "n": len(self.transitions) + 1,
            "action": action,
            "target": _describe(spec, element),
            "from_state": before.get("state_id"),
            "to_state": after.get("state_id"),
            "url_before": before.get("url"), "url_after": after.get("url"),
            "nav": (delta.get("nav") or [])[:8],
            "dom": {"mutations": delta.get("mutations", 0),
                    "nodes_added": delta.get("added", 0),
                    "nodes_removed": delta.get("removed", 0),
                    "attr_changes": delta.get("attrs", 0)},
            "regions_added": sorted(after_regions - before_regions)[:8],
            "regions_removed": sorted(before_regions - after_regions)[:8],
            "controls_added": ["%s %r" % (r, n) for r, n in added],
            "controls_removed": ["%s %r" % (r, n) for r, n in removed],
            "requests": [{"method": r.get("method"), "path": _path_template(r.get("url", "")),
                          "status": r.get("status"), "type": r.get("type"),
                          "ms": r.get("duration_ms")} for r in data_calls[:8]],
            "requests_other": len(other_calls),
            "fetch_hook": (delta.get("fetches") or [])[:10],
            "screenshot": after.get("screenshot", ""),
            "applied": True,
            "note": note,
        }
        rec["outcome"] = _outcome(rec)
        self.transitions.append(rec)
        if self.bank is not None:
            self.bank.add("behavior", self.target,
                          "transition %02d %s" % (rec["n"], rec["target"][:40]),
                          "cdp:Input+Runtime",
                          json.dumps({"action": action, "target": spec}, sort_keys=True),
                          json.dumps(rec, indent=1, default=str).encode(),
                          {"outcome": rec["outcome"]}, confidence="observed",
                          limitation="one observed run; timing/race states not exercised")
        return {"ok": True, "transition": rec}

    # -- exploration -------------------------------------------------------
    def explore(self, max_controls: int = 8, include_destructive: bool = False,
                prefer_regions=("navigation", "complementary", "menu", "tablist",
                                "toolbar", "tree", "listbox")) -> list:
        """Reload, then exercise one candidate control at a time.

        Deliberately dumb and safe: each probe starts from a fresh load, so the
        state graph we report is from the *base* state, not a pile-up of side
        effects. Destructive-looking controls are skipped unless asked for.
        """
        base = getattr(self, "base_snap", None) or self.snapshot(label="base", shot=True)
        cands = _candidates(base, max_controls, include_destructive, prefer_regions)
        results = []
        for cand in cands:
            self._reload_base()
            out = self.do_click_target({"role": cand["role"], "name": cand["name"]},
                                       label="explore-%s" % re.sub(r"\W+", "-", cand["name"])[:24])
            out["candidate"] = cand
            results.append(out)
            if out.get("ok") and out["transition"]["outcome"] in ("modal-opened",):
                self.send_key("Escape", "Escape")
                self.settle(0.2, 1.0)
        self._reload_base()
        return results

    def _reload_base(self) -> None:
        if not self.base_url:
            return
        try:
            self.sess.call("Page.navigate", {"url": self.base_url}, timeout=30)
            self.wait_ready()
        except (CDPError, TimeoutError):
            pass
        self.evaluate(probes.ARM)

    def run_steps(self, steps: list) -> list:
        """Execute a scripted flow — the way you capture a real UX path."""
        out = []
        for step in steps:
            act = (step.get("action") or "").lower()
            label = step.get("label") or act
            if act == "click":
                r = self.do_click_target(step.get("target") or _spec_from_step(step), label)
            elif act == "type":
                r = self.do_type(step.get("target") or _spec_from_step(step),
                                 step.get("text", ""), label)
            elif act == "key":
                r = self.do_key(step.get("key", "Escape"),
                                _mods(step.get("modifiers", [])), label)
            elif act == "wait":
                time.sleep(float(step.get("ms", 500)) / 1000.0)
                self.settle()
                r = {"ok": True, "transition": None}
            elif act == "screenshot":
                r = {"ok": True, "screenshot": self.screenshot(label)}
            elif act == "eval":
                r = {"ok": True, "value": self.evaluate(step.get("expression", "null"))}
            elif act == "scroll":
                self.evaluate("window.scrollBy(0, %d)" % int(step.get("px", 400)))
                self.settle()
                r = {"ok": True}
            else:
                r = {"ok": False, "reason": "unknown action %r" % act}
            r["step"] = step
            out.append(r)
        return out

    # -- report ------------------------------------------------------------
    def report(self, extra: dict = None) -> dict:
        # storage after the run: this is where the app's client-side model lands
        try:
            storage_end = self.evaluate(probes.STORAGE_SHAPES) or {}
        except CDPError:
            storage_end = {}
        rep = {
            "lane": "behavior",
            "mode": self.mode,
            "target": self.target,
            "captured_at": self.started,
            "debuggee": self.debuggee,
            "cdp_port": self.port,
            "viewport": VIEWPORT,
            "title": self.title,
            "base_url": self.base_url,
            "states": self.states,
            "transitions": self.transitions,
            "screens": self.screens,
            "requests": [{k: v for k, v in r.items() if k != "schema"}
                         for r in self.harv.requests[:400]],
            "endpoints": self.harv.endpoints(),
            "bodies": self.harv.bodies(),
            "controls": sorted(getattr(self, "_ctl_index", {}).values(),
                               key=lambda c: (c.get("owner") or "", c.get("role") or "",
                                              c.get("name") or "")),
            "console": self.harv.console,
            "errors": self.harv.errors,
            "storage": {"initial": getattr(self, "storage_shapes", {}),
                        "final": storage_end},
            "commands_sent": len(self.sess.commands),
            "limitations": [
                "one observed run: alternate paths, permission states and "
                "server-side branches are not exercised",
                "viewport is %dx%d @1x; responsive breakpoints not captured"
                % (VIEWPORT["width"], VIEWPORT["height"]),
                "logged-out / unauthenticated unless the attached profile was "
                "already signed in",
            ],
        }
        if extra:
            rep.update(extra)
        return rep


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _state_sig(snap: dict) -> str:
    """A state is (route, visible region anatomy, dialog presence).

    Region *geometry* is part of the fingerprint on purpose: a sidebar that
    collapses from 240px to 72px is a different state with the same DOM regions,
    and that is exactly the kind of transition a rebuild has to reproduce.
    """
    regions = []
    for r in snap.get("regions", []):
        b = r.get("bbox") or {}
        regions.append("%s:%s:%dx%d" % (r.get("role", ""), (r.get("label") or "")[:24],
                                        int(b.get("w", 0) / 25) * 25,
                                        int(b.get("h", 0) / 25) * 25))
    return "%s|%s|%s" % (snap.get("path") or "", "|".join(sorted(regions)[:10]),
                         "dialog" if (snap.get("counts") or {}).get("dialogs") else "")


def _outcome(rec: dict) -> str:
    if rec["url_before"] != rec["url_after"]:
        return "navigated"
    if rec["dom"]["mutations"] == 0 and not rec["requests"] and not rec["controls_added"]:
        return "no-visible-change"
    if any("dialog" in r for r in rec["regions_added"]):
        return "modal-opened"
    if rec["controls_added"] or rec["regions_added"]:
        return "panel-opened"
    if rec["controls_removed"] or rec["regions_removed"]:
        return "panel-closed"
    if rec["requests"]:
        return "data-updated"
    return "dom-updated"


def _describe(spec: dict, element: dict) -> str:
    if element:
        nm = element.get("name") or ""
        return "%s %r" % (element.get("role") or "element", nm) if nm else \
            (element.get("role") or "element")
    if spec.get("key"):
        return "key %r" % spec["key"]
    for k in ("text", "selector", "role", "id"):
        if spec.get(k):
            return "%s=%s" % (k, spec[k])
    return json.dumps(spec, sort_keys=True)[:80]


def _spec_from_step(step: dict) -> dict:
    for k in ("selector", "text", "role", "id"):
        if step.get(k):
            spec = {k: step[k]}
            if step.get("name"):
                spec["name"] = step["name"]
            return spec
    return {}


def _mods(names) -> int:
    table = {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "cmd": 4,
             "command": 4, "shift": 8}
    total = 0
    for n in names or []:
        total |= table.get(str(n).lower(), 0)
    return total


def _vk(key: str) -> int:
    table = {"Enter": 13, "Escape": 27, "Tab": 9, "Backspace": 8, " ": 32,
             "ArrowDown": 40, "ArrowUp": 38, "ArrowLeft": 37, "ArrowRight": 39}
    if key in table:
        return table[key]
    return ord(key.upper()) if len(key) == 1 else 0


def _candidates(snap: dict, limit: int, include_destructive: bool,
                prefer_regions) -> list:
    """Rank visible controls into a safe, non-redundant probe list."""
    out, seen = [], set()
    ctrls = snap.get("controls", [])
    scored = []
    for c in ctrls:
        name = (c.get("name") or "").strip()
        if not name or c.get("disabled"):
            continue
        if not include_destructive and _DESTRUCTIVE.search(name):
            continue
        if c.get("editable"):
            continue
        key = (c.get("role"), name.lower())
        if key in seen:
            continue
        seen.add(key)
        score = 0
        owner = (c.get("owner") or "")
        if any(r in owner for r in prefer_regions):
            score += 3
        if c.get("role") in ("tab", "menuitem", "treeitem", "switch", "menuitemcheckbox"):
            score += 2
        if c.get("role") in ("button", "link"):
            score += 1
        b = c.get("bbox") or {}
        if b.get("w", 0) < 240 and b.get("h", 0) < 120:
            score += 1        # small = a control, big = a container
        if b.get("y", 0) < 200:
            score += 1
        scored.append((score, {"role": c.get("role"), "name": name,
                               "owner": owner, "bbox": b}))
    scored.sort(key=lambda s: -s[0])
    for _, cand in scored[:limit]:
        out.append(cand)
    return out


def area_ignored(node: dict) -> bool:
    return bool(node.get("ignored"))


# --------------------------------------------------------------------------
# public entry points
# --------------------------------------------------------------------------
def capture_web(url: str, bank, headless: bool = True, port: int = None,
                settle_ms: int = 900, steps: list = None, max_controls: int = 8,
                explore: bool = True, keep_open: bool = False,
                extra_args: list = None, timeout: float = 60) -> dict:
    """Launch a browser at `url`, capture its behaviour, close it."""
    handle = launch_chrome(url, port=port, headless=headless, extra_args=extra_args)
    if handle.get("error"):
        return {"lane": "behavior", "mode": "launch-web", "target": url,
                "error": handle["error"], "captured_at": _now()}
    proc, port = handle["proc"], handle["port"]
    try:
        targets = http_targets(port)
        page = pick_page(targets, url)
        if not page.get("webSocketDebuggerUrl"):
            return {"lane": "behavior", "mode": "launch-web", "target": url,
                    "error": "no page target", "captured_at": _now()}
        sess = Session(page["webSocketDebuggerUrl"], timeout=timeout)
        info = http_version(port)
        cap = Capture(sess, bank, url, "launch-web", port, settle_ms,
                      {"browser": info.get("Browser"),
                       "protocol": info.get("Protocol-Version"),
                       "target_id": page.get("id"), "argv": handle["argv"][:12]})
        try:
            boot = cap.open(url if not page.get("url", "").startswith("http") else None)
            if steps:
                cap.run_steps(steps)
            elif explore:
                cap.explore(max_controls=max_controls)
            rep = cap.report({"boot": boot})
            rep["limitations"].append(
                "launched a fresh headless profile: no cookies, no extensions, "
                "no signed-in session")
            return rep
        finally:
            sess.close()
    finally:
        if not keep_open:
            _kill(proc)
            shutil.rmtree(handle.get("profile_dir", ""), ignore_errors=True)


def capture_attach(port: int, bank, target: str = "", steps: list = None,
                   max_controls: int = 8, explore: bool = True,
                   url_prefix: str = None, timeout: float = 60) -> dict:
    """Attach to an already-running debuggee (Electron app or your own Chrome)."""
    try:
        info = http_version(port)
    except Exception as e:  # noqa: BLE001
        return {"lane": "behavior", "mode": "attach", "target": target,
                "error": "no debugger on port %d (%s)" % (port, e),
                "captured_at": _now()}
    targets = http_targets(port)
    page = pick_page(targets, url_prefix or target)
    if not page.get("webSocketDebuggerUrl"):
        return {"lane": "behavior", "mode": "attach", "target": target,
                "error": "no page target on port %d" % port, "captured_at": _now()}
    sess = Session(page["webSocketDebuggerUrl"], timeout=timeout)
    cap = Capture(sess, bank, target or page.get("url", ""), "attach", port, 900,
                  {"browser": info.get("Browser"),
                   "protocol": info.get("Protocol-Version"),
                   "target_id": page.get("id"), "page_url": page.get("url")})
    try:
        # attaching to a web target: drive it to the URL you asked about.
        # Electron targets are left exactly where the user left them.
        boot = cap.open(target if (target or "").startswith(("http://", "https://")) else None)
        if steps:
            cap.run_steps(steps)
        elif explore:
            cap.explore(max_controls=max_controls)
        rep = cap.report({"boot": boot})
        rep["limitations"].append(
            "attached to a live session: state depends on what the user had "
            "already done; no reload was performed before the base snapshot")
        return rep
    finally:
        sess.close()


def capture_electron(app_path: str, bank, steps: list = None, port: int = None,
                     max_controls: int = 8, explore: bool = True,
                     launch_app: bool = False, settle_s: float = 8.0,
                     timeout: float = 60) -> dict:
    """Behavioural capture for an Electron app.

    Default: attach if a port is already open (non-intrusive). With
    `launch_app=True` the app is started in an isolated profile — opt-in,
    because it puts a window on the user's screen.
    """
    found = attach_port(app_path)
    if found.get("ready"):
        rep = capture_attach(found["port"], bank, app_path, steps=steps,
                             max_controls=max_controls, explore=explore,
                             timeout=timeout)
        rep["mode"] = "attach-electron"
        return rep
    if not launch_app:
        return {
            "lane": "behavior", "mode": "electron-unavailable", "target": app_path,
            "captured_at": _now(),
            "error": "no debug port open on this app",
            "fix": "start it with: %s --remote-debugging-port=9222 "
                   "(or rerun coll with --launch-app)" % (electron_executable(app_path) or app_path),
        }
    handle = launch_electron(app_path, port=port)
    if handle.get("error"):
        return {"lane": "behavior", "mode": "launch-app", "target": app_path,
                "error": handle["error"], "captured_at": _now()}
    proc, port = handle["proc"], handle["port"]
    try:
        time.sleep(settle_s)   # Electron needs a beat before its renderer appears
        rep = capture_attach(port, bank, app_path, steps=steps,
                             max_controls=max_controls, explore=explore,
                             timeout=timeout)
        rep["mode"] = "launch-app"
        rep["debuggee"] = dict(rep.get("debuggee") or {},
                               argv=handle["argv"][:10],
                               profile_dir=handle["profile_dir"])
        rep["limitations"].append(
            "app launched in an isolated profile: first-run/login state is not "
            "the user's real state")
        return rep
    finally:
        _kill(proc)
        shutil.rmtree(handle.get("profile_dir", ""), ignore_errors=True)


def _kill(proc) -> None:
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:  # noqa: BLE001
            pass
    for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        try:
            if stream:
                stream.close()
        except Exception:  # noqa: BLE001
            pass

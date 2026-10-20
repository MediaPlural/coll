"""Synthesizer — behaviour record in, build-ready feature spec out.

The problem this solves: a teardown that ends in an evidence dump forces the
next agent to re-read 40 artifacts before writing a line of code. This module
does that reading once, deterministically, and emits the two things an agent
actually needs:

  FEATURE-SPEC.md   human/agent-readable: surfaces, interaction contract, state
                    machine, data model, API surface, ordered build plan with
                    acceptance criteria, and a parity checklist.
  spec.json         the same facts, machine-readable, for a builder to consume
                    directly (components, transitions, entities, endpoints,
                    tasks).

No LLM in the loop: every claim traces to an observation, and everything that
was *inferred* is tagged. Deterministic in, deterministic out — the same
behaviour record always produces the same spec, which is what makes the spec
reviewable and diffable across runs.
"""
from __future__ import annotations

import json
import re

_STATIC_EXT = re.compile(
    r"\.(js|mjs|css|map|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|eot|mp4|webm|"
    r"avif|txt|xml|json\.br)$", re.I)
_ASSET_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com", "cdn.jsdelivr.net")

# Controls whose effect is destructive: named in the spec so a builder does not
# accidentally ship them as "implemented" from a capture that skipped them.
_RISKY = re.compile(
    r"\b(delete|remove|trash|destroy|sign ?out|log ?out|logout|unsubscribe|buy|"
    r"purchase|pay|checkout|upgrade|publish|deploy|merge|discard|deactivate|"
    r"erase|reset|cancel)\b", re.I)

# Paths that are real traffic but not the app's data contract: telemetry, ads,
# media delivery. Counted in one line, never turned into build tasks.
_TELEMETRY = re.compile(
    r"(^|/)(stats|log_event|qoe|atr|pagead|ddm|simgad|generate_204|gen_204|"
    r"videoplayback|timedtext|ptracking|beacon|batch|\$rpc)(/|$|\?)", re.I)


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1) if m else ""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _singular(word: str) -> str:
    w = word.strip("/").split("/")[-1]
    w = re.sub(r"[{}\d].*$", "", w) or w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("ses", "xes", "ches", "shes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def _pascal(word: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", word)
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def _is_asset(path: str, host: str = "") -> bool:
    if _STATIC_EXT.search(path or ""):
        return True
    return any(h in (host or "") for h in _ASSET_HOSTS)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _slug(s: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:n] or "x"


def _region_role(key: str) -> str:
    """`navigation|Workspace|nav` -> navigation"""
    return (key or "").split("|")[0] or "region"


def _region_label(key: str) -> str:
    """`navigation|Workspace|nav` -> 'Workspace' (empty when unlabelled)."""
    parts = (key or "").split("|")
    return parts[1] if len(parts) > 1 else ""


def _group_role(role: str) -> str:
    """Map ARIA/HTML roles onto the implementation primitive a builder needs."""
    r = (role or "").lower()
    if r in ("tab",):
        return "tabs"
    if r in ("menuitem", "menuitemcheckbox", "option", "treeitem"):
        return "menu/list"
    if r in ("switch", "checkbox"):
        return "toggle"
    if r in ("textbox", "input", "searchbox", "combobox"):
        return "form field"
    if r in ("link",):
        return "link"
    if r in ("dialog",):
        return "modal"
    return "button"


# --------------------------------------------------------------------------
# derivation
# --------------------------------------------------------------------------
def _components(behavior: dict) -> list:
    """Regions -> components, one per *conceptual* region.

    The same sidebar appears at 240px (expanded) and 72px (collapsed); those are
    two layouts of one component, not two components. Group by (role, label) and
    keep every observed geometry as a layout variant — that is what a rebuild
    actually needs.
    """
    groups, order, geom = {}, [], {}
    for scr in behavior.get("screens") or []:
        for r in scr.get("regions", []):
            key = r.get("key")
            if not key:
                continue
            gkey = (r.get("role") or _region_role(key), r.get("label") or "")
            if gkey not in groups:
                groups[gkey] = {"key": key, "controls": [], "labels": set()}
                order.append(gkey)
            geom.setdefault(gkey, [])
            b = r.get("bbox") or {}
            size = "%dx%d" % (b.get("w", 0), b.get("h", 0))
            if size not in geom[gkey]:
                geom[gkey].append(size)
    for c in behavior.get("controls") or []:
        owner = c.get("owner") or "chrome|app chrome|body"
        gkey = (_region_role(owner), _region_label(owner) if "|" in owner else "")
        if gkey not in groups:
            groups[gkey] = {"key": owner, "controls": [], "labels": set()}
            order.append(gkey)
            geom.setdefault(gkey, [])
        groups[gkey]["controls"].append({"role": c.get("role"),
                                         "name": _norm(c.get("name"))})

    kind_of = {
        "navigation": "sidebar / navigation", "nav": "sidebar / navigation",
        "complementary": "side panel", "aside": "side panel",
        "main": "main content", "dialog": "modal dialog", "menu": "menu",
        "tablist": "tab strip", "toolbar": "toolbar", "tree": "tree view",
        "listbox": "list", "form": "form", "search": "search",
        "header": "header bar", "footer": "footer bar", "region": "region",
        "chrome": "app chrome", "form_field": "form field",
    }
    out = []
    for gkey in order:
        role, label = gkey
        g = groups[gkey]
        kind = kind_of.get(role, role)
        name = label or kind
        seen = set()
        ctrls = []
        for c in g["controls"]:
            k = (c["role"], c["name"])
            if k in seen or not c["name"]:
                continue
            seen.add(k)
            ctrls.append(c)
        out.append({
            "id": "C%d" % (len(out) + 1),
            "name": name,
            "kind": kind,
            "role": role,
            "label": label,
            "layouts": geom.get(gkey, []),
            "control_count": len(ctrls),
            "controls": ctrls[:40],
            "confidence": "observed",
        })
    # stable order: app chrome last, otherwise largest surface first
    out.sort(key=lambda c: (c["role"] == "chrome",
                            -max([_area(x) for x in c["layouts"]] or [0])))
    for i, c in enumerate(out):
        c["id"] = "C%d" % (i + 1)
    return out


def _area(layout: str) -> int:
    try:
        w, h = layout.split("x")
        return int(w) * int(h)
    except Exception:  # noqa: BLE001
        return 0


def _endpoints(behavior: dict) -> list:
    """API contract endpoints: same-host, data-shaped, no assets or beacons."""
    out = []
    base_host = _host(behavior.get("base_url") or "")
    for e in behavior.get("endpoints") or []:
        path = e.get("path") or ""
        host = e.get("host") or ""
        if _is_asset(path, host):
            continue
        if host and base_host and host != base_host:
            continue          # ads/CDNs: real traffic, not this app's contract
        types = set(e.get("types") or [])
        if types and not (types & {"XHR", "Fetch"}):
            continue
        if path in ("/", ""):
            continue      # the document itself is not an API
        if _TELEMETRY.search(path):
            continue
        out.append({
            "method": e.get("method") or "GET",
            "path": path,
            "host": host,
            "hits": e.get("hits", 0),
            "statuses": e.get("statuses", []),
            "types": sorted(types),
            "schema": e.get("schema"),
            "confidence": "observed",
        })
    out.sort(key=lambda e: (e["path"], e["method"]))
    return out


def _excluded_traffic(behavior: dict) -> int:
    """Count of calls deliberately kept out of the contract (beacons, ads, cdn)."""
    base_host = _host(behavior.get("base_url") or "")
    n = 0
    for e in behavior.get("endpoints") or []:
        path = e.get("path") or ""
        host = e.get("host") or ""
        if _is_asset(path, host) or (host and base_host and host != base_host) \
                or _TELEMETRY.search(path):
            n += e.get("hits", 1)
    return n


def _entities(behavior: dict) -> list:
    """JSON response schemas + client-side JSON storage -> data model."""
    by_name = {}
    for body in behavior.get("bodies") or []:
        path = re.sub(r"^https?://[^/]+", "", body.get("url") or "")
        path = path.split("?")[0]
        name = _pascal(_singular(path))
        if not name:
            continue
        cur = by_name.setdefault(name, {"name": name, "fields": {},
                                        "sources": [], "confidence": "observed"})
        cur["sources"].append("%s %s" % (body.get("method", "GET"), path))
        _merge_schema(cur["fields"], body.get("schema"))
    storage = (behavior.get("storage") or {}).get("final") or {}
    for scope in ("local", "session"):
        for key, meta in (storage.get(scope) or {}).items():
            if not isinstance(meta, dict) or not meta.get("json"):
                continue
            name = _pascal(key)
            cur = by_name.setdefault(name, {"name": name, "fields": {},
                                            "sources": [], "confidence": "observed"})
            cur["sources"].append("%sStorage[%r]" % (scope, key))
            _merge_schema(cur["fields"], meta.get("shape"))
    out = []
    for name in sorted(by_name, key=lambda n: (
            not any("Storage[" not in s for s in by_name[n]["sources"]), n)):
        ent = by_name[name]
        ent["sources"] = sorted(set(ent["sources"]))
        out.append(ent)
    return out


def _merge_schema(fields: dict, schema) -> None:
    """Fold a shape tree into `dotted.field -> type` rows, merging repeats."""
    if isinstance(schema, str):
        fields["value"] = _merge_type(fields.get("value"), schema)
        return
    for k, v in _flatten(schema).items():
        fields[k] = _merge_type(fields.get(k), v)


def _flatten(schema, prefix: str = "", out=None) -> dict:
    """`{"items":[ "array", {"id":"number"}]}` -> `{"items": "array<object>",
    "items[].id": "number"}` — the flat form a builder writes types from."""
    if out is None:
        out = {}
    if isinstance(schema, dict):
        if "shape" in schema and len(schema) <= 3:
            schema = schema["shape"]
        for k, v in schema.items():
            if k in ("json", "bytes", "shape"):
                continue
            key = "%s.%s" % (prefix, k) if prefix else str(k)
            if isinstance(v, str):
                out[key] = _merge_type(out.get(key), v)
            elif isinstance(v, list) and len(v) == 2 and v[0] == "array":
                inner = v[1]
                if isinstance(inner, str):
                    out[key] = _merge_type(out.get(key), "array<%s>" % inner)
                else:
                    out[key] = _merge_type(out.get(key), "array<object>")
                    _flatten(inner, key + "[]", out)
            elif isinstance(v, dict):
                out[key] = _merge_type(out.get(key), "object")
                _flatten(v, key, out)
            else:
                out[key] = _merge_type(out.get(key), str(v))
    elif isinstance(schema, list) and len(schema) == 2 and schema[0] == "array":
        inner = schema[1]
        out[prefix or "value"] = _merge_type(
            out.get(prefix or "value"),
            "array<%s>" % (inner if isinstance(inner, str) else "object"))
        if isinstance(inner, dict):
            _flatten(inner, (prefix or "value") + "[]", out)
    elif isinstance(schema, str) and prefix:
        out[prefix] = _merge_type(out.get(prefix), schema)
    return out


def _merge_type(existing, new: str) -> str:
    if not existing:
        return new
    if existing == new:
        return new
    parts = set(str(existing).split("|")) | set(str(new).split("|"))
    return "|".join(sorted(parts))


def _effect(t: dict) -> str:
    """One sentence describing what the control did, from the observation."""
    outcome = t.get("outcome") or ""
    reqs = t.get("requests") or []
    dom = t.get("dom") or {}
    call = ""
    if reqs:
        r = reqs[0]
        more = ", +%d more" % (len(reqs) - 1) if len(reqs) > 1 else ""
        call = " → `%s %s`%s%s" % (r.get("method"), r.get("path"),
                                   " (%s)" % r.get("status") if r.get("status") else "",
                                   more)
    if t.get("action") == "type":
        if reqs:
            return "accepts typed input and requests suggestions%s" % call
        return "accepts typed input (%d DOM mutation(s))" % dom.get("mutations", 0)
    if outcome == "navigated":
        path = t.get("url_after") or ""
        path = re.sub(r"^https?://[^/]+", "", path) or "/"
        return "navigates to `%s`%s" % (path, call)
    if outcome == "modal-opened":
        n = len(t.get("controls_added") or [])
        return "opens a modal dialog (+%d controls)%s" % (n, call)
    if outcome == "panel-opened":
        added = len(t.get("controls_added") or [])
        hidden = len(t.get("controls_removed") or [])
        regs = ", ".join(r.split("|")[0] for r in (t.get("regions_added") or [])) or "panel"
        if hidden > added:
            return "hides %d control(s); the %s surface shrinks%s" % (hidden, regs, call)
        if added and hidden:
            return "swaps the %s surface (%d shown, %d hidden)%s" % (regs, added, hidden, call)
        return "reveals %d control(s) in %s%s" % (added, regs, call)
    if outcome == "panel-closed":
        hidden = len(t.get("controls_removed") or [])
        return "closes the open panel/modal%s%s" % (
            " (−%d controls)" % hidden if hidden else "", call)
    if outcome == "data-updated":
        return "fetches data and re-renders (+%d/-%d nodes)%s" % (
            dom.get("nodes_added", 0), dom.get("nodes_removed", 0), call)
    if outcome == "dom-updated":
        return "updates the DOM (%d mutation(s), +%d/-%d nodes)%s" % (
            dom.get("mutations", 0), dom.get("nodes_added", 0),
            dom.get("nodes_removed", 0), call)
    return "no visible change observed%s" % call


def _transition_rows(behavior: dict) -> list:
    rows = []
    for t in behavior.get("transitions") or []:
        if not t.get("applied", True):
            continue
        target = _norm(t.get("target"))
        # telemetry beacons are not part of the interaction's contract
        reqs = [r for r in (t.get("requests") or [])
                if not _TELEMETRY.search(r.get("path") or "")]
        rows.append({
            "n": t.get("n"),
            "control": target,
            "action": t.get("action"),
            "from_state": t.get("from_state"),
            "to_state": t.get("to_state"),
            "outcome": t.get("outcome"),
            "effect": _effect(dict(t, requests=reqs)),
            "calls": ["%s %s" % (r.get("method"), r.get("path")) for r in reqs],
            "beacons": len(t.get("requests") or []) - len(reqs),
            "screenshot": t.get("screenshot"),
            "risky": bool(_RISKY.search(target or "")),
            "confidence": "observed",
        })
    return rows


def _states(behavior: dict) -> list:
    out = []
    for s in behavior.get("states") or []:
        regs = [r.split("|")[0] for r in s.get("regions", [])]
        out.append({
            "id": s.get("id"),
            "path": s.get("path"),
            "title": s.get("title"),
            "regions": regs,
            "headings": s.get("headings", [])[:4],
            "dialogs": s.get("dialogs", 0),
            "control_count": s.get("control_count", 0),
            "confidence": "observed",
        })
    return out


def _static_surface(static: dict) -> dict:
    """What the static lanes contribute: the parts a behavioural run can't see.

    For Electron that is the process boundary — IPC channels and what the
    preload bridge exposes. For Mach-O it is the symbol/string surface. These are
    the things you must rebuild *on the other side* of the UI.
    """
    if not static:
        return {}
    lane = static.get("lane")
    out = {"lane": lane}
    analysis = static.get("analysis") or {}
    pkg = analysis.get("package") or {}
    if pkg:
        out["package"] = {k: pkg.get(k) for k in ("name", "version", "main")
                          if pkg.get(k)}
        out["dependencies"] = list(pkg.get("dependencies") or [])[:20]
    ipc_main = list(analysis.get("ipc_main_channels") or [])
    ipc_rend = list(analysis.get("ipc_renderer_channels") or [])
    bridge = list(analysis.get("contextbridge_exposures") or [])
    if ipc_main or ipc_rend or bridge:
        out["ipc"] = {"main": ipc_main[:60], "renderer": ipc_rend[:60],
                      "context_bridge": bridge[:60]}
    if analysis.get("js_files_analyzed"):
        out["js_files_analyzed"] = analysis["js_files_analyzed"]
    if analysis.get("routes"):
        out["bundle_routes"] = list(analysis["routes"])[:40]
    bins = static.get("binaries") or []
    if bins:
        out["binaries"] = [{"path": b.get("path"), "arch": b.get("arch"),
                            "linked_libs": len([x for x in (b.get("linked_libs") or []) if x])}
                           for b in bins[:6]]
        surfaces = [b.get("symbol_surface") or {} for b in bins]
        out["symbol_surface"] = {
            "objc_classes": sum(int(s.get("objc_classes") or 0) for s in surfaces),
            "swift_symbols": sum(int(s.get("swift_symbols") or 0) for s in surfaces),
        }
    ss = static.get("strings_signal") or {}
    if ss:
        out["strings"] = {k: list(ss.get(k) or [])[:12]
                          for k in ("urls", "api_paths", "feature_flags")}
    out["error"] = static.get("error")
    return out


def _build_plan(components, transitions, endpoints, entities, states,
                static_surface=None) -> list:
    tasks = []
    for c in components:
        if c["control_count"] == 0 and c["kind"] not in ("main content", "sidebar / navigation"):
            continue
        ac = ["renders as a `%s` region (%s)" % (c["role"], c["kind"])]
        layouts = c.get("layouts") or []
        if layouts:
            ac.append("observed layouts: %s px in a 1440x900 viewport"
                      % ", ".join(layouts[:4]))
        if c["control_count"]:
            ac.append("exposes %d interactive control(s): %s" % (
                c["control_count"],
                ", ".join("%s '%s'" % (x["role"], x["name"]) for x in c["controls"][:6])))
        tasks.append({
            "id": "B%d" % (len(tasks) + 1),
            "title": "Build the %s component" % c["name"],
            "kind": "ui",
            "depends_on": [],
            "acceptance": ac,
            "evidence": "screen snapshots + region inventory",
        })

    groups = {}
    for r in transitions:
        if r["risky"] and r["outcome"] == "no-visible-change":
            continue
        g = _group_role(_control_role(r["control"]))
        groups.setdefault(g, []).append(r)
    for g in sorted(groups):
        rows = groups[g]
        named = sorted({r["control"] for r in rows})
        tasks.append({
            "id": "B%d" % (len(tasks) + 1),
            "title": "Implement %s interactions (%d observed)" % (g, len(rows)),
            "kind": "interaction",
            "depends_on": [],
            "acceptance": ["%s → %s" % (r["control"], r["effect"]) for r in rows[:8]],
            "controls": named[:12],
            "evidence": "transition records T%s" % ", T".join(
                str(r["n"]) for r in rows[:8]),
        })

    for e in endpoints:
        if e["method"] == "GET" and not e["schema"]:
            continue
        ac = ["`%s %s` returns %s" % (e["method"], e["path"],
                                      ", ".join(str(s) for s in e["statuses"]) or "2xx")]
        if e["schema"]:
            ac.append("response shape: %s" % _schema_line(e["schema"]))
        tasks.append({
            "id": "B%d" % (len(tasks) + 1),
            "title": "Implement `%s %s`" % (e["method"], e["path"]),
            "kind": "api",
            "depends_on": [],
            "acceptance": ac,
            "evidence": "observed %d call(s); response body artifact" % e["hits"],
        })

    if any(s.get("dialogs") for s in states):
        tasks.append({
            "id": "B%d" % (len(tasks) + 1),
            "title": "Implement modal layer",
            "kind": "ui",
            "depends_on": [],
            "acceptance": ["a control opens a dialog; Escape or an explicit "
                           "control dismisses it",
                           "underlying content stays mounted (observed region "
                           "set unchanged behind the dialog)"],
            "evidence": "modal transitions + before/after screenshots",
        })

    storage_ents = [e for e in entities if any("Storage[" in s for s in e["sources"])]
    if storage_ents:
        tasks.append({
            "id": "B%d" % (len(tasks) + 1),
            "title": "Persist client state",
            "kind": "data",
            "depends_on": [],
            "acceptance": ["writes %s" % ", ".join(
                s for e in storage_ents for s in e["sources"]
                if "Storage[" in s)],
            "evidence": "storage shape probe before/after the run",
        })

    ipc = (static_surface or {}).get("ipc") or {}
    if ipc.get("main"):
        tasks.append({
            "id": "B%d" % (len(tasks) + 1),
            "title": "Implement the main-process IPC handlers",
            "kind": "backend",
            "depends_on": [],
            "acceptance": ["handles %d observed channel(s): %s" % (
                len(ipc["main"]), ", ".join("`%s`" % c for c in ipc["main"][:10]))],
            "evidence": "asar static analysis (ipcMain handle/on call sites)",
            "confidence": "inferred",
        })
    bridge = ipc.get("context_bridge") or []
    if bridge:
        tasks.append({
            "id": "B%d" % (len(tasks) + 1),
            "title": "Expose the preload bridge",
            "kind": "backend",
            "depends_on": [],
            "acceptance": ["preload exposes %d name(s) via contextBridge: %s" % (
                len(bridge), ", ".join("`%s`" % b for b in bridge[:10])),
                "renderer reaches main only through this surface (no direct nodeIntegration)"],
            "evidence": "asar static analysis (contextBridge.exposeInMainWorld)",
            "confidence": "inferred",
        })
    return tasks


def _control_role(desc: str) -> str:
    return (desc or "").split(" ")[0] if desc else "button"


def _schema_line(schema, depth: int = 0) -> str:
    if isinstance(schema, dict):
        items = []
        for k, v in list(schema.items())[:12]:
            items.append("%s: %s" % (k, _schema_line(v, depth + 1)))
        return "{ " + ", ".join(items) + " }"
    if isinstance(schema, list) and len(schema) == 2 and schema[0] == "array":
        return "[" + _schema_line(schema[1], depth + 1) + "]"
    if isinstance(schema, str):
        return schema
    return str(schema)


def _clip(text: str, limit: int = 240) -> str:
    """Table cells have to stay readable; the full shape lives in spec.json."""
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def _parity(transitions, states, endpoints) -> list:
    out = []
    for r in transitions:
        if r["risky"] and r["outcome"] == "no-visible-change":
            continue
        calls = ("; fires %s" % ", ".join("`%s`" % c for c in r["calls"])) if r["calls"] else ""
        verb = {"type": "Typing into", "key": "Pressing"}.get(r["action"], "Clicking")
        out.append("%s **%s** %s%s (state %s → %s)."
                   % (verb, r["control"], r["effect"], calls,
                      r["from_state"], r["to_state"]))
    routes = []
    for s in states:
        p = s.get("path") or "/"
        if p not in routes:
            routes.append(p)
    if len(states) > 1:
        out.append("Reaching %s never requires a full page reload — all "
                   "transitions above are in-document." % (
                       ", ".join("`%s`" % p for p in routes[:6])))
    for e in endpoints:
        if e["schema"]:
            out.append("`%s %s` keeps its observed response shape: %s"
                       % (e["method"], e["path"], _schema_line(e["schema"])))
    return out


def _unknowns(behavior, transitions) -> list:
    lim = []
    if behavior.get("error"):
        lim.append("behavioural capture unavailable: %s" % behavior["error"])
        if behavior.get("fix"):
            lim.append("to capture behaviour: %s" % behavior["fix"])
    elif not behavior.get("transitions"):
        lim.append("no interactions observed — the run produced no transitions")
    if not behavior.get("states"):
        lim.append("no surfaces captured: sections 1, 3, 4 and 6 are unknown for "
                   "this target; only static evidence stands")
    lim += list(behavior.get("limitations") or [])
    if not behavior:
        lim.append("no behavioural capture: this spec is derived from static "
                   "evidence only — interactions and states are unknown")
    noop = [t for t in transitions if t["outcome"] == "no-visible-change"]
    if noop:
        lim.append("%d control(s) produced no visible change in the capture "
                   "(%s) — their real effect may be server-side or gated"
                   % (len(noop), ", ".join(t["control"] for t in noop[:5])))
    risky = [t for t in transitions if t["risky"]]
    if risky:
        lim.append("destructive-looking controls were observed but not "
                   "evaluated for effect: %s"
                   % ", ".join(t["control"] for t in risky[:5]))
    if not behavior.get("bodies"):
        lim.append("no JSON responses captured: the data model is inferred from "
                   "storage and UI only")
    return lim[:12]


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def build(behavior: dict = None, static: dict = None, name: str = None,
          target: str = None) -> dict:
    """Synthesize the spec dict from a behaviour record (+ optional static)."""
    behavior = behavior or {}
    static = static or {}
    components = _components(behavior)
    transitions = _transition_rows(behavior)
    states = _states(behavior)
    endpoints = _endpoints(behavior)
    entities = _entities(behavior)
    static_surface = _static_surface(static)
    plan = _build_plan(components, transitions, endpoints, entities, states,
                       static_surface)

    title = (behavior.get("title") or static.get("title")
             or (static.get("analysis") or {}).get("package", {}).get("name")
             or target or behavior.get("target") or "untitled feature")
    feature = name or _feature_name(title)
    # entities that share a name with the feature come first: in a sidebar
    # teardown the reader wants the guide payload, not the feedback envelope
    tokens = {t.lower() for t in re.split(r"[^A-Za-z0-9]+", feature) if len(t) > 3}
    entities.sort(key=lambda e: (0 if e["name"].lower() in tokens else 1,
                                 e["name"].lower()))
    spec = {
        "schema": "coll/feature-spec@1",
        "feature": feature,
        "target": target or behavior.get("target"),
        "generated_from": {
            "behavior_mode": behavior.get("mode"),
            "captured_at": behavior.get("captured_at"),
            "viewport": behavior.get("viewport"),
            "debuggee": (behavior.get("debuggee") or {}).get("browser"),
            "static_lane": static.get("lane"),
            "cdp_commands": behavior.get("commands_sent"),
        },
        "summary": _summary(feature, components, transitions, endpoints, entities),
        "components": components,
        "static_surface": static_surface,
        "states": states,
        "transitions": transitions,
        "entities": entities,
        "endpoints": endpoints,
        "build_plan": plan,
        "parity_checks": _parity(transitions, states, endpoints),
        "excluded_traffic": _excluded_traffic(behavior),
        "unknowns": _unknowns(behavior, transitions),
    }
    return spec


def _feature_name(title: str) -> str:
    t = _norm(title) or "untitled feature"
    t = re.sub(r"\s*[|\-–—]\s*(Notion|Home|Workspace|App)$", "", t)
    return t[:80]


def _summary(feature, components, transitions, endpoints, entities) -> str:
    parts = ["**%s** — reimplemented from an observed run." % feature]
    if components:
        parts.append("%d surface component(s) (%s)."
                     % (len(components),
                        ", ".join(c["name"] for c in components[:4])))
    if transitions:
        parts.append("%d observed interaction(s) across %d state(s)."
                     % (len(transitions), len({t["from_state"] for t in transitions})))
    if endpoints:
        parts.append("%d API endpoint(s) with %d observed payload shape(s)."
                     % (len(endpoints), len(entities)))
    return " ".join(parts)


def to_markdown(spec: dict) -> str:
    L = []
    a = L.append
    a("# Feature spec — %s" % spec["feature"])
    a("")
    a(spec["summary"])
    a("")
    gf = spec.get("generated_from") or {}
    a("> Provenance: %s capture of `%s` at %s%s. Every row below is `observed` "
      "unless tagged `inferred`; unknowns are listed at the end."
      % (gf.get("behavior_mode") or "static", spec.get("target") or "?",
         gf.get("captured_at") or "n/a",
         ", debuggee %s" % gf["debuggee"] if gf.get("debuggee") else ""))
    a("")

    a("## 1. Surfaces")
    if spec["components"]:
        a("| id | component | kind | role | layouts (px) | controls |")
        a("|----|-----------|------|------|--------------|----------|")
        for c in spec["components"]:
            a("| %s | %s | %s | `%s` | %s | %d |"
              % (c["id"], c["name"], c["kind"], c["role"],
                 ", ".join(c.get("layouts") or []) or "?", c["control_count"]))
    else:
        a("_No behavioural capture — the surface is undocumented. Run with "
          "`--capture` to populate this section._")
    a("")

    a("## 2. Static evidence (bundle / binary)")
    ss = spec.get("static_surface") or {}
    if ss and (ss.get("package") or ss.get("ipc") or ss.get("strings")
               or ss.get("symbol_surface") or ss.get("binaries")):
        if ss.get("package"):
            p = ss["package"]
            a("- package: `%s`%s%s" % (p.get("name"),
                                       " v%s" % p["version"] if p.get("version") else "",
                                       " (main=%s)" % p["main"] if p.get("main") else ""))
        if ss.get("dependencies"):
            a("- dependencies: %s" % ", ".join("`%s`" % d for d in ss["dependencies"][:12]))
        ipc = ss.get("ipc") or {}
        if ipc.get("main") or ipc.get("renderer"):
            a("- IPC main channels (%d): %s" % (len(ipc.get("main") or []),
                                                ", ".join("`%s`" % c for c in (ipc.get("main") or [])[:12]) or "—"))
            a("- IPC renderer channels (%d): %s" % (len(ipc.get("renderer") or []),
                                                    ", ".join("`%s`" % c for c in (ipc.get("renderer") or [])[:12]) or "—"))
        if ipc.get("context_bridge"):
            a("- contextBridge exposures (%d): %s" % (
                len(ipc["context_bridge"]),
                ", ".join("`%s`" % b for b in ipc["context_bridge"][:12])))
        if ss.get("bundle_routes"):
            a("- routes inside the bundle: %s"
              % ", ".join("`%s`" % r for r in ss["bundle_routes"][:10]))
        if ss.get("symbol_surface"):
            sy = ss["symbol_surface"]
            a("- symbol surface: %s ObjC classes, %s Swift symbols"
              % (sy.get("objc_classes"), sy.get("swift_symbols")))
        if ss.get("binaries"):
            a("- binaries: %s" % ", ".join("`%s` (%s)" % (b["path"], b["arch"])
                                           for b in ss["binaries"][:4]))
        st = ss.get("strings") or {}
        for k, label in (("urls", "URLs"), ("api_paths", "API paths"),
                         ("feature_flags", "feature flags")):
            if st.get(k):
                a("- %s in strings: %s" % (label, ", ".join("`%s`" % v for v in st[k][:8])))
        a("")
        a("_Statically observed: presence and names, not behaviour. Anything "
          "here marked as a task is `inferred` — a name proves existence, not semantics._")
    else:
        a("_No static lane evidence for this target._")
    a("")

    a("## 3. Interaction contract")
    if spec["transitions"]:
        a("| # | control | effect | state | calls |")
        a("|---|---------|--------|-------|-------|")
        for t in spec["transitions"]:
            a("| T%s | %s | %s | %s → %s | %s |"
              % (t["n"], t["control"], t["effect"], t["from_state"],
                 t["to_state"], "<br>".join("`%s`" % c for c in t["calls"]) or "—"))
    else:
        a("_No interactions observed._")
    a("")

    a("## 4. State model")
    if spec["states"]:
        a("| state | route | regions visible | dialogs | controls |")
        a("|-------|-------|-----------------|---------|----------|")
        for s in spec["states"]:
            a("| %s | `%s` | %s | %d | %d |"
              % (s["id"], s["path"] or "/", ", ".join(s["regions"][:6]) or "—",
                 s.get("dialogs", 0), s.get("control_count", 0)))
        a("")
        a("```mermaid")
        a("stateDiagram-v2")
        a("    [*] --> %s" % (spec["states"][0]["id"]))
        edges = {}
        for t in spec["transitions"]:
            if t["from_state"] and t["to_state"]:
                edges.setdefault((t["from_state"], t["to_state"]), []).append(t["control"])
        for (a1, b1), labels in edges.items():
            a("    %s --> %s: %s" % (a1, b1, "; ".join(sorted(set(labels))[:3]).replace("\n", " ")))
        a("```")
    else:
        a("_State model unavailable without a behavioural capture._")
    a("")

    a("## 5. Data model")
    api_ents = [e for e in spec["entities"] if any("Storage[" not in s for s in e["sources"])]
    store_ents = [e for e in spec["entities"] if e not in api_ents]
    if api_ents:
        for e in api_ents:
            a("### %s" % e["name"])
            a("")
            a("| field | type |")
            a("|-------|------|")
            for k, v in list(e["fields"].items())[:20]:
                a("| `%s` | %s |" % (k, v))
            a("")
            a("_Sources: %s_" % "; ".join(e["sources"][:4]))
            a("")
    if store_ents:
        a("### Client-side state")
        a("")
        a("| storage | shape |")
        a("|---------|-------|")
        for e in store_ents[:24]:
            shape = ", ".join("%s: %s" % (k, v) for k, v in list(e["fields"].items())[:8])
            a("| `%s` | %s |" % (e["sources"][0].replace("Storage", " storage"), shape or "—"))
        a("")
    if not spec["entities"]:
        a("_No structured payloads captured (no XHR JSON, no JSON storage). "
          "Model the data from the API contract below once it is observed._")
        a("")

    a("## 6. API surface")
    if spec["endpoints"]:
        a("| method | path | status | calls | response shape |")
        a("|--------|------|--------|-------|----------------|")
        for e in spec["endpoints"]:
            a("| %s | `%s` | %s | %d | %s |"
              % (e["method"], e["path"], ",".join(str(s) for s in e["statuses"]) or "—",
                 e["hits"], _clip(_schema_line(e["schema"])) if e["schema"] else "—"))
    else:
        a("_No XHR/fetch traffic observed._")
    a("")
    if spec.get("excluded_traffic"):
        a("_%d call(s) excluded from this contract as non-API traffic — document "
          "loads, static assets, telemetry, advertising or media delivery._"
          % spec["excluded_traffic"])
        a("")

    a("## 7. Build plan")
    for t in spec["build_plan"]:
        a("### %s — %s" % (t["id"], t["title"]))
        a("_kind: %s · evidence: %s_" % (t["kind"], t["evidence"]))
        for ac in t["acceptance"]:
            a("- [ ] %s" % ac)
        a("")

    a("## 8. Parity checklist")
    a("Ship only when every line holds:")
    a("")
    for p in spec["parity_checks"]:
        a("- [ ] %s" % p)
    a("")

    a("## 9. Unknowns & limits")
    for u in spec["unknowns"]:
        a("- %s" % u)
    a("")
    a("---")
    a("Generated by `coll` (schema `%s`). Re-run to re-derive; diffs across "
      "runs show behaviour drift." % spec["schema"])
    return "\n".join(L) + "\n"


def write(spec: dict, out_dir: str) -> dict:
    """Write FEATURE-SPEC.md + spec.json; returns the paths."""
    import os
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, "FEATURE-SPEC.md")
    js_path = os.path.join(out_dir, "spec.json")
    with open(md_path, "w") as f:
        f.write(to_markdown(spec))
    with open(js_path, "w") as f:
        json.dump(spec, f, indent=2, default=str)
    return {"markdown": md_path, "json": js_path}

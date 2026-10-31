"""Lane B — Electron / JavaScript bundle feature-surface extraction.

Finds the real app logic (asar or plain JS dir) and extracts the surface a
feature lives on: entry points, module graph, IPC channels, contextBridge
exposures, routes, command IDs (Obsidian-style `namespace:verb`), URLs, API
paths, and feature flags. Static only — no code is executed.

Every `.asar` in the bundle is enumerated and analyzed (an Electron app ships
a small `app.asar` wrapper AND a large primary bundle like `obsidian.asar`;
missing the latter is a known defect this lane now closes).
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter

from .shell import run, slug

_REQUIRE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")
_IMPORT_FROM = re.compile(r"""from\s+['"]([^'"]+)['"]""")
_IMPORT_DYN = re.compile(r"""import\(\s*['"]([^'"]+)['"]\s*\)""")
_IPC_MAIN = re.compile(r"""ipcMain\.(on|handle|once|removeHandler)\(\s*['"]([^'"]+)['"]""")
_IPC_RENDER = re.compile(r"""ipcRenderer\.(invoke|send|sendSync|on|once|removeListener)\(\s*['"]([^'"]+)['"]""")
_EXPOSE = re.compile(r"""contextBridge\.exposeInMainWorld\(\s*['"]([^'"]+)['"]""")
_ROUTE = re.compile(r"""(?:<Route[^>]*path\s*=\s*|path\s*:\s*|route\(\s*)['"]([^'"]+)['"]""")

# Obsidian-style command IDs: namespace:verb, e.g. "app:open-vault",
# "editor:insert-link", "graph:open", "file-explorer:open", "quick-switcher:open".
# Namespaces are the app's own action domains (not a closed list, but the
# dominant ones for a PKM/notes app — keep it permissive on the verb side).
_COMMAND = re.compile(
    r"""(["'])((?:app|editor|workspace|file|files|view|markdown|graph|note|template|"""
    r"""open|insert|toggle|delete|copy|rename|reveal|publish|sync|slides|daily|random|"""
    r"""outline|properties|canvas|bookmarks|webviewer|search|switcher|hotkey|theme|"""
    r"""setting|command|plugin|window|obsidian|vault|tag|tags|backlink|backlinks|"""
    r"""quick-switcher|file-explorer|starred|page-preview|word-count|audio-recorder|"""
    r"""note-composer|open-in-default-app|templates|workspaces|outgoing-links)[a-z0-9-]*:"""
    r"""[a-z0-9][a-z0-9-]*)\1"""
)
_URL = re.compile(r"""https?://[^\s"'`<>]+""")
_API_PATH = re.compile(r"""["'](/api/[^"']+)["']""")
_FLAG = re.compile(r"""["']([a-z][a-z0-9-]*(?:\.enable|\.enabled|\.disabled|\.beta|\.feature|\.experimental))["']""")

_JS_EXT = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}

# Minified renderer bundles are routinely 10-30 MB; skip only pathological files.
_MAX_FILE_BYTES = 64_000_000
_MAX_URLS = 60
_MAX_COMMANDS = 160


def _find_asars(app_root: str):
    res = os.path.join(app_root, "Contents", "Resources")
    out = []
    if os.path.isdir(res):
        for fn in sorted(os.listdir(res)):
            if fn.endswith(".asar"):
                out.append(os.path.join(res, fn))
    return out


def _analyze_js_dir(js_dir: str, bank, target: str, max_files: int = 20000) -> dict:
    files = []
    for dirpath, dirnames, filenames in os.walk(js_dir):
        dirnames[:] = [d for d in dirnames if d not in {"node_modules", ".git"}]
        for fn in filenames:
            if os.path.splitext(fn)[1] in _JS_EXT:
                files.append(os.path.join(dirpath, fn))
    files = files[:max_files]

    requires = Counter()
    commands = Counter()
    ipc_main = set()
    ipc_render = set()
    exposes = set()
    routes = set()
    urls = set()
    api_paths = set()
    flags = set()
    pkg = None

    for fp in files:
        try:
            if os.path.getsize(fp) > _MAX_FILE_BYTES:
                continue
            txt = open(fp, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        rel = os.path.relpath(fp, js_dir)
        if rel == "package.json":
            try:
                pkg = json.loads(txt)
            except Exception:
                pkg = None
        for m in _REQUIRE.finditer(txt):
            requires[m.group(1)] += 1
        for m in _IMPORT_FROM.finditer(txt):
            requires[m.group(1)] += 1
        for m in _IMPORT_DYN.finditer(txt):
            requires[m.group(1)] += 1
        for m in _IPC_MAIN.finditer(txt):
            ipc_main.add(m.group(2))
        for m in _IPC_RENDER.finditer(txt):
            ipc_render.add(m.group(2))
        for m in _EXPOSE.finditer(txt):
            exposes.add(m.group(1))
        for m in _ROUTE.finditer(txt):
            routes.add(m.group(1))
        for m in _COMMAND.finditer(txt):
            commands[m.group(2)] += 1
        if len(urls) < _MAX_URLS:
            for m in _URL.finditer(txt):
                urls.add(m.group(0).rstrip(".,;"))
                if len(urls) >= _MAX_URLS:
                    break
        for m in _API_PATH.finditer(txt):
            api_paths.add(m.group(1))
        for m in _FLAG.finditer(txt):
            flags.add(m.group(1))

    top_requires = sorted(requires.items(), key=lambda kv: -kv[1])[:60]
    ext_requires = [(m, c) for m, c in top_requires if not m.startswith(".")]
    report = {
        "js_files_analyzed": len(files),
        "external_modules": ext_requires,
        "ipc_main_channels": sorted(ipc_main),
        "ipc_renderer_channels": sorted(ipc_render),
        "contextbridge_exposures": sorted(exposes),
        "routes": sorted(routes),
        "commands": [c for c, _ in commands.most_common(_MAX_COMMANDS)],
        "urls": sorted(urls)[:_MAX_URLS],
        "api_paths": sorted(api_paths)[:60],
        "feature_flags": sorted(flags)[:60],
    }
    if pkg:
        report["package"] = {
            "name": pkg.get("name"),
            "version": pkg.get("version"),
            "main": pkg.get("main"),
            "dependencies": list((pkg.get("dependencies") or {}).keys())[:80],
        }
    bank.add("electron", target, "js surface analysis", "coll-regex",
             "static regex over extracted JS", json.dumps(report, indent=2),
             report, confidence="inferred",
             limitation="static text patterns; minified/bundled code may hide channels; no runtime tracing")
    return report


def _merge_reports(reports: list) -> dict:
    """Union several _analyze_js_dir reports (multiple asars) into one."""
    if not reports:
        return {}
    if len(reports) == 1:
        return reports[0]
    merged = {
        "js_files_analyzed": 0,
        "external_modules": [],
        "ipc_main_channels": set(),
        "ipc_renderer_channels": set(),
        "contextbridge_exposures": set(),
        "routes": set(),
        "commands": Counter(),
        "urls": set(),
        "api_paths": set(),
        "feature_flags": set(),
    }
    for r in reports:
        merged["js_files_analyzed"] += r.get("js_files_analyzed", 0)
        merged["external_modules"] += r.get("external_modules", [])
        merged["ipc_main_channels"] |= set(r.get("ipc_main_channels", []))
        merged["ipc_renderer_channels"] |= set(r.get("ipc_renderer_channels", []))
        merged["contextbridge_exposures"] |= set(r.get("contextbridge_exposures", []))
        merged["routes"] |= set(r.get("routes", []))
        merged["commands"].update(r.get("commands", []))
        merged["urls"] |= set(r.get("urls", []))
        merged["api_paths"] |= set(r.get("api_paths", []))
        merged["feature_flags"] |= set(r.get("feature_flags", []))
        if "package" in r and "package" not in merged:
            merged["package"] = r["package"]
    merged["ipc_main_channels"] = sorted(merged["ipc_main_channels"])
    merged["ipc_renderer_channels"] = sorted(merged["ipc_renderer_channels"])
    merged["contextbridge_exposures"] = sorted(merged["contextbridge_exposures"])
    merged["routes"] = sorted(merged["routes"])
    merged["commands"] = [c for c, _ in merged["commands"].most_common(_MAX_COMMANDS)]
    merged["urls"] = sorted(merged["urls"])[:_MAX_URLS]
    merged["api_paths"] = sorted(merged["api_paths"])[:60]
    merged["feature_flags"] = sorted(merged["feature_flags"])[:60]
    return merged


def inspect_electron(target: str, bank) -> dict:
    target = os.path.abspath(target)
    report: dict = {"target": target, "lane": "electron"}

    asars = []
    if target.endswith(".app"):
        asars = _find_asars(target)
    elif target.endswith(".asar"):
        asars = [target]

    reports = []
    extracted_any = False
    if asars:
        report["asars"] = asars
        # primary = largest (the real app bundle, not the thin wrapper)
        primary = max(asars, key=os.path.getsize)
        report["asar"] = primary
        ext_root = os.path.join(bank.root, "asar-extracted")
        for i, asar in enumerate(asars):
            rc, listing, _, cmd = run(["asar", "list", asar], timeout=60)
            if rc == 0:
                files = [l for l in listing.decode("utf-8", "replace").splitlines() if l.strip()]
                bank.add("electron", target, f"asar file inventory #{i}", "asar list", cmd,
                         listing, {"file_count": len(files), "asar": asar})
            ext = os.path.join(ext_root, str(i))
            rc, _, err, cmd = run(["asar", "extract", asar, ext], timeout=300)
            if rc == 0:
                reports.append(_analyze_js_dir(ext, bank, target))
                extracted_any = True
            else:
                report.setdefault("asar_extract_errors", []).append(
                    f"{os.path.basename(asar)}: {err.decode('utf-8', 'replace')[:200]}")
    elif os.path.isdir(target):
        reports.append(_analyze_js_dir(target, bank, target))
        extracted_any = True

    if reports:
        report["analysis"] = _merge_reports(reports)
    elif extracted_any:
        report["error"] = "extracted asar(s) but found no analyzable JS"
    else:
        report["error"] = "no asar or JS directory found"
    return report

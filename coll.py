#!/usr/bin/env python3
"""Coll — reverse-engineer a feature from an app.

Usage:
  python3 coll.py <target> [--lane auto|apple|electron|web|linux|windows|unix]
                           [--out DIR] [--capture] [--spec] [--steps FILE]

Static lanes (default — nothing is executed):
  apple    — macOS .app / Mach-O (Info.plist, entitlements, symbols, strings)
  electron — Electron/JS bundle (asar, module graph, IPC, routes)
  web      — web app / SPA surface (frameworks, routes, APIs, analytics)
  linux    — ELF binaries (identity, arch, NEEDED libs, symbols, strings)
  windows  — PE binaries (identity, arch, imported DLLs/functions, strings)
  unix     — generic non-Mach-O Unix binaries (best-effort object scan)

Behaviour + runtime lanes (opt-in):
  --capture     drive a real browser/app over CDP: screens, controls, clicks,
                state transitions, API call graph, response schemas, storage
  --spec        synthesize a build-ready FEATURE-SPEC.md + spec.json
  --trace PID   attach lldb to a live process and capture its stack
  --trace EXE   launch under lldb instead (works when attach is SIP-denied)

Findings print concisely to stdout; full evidence + a verifiable manifest
land in --out. The static lanes never execute the target; the behaviour lane
launches its own browser (or an Electron app you explicitly allow) and says so
in the report.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import EvidenceBank
from engine import apple, electron, web, linux, windows, unix, capability
from engine import run

_OBJECT_RE = re.compile(
    r"object|executable|shared library|relocatable|archive|core file|COFF|a\.out",
    re.I,
)

_CAPTURE_LANES = ("web", "electron")


def _file_type(path: str) -> str:
    if not os.path.isfile(path):
        return ""
    rc, out, _, _ = run(["file", "-b", path], timeout=20)
    return out.decode("utf-8", "replace") if rc == 0 else ""


def detect_lane(target: str) -> str:
    if target.startswith(("http://", "https://")):
        return "web"
    t = os.path.abspath(target)
    if t.endswith(".asar"):
        return "electron"
    if t.endswith(".app") or os.path.exists(os.path.join(t, "Contents")):
        res = os.path.join(t, "Contents", "Resources")
        if os.path.isdir(res):
            # unpacked app/ dir (VSCode, Cursor) or any .asar (Obsidian's obsidian.asar)
            if os.path.isdir(os.path.join(res, "app")):
                return "electron"
            if any(f.endswith(".asar") for f in os.listdir(res)):
                return "electron"
        return "apple"
    if os.path.isdir(t):
        return "electron"
    # regular file: sniff the container format with `file`
    ft = _file_type(t)
    if "ELF" in ft:
        return "linux"
    if "PE32" in ft or "MS Windows" in ft:
        return "windows"
    if "Mach-O" in ft:
        return "apple"
    if _OBJECT_RE.search(ft):
        return "unix"
    return "apple"


def render(report: dict) -> str:
    out = []
    lane = report.get("lane")
    out.append(f"# Coll — {lane.upper()} — {report['target']}")

    if lane == "apple":
        if "info_plist" in report:
            out.append("\n## Bundle identity")
            for k, v in report["info_plist"].items():
                out.append(f"  {k}: {v}")
        if "entitlements" in report:
            out.append("\n## Entitlements")
            out.append(f"  {json.dumps(report['entitlements'], default=str)[:600]}")
        if "codesign" in report:
            out.append("\n## Signature")
            for line in report["codesign"].splitlines()[:6]:
                out.append(f"  {line}")
        out.append(f"\n## Binaries ({report.get('binary_count', 0)} total)")
        for b in report.get("binaries", [])[:4]:
            out.append(f"  - {b['path']}")
            out.append(f"      {b['arch']}")
            libs = [l for l in b.get("linked_libs", []) if l]
            out.append(f"      linked: {len(libs)} libs, e.g. {', '.join(libs[:4])}")
            s = b.get("symbol_surface")
            if s:
                out.append(f"      objc_classes={s.get('objc_classes')} "
                           f"protocols={s.get('objc_protocols')} "
                           f"categories={s.get('objc_categories')} "
                           f"swift_symbols={s.get('swift_symbols')}")
                if s.get("swift_sample"):
                    out.append(f"      swift types: {', '.join(s['swift_sample'][:8])}")
        if "strings_signal" in report:
            ss = report["strings_signal"]
            out.append(f"\n## Strings signal (selectors={ss.get('selector_count')})")
            out.append(f"  URLs: {', '.join(ss.get('urls', [])[:6])}")
            out.append(f"  API paths: {', '.join(ss.get('api_paths', [])[:10])}")
            out.append(f"  selectors: {', '.join(ss.get('selectors', [])[:15])}")
            out.append(f"  feature flags: {', '.join(ss.get('feature_flags', [])[:10])}")

    elif lane == "electron":
        if "asar" in report:
            out.append(f"\n## asar: {report['asar']} ({report.get('asar_file_count', '?')} files)")
        a = report.get("analysis")
        if a:
            if "package" in a:
                p = a["package"]
                out.append(f"\n## package: {p.get('name')} v{p.get('version')} (main={p.get('main')})")
                out.append(f"  deps: {', '.join(p.get('dependencies', [])[:12])}")
            out.append(f"\n## JS files analyzed: {a.get('js_files_analyzed')}")
            out.append(f"  external modules: {', '.join(f'{m}×{c}' for m, c in a.get('external_modules', [])[:20])}")
            out.append(f"\n## IPC main channels ({len(a.get('ipc_main_channels', []))})")
            out.append("  " + ", ".join(a["ipc_main_channels"][:40]))
            out.append(f"\n## IPC renderer channels ({len(a.get('ipc_renderer_channels', []))})")
            out.append("  " + ", ".join(a["ipc_renderer_channels"][:40]))
            out.append(f"\n## contextBridge exposures ({len(a.get('contextbridge_exposures', []))})")
            out.append("  " + ", ".join(a["contextbridge_exposures"][:40]))
            out.append(f"\n## routes ({len(a.get('routes', []))})")
            out.append("  " + ", ".join(a["routes"][:40]))
    elif lane == "web":
        if "error" in report:
            out.append(f"\n## error: {report['error']}")
        else:
            out.append(f"\n## page: {report.get('title')} (HTTP {report.get('http_status')})")
            fw = report.get("framework") or {}
            for name, v in fw.items():
                ver = v.get("version") if isinstance(v, dict) else None
                out.append(f"  framework: {name}" + (f" v{ver}" if ver else ""))
            out.append(f"\n## app bundles analyzed: {report.get('bundles_analyzed')}")
            out.append(f"  libraries: {', '.join(report.get('libraries', []))}")
            out.append(f"\n## routes ({len(report.get('routes', []))})")
            out.append("  " + ", ".join(report["routes"][:40]))
            out.append(f"\n## api endpoints ({len(report.get('api_endpoints', []))})")
            out.append("  " + ", ".join(report["api_endpoints"][:40]))
            out.append(f"\n## auth signals: {', '.join(report.get('auth_signals', []))}")
            out.append(f"\n## analytics ({len(report.get('analytics', []))})")
            for a in report.get("analytics", [])[:15]:
                out.append(f"  - {a}")
            out.append(f"\n## env config keys ({len(report.get('env_config_keys', []))})")
            out.append("  " + ", ".join(report["env_config_keys"][:40]))
            out.append(f"\n## domains ({len(report.get('domains', []))})")
            out.append("  " + ", ".join(report["domains"][:40]))
    elif lane == "capability":
        out.append(f"\n## pages fetched")
        for p in report.get("pages_fetched", []):
            out.append(f"  - {p['url']} (HTTP {p['status']})")
        out.append(f"\n## pricing signals ({len(report.get('pricing', []))})")
        for p in report.get("pricing", [])[:20]:
            out.append(f"  - {p}")
        out.append(f"\n## business model ({len(report.get('business_model', []))})")
        out.append("  " + ", ".join(report.get("business_model", [])[:20]))
        out.append(f"\n## trust posture ({len(report.get('trust', []))})")
        out.append("  " + ", ".join(report.get("trust", [])[:20]))
        out.append("\n## NOTE: pricing/claims are vendor-reported, not independently reconciled")
    elif lane == "behavior":
        out.append(f"\n## capture: {report.get('mode')} — {report.get('base_url') or ''}")
        if report.get("error"):
            out.append(f"  error: {report['error']}")
            if report.get("fix"):
                out.append(f"  fix:   {report['fix']}")
        d = report.get("debuggee") or {}
        if d.get("browser"):
            out.append(f"  debuggee: {d.get('browser')} (CDP {d.get('protocol')})")
        vp = report.get("viewport") or {}
        out.append(f"  viewport: {vp.get('width', '?')}x{vp.get('height', '?')}"
                   f"  ·  cdp commands: {report.get('commands_sent', '—')}"
                   f"  ·  captured: {report.get('captured_at')}")
        st = report.get("states") or []
        out.append(f"\n## states ({len(st)})")
        for s in st[:12]:
            out.append(f"  {s['id']}: {s.get('path')}  regions={len(s.get('regions', []))}"
                       f" dialogs={s.get('dialogs', 0)} controls={s.get('control_count')}")
        tr = report.get("transitions") or []
        out.append(f"\n## transitions ({len(tr)} observed)")
        for t in tr[:24]:
            reqs = ", ".join(f"{r['method']} {r['path']}" for r in t.get("requests", [])[:3])
            out.append(f"  T{t.get('n')} {t.get('action')}: {t.get('target')}"
                       f"  [{t.get('from_state')}→{t.get('to_state')}]"
                       f"  {t.get('outcome')}" + (f"  → {reqs}" if reqs else ""))
            if t.get("error"):
                out.append(f"       ! {t['error']}")
        ep = report.get("endpoints") or []
        out.append(f"\n## api call graph ({len(ep)} endpoints)")
        for e in ep[:24]:
            out.append(f"  {e['method']:4} {e['path']}  ×{e['hits']}"
                       f"  {','.join(str(s) for s in e['statuses'])}")
        ents = report.get("bodies") or []
        if ents:
            out.append(f"\n## observed payloads ({len(ents)})")
            for b in ents[:10]:
                out.append(f"  {b.get('method')} {b.get('url')[-60:]}")
        cons = report.get("console") or []
        errs = report.get("errors") or []
        if cons or errs:
            out.append(f"\n## console: {len(cons)} entries, {len(errs)} errors")
            for e in errs[:5]:
                out.append(f"  ! {e.get('text', '')[:120]}")
        shots = [s.get("screenshot") for s in report.get("screens", []) if s.get("screenshot")]
        out.append(f"\n## screens: {len(shots)} PNGs "
                   f"(e.g. {', '.join(shots[:3])})")
        for lim in report.get("limitations", [])[:5]:
            out.append(f"  · limit: {lim}")
    elif lane == "trace":
        out.append(f"\n## mode: {report.get('mode')}  pid={report.get('pid')}"
                   f"  path={report.get('path')}")
        if report.get("error"):
            out.append(f"  error: {report['error']}")
        s = report.get("summary") or {}
        out.append(f"  stop reason: {s.get('stop_reason')}")
        out.append(f"  threads: {len(s.get('threads', []))}  frames: {len(s.get('frames', []))}")
        for f in s.get("frames", [])[:14]:
            out.append(f"    {f}")
        if report.get("limitation"):
            out.append(f"  · limit: {report['limitation']}")
    elif lane == "linux":
        if "error" in report:
            out.append(f"\n## error: {report['error']}")
        else:
            out.append(f"\n## format: {report.get('format')}")
            if report.get("arch"):
                out.append(f"  arch: {report['arch']}")
            if report.get("entry_point"):
                out.append(f"  entry point: {report['entry_point']}")
            out.append(f"\n## linked libs ({len(report.get('linked_libs', []))})")
            out.append("  " + ", ".join(report["linked_libs"][:40]))
            s = report.get("symbol_surface") or {}
            out.append(f"\n## symbols: {s.get('symbols', 0)} "
                       f"(defined={s.get('defined', 0)} undefined={s.get('undefined', 0)} "
                       f"weak={s.get('weak', 0)})")
            if s.get("sample"):
                out.append("  sample: " + ", ".join(s["sample"][:20]))
            ss = report.get("strings_signal") or {}
            out.append("\n## strings signal")
            out.append(f"  URLs: {', '.join(ss.get('urls', [])[:6])}")
            out.append(f"  API paths: {', '.join(ss.get('api_paths', [])[:10])}")
            out.append(f"  feature flags: {', '.join(ss.get('feature_flags', [])[:10])}")
            out.append(f"  library paths: {', '.join(ss.get('library_paths', [])[:12])}")
    elif lane == "windows":
        if "error" in report:
            out.append(f"\n## error: {report['error']}")
        else:
            out.append(f"\n## format: {report.get('format')}")
            if report.get("arch"):
                out.append(f"  arch: {report['arch']}")
            if report.get("entry_point"):
                out.append(f"  entry point: {report['entry_point']}")
            if report.get("subsystem"):
                out.append(f"  subsystem: {report['subsystem']}")
            out.append(f"\n## imported DLLs ({len(report.get('imported_dlls', []))})")
            out.append("  " + ", ".join(report["imported_dlls"][:40]))
            out.append(f"\n## imported functions ({report.get('import_count', 0)})")
            out.append("  " + ", ".join(report.get("imported_symbols", [])[:40]))
            s = report.get("symbol_surface") or {}
            out.append(f"\n## symbol table: {s.get('symbols', 0)} ({s.get('note', '')})".rstrip())
            ss = report.get("strings_signal") or {}
            out.append("\n## strings signal")
            out.append(f"  URLs: {', '.join(ss.get('urls', [])[:6])}")
            out.append(f"  API paths: {', '.join(ss.get('api_paths', [])[:10])}")
            out.append(f"  feature flags: {', '.join(ss.get('feature_flags', [])[:10])}")
            out.append(f"  DLL/registry: {', '.join(ss.get('dll_registry', [])[:12])}")
    elif lane == "unix":
        if "error" in report:
            out.append(f"\n## error: {report['error']}")
        else:
            out.append(f"\n## format: {report.get('format')}")
            if report.get("arch"):
                out.append(f"  arch: {report['arch']}")
            s = report.get("symbol_surface") or {}
            out.append(f"\n## symbols: {s.get('symbols', 0)} "
                       f"(defined={s.get('defined', 0)} undefined={s.get('undefined', 0)})")
            if s.get("sample"):
                out.append("  sample: " + ", ".join(s["sample"][:20]))
            ss = report.get("strings_signal") or {}
            out.append("\n## strings signal")
            out.append(f"  URLs: {', '.join(ss.get('urls', [])[:6])}")
            out.append(f"  API paths: {', '.join(ss.get('api_paths', [])[:10])}")
            out.append(f"  feature flags: {', '.join(ss.get('feature_flags', [])[:10])}")
            out.append(f"  library paths: {', '.join(ss.get('library_paths', [])[:12])}")

    return "\n".join(out)


def _static_report(lane: str, target: str, bank: EvidenceBank) -> dict:
    if lane == "apple":
        return apple.inspect_apple(target, bank)
    if lane == "web":
        return web.inspect_web(target, bank)
    if lane == "capability":
        return capability.inspect_capability(target, bank)
    if lane == "linux":
        return linux.inspect_linux(target, bank)
    if lane == "windows":
        return windows.inspect_windows(target, bank)
    if lane == "unix":
        return unix.inspect_unix(target, bank)
    return electron.inspect_electron(target, bank)


def _run_capture(args, lane: str, bank: EvidenceBank) -> dict:
    from engine import behavior
    steps = None
    if args.steps:
        with open(args.steps) as f:
            loaded = json.load(f)
        # accept both a bare list of steps and a document with metadata
        steps = loaded.get("steps") if isinstance(loaded, dict) else loaded
        if not isinstance(steps, list):
            raise SystemExit("--steps file must be a JSON array or {\"steps\": [...]}")
    common = dict(steps=steps, max_controls=args.max_controls,
                  explore=not args.no_explore, timeout=args.timeout)
    if lane == "web":
        return behavior.capture_web(args.target, bank, headless=not args.headed,
                                    port=args.attach, **common)
    if args.attach:
        return behavior.capture_attach(args.attach, bank, target=args.target, **common)
    return behavior.capture_electron(args.target, bank, launch_app=args.launch_app,
                                     settle_s=args.app_settle, **common)


def _run_trace(args, bank: EvidenceBank) -> dict:
    from engine import trace as trace_lane
    target = args.trace
    try:
        target_val = int(target)
    except (TypeError, ValueError):
        target_val = target
    rep = trace_lane.trace(target_val, symbol=args.symbol,
                           timeout=args.timeout)
    bank.add("trace", str(args.trace), "lldb %s" % rep.get("mode"),
             "lldb", rep.get("command", ""),
             json.dumps(rep, indent=1, default=str).encode(),
             {"mode": rep.get("mode"), "frames": len((rep.get("summary") or {}).get("frames", []))},
             confidence="observed",
             limitation=rep.get("limitation", ""))
    return rep


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target")
    ap.add_argument("--lane", choices=["auto", "apple", "electron", "web", "capability", "linux",
                                       "windows", "unix"], default="auto")
    ap.add_argument("--out", default=None,
                    help="evidence output dir (default: coll-out/<basename>)")
    ap.add_argument("--index", action="store_true",
                    help="build the ObjC/Swift feature index (apple lane)")
    ap.add_argument("--find", default=None,
                    help="query the feature index for a keyword (apple lane)")
    ap.add_argument("--limit", type=int, default=40,
                    help="max hits per category for --find")
    ap.add_argument("--sweep", default=None,
                    help="sweep a terms JSON (id+aliases) and print term->hits (any lane)")

    g = ap.add_argument_group("behaviour capture (opt-in, drives the target)")
    g.add_argument("--capture", action="store_true",
                   help="capture live behaviour over CDP (web: launches headless "
                        "Chrome; electron: attaches if a debug port is open)")
    g.add_argument("--attach", type=int, default=None, metavar="PORT",
                   help="attach to an existing CDP port instead of launching")
    g.add_argument("--launch-app", action="store_true",
                   help="launch an Electron app with --remote-debugging-port in an "
                        "isolated profile (puts a window on screen)")
    g.add_argument("--steps", default=None, metavar="FILE",
                   help="JSON list of scripted interactions to run as the flow")
    g.add_argument("--max-controls", type=int, default=8,
                   help="auto-explore budget: controls clicked (default 8)")
    g.add_argument("--no-explore", action="store_true",
                   help="do not auto-click; only snapshot the initial screen")
    g.add_argument("--headed", action="store_true",
                   help="run the browser with a visible window")
    g.add_argument("--app-settle", type=float, default=8.0,
                   help="seconds to wait after launching an Electron app")

    g2 = ap.add_argument_group("build-ready output")
    g2.add_argument("--spec", action="store_true",
                    help="synthesize FEATURE-SPEC.md + spec.json from the findings")
    g2.add_argument("--spec-name", default=None, help="override the feature name")

    g3 = ap.add_argument_group("runtime tracing (lldb)")
    g3.add_argument("--trace", default=None, metavar="PID|EXE",
                    help="capture a live stack: PID to attach, or a path to launch")
    g3.add_argument("--symbol", default=None,
                    help="breakpoint symbol for --trace")
    ap.add_argument("--timeout", type=float, default=60.0,
                    help="per-operation timeout in seconds (default 60)")
    args = ap.parse_args()

    lane = detect_lane(args.target) if args.lane == "auto" else args.lane
    base = os.path.basename(os.path.abspath(args.target).rstrip("/")) or "target"
    out = args.out or os.path.join("coll-out", base)
    bank = EvidenceBank(out)

    if args.sweep:
        from engine import sweep as _sweep
        print(json.dumps(_sweep.sweep(args.target, args.sweep, out, lane), indent=2, ensure_ascii=False))
        return

    # feature-index mode (apple lane)
    if lane == "apple" and (args.index or args.find):
        from engine import apple_v2
        db = os.path.join(bank.root, "objc-index.db")
        if not os.path.exists(db):
            print("# building feature index (first run) ...")
            print("# index:", json.dumps(apple_v2.index_objc(args.target, db), default=str))
        if args.find:
            hits = apple_v2.find(db, args.find, limit=args.limit)
            print(f"# find '{args.find}' — {len(hits)} hits")
            for kind, text in hits:
                print(f"  [{kind}] {text}")
        else:
            print(f"# index -> {db}")
        return

    # runtime trace lane (lldb)
    if args.trace:
        rep = _run_trace(args, bank)
        manifest = bank.save_manifest()
        print(render(rep))
        print(f"\n# evidence -> {os.path.abspath(bank.root)}  (manifest: {manifest})")
        return

    # static + optional behaviour
    static = None
    try:
        static = _static_report(lane, args.target, bank)
    except Exception as e:  # noqa: BLE001 — behaviour capture must not die for
        print(f"# static lane '{lane}' skipped: {e}", file=sys.stderr)  # a bad path

    behavior_report = None
    if args.capture or args.attach:
        if lane not in _CAPTURE_LANES and not args.attach:
            print(f"# capture is not available for lane '{lane}' — skipping",
                  file=sys.stderr)
        else:
            behavior_report = _run_capture(args, lane, bank)

    manifest = bank.save_manifest()

    if static is not None:
        print(render(static))
    if behavior_report is not None:
        print(render(behavior_report))
        # the raw behaviour record is a deliverable in its own right: it is what
        # a builder re-reads when the spec's summary is not enough
        bpath = os.path.join(bank.root, "behavior.json")
        with open(bpath, "w") as fh:
            json.dump(behavior_report, fh, indent=1, default=str)
        print("\n# behaviour record -> %s" % bpath)

    spec_paths = None
    if args.spec:
        from engine import spec as spec_mod
        spec = spec_mod.build(behavior=behavior_report, static=static,
                              name=args.spec_name, target=args.target)
        spec_paths = spec_mod.write(spec, bank.root)
        print("\n# FEATURE-SPEC -> %s" % spec_paths["markdown"])
        print("# spec.json    -> %s" % spec_paths["json"])
        print("\n" + spec_mod.to_markdown(spec).split("## 1.")[0].strip())

    print(f"\n# evidence -> {os.path.abspath(bank.root)}  (manifest: {manifest})")
    if args.spec and spec_paths:
        print("# build-ready spec: %s" % os.path.abspath(spec_paths["markdown"]))


if __name__ == "__main__":
    main()

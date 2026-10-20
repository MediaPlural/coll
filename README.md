# Coll — reverse-engineer a feature from an app

Built for a common job: *"I like feature X in app Y — build it for me."* Coll
looks inside app Y and hands back a **build-ready feature spec**: what the
feature's surfaces are, what each control does, what state it moves between,
what it reads and writes, and what you have to build to match it.

Two engines, one evidence contract:

- **Static lanes** — read bytes without executing anything (`otool`, `nm`,
  `objdump`, `lipo`, `plutil`, `codesign`, `strings`, `lldb`, `swift-demangle`).
- **Behaviour lane** — drive a real Chromium (web page or Electron app) over the
  Chrome DevTools Protocol and watch the feature run: clicks, DOM deltas, SPA
  routes, API call graph, response schemas, client storage. Stdlib-only CDP
  client — no Playwright, no npm, no browser driver.
- **Synthesizer** — turns both into `FEATURE-SPEC.md` + `spec.json`: surfaces,
  interaction contract, state machine (mermaid), data model, API surface, an
  ordered build plan with acceptance criteria, and a parity checklist. Rule-based
  and deterministic: same observation → same spec → diffable across runs.

**Not a dependency on REA.** This project took REA's *pattern* (agent interface +
evidence discipline + per-target backends) and synthesized its own — no
dependency on Hopper/Ghidra/IDA either. Python 3 stdlib orchestrator, installed
free Apple toolchain, evidence-pinned findings.

## Quick start

```
# 1. static surface only (safe: nothing is executed)
python3 coll.py /Applications/Notion.app

# 2. behaviour + build-ready spec for a web app
python3 coll.py https://app.example.com --capture --spec

# 3. behaviour for a desktop Electron app (attach if a debug port is open)
python3 coll.py /Applications/Notion.app --capture --spec
python3 coll.py /Applications/Obsidian.app --capture --spec --launch-app

# 4. scripted flow — capture a real UX path, not random clicks
python3 coll.py https://www.youtube.com/ --capture --spec --no-explore \
        --steps demos/youtube-sidebar/steps.json

# 5. runtime stack of a live process (lldb)
python3 coll.py whatever --trace 1234
python3 coll.py whatever --trace /path/to/binary --symbol malloc
```

Findings print to stdout; full evidence (`manifest.json` + byte-pinned
artifacts), the raw `behavior.json`, and the spec land in `--out`
(default `coll-out/<name>`).

## Lanes

| Lane | Target | What you get | Status |
|------|--------|--------------|--------|
| `apple` | `.app` / Mach-O | Info.plist, entitlements, signature, binaries, linked libs, ObjC/Swift symbol surface, strings signal | ✅ |
| `electron` | `.asar` / JS bundle | package, module graph, IPC main/renderer channels, contextBridge exposures, routes | ✅ |
| `web` | URL / SPA | framework + version, libraries, routes, API paths, auth signals, analytics, env keys | ✅ |
| `behavior` | URL, `.app`, or CDP port | screens, controls + owning region, state machine, transitions with DOM deltas, API call graph, JSON schemas, storage shapes, PNG per state | ✅ |
| `trace` | PID or executable | lldb attach/launch, backtrace, breakpoint-on-symbol | ✅ |
| `linux` / `windows` / `unix` | ELF / PE / other | identity, arch, libs/imports, symbol + strings surface | ✅ |

## Flags that matter

```
--capture          run the behaviour lane (web: launches headless Chrome;
                   electron: attaches when a debug port is open)
--attach PORT      attach to an existing CDP debugger instead of launching
--launch-app       launch an Electron app with --remote-debugging-port in an
                   isolated profile (opt-in: it puts a window on screen)
--steps FILE       JSON flow: click/type/key/wait/screenshot/eval/scroll
--max-controls N   auto-explore budget (default 8; destructive controls skipped)
--no-explore       observe only — snapshot the base screen, click nothing
--spec             emit FEATURE-SPEC.md + spec.json
--trace PID|EXE    lldb runtime trace
```

`--steps` entry: `{"action":"click","text":"Show more"}` (or `role`/`name`,
`selector`, `id`), `{"action":"type","selector":"input[name=q]","text":"x"}`,
`{"action":"key","key":"Enter"}`.

## What the spec looks like

`demos/youtube-sidebar/FEATURE-SPEC.md` — a real capture of YouTube's guide
sidebar: collapse/expand transitions with control deltas, `/channel/…` and
`/results?search_query=…` routes, the InnerTube endpoints with observed response
shapes, and a build plan whose acceptance criteria are the observed behaviours.

`demos/notion-static/FEATURE-SPEC.md` — the other half: what a static-only
teardown of an Electron app can honestly say (package, IPC channels,
contextBridge surface) and exactly which command closes the behaviour gap.

`spec.json` carries the same facts machine-readably (`components`, `states`,
`transitions`, `entities`, `endpoints`, `build_plan`, `parity_checks`).

## Demos

```
python3 demos/run_demo.py youtube   # live behavioural capture (needs Chrome)
python3 demos/run_demo.py notion    # static/Electron path (no browser)
```

Checked-in outputs live under `demos/<name>/`; raw evidence is regenerated into
`demos/<name>/evidence/` (git-ignored).

## Laws

- **Static by default.** No target byte is executed; the behaviour lane launches
  its *own* browser, or an Electron app you explicitly allow, and says so in the
  report.
- **No DRM breaking, no destructive auto-clicks.** Delete/remove/pay/publish-style
  controls are never auto-clicked; they are named in the unknowns instead.
- **Evidence-backed.** Every finding carries the exact reproduction command, a
  blake2b digest of the artifact, a confidence (`observed` | `inferred`), and an
  explicit limitation.
- **Observed ≠ invented.** The spec never asserts a behaviour that was not seen;
  anything derived (e.g. IPC semantics from a channel name) is tagged.
- **Platform-agnostic contracts.** The report schema is stable; new backends slot
  in and fail closed when a tool is missing.

## Layout

```
coll.py              CLI front door (lane detect + render + orchestrate)
engine/shell.py      safe subprocess (argv-only, quoted repro)
engine/evidence.py   Finding + EvidenceBank (digest, manifest)
engine/apple.py      Lane A — .app / Mach-O
engine/electron.py   Lane B — Electron / JS bundle
engine/web.py        Lane W — web/SPA static surface
engine/linux.py  engine/windows.py  engine/unix.py  — platform lanes
engine/trace.py      Lane T — lldb runtime tracing
engine/ws.py         minimal RFC 6455 WebSocket client (stdlib)
engine/cdp.py        CDP session: discovery, command correlation, event pump
engine/probes.py     in-page JS: snapshot, ARM observers, resolve/click/type
engine/behavior.py   Lane D — behavioural capture (screens, states, transitions)
engine/spec.py       synthesizer — behaviour record -> FEATURE-SPEC.md/spec.json
tests/               unit + live end-to-end tests (fixture SPA in tests/)
demos/               checked-in demo teardowns + regeneration script
```

## Tests

```
python3 -m unittest discover -s tests      # 66 tests, ~11s
```

Unit tests cover WebSocket framing, CDP correlation/timeouts, probe JS syntax
(parsed with node when present), behaviour helpers and spec synthesis. The live
end-to-end test spins up a fixture SPA (`tests/fixture_app.py`), drives it with
real headless Chrome, and asserts on states, transitions, API graph, observed
JSON schemas, storage shapes and the generated spec; it skips cleanly when no
Chromium-family browser is installed.

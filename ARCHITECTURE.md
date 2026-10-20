# Architecture — Coll

> Doctrine: the default take from an external teardown is a **lesson/approach**,
> not their artifact. Code-take only when the license is clean AND synthesis
> genuinely costs more. Here: license is MIT (clean), but the directive was
> explicit — build it fresh, better, not dependent on them. So this document
> records the *pattern* taken and the fresh synthesis built on top.

## Source
- `rea-agents` (MIT, ~135k stars), April 2026 → "Reverse Engineer Anything."
  Started as a Hopper-disassembler MCP wrapper, grew into a general
  reverse-engineering MCP + investigation workflow for Claude Code/Codex/Cursor.

## What was taken (pattern, not code)
1. **One agent interface over a catalog of per-target backends** — an agent
   asks "inspect this," the tool routes by target type, returns findings the
   agent can follow up on.
2. **Evidence discipline** — every finding ships with its reproduction input +
   explicit limitations; "concise findings + reproduction inputs"; no fake
   conclusions. This is the single highest-value idea in REA and it maps
   one-to-one onto the verify-never-report principle.
3. **Investigation model** — inspect → trace → follow-up question → rebuild.
4. **Ownership/provenance** — produced artifacts are attributed, never claimed
   as established fact without evidence.

## What was built fresh (surpass thesis)
| REA's way | Coll's way |
|-----------|----------------|
| Hopper/Ghidra/IDA seats (paid / heavy) | installed free Apple toolchain + `lldb` |
| Node/TS tree: Playwright, WABT, JADX, apktool, Binwalk, pwntools | Python 3 stdlib orchestrator (incl. a hand-rolled CDP client) |
| Claude Code-first npm package | native CLI + (next) MCP + skill, callable by any agent |
| kitchen-sink catalog of 20 target types | the lanes you actually use, done deep |
| macOS parser bolted on late | Apple/macOS-native from day one |
| surface inventory as the deliverable | a build-ready feature spec as the deliverable |

## Evidence contract
```
Finding: {lane, target, label, tool, command, summary, artifact, digest, confidence, limitation}
```
- `command` is the exact shlex-quoted argv → reproducible by hand.
- `digest` is blake2b-128 of the raw artifact → re-verifiable byte-for-byte.
- `confidence` ∈ observed | inferred | unknown → the finding never overclaims.
- `limitation` is mandatory on inferred findings → e.g. "strings is heuristic".

## Lane status
- ✅ **apple** — `.app`/Mach-O: Info.plist facts, entitlements, signature,
  binary inventory (arch, linked libs), ObjC class/protocol/category surface,
  demangled Swift symbol surface, strings signal (selectors/URLs/flags).
  Verified end-to-end on Telegram.app (31 binaries, 291 ObjC classes, 1,930
  Swift symbols, 4,382 selectors, live feature flags).
- ✅ **electron** — `.asar`/JS: package facts, external-module histogram, IPC
  main/renderer channels, contextBridge exposures, routes. Verified on
  Notion.app (13 exposures incl. `__electronApi`, `__mediaApi`,
  `__sqliteBrowserApi`).
- ✅ **web** — HTML + own JS bundles: framework/version, libraries, routes, API
  paths, auth signals, analytics, env keys, domains.
- ✅ **behavior** — CDP-driven capture (see below). Verified live on
  youtube.com (4 states, 6 transitions, InnerTube API graph with response
  schemas) and on the fixture SPA in `tests/`.
- ✅ **trace** — lldb attach / launch-under-debugger, backtrace, breakpoint-on-
  symbol. SIP-denied attaches are detected and reported with the fallback.
- ✅ **linux / windows / unix** — ELF/PE/other: identity, arch, libs/imports,
  symbol + strings surface.

## Behaviour lane (Lane D) — the upgrade that mattered

Static lanes answer "what classes exist". They cannot answer "what happens when
I click this". Lane D answers the second question, and it is what makes the
output build-ready.

```
launch/attach ──► CDP session ──► probes ──► state clustering ──► spec
 Chrome/Electron    ws.py + cdp.py   probes.py    behavior.py       spec.py
```

1. **Transport (`ws.py`, `cdp.py`)** — hand-rolled RFC 6455 client and a CDP
   session with command/response correlation and an event pump. `ws.py` is
   ~200 lines and removes Playwright/selenium from the dependency graph.
2. **Probes (`probes.py`)** — one self-evaluating JS expression per question:
   visible regions (`nav/aside/main/dialog/…`), interactive controls with the
   region that owns them, storage shapes, a MutationObserver + history/fetch
   hook, and target resolution that climbs from text to the nearest clickable
   ancestor (real apps nest text inside custom elements).
3. **Capture (`behavior.py`)** — screen snapshots with PNG evidence, state
   clustering on `(route, region anatomy incl. geometry, dialog presence)`,
   transitions with the network calls they caused, JSON response schemas pulled
   via `Network.getResponseBody`, storage before/after, AX-tree walk.
   Interactions are dispatched as real input events (trusted clicks), and a
   fresh reload per auto-explored control keeps the state graph honest.
4. **Synthesis (`spec.py`)** — deterministic, rule-based: components (grouped by
   conceptual region, with layout variants), interaction contract, state machine,
   data model (flattened schema paths), API surface (same-host XHR only;
   telemetry/ads/assets excluded and counted), build plan, parity checklist.

Deliberate constraints, stated in every report: one observed run (no permission
or server-side branches), one viewport, logged-out unless the attached profile
was already signed in, and no auto-click of destructive controls.

## Roadmap (unbuilt, in priority order)
1. **Lane A v2 — full ObjC method dump** via `objdump --macho --objc-meta-data`
   (Telegram yields 517k lines: classes + methods + protocols), parsed to a
   queryable class/method index. This is the "find the exact method behind a
   feature" upgrade.
2. **Multi-viewport capture** — the same flow at 2–3 breakpoints so responsive
   behaviour lands in the state model instead of the unknowns.
3. **Diff mode** — run the same flow against two builds and diff the spec
   (`coll diff old.json new.json`) → a behavior-drift report.
4. **MCP + skill wiring** — expose the engine as an MCP server + a discoverable
   skill so any agent invokes it the same way.
5. **Other platforms on demand** — APK lane only when a real job demands it;
   contracts are already platform-agnostic.

## Open decisions
- **Name** — "Coll" is a working title. Run through the naming protocol or
  name it outright.
- **Next lane** — ratify the roadmap order above, or point at a real feature
  you want pulled out of a specific app as the first production job.

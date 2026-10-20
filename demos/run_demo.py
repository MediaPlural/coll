#!/usr/bin/env python3
"""Regenerate the checked-in demo teardowns.

Two demos, chosen because between them they cover both halves of the tool:

  youtube-sidebar  a live behavioural capture of a real shipped sidebar
                   (YouTube's guide drawer: collapse, expand, route, search)
                   -> FEATURE-SPEC.md an agent can build from
  notion-static    the static path on the app this tool was built for: Notion
                   (Electron/asar, no debug port available) -> a spec that is
                   explicit about what static evidence can and cannot say

Usage:
  python3 demos/run_demo.py youtube      # live capture (needs Chrome)
  python3 demos/run_demo.py notion       # static only, no browser
  python3 demos/run_demo.py all

Raw evidence (screenshots, AX trees, response bodies) lands in
`demos/<name>/evidence/` and is git-ignored; the spec, the screenshots and the
manifest are copied into the tracked tree so the demo is reviewable without a
re-run.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMOS = os.path.join(ROOT, "demos")

YOUTUBE_STEPS = {
    "feature": "YouTube guide sidebar",
    "steps": [
        {"action": "click", "role": "button", "name": "Guide", "label": "collapse-guide"},
        {"action": "click", "role": "button", "name": "Guide", "label": "expand-guide"},
        {"action": "click", "text": "Show more", "label": "expand-sections"},
        {"action": "click", "text": "Music", "label": "navigate-music"},
        {"action": "type", "selector": "input[name=search_query]",
         "text": "reverse engineer app", "label": "type-query"},
        {"action": "key", "key": "Enter", "label": "submit-search"},
    ],
}


def _run(cmd: list, cwd: str = ROOT) -> int:
    print("$ " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=cwd)
    return r.returncode


def _collect(name: str, evidence: str, keep_screens: bool = True) -> None:
    """Copy the reviewable parts of a run into demos/<name>/."""
    dest = os.path.join(DEMOS, name)
    os.makedirs(dest, exist_ok=True)
    for fname in ("FEATURE-SPEC.md", "spec.json", "behavior.json", "manifest.json"):
        src = os.path.join(evidence, fname)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dest, fname))
    shots = sorted(f for f in os.listdir(evidence) if f.endswith(".png"))
    sdir = os.path.join(dest, "screens")
    if keep_screens and shots:
        shutil.rmtree(sdir, ignore_errors=True)   # never leave stale frames
        os.makedirs(sdir, exist_ok=True)
        for f in shots:
            shutil.copy2(os.path.join(evidence, f), os.path.join(sdir, f))
    print("# collected -> %s (%d screenshots)" % (dest, len(shots)))


def demo_youtube() -> int:
    evidence = os.path.join(DEMOS, "youtube-sidebar", "evidence")
    shutil.rmtree(evidence, ignore_errors=True)
    steps_path = os.path.join(DEMOS, "youtube-sidebar", "steps.json")
    os.makedirs(os.path.dirname(steps_path), exist_ok=True)
    with open(steps_path, "w") as f:
        json.dump(YOUTUBE_STEPS, f, indent=2)
    rc = _run([sys.executable, "coll.py", "https://www.youtube.com/",
               "--capture", "--spec", "--no-explore", "--steps", steps_path,
               "--out", evidence, "--spec-name", "YouTube guide sidebar",
               "--timeout", "90"])
    if rc == 0:
        _collect("youtube-sidebar", evidence)
    return rc


def demo_notion() -> int:
    app = "/Applications/Notion.app"
    if not os.path.exists(app):
        print("# Notion.app not installed — skipping the static demo")
        return 0
    evidence = os.path.join(DEMOS, "notion-static", "evidence")
    shutil.rmtree(evidence, ignore_errors=True)
    rc = _run([sys.executable, "coll.py", app, "--capture", "--spec",
               "--no-explore", "--out", evidence,
               "--spec-name", "Notion desktop shell"])
    if rc == 0:
        _collect("notion-static", evidence, keep_screens=False)
    return rc


def main() -> int:
    which = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    rc = 0
    if which in ("youtube", "all"):
        rc |= demo_youtube()
    if which in ("notion", "all"):
        rc |= demo_notion()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

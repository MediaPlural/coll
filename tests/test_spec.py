"""Unit tests — spec synthesis (deterministic, no browser needed)."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import spec as spec_mod

BEHAVIOR = {
    "lane": "behavior",
    "mode": "launch-web",
    "target": "https://demo.test/",
    "captured_at": "2026-10-20T00:00:00Z",
    "viewport": {"width": 1440, "height": 900},
    "debuggee": {"browser": "Chrome/155"},
    "title": "Demo — sidebar",
    "base_url": "https://demo.test/",
    "commands_sent": 40,
    "states": [
        {"id": "S1", "path": "/", "title": "Demo", "regions": ["nav|Sidebar|nav", "main||main"],
         "headings": ["Home"], "dialogs": 0, "control_count": 3},
        {"id": "S2", "path": "/inbox", "title": "Demo", "regions": ["nav|Sidebar|nav", "main||main"],
         "headings": ["Inbox"], "dialogs": 0, "control_count": 3},
        {"id": "S3", "path": "/inbox", "title": "Demo",
         "regions": ["nav|Sidebar|nav", "main||main", "dialog|Search|dialog"],
         "headings": ["Inbox"], "dialogs": 1, "control_count": 5},
    ],
    "transitions": [
        {"n": 1, "action": "click", "target": "button 'Inbox'", "from_state": "S1",
         "to_state": "S2", "url_before": "https://demo.test/", "url_after": "https://demo.test/inbox",
         "dom": {"mutations": 6, "nodes_added": 3, "nodes_removed": 1},
         "regions_added": [], "regions_removed": [], "controls_added": [], "controls_removed": [],
         "requests": [{"method": "GET", "path": "/api/inbox", "status": 200, "type": "Fetch"}],
         "outcome": "navigated", "screenshot": "003-after.png", "applied": True},
        {"n": 2, "action": "click", "target": "button 'Search'", "from_state": "S2",
         "to_state": "S3", "url_before": "https://demo.test/inbox", "url_after": "https://demo.test/inbox",
         "dom": {"mutations": 2, "nodes_added": 2, "nodes_removed": 0},
         "regions_added": ["dialog|Search|dialog"], "regions_removed": [],
         "controls_added": ["textbox 'Search demo'", "button 'Run'"], "controls_removed": [],
         "requests": [], "outcome": "modal-opened", "screenshot": "005-after.png", "applied": True},
        {"n": 3, "action": "click", "target": "button 'Delete page'", "from_state": "S2",
         "to_state": "S2", "url_before": "https://demo.test/inbox", "url_after": "https://demo.test/inbox",
         "dom": {"mutations": 0}, "regions_added": [], "regions_removed": [],
         "controls_added": [], "controls_removed": [], "requests": [],
         "outcome": "no-visible-change", "applied": True},
        {"n": 4, "action": "click", "target": "button 'Ghost'", "applied": False,
         "error": "not found"},
    ],
    "screens": [
        {"id": "K0", "state_id": "S1", "url": "https://demo.test/", "path": "/",
         "title": "Demo", "regions": [{"key": "nav|Sidebar|nav", "role": "nav",
                                       "label": "Sidebar", "bbox": {"x": 0, "y": 0, "w": 240, "h": 900}},
                                      {"key": "main||main", "role": "main", "label": "",
                                       "bbox": {"x": 240, "y": 0, "w": 1200, "h": 900}}],
         "control_count": 3, "dialog_count": 0, "screenshot": "001-base.png"},
    ],
    "controls": [
        {"role": "button", "name": "Inbox", "owner": "nav|Sidebar|nav", "bbox": {"w": 200, "h": 32}},
        {"role": "button", "name": "Search", "owner": "nav|Sidebar|nav", "bbox": {"w": 200, "h": 32}},
        {"role": "button", "name": "New page", "owner": "nav|Sidebar|nav", "bbox": {"w": 200, "h": 32}},
        {"role": "link", "name": "Home", "owner": "main||main", "bbox": {"w": 90, "h": 20}},
    ],
    "endpoints": [
        {"method": "GET", "path": "/api/inbox", "host": "demo.test", "hits": 1,
         "statuses": [200], "types": ["Fetch"],
         "schema": {"items": ["array", {"id": "number", "title": "string", "unread": "bool"}]}},
        {"method": "GET", "path": "/static/app.js", "host": "demo.test", "hits": 1,
         "statuses": [200], "types": ["Script"], "schema": None},
        {"method": "GET", "path": "/", "host": "demo.test", "hits": 1,
         "statuses": [200], "types": ["Document"], "schema": None},
    ],
    "bodies": [
        {"method": "GET", "url": "https://demo.test/api/inbox",
         "schema": {"items": ["array", {"id": "number", "title": "string", "unread": "bool"}]}},
    ],
    "storage": {"initial": {"local": {}, "session": {}, "cookies": []},
                "final": {"local": {"demo.sidebar": {"json": True, "bytes": 20,
                                                     "shape": {"open": "bool", "width": "number"}}},
                          "session": {}, "cookies": ["sid"]}},
    "console": [], "errors": [],
    "limitations": ["one observed run"],
}

STATIC = {"lane": "electron", "target": "Demo.app",
          "analysis": {"package": {"name": "demo-app", "version": "1.0.0", "main": "main.js"}}}


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.spec = spec_mod.build(BEHAVIOR, STATIC, target="https://demo.test/")

    def test_schema_and_feature_name(self):
        self.assertEqual(self.spec["schema"], "coll/feature-spec@1")
        self.assertTrue(self.spec["feature"].startswith("Demo"))

    def test_components_derive_from_regions(self):
        names = [c["kind"] for c in self.spec["components"]]
        self.assertIn("sidebar / navigation", names)
        self.assertIn("main content", names)

    def test_sidebar_owns_its_controls(self):
        sidebar = next(c for c in self.spec["components"]
                       if c["kind"] == "sidebar / navigation")
        self.assertEqual(sidebar["control_count"], 3)
        self.assertIn("Inbox", [x["name"] for x in sidebar["controls"]])

    def test_transitions_skip_unapplied(self):
        self.assertEqual(len(self.spec["transitions"]), 3)
        self.assertNotIn("Ghost", json.dumps(self.spec["transitions"]))

    def test_destructive_marked_risky(self):
        risky = [t for t in self.spec["transitions"] if t["risky"]]
        self.assertEqual(len(risky), 1)
        self.assertIn("Delete", risky[0]["control"])

    def test_endpoints_exclude_assets_and_documents(self):
        paths = [e["path"] for e in self.spec["endpoints"]]
        self.assertEqual(paths, ["/api/inbox"])

    def test_entity_fields_flattened(self):
        inbox = next(e for e in self.spec["entities"] if e["name"] == "Inbox")
        self.assertEqual(inbox["fields"]["items"], "array<object>")
        self.assertEqual(inbox["fields"]["items[].id"], "number")

    def test_storage_entity_from_lifecycle(self):
        names = [e["name"] for e in self.spec["entities"]]
        self.assertIn("DemoSidebar", names)

    def test_build_plan_covers_ui_interactions_api(self):
        kinds = {t["kind"] for t in self.spec["build_plan"]}
        self.assertTrue({"ui", "interaction", "api", "data"} <= kinds)
        ids = [t["id"] for t in self.spec["build_plan"]]
        self.assertEqual(ids, ["B%d" % (i + 1) for i in range(len(ids))])

    def test_parity_checks_present_and_testable(self):
        self.assertGreaterEqual(len(self.spec["parity_checks"]), 3)
        self.assertTrue(any("Clicking **button 'Inbox'**" in p
                            for p in self.spec["parity_checks"]))

    def test_unknowns_call_out_risky_and_noop(self):
        text = " ".join(self.spec["unknowns"])
        self.assertIn("no visible change", text)
        self.assertIn("destructive", text)

    def test_markdown_has_all_sections(self):
        md = spec_mod.to_markdown(self.spec)
        for heading in ("## 1. Surfaces", "## 2. Static evidence",
                        "## 3. Interaction contract", "## 4. State model",
                        "## 5. Data model", "## 6. API surface", "## 7. Build plan",
                        "## 8. Parity checklist", "## 9. Unknowns & limits"):
            self.assertIn(heading, md)
        self.assertIn("stateDiagram-v2", md)

    def test_deterministic(self):
        again = spec_mod.build(BEHAVIOR, STATIC, target="https://demo.test/")
        self.assertEqual(json.dumps(self.spec, sort_keys=True),
                         json.dumps(again, sort_keys=True))


class TestStaticOnly(unittest.TestCase):
    def test_static_only_is_honest(self):
        sp = spec_mod.build(None, STATIC, target="Demo.app")
        self.assertEqual(sp["components"], [])
        self.assertTrue(any("no behavioural capture" in u for u in sp["unknowns"]))
        self.assertIn("_No behavioural capture", spec_mod.to_markdown(sp))


class TestWrite(unittest.TestCase):
    def test_write_creates_both_artifacts(self):
        import tempfile
        sp = spec_mod.build(BEHAVIOR, STATIC, target="https://demo.test/")
        d = tempfile.mkdtemp(prefix="coll-spec-")
        paths = spec_mod.write(sp, d)
        self.assertTrue(os.path.exists(paths["markdown"]))
        self.assertTrue(os.path.exists(paths["json"]))
        with open(paths["json"]) as f:
            self.assertEqual(json.load(f)["schema"], "coll/feature-spec@1")


if __name__ == "__main__":
    unittest.main()

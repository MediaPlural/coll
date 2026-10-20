"""End-to-end test — real Chrome, real CDP, real fixture app.

Skipped automatically when no Chromium-family browser is installed, so the suite
stays green on machines that only do static analysis. When it does run, it
exercises the whole chain: launch -> attach -> snapshot -> click -> network ->
JSON schema -> storage -> spec synthesis.

Run:  python3 -m unittest tests.test_live_capture -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import EvidenceBank, behavior, spec as spec_mod


def _browser_available() -> bool:
    return bool(behavior.chrome_binary())


@unittest.skipUnless(_browser_available(), "no Chromium-family browser installed")
class TestLiveCapture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import fixture_app
        cls.srv, cls.port = fixture_app.serve(0)
        cls.url = "http://127.0.0.1:%d/" % cls.port
        cls.out = tempfile.mkdtemp(prefix="coll-live-")
        cls.bank = EvidenceBank(cls.out)
        cls.steps = [
            {"action": "click", "text": "Inbox", "label": "open-inbox"},
            {"action": "click", "text": "New item", "label": "create-item"},
            {"action": "click", "text": "Open search", "label": "open-search"},
            {"action": "type", "selector": "input#q", "text": "roadmap"},
            {"action": "click", "text": "Run search", "label": "run-search"},
        ]
        cls.report = behavior.capture_web(cls.url, cls.bank, headless=True,
                                         steps=cls.steps, explore=False)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.out, ignore_errors=True)

    # -- capture basics ----------------------------------------------------
    def test_capture_succeeded(self):
        self.assertIsNone(self.report.get("error"), self.report.get("error"))
        self.assertEqual(self.report["lane"], "behavior")
        self.assertEqual(self.report["mode"], "launch-web")
        self.assertIn("Chrome", self.report["debuggee"]["browser"])

    def test_screen_snapshots_have_png_artifacts(self):
        shots = [s for s in self.report["screens"] if s.get("screenshot")]
        self.assertGreaterEqual(len(shots), 4)
        for s in shots[:3]:
            p = os.path.join(self.out, s["screenshot"])
            self.assertTrue(os.path.exists(p), p)
            with open(p, "rb") as f:
                self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")

    def test_states_clustered_across_routes(self):
        paths = [s["path"] for s in self.report["states"]]
        self.assertIn("/", paths)
        self.assertIn("/inbox", paths)
        self.assertGreaterEqual(len(self.report["states"]), 2)

    def test_transitions_record_effects(self):
        tr = self.report["transitions"]
        self.assertEqual(len(tr), 5)
        inbox = tr[0]
        self.assertEqual(inbox["outcome"], "navigated")
        self.assertEqual(inbox["to_state"], "S2")
        self.assertIn("/api/inbox", [r["path"] for r in inbox["requests"]])
        modal = next(t for t in tr if t["outcome"] == "modal-opened")
        self.assertTrue(modal["controls_added"])
        self.assertGreater(modal["dom"]["mutations"], 0)

    def test_unresolvable_target_is_reported_not_raised(self):
        # a flow step that cannot find its target must degrade, never crash
        rep = behavior.capture_web(
            self.url, self.bank, headless=True, explore=False,
            steps=[{"action": "click", "text": "No such control anywhere"}])
        self.assertIsNone(rep.get("error"))
        first = rep["transitions"][0]
        self.assertFalse(first["applied"])
        self.assertTrue(first.get("error"), "the miss must be recorded with a reason")

    # -- API + data model --------------------------------------------------
    def test_api_call_graph(self):
        eps = {(e["method"], e["path"]) for e in self.report["endpoints"]}
        self.assertIn(("GET", "/api/inbox"), eps)
        self.assertIn(("POST", "/api/items"), eps)
        self.assertIn(("GET", "/api/search"), eps)

    def test_json_response_schemas_observed(self):
        schemas = {b["url"].split("?")[0].split("/")[-1]: b["schema"]
                   for b in self.report["bodies"]}
        self.assertIn("inbox", schemas)
        items = schemas["inbox"]["items"]
        self.assertEqual(items[0], "array")
        self.assertEqual(items[1]["id"], "number")
        self.assertEqual(items[1]["unread"], "bool")

    def test_storage_written_by_the_app_is_seen(self):
        local = (self.report["storage"]["final"].get("local") or {})
        self.assertIn("fixture.items", local)
        self.assertEqual(local["fixture.items"]["shape"], "array[string]")

    def test_console_error_captured(self):
        # the fixture has no favicon; that 404 is real observed behaviour
        texts = " ".join(e.get("text", "") for e in self.report["errors"])
        self.assertIn("404", texts)

    # -- spec synthesis on live data ---------------------------------------
    def test_spec_builds_from_live_capture(self):
        sp = spec_mod.build(self.report, None, target=self.url)
        kinds = {c["kind"] for c in sp["components"]}
        self.assertTrue(any("navigation" in k for k in kinds), kinds)
        self.assertGreaterEqual(len(sp["build_plan"]), 5)
        self.assertTrue(any("/api/inbox" in p for p in sp["parity_checks"]))
        md = spec_mod.to_markdown(sp)
        self.assertIn("## 3. Interaction contract", md)
        self.assertIn("button 'Inbox'", md)
        paths = spec_mod.write(sp, self.out)
        self.assertTrue(os.path.exists(paths["markdown"]))
        with open(paths["json"]) as f:
            self.assertGreater(len(json.load(f)["transitions"]), 0)


@unittest.skipUnless(_browser_available(), "no Chromium-family browser installed")
class TestAttachPath(unittest.TestCase):
    def test_attach_to_running_debuggee(self):
        import fixture_app
        srv, port = fixture_app.serve(0)
        url = "http://127.0.0.1:%d/" % port
        handle = behavior.launch_chrome(url, headless=True)
        try:
            self.assertTrue(handle.get("ready"), handle.get("error"))
            out = tempfile.mkdtemp(prefix="coll-attach-")
            bank = EvidenceBank(out)
            rep = behavior.capture_attach(handle["port"], bank, target=url,
                                          explore=False, steps=None)
            self.assertIsNone(rep.get("error"))
            self.assertEqual(rep["mode"], "attach")
            self.assertTrue(rep["states"], "attach must snapshot the live page")
            shutil.rmtree(out, ignore_errors=True)
        finally:
            behavior._kill(handle["proc"])
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()

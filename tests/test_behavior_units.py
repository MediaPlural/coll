"""Unit tests — behaviour helpers (path templating, outcomes, candidate safety)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import behavior


class TestPathTemplate(unittest.TestCase):
    def test_folds_numeric_ids(self):
        self.assertEqual(
            behavior._path_template("https://x.dev/api/items/12345"), "/api/items/{id}")

    def test_folds_uuids(self):
        self.assertEqual(
            behavior._path_template("https://x.dev/api/notes/0f8fad5b-d9cb-469f-a165-70867728950e"),
            "/api/notes/{uuid}")

    def test_keeps_query_out(self):
        self.assertEqual(behavior._path_template("https://x.dev/api/search?q=roadmap"),
                         "/api/search")

    def test_relative_path(self):
        self.assertEqual(behavior._path_template("/api/inbox"), "/api/inbox")

    def test_empty(self):
        self.assertEqual(behavior._path_template(""), "/")


class TestOutcome(unittest.TestCase):
    def _rec(self, **kw):
        base = {"url_before": "https://x/", "url_after": "https://x/",
                "dom": {"mutations": 0}, "requests": [],
                "controls_added": [], "controls_removed": [],
                "regions_added": [], "regions_removed": []}
        base.update(kw)
        return base

    def test_navigated_wins(self):
        self.assertEqual(
            behavior._outcome(self._rec(url_after="https://x/inbox")), "navigated")

    def test_no_visible_change(self):
        self.assertEqual(behavior._outcome(self._rec()), "no-visible-change")

    def test_modal(self):
        self.assertEqual(
            behavior._outcome(self._rec(dom={"mutations": 3},
                                        regions_added=["dialog|Search|dialog"])),
            "modal-opened")

    def test_data_updated(self):
        self.assertEqual(
            behavior._outcome(self._rec(requests=[{"method": "GET"}])), "data-updated")

    def test_dom_updated(self):
        self.assertEqual(behavior._outcome(self._rec(dom={"mutations": 2})), "dom-updated")


class TestCandidateSafety(unittest.TestCase):
    SNAP = {"controls": [
        {"role": "button", "name": "Delete page", "owner": "nav|W|nav", "bbox": {"w": 80, "h": 30}},
        {"role": "button", "name": "New page", "owner": "nav|W|nav", "bbox": {"w": 80, "h": 30}},
        {"role": "textbox", "name": "Search", "owner": "nav|W|nav", "bbox": {"w": 80, "h": 30}, "editable": True},
        {"role": "button", "name": "New page", "owner": "nav|W|nav", "bbox": {"w": 80, "h": 30}},
        {"role": "link", "name": "Settings", "owner": "nav|W|nav", "bbox": {"w": 80, "h": 30}},
    ]}

    def test_destructive_controls_skipped_by_default(self):
        names = [c["name"] for c in behavior._candidates(self.SNAP, 10, False, ("navigation",))]
        self.assertNotIn("Delete page", names)

    def test_destructive_included_when_asked(self):
        names = [c["name"] for c in behavior._candidates(self.SNAP, 10, True, ("navigation",))]
        self.assertIn("Delete page", names)

    def test_editable_fields_not_auto_clicked(self):
        names = [c["name"] for c in behavior._candidates(self.SNAP, 10, False, ("navigation",))]
        self.assertNotIn("Search", names)

    def test_duplicates_collapsed(self):
        names = [c["name"] for c in behavior._candidates(self.SNAP, 10, False, ("navigation",))]
        self.assertEqual(names.count("New page"), 1)

    def test_limit_respected(self):
        self.assertLessEqual(len(behavior._candidates(self.SNAP, 1, False, ("navigation",))), 1)


class TestSchema(unittest.TestCase):
    def test_nested_array_shape(self):
        shape = behavior.infer_schema({"items": [{"id": 1, "tags": ["a"]}], "ok": True})
        self.assertEqual(shape["items"][0], "array")
        self.assertEqual(shape["items"][1]["id"], "number")
        self.assertEqual(shape["items"][1]["tags"][0], "array")
        self.assertEqual(shape["ok"], "bool")

    def test_empty_array_and_null(self):
        shape = behavior.infer_schema({"a": [], "b": None})
        self.assertEqual(shape["a"], "array")
        self.assertEqual(shape["b"], "null")

    def test_depth_guard(self):
        deep = current = {}
        for i in range(12):
            current["n"] = {}
            current = current["n"]
        out = behavior.infer_schema(deep)
        self.assertIn("...", repr(out))


class TestStateSig(unittest.TestCase):
    def test_same_regions_same_sig(self):
        a = {"path": "/x", "regions": [{"role": "nav", "label": ""}], "counts": {}}
        b = {"path": "/x", "regions": [{"role": "nav", "label": ""}], "counts": {}}
        self.assertEqual(behavior._state_sig(a), behavior._state_sig(b))

    def test_dialog_changes_sig(self):
        a = {"path": "/x", "regions": [], "counts": {"dialogs": 0}}
        b = {"path": "/x", "regions": [], "counts": {"dialogs": 1}}
        self.assertNotEqual(behavior._state_sig(a), behavior._state_sig(b))


class TestModifiers(unittest.TestCase):
    def test_modifier_bits(self):
        self.assertEqual(behavior._mods(["meta"]), 4)
        self.assertEqual(behavior._mods(["shift", "meta"]), 12)
        self.assertEqual(behavior._mods([]), 0)
        self.assertEqual(behavior._mods(["nonsense"]), 0)


if __name__ == "__main__":
    unittest.main()

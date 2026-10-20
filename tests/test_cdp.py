"""Unit tests — CDP session: id correlation, event backlog, error surfacing."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.cdp import CDPError, Session


class FakeWS:
    """Scripted transport. Replies are popped in order; None == 'no data'."""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.sent = []
        self.closed = False

    def send_text(self, text):
        self.sent.append(json.loads(text))

    def recv_text(self, timeout=None):
        return self.replies.pop(0) if self.replies else None

    def close(self):
        self.closed = True


def reply(mid, result=None, error=None):
    msg = {"id": mid}
    if error:
        msg["error"] = {"message": error}
    else:
        msg["result"] = result or {}
    return json.dumps(msg)


def event(method, params=None):
    return json.dumps({"method": method, "params": params or {}})


class TestSession(unittest.TestCase):
    def test_call_returns_result_and_skips_interleaved_events(self):
        transport = FakeWS([event("Page.loadEventFired"),
                            reply(1, {"frameTree": {}})])
        sess = Session(transport=transport, timeout=1)
        out = sess.call("Page.getFrameTree")
        self.assertEqual(out, {"frameTree": {}})
        self.assertEqual(transport.sent[0]["method"], "Page.getFrameTree")
        backlog = sess.drain()
        self.assertEqual(len(backlog), 1, "interleaved event must be kept")
        self.assertEqual(backlog[0]["method"], "Page.loadEventFired")

    def test_call_numbers_commands_monotonically(self):
        transport = FakeWS([reply(1), reply(2)])
        sess = Session(transport=transport, timeout=1)
        sess.call("A")
        sess.call("B")
        self.assertEqual([c["id"] for c in transport.sent], [1, 2])
        self.assertEqual([c["method"] for c in sess.commands], ["A", "B"])

    def test_error_raises_cdp_error(self):
        transport = FakeWS([reply(1, error="No target with given id")])
        sess = Session(transport=transport, timeout=1)
        with self.assertRaises(CDPError) as ctx:
            sess.call("Page.navigate", {"url": "chrome://nope"})
        self.assertIn("No target", str(ctx.exception))

    def test_drain_clears_backlog(self):
        transport = FakeWS([event("A"), reply(1), event("B")])
        sess = Session(transport=transport, timeout=1)
        sess.call("X")
        # the second event arrives only if the pump reads it
        sess.pump(0.05)
        self.assertEqual(len(sess.drain()), 1)
        self.assertEqual(sess.drain(), [])

    def test_pump_owns_its_events(self):
        transport = FakeWS([event("Network.requestWillBeSent")])
        sess = Session(transport=transport, timeout=0.2)
        got = sess.pump(0.05)
        self.assertEqual([e["method"] for e in got], ["Network.requestWillBeSent"])
        self.assertEqual(sess.drain(), [], "pump must not also queue its events")

    def test_evaluate_unwraps_value(self):
        transport = FakeWS([reply(1, {"result": {"value": 42}})])
        sess = Session(transport=transport, timeout=1)
        self.assertEqual(sess.evaluate("40 + 2"), 42)

    def test_evaluate_surfaces_js_exception(self):
        transport = FakeWS([reply(1, {"result": {},
                                      "exceptionDetails": {"text": "ReferenceError"}})])
        sess = Session(transport=transport, timeout=1)
        with self.assertRaises(CDPError):
            sess.evaluate("nope()")

    def test_timeout_without_reply(self):
        transport = FakeWS([])
        sess = Session(transport=transport, timeout=0.05)
        with self.assertRaises(TimeoutError):
            sess.call("Page.enable", timeout=0.05)


if __name__ == "__main__":
    unittest.main()

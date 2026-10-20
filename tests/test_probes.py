"""Unit tests — the in-page probes must be syntactically valid JavaScript.

The probes are the lane's eyes; a stray parenthesis there would only be caught
by a live browser run, which is a slow and flaky place to find a typo. Node is
used *only* as a parser here (never as a runtime dependency of the tool), and the
test skips cleanly when node is not installed.
"""
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import probes

NODE = shutil.which("node") or (
    "/Users/sharpe/.hermes/node/bin/node"
    if os.path.exists("/Users/sharpe/.hermes/node/bin/node") else "")


def _parses(expr: str) -> tuple:
    """Compile `expr` with node's parser. Returns (ok, stderr)."""
    script = "new Function('return (' + process.argv[1] + ');');"
    r = subprocess.run([NODE, "-e", script, expr], capture_output=True, text=True,
                       timeout=20)
    return r.returncode == 0, r.stderr


@unittest.skipUnless(NODE, "node not installed (parser check only)")
class TestProbeSyntax(unittest.TestCase):
    def test_named_probes_parse(self):
        for name in ("SNAPSHOT", "ARM", "DELTA", "STORAGE_SHAPES"):
            expr = getattr(probes, name)
            ok, err = _parses(expr)
            self.assertTrue(ok, "%s does not parse: %s" % (name, err.strip()[:300]))

    def test_parameterised_probes_parse(self):
        cases = {
            "target_probe": probes.target_probe("c3"),
            "resolve_probe": probes.resolve_probe({"text": 'New "page"'}),
            "type_probe": probes.type_probe("c1", 'quote " and \\ backslash'),
        }
        for name, expr in cases.items():
            ok, err = _parses(expr)
            self.assertTrue(ok, "%s does not parse: %s" % (name, err.strip()[:300]))

    def test_probe_embeds_values_safely(self):
        expr = probes.type_probe("c1", "</script><script>alert(1)</script>")
        self.assertIn('"</script><script>alert(1)</script>"', expr)
        ok, err = _parses(expr)
        self.assertTrue(ok, err[:200])


if __name__ == "__main__":
    unittest.main()

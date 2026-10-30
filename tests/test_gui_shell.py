"""The GUI shell's page and script agree, and the script calls only the endpoints the server is built to answer."""
import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "toolkit" / "gui" / "static"
# The server (gui_server.py) answers these, under automation's names; /api/builds is preconfig's own.
ENDPOINTS = {"/api/orchestrators", "/api/tasks", "/api/tasks/<id>/cancel", "/api/builds"}


class ShellTests(unittest.TestCase):
    def setUp(self):
        self.html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.js = (STATIC / "app.js").read_text(encoding="utf-8")

    def test_every_element_the_script_uses_is_on_the_page(self):
        ids = set(re.findall(r'\bid="([\w-]+)"', self.html))
        used = set(re.findall(r'\$\("([\w-]+)"\)', self.js))
        self.assertTrue(used)
        self.assertEqual(sorted(used - ids), [])

    def test_script_calls_only_the_known_endpoints(self):
        called = {re.sub(r'" \+ \w+(\.\w+)* \+ "', "<id>", path) for path in re.findall(r'"(/api/[^"]*(?:" \+ [\w.]+ \+ "[^"]*)*)"', self.js)}
        self.assertEqual(called, ENDPOINTS)

    def test_values_are_never_written_as_html(self):
        self.assertNotRegex(self.js, r"innerHTML|outerHTML|insertAdjacentHTML|document\.write")


if __name__ == "__main__":
    unittest.main()

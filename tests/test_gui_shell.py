"""The GUI shell's page and script agree, and the script calls only the endpoints the server is built to answer."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "toolkit" / "gui" / "static"
# The server (gui_server.py) answers these, under automation's names; /api/builds is preconfig's own. "/api/runs/" is
# followed by the run ID.
ENDPOINTS = {"/api/orchestrators", "/api/tasks", "/api/tasks/<id>/cancel", "/api/builds", "/api/runs", "/api/runs.csv", "/api/runs/", "/api/runs/<id>/rows.csv",
             "/api/tasks/<id>/events", "/api/tasks/<id>/result",  # pick.js and build.js follow their tasks (step 8), and so does the Inventory (step 10)
             "/api/builds/<id>/reference", "/api/builds/<id>/summary", "/api/builds/<id>/<id>/template", "/api/builds/<id>/<id>/csv"}
SCRIPTS = ("app.js", "tables.js", "runs.js", "pick.js", "build.js", "inventory.js")  # in the order index.html loads them


class ShellTests(unittest.TestCase):
    def setUp(self):
        self.html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.js = "\n".join((STATIC / name).read_text(encoding="utf-8") for name in SCRIPTS)

    def test_the_page_loads_every_script_in_order(self):
        self.assertEqual(re.findall(r'<script src="([\w.]+)"></script>', self.html), list(SCRIPTS))
        self.assertEqual(sorted(SCRIPTS), sorted(path.name for path in STATIC.glob("*.js")))  # no script file is left out

    def test_every_element_the_script_uses_is_on_the_page(self):
        ids = set(re.findall(r'\bid="([\w-]+)"', self.html))
        used = set(re.findall(r'\$\("([\w-]+)"\)', self.js))
        self.assertTrue(used)
        self.assertEqual(sorted(used - ids), [])

    def test_script_calls_only_the_known_endpoints(self):
        called = {re.sub(r'" \+ \w+(\.\w+)* \+ "', "<id>", path) for path in re.findall(r'"(/api/[^"]*(?:" \+ [\w.]+ \+ "[^"]*)*)"', self.js)}
        self.assertEqual(called, ENDPOINTS)

    @unittest.skipUnless(shutil.which("node"), "needs node to run the script's own function")  # guard-ok: zero new dependencies; runs wherever node is
    def test_download_links_go_only_to_this_servers_api(self):
        function = re.search(r"^function apiHref\(.*$", self.js, re.M).group(0)
        cases = ["/api/builds/a/b/template", "/api/x?y=1", "https://elsewhere.test/api/x", "//elsewhere.test/api/x",
                 "javascript:alert(1)", "/apix", "api/x", " /api/x", "", None, 5]
        script = function + "\nconsole.log(JSON.stringify(" + json.dumps(cases) + ".map(apiHref)));"
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), ["/api/builds/a/b/template", "/api/x?y=1"] + [""] * 9)

    def test_values_are_never_written_as_html(self):
        self.assertNotRegex(self.js, r"innerHTML|outerHTML|insertAdjacentHTML|document\.write")


if __name__ == "__main__":
    unittest.main()

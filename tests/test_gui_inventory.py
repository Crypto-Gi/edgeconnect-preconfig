"""Inventory (docs/gui/BUILD_PLAN.md step 10): the appliances from `list` and the preconfigs on the Orchestrator from `preconfigs`,
one shown as redacted YAML. A secret of a fixture preconfig, also one whose " breaks the YAML, never reaches a response: the list
JSON, the show JSON, the event stream or the task JSON. GET only. Offline: the fake Orchestrator of the engine tests."""
import copy
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_gui_server import ServerCase  # noqa: E402  guard-ok: the server over the shared build and fake Orchestrator
import test_golden  # noqa: E402  guard-ok: its APPLIANCES are what the fake Orchestrator lists
from test_golden import APPLIANCES, PRECONFIGS, SECRETS  # noqa: E402  guard-ok: the golden fixture's appliances, preconfigs and secrets
import engine  # noqa: E402  guard-ok: imported after the sys.path insert (test_gui_tasks sets it)

LEAKS = SECRETS + tuple(p["configData"] for p in PRECONFIGS if p.get("configData"))
SCRIPT = (Path(__file__).resolve().parents[1] / "toolkit" / "gui" / "static" / "inventory.js").read_text(encoding="utf-8")


class InventoryTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.orch.existing[:] = copy.deepcopy(PRECONFIGS)

    def context(self):
        return {"dotenv": str(self.on), "out_dir": self.tmp, "work_dir": self.tmp / "work"}

    def follow(self, body, *states):
        """Run a task over HTTP and read everything the page can read of it; returns the task, its result and its events."""
        task = self.post_task(body)
        self.wait(task, *states)
        events = self.events(task)
        self.request("GET", "/api/tasks")
        self.request("GET", "/api/tasks/" + task.id)
        result = json.loads(self.request("GET", "/api/tasks/{}/result".format(task.id))[1])["result"]
        return task, result, events

    def assertNothingLeaked(self):
        self.assertTrue(self.bodies)
        for leak in LEAKS:
            self.assertEqual([body for body in self.bodies if leak in body], [], leak)
        self.assertEqual({method for method, _ in self.orch.calls}, {"GET"}, self.orch.calls)

    def test_the_list_has_every_preconfig_and_never_its_config_data(self):
        task, result, events = self.follow({"action": "preconfigs", "orchestrator": "lab"}, "done", "failed")
        self.assertEqual(task.state, "done", task.message)
        rows = [r for r in result["outputs"] if r["kind"] == "preconfig"]
        self.assertEqual(rows[0], {"kind": "preconfig", "name": "br-1", "serial": "001BBC000001", "tag": "east", "last_change": 1761782400000, "id": 7})
        self.assertEqual([r["name"] for r in rows], ["br-1", "br-2", "br-3", "br-4"])
        self.assertTrue([e for e in events if "br-1" in e.get("line", "")], "the table is streamed")
        self.assertNotIn("configData", "".join(self.bodies))
        self.assertNothingLeaked()

    def test_show_is_the_redacted_yaml_in_the_result_and_the_stream(self):
        task, result, events = self.follow({"action": "preconfigs", "orchestrator": "lab", "show": "7"}, "done", "failed")
        self.assertEqual(task.state, "done", task.message)
        shown = [r for r in result["outputs"] if r["kind"] == "preconfig-yaml"]
        self.assertEqual([(r["name"], r["id"]) for r in shown], [("br-1", 7)])
        for path in ("applianceInfo.adminPassword", "ikeConfig.presharedKey", "nac.server[0].key"):
            self.assertIn("<SECRET:$.{}>".format(path), shown[0]["text"])
        self.assertIn("hostname: br-1", shown[0]["text"])
        self.assertTrue([e for e in events if "adminPassword: <SECRET:" in e.get("line", "")], events)
        self.assertNothingLeaked()

    def test_a_yaml_that_does_not_parse_shows_the_cli_message_never_its_text(self):
        # br-2's adminPassword is "Br0ken"Pw-66": its " breaks the YAML
        task, result, events = self.follow({"action": "preconfigs", "orchestrator": "lab", "show": "br-2"}, "done", "failed")
        self.assertEqual(task.state, "failed")
        self.assertEqual(task.message, "error: preconfig br-2 (id 8): its YAML does not parse (line 3, column 26); its text is not shown, as it may hold a secret")
        self.assertEqual([r for r in result["outputs"] if r["kind"] == "preconfig-yaml"], [])
        self.assertIn(task.message, [e.get("line") for e in events])
        self.assertNotIn("adminPassword", "".join(self.bodies))
        self.assertNothingLeaked()

    def test_the_appliances_are_the_rows_list_records(self):
        task, result, _ = self.follow({"action": "list", "orchestrator": "lab"}, "done", "failed")
        self.assertEqual(task.state, "done", task.message)
        self.assertEqual([r for r in result["outputs"] if r["kind"] == "appliance"],
                         [{"kind": "appliance", "hostname": "h1", "nepk": "9.NE", "model": "EC-V", "ecos": "9.7.1.0", "site": "S1", "reachable": "yes"},
                          {"kind": "appliance", "hostname": "h2", "nepk": "8.NE", "model": "EC-XS", "ecos": "9.6.2.1", "site": "S1", "reachable": "NO"}])
        self.assertEqual(task.message, "2 appliance(s), 1 reachable")
        found = engine.list_appliances("lab", "h2", **self.context()).outputs
        self.assertEqual([r["hostname"] for r in found if r["kind"] == "appliance"], ["h2"])

    def test_a_value_that_would_break_the_printed_table_stays_whole_in_its_row(self):
        for hostname in ("a\nb", "a\x85b", "a\rb", "x\n+---+\ny", "x | y", "wide 名前"):
            with self.subTest(hostname=hostname), mock.patch.object(test_golden, "APPLIANCES", [dict(APPLIANCES[0], hostName=hostname), APPLIANCES[1]]):
                result = engine.list_appliances("lab", **self.context())
                self.assertEqual((result.status, sorted(r["hostname"] for r in result.outputs if r["kind"] == "appliance")), ("done", sorted([hostname, "h2"])))

    def test_the_engine_runs_exactly_the_two_preconfigs_shapes(self):
        for argv in (["preconfigs", "--orchestrator", "lab"], ["preconfigs", "--orchestrator", "lab", "--show", "br-1"]):
            self.assertEqual(engine.run(argv, **self.context()).status, "done", argv)
        self.orch.calls[:] = []
        for argv in (["preconfigs"], ["preconfigs", "--orchestrator", "lab", "--show"], ["preconfigs", "--orch", "lab"],
                     ["preconfigs", "--orchestrator", "lab", "--search", "x"], ["preconfigs", "--show", "br-1", "--orchestrator", "lab"],
                     ["preconfigs", "--orchestrator", "lab", "--show", "br-1", "--show", "br-2"], ["preconfigs", "--orchestrator=lab"]):
            result = engine.run(argv, **self.context())
            self.assertEqual((result.status, result.exit_code), ("rejected", 2), argv)
            self.assertIn("refused, nothing sent", result.text)
        self.assertEqual(self.orch.calls, [])

    @unittest.skipUnless(shutil.which("node"), "needs node to run the script's own function")  # guard-ok: zero new dependencies; runs wherever node is
    def test_a_last_change_out_of_the_date_range_is_shown_as_sent(self):
        function = re.search(r"^function changed\(.*?^}$", SCRIPT, re.M | re.S).group(0)
        cases = [1761782400000, 1761782400, 8.64e15, 9e15, -9e15, None, "soon"]
        script = function + "\nconsole.log(JSON.stringify(" + json.dumps(cases) + ".map(changed)));"
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), ["2025-10-30 00:00 UTC", "2025-10-30 00:00 UTC", "+275760-09-13 00:00 UTC", "9000000000000000",
                                                     "-9000000000000000", "", "soon"])

    def test_the_script_names_no_global_the_browser_already_has(self):
        names = set(re.findall(r"^(?:async )?(?:function|const|let|class) (\w+)", SCRIPT, re.M))
        self.assertEqual(names & {"status", "name", "close", "open", "stop", "print", "focus", "blur", "find", "origin", "length", "event", "history", "screen"}, set())

    def test_a_preconfigs_task_needs_a_configured_orchestrator(self):
        for orchestrator in (None, "", "nope"):
            response, text = self.request("POST", "/api/tasks", {"action": "preconfigs", "orchestrator": orchestrator})
            self.assertEqual(response.status, 400, text)
        self.assertEqual(self.server.tasks.list(), [])


if __name__ == "__main__":
    unittest.main()

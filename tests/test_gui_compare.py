"""Compare with other sites (docs/gui/BUILD_PLAN.md step 8b) through the engine and the task layer: the engine runs only the compare
command lines its helper builds, a preview reads each site once and later runs reuse its captures (named by task, never by folder),
every picker row carries its lines, Save validates on the build's validator, and a compare waits for a build of a box it reads.
Offline: the synthetic compare captures of the golden tests and the engine tests' fake Orchestrator."""
import csv
import itertools
import json
import shutil
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_gui_tasks import Fixture  # noqa: E402  guard-ok: sets the paths and the run log off; the fake Orchestrator
from test_golden import COMPARE_CAPTURES  # noqa: E402  guard-ok: the synthetic base and partner captures
import capture  # noqa: E402  guard-ok: imported after the sys.path insert
import engine  # noqa: E402  guard-ok: imported after the sys.path insert
from gui_tasks import TaskError  # noqa: E402  guard-ok: imported after the sys.path insert

FIXTURE = {"branch-a": "base", "branch-b": "scalars", "3.ne": "scalars"}  # typed name -> capture in tests/fixtures/compare/captures
SCALARS = {"hostname": "branch-b", "nepk": "3.NE"}
SECRET = "Pp0e-Pw-91"  # a PPPoE password in the sample's capture: the CLI never reads it, and nothing the GUI shows may hold it


def pppoe(folder, hostname):
    """The capture of `folder` with a PPPoE session whose password is left in the capture, as if its redaction had missed it."""
    session = {"USERNAME": "user", "PASSWORD": SECRET, "ETH": "wan0", "LCP-FAILURE": 3, "LCP-INTERVAL": 20, "DNSTYPE": "AUTO",
               "CONNECT_TIMEOUT": 30, "CONNECT_POLL": 5, "SERVICENAME": "", "ACNAME": "", "DEFAULTROUTE": True}
    (folder / "capture" / "pppoe.live.json").write_text(json.dumps({"label": "live", "data": {"pppoe0": session}}), encoding="utf-8")
    index = json.loads((folder / "capture-index.json").read_text(encoding="utf-8"))
    (folder / "capture-index.json").write_text(json.dumps(index + [{"id": "pppoe", "label": "live", "status": "ok", "file": "capture/pppoe.live.json"}]), encoding="utf-8")
    data = json.loads((folder / "capture" / "appliance.live.json").read_text(encoding="utf-8"))
    data["data"]["hostName"] = hostname
    (folder / "capture" / "appliance.live.json").write_text(json.dumps(data), encoding="utf-8")


class CompareTests(Fixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.reads, cls.gate, numbers = [], threading.Event(), itertools.count(1)
        cls.gate.set()

        def reading(orchestrator, appliance, role, pause, out_root, *args, **kwargs):
            """A capture as capture() leaves it, from the fixture of the typed site; a reference read of branch-b waits on gate."""
            if role == "reference" and appliance == "branch-b":
                cls.gate.wait(10)
            cls.reads.append((role, appliance))
            folder = out_root / "{}-{}-{}".format(appliance, role, next(numbers))
            shutil.copytree(str(COMPARE_CAPTURES / FIXTURE[appliance.lower()]), str(folder))
            run = json.loads((folder / "run.json").read_text(encoding="utf-8"))
            run.update(stage="capture", role=role, orchestrator=orchestrator)
            if appliance.startswith("secret-"):
                pppoe(folder, appliance)
                run["appliance"]["hostName"] = appliance
            (folder / "run.json").write_text(json.dumps(run), encoding="utf-8")
            return folder
        cls.patches.append(mock.patch.object(capture, "capture", side_effect=reading))
        cls.patches[-1].start()
        tasks = cls.manager_for()
        task = tasks.create({"action": "build", "orchestrator": "lab", "hostname": "branch-a", "nepk": "1.NE"})
        while task.state not in ("done", "failed"):
            threading.Event().wait(0.02)
        assert task.state == "done", task.result.text
        cls.base = cls.tmp / "preconfig-output" / "branch-a"
        FIXTURE.update({"secret-a": "base", "secret-b": "scalars"})

    @classmethod
    def manager_for(cls):
        from gui_tasks import TaskManager
        return TaskManager(str(cls.on), str(cls.tmp), str(cls.tmp / "work"))

    def setUp(self):
        super().setUp()
        del self.reads[:]

    def compare(self, tasks, **request):
        return self.wait(tasks.create(dict({"action": "compare", "build": "branch-a", "sites": [SCALARS]}, **request)), "done", "failed")

    def test_the_engine_runs_only_the_compare_lines_its_helper_builds(self):
        folder, context = str(self.base), {"dotenv": str(self.on), "out_dir": self.tmp, "work_dir": self.tmp / "work"}
        for argv in (["compare", folder, "branch-b"],  # would ask
                     ["compare", folder, "branch-b", "--dry-run", "--yes"],
                     ["compare", folder, "a", "b", "c", "d", "e", "--dry-run"],
                     ["compare", folder, "--dry-run"],
                     ["compare", folder, "branch-b", "--captures", "--dry-run"],
                     ["compare", folder, "branch-b", "--captures", "x", "y", "--dry-run"],
                     ["compare", folder, "branch-b", "--cap", "x", "--dry-run"],
                     ["compare", folder, "branch-b", "--fix", "a", "--set", "b", "--dry-run"],
                     ["compare", folder, "branch-b", "--name", "--dry-run"],
                     ["compare", folder, "branch-b", "--dry-run", "--name", "n"]):
            with self.subTest(argv=argv):
                result = engine.run(argv, **context)
                self.assertEqual((result.status, result.exit_code), ("rejected", 2))
                self.assertIn("the GUI runs only the command lines it builds", result.text)
        self.assertEqual(self.reads, [])
        for kwargs in ({"dry_run": True}, {"name": "n", "settings": ["a"], "fixed": ["b"], "captures": ["x"], "dry_run": True}, {"name": "n"}):
            with mock.patch.object(engine, "run", side_effect=lambda argv, **context: argv):
                argv = engine.compare(folder, ["branch-b"], **kwargs)
            self.assertTrue(engine._built_here(argv, engine._line(argv)), argv)

    def test_a_preview_reads_each_site_once_and_later_runs_reuse_its_captures(self):
        tasks = self.manager()
        before = sorted(p.name for p in self.base.iterdir())
        first = self.compare(tasks)
        self.assertEqual(first.state, "done", first.result.text)
        self.assertEqual(self.reads, [("sample", "branch-b")])
        kinds = [r["kind"] for r in first.result.outputs]
        self.assertEqual((kinds[0], kinds.count("capture"), kinds[-1]), ("dry-run", 1, "picker"))
        rows = {r["path"]: r for r in first.result.outputs[-1]["rows"]}
        self.assertTrue(all("lines" in r for r in rows.values()))  # R9a: ticked or not, for the hover
        self.assertTrue(rows["localRoutes.routes[1].metric"]["on"])  # 50 at branch-a, 60 at branch-b: pre-ticked
        again = self.compare(tasks, captures_from=first.id, set=["applianceInfo.hostname"])
        self.assertIn("is always per site already", again.result.text)  # refused, and still no site read again
        later = self.compare(tasks, captures_from=again.id)
        self.assertEqual((later.state, self.reads), ("done", [("sample", "branch-b")]))
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), before)  # a preview writes no site folder
        self.assertEqual(self.orch.calls, [])  # nor calls the Orchestrator: the capture is read by capture(), here the fixture

    def test_save_reuses_the_captures_and_validates_on_the_builds_validator(self):
        tasks = self.manager()
        first = self.compare(tasks)
        saved = self.compare(tasks, captures_from=first.id, save=True, name="cmp")
        self.assertEqual(saved.state, "done", saved.result.text)
        self.assertEqual(self.reads, [("sample", "branch-b")])
        self.assertEqual([r for r in saved.result.outputs if r["kind"] == "saved"], [{"kind": "saved", "folder": str(self.base / "site-cmp"), "validated": True}])
        self.assertEqual([r["folder"] for r in saved.result.outputs if r["kind"] == "picker"], [str(self.base / "site-cmp")])  # what Save downloads from
        with (self.base / "site-cmp" / "sites.csv").open(newline="", encoding="utf-8") as handle:
            self.assertEqual([r["hostname"] for r in csv.DictReader(handle)], ["branch-a", "branch-b"])
        self.assertTrue(self.orch.calls)
        self.assertEqual({path for method, path in self.orch.calls if method == "POST"}, {"/gms/appliance/preconfiguration/validate"})
        # not a validator: refused before anything is sent
        none = self.manager(self.dotenv("cmp-none.env", self.on.read_text(encoding="utf-8").replace("EC_VALIDATORS=lab", "EC_VALIDATORS=other")))
        self.orch.calls[:] = []
        refused = self.compare(none, captures_from=None, save=True, name="cmp2")
        self.assertEqual(refused.state, "failed")
        self.assertIn("lab is not a validator Orchestrator", refused.result.text)
        self.assertEqual(self.orch.calls, [])

    def test_captures_come_only_from_an_earlier_compare_of_the_same_build_and_sites(self):
        tasks = self.manager()
        first = self.compare(tasks)
        listing = self.wait(tasks.create({"action": "list", "orchestrator": "lab"}), "done", "failed")
        for request, message in [
            ({"captures_from": "task-999"}, "captures_from must name an earlier compare"),
            ({"captures_from": listing.id}, "captures_from must name an earlier compare"),
            ({"captures_from": first.id, "sites": [{"hostname": "", "nepk": "3.NE"}]}, "captures_from must name an earlier compare"),
            ({"captures_from": ["/tmp"]}, "captures_from must name an earlier compare"),
            ({"sites": []}, "sites must list 1 to 4"),
            ({"sites": [SCALARS] * 5}, "sites must list 1 to 4"),
            ({"sites": ["branch-b"]}, "sites must list 1 to 4"),
            ({"sites": [{"hostname": "../x", "nepk": "3.NE"}]}, "is not an appliance hostname"),
            ({"sites": [{"hostname": "branch-b"}]}, "nepk is required"),
            ({"build": "../branch-a"}, "build must name a build folder"),
        ]:
            with self.subTest(request=request), self.assertRaisesRegex(TaskError, message):
                tasks.create(dict({"action": "compare", "build": "branch-a", "sites": [SCALARS]}, **request))

    def test_a_compare_waits_for_a_build_of_a_box_it_reads(self):
        tasks = self.manager()
        self.gate.clear()
        self.addCleanup(self.gate.set)
        build = tasks.create({"action": "build", "orchestrator": "lab", "hostname": "branch-b", "nepk": "3.NE"})
        self.wait(build, "running")
        waiting = tasks.create({"action": "compare", "build": "branch-a", "sites": [{"hostname": "", "nepk": "3.NE"}]})  # the same box, by nePk
        threading.Event().wait(0.3)
        self.assertEqual(waiting.state, "queued")
        self.gate.set()
        self.assertEqual(self.wait(waiting, "done", "failed").state, "done", waiting.result and waiting.result.text)
        self.wait(build, "done", "failed")

    def test_a_read_that_is_gone_is_never_reused(self):
        tasks = self.manager()
        first = self.compare(tasks)
        self.gate.clear()
        self.addCleanup(self.gate.set)
        build = tasks.create({"action": "build", "orchestrator": "lab", "hostname": "branch-b", "nepk": "3.NE"})
        self.wait(build, "running")
        late = tasks.create({"action": "compare", "build": "branch-a", "sites": [SCALARS], "captures_from": first.id})  # waits for the build
        shutil.rmtree([r["folder"] for r in first.result.outputs if r["kind"] == "capture"][0])  # the work folder was cleared meanwhile
        self.gate.set()
        self.assertEqual(self.wait(late, "done", "failed").state, "failed")
        self.wait(build, "done", "failed")
        for gone in (first, late):  # late recorded the dead folder as its capture: neither may hand it on
            with self.subTest(gone=gone.id), self.assertRaisesRegex(TaskError, "the earlier read is gone"):
                tasks.create({"action": "compare", "build": "branch-a", "sites": [SCALARS], "captures_from": gone.id})

    def test_a_compare_never_shows_a_secret_of_a_site(self):
        tasks = self.manager()
        built = self.wait(tasks.create({"action": "build", "orchestrator": "lab", "hostname": "secret-a", "nepk": "1.NE"}), "done", "failed")
        self.assertEqual(built.state, "done", built.result.text)
        site = {"hostname": "secret-b", "nepk": "3.NE"}
        first = self.compare(tasks, build="secret-a", sites=[site])
        saved = self.compare(tasks, build="secret-a", sites=[site], captures_from=first.id, save=True, name="sec")
        self.assertEqual(saved.state, "done", saved.result.text)
        rows = {r["path"]: r for r in first.result.outputs[-1]["rows"]}
        self.assertTrue(any("password" in path and row["on"] for path, row in rows.items()), list(rows))  # the secret row is there
        for task in (first, saved):
            shown = json.dumps([task.result.text, task.result.outputs, task.events])
            self.assertNotIn(SECRET, shown)
        self.assertNotIn(SECRET, (self.tmp / "preconfig-output" / "secret-a" / "site-sec" / "sites.csv").read_text(encoding="utf-8"))

    def test_a_build_made_as_Lab_shares_its_locks_with_tasks_for_lab(self):
        from gui_tasks import TaskManager, _identity
        folder = self.tmp / "made-as-Lab"
        folder.mkdir()
        (folder / "build.json").write_text(json.dumps({"orchestrator": "Lab", "appliance": {"hostName": "Branch-B", "nePk": "3.NE"}}), encoding="utf-8")
        self.assertIn(_identity("lab", "host", "branch-b"), TaskManager._keys(folder))


if __name__ == "__main__":
    unittest.main()

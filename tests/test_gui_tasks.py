"""GUI tasks (docs/gui/BUILD_PLAN.md step 5): each action is one engine run in its own thread with a state; reads run in parallel,
runs on one appliance queue, an upload holds its Orchestrator's lock and is sent only after the typed approval. Offline: the
fake Orchestrator of the engine tests."""
import csv
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "gui"))
os.environ["EDGECONNECT_REPORT_DIR"] = ""  # a command logs to reports/ (runlog.py); the run-log tests set their own folder
import capture  # noqa: E402  guard-ok: modules are imported after the sys.path insert, as in the other test modules
import cli  # noqa: E402  guard-ok: imported after the sys.path insert
import common  # noqa: E402  guard-ok: imported after the sys.path insert
import engine  # noqa: E402  guard-ok: imported after the sys.path insert
import upload  # noqa: E402  guard-ok: imported after the sys.path insert
import validate  # noqa: E402  guard-ok: imported after the sys.path insert
from gui_tasks import TaskError, TaskManager, dumps  # noqa: E402  guard-ok: imported after the sys.path insert
from test_golden import write_capture  # noqa: E402  guard-ok: the shared synthetic capture
from test_gui_engine import Echoing, Unreadable  # noqa: E402  guard-ok: the engine tests' fake Orchestrator and stdin

KEY = "fake-KEY-never-in-a-response-5e1d"
ENV = "orch_1=lab\norch_2=other\nlab_base_url=https://orch.example.com\nlab_api_key={0}\nother_base_url=https://other.example.com\nother_api_key={0}\nEC_VALIDATORS=lab\n".format(KEY)


class Fixture(unittest.TestCase):
    """One build (h1) and one site type (t) in a scratch output folder, with .env files beside it; a new TaskManager per test."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.orch = Echoing()
        environ = {k: v for k, v in os.environ.items() if k.upper() != "EC_VALIDATORS" and not k.lower().endswith("_gui_preconfig_uploads")}
        cls.patches = [mock.patch.dict(os.environ, environ, clear=True), mock.patch.object(sys, "stdin", Unreadable()),
                       mock.patch.object(capture, "capture", side_effect=lambda *a, **k: write_capture(common.work_dir() / "cap-{}".format(threading.get_ident()))),
                       mock.patch.object(cli, "client_for", return_value=cls.orch), mock.patch.object(validate, "client_for", return_value=cls.orch),
                       mock.patch.object(upload, "client_for", return_value=cls.orch)]
        for patch in cls.patches:
            patch.start()
        cls.on = cls.dotenv("on.env", ENV + "lab_gui_preconfig_uploads=on\n")
        context = {"dotenv": str(cls.on), "out_dir": cls.tmp, "work_dir": cls.tmp / "work"}
        assert engine.build("lab", "h1", **context).status == "done"
        cls.build = cls.tmp / "preconfig-output" / "h1"
        assert engine.variables(cls.build, "top10", name="t", yes=True, **context).status == "done"
        cls.site = cls.build / "site-t"
        with (cls.site / "sites.csv").open(newline="", encoding="utf-8") as handle:
            cls.example = next(csv.DictReader(handle))

    @classmethod
    def tearDownClass(cls):
        for patch in reversed(cls.patches):
            patch.stop()
        shutil.rmtree(str(cls.tmp), ignore_errors=True)

    @classmethod
    def dotenv(cls, name, text):
        path = cls.tmp / name
        path.write_text(text, encoding="utf-8")
        return path

    def setUp(self):
        self.orch.existing[:] = [{"id": 1, "name": "taken", "serialNum": None}]
        self.orch.calls[:] = []
        self.orch.together = None
        self.managers = []
        self.addCleanup(self.cancel_all)

    def cancel_all(self):
        for tasks in self.managers:  # a task left waiting for its approval would keep its thread for 10 minutes
            for task in tasks.list():
                try:
                    tasks.cancel(task.id)
                except TaskError:
                    pass

    def manager(self, dotenv=None, **options):
        tasks = TaskManager(str(dotenv or self.on), str(self.tmp), str(self.tmp / "work"), **options)
        self.managers.append(tasks)
        return tasks

    def wait(self, task, *states):
        deadline = time.monotonic() + 15
        while task.state not in states:
            self.assertLess(time.monotonic(), deadline, "task stayed {} ({})".format(task.state, task.message))
            time.sleep(0.01)
        return task

    def csv_text(self, rows):
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(self.example))
        writer.writeheader()
        writer.writerows({**self.example, "preconfig_name": name, "hostname": name, **cells} for name, cells in rows)
        return out.getvalue()

    def upload(self, tasks, *names, orchestrator="lab"):
        return tasks.create({"action": "upload", "orchestrator": orchestrator, "build": "h1", "site": "t", "csv_text": self.csv_text([(n, {}) for n in names])})


class TaskTests(Fixture):
    def test_an_upload_waits_for_the_typed_nickname_then_uploads(self):
        tasks = self.manager()
        task = self.wait(self.upload(tasks, "site-x", "site-y"), "awaiting_approval", "failed", "rejected")
        self.assertEqual((task.state, task.view()["result"]["counts"], self.orch.posts()), ("awaiting_approval", {"PLANNED": 2}, []))
        self.assertEqual(task.view()["result"]["approval"]["gates"][0]["expected"], "lab")
        self.assertEqual(task.view()["result"]["approval"]["counts"], {"ready": 2, "invalid": 0, "conflict": 0, "rows": 2})
        self.assertIsNotNone(task.view()["deadline"])
        tasks.approve(task.id, ["lab"])
        self.wait(task, "done", "failed", "rejected")
        self.assertEqual((task.state, task.message, task.view()["deadline"]), ("done", "2 CREATED", None))
        self.assertEqual([e["name"] for e in self.orch.existing[1:]], ["site-x", "site-y"])
        self.assertEqual([e["state"] for e in task.events if "state" in e], ["queued", "running", "awaiting_approval", "uploading", "done"])
        sent = [i for i, e in enumerate(task.events) if e.get("line", "").startswith("UPLOADED | site-x")]
        self.assertLess(sent[0], [i for i, e in enumerate(task.events) if e.get("state") == "done"][0])  # streamed before the end

    def test_the_upload_lock_queues_a_second_upload_and_reads_still_run(self):
        tasks = self.manager()
        first = self.wait(self.upload(tasks, "site-a"), "awaiting_approval")
        second = self.upload(tasks, "site-b")
        read = self.wait(tasks.create({"action": "validate_yaml", "orchestrator": "lab", "files": [(self.site / "rendered" / "h1_preconfig.yml").read_text(encoding="utf-8")]}),
                         "done", "failed")
        self.assertEqual((read.state, second.state), ("done", "queued"))
        tasks.cancel(first.id)
        self.wait(first, "cancelled")
        self.wait(second, "awaiting_approval")
        tasks.cancel(second.id)
        self.assertEqual((self.wait(second, "cancelled").state, self.orch.posts()), ("cancelled", []))
        # another Orchestrator has its own lock
        other = self.manager(self.dotenv("both.env", ENV + "lab_gui_preconfig_uploads=on\nother_gui_preconfig_uploads=on\n"))
        held = self.wait(self.upload(other, "site-c"), "awaiting_approval")
        elsewhere = other.create({"action": "upload", "orchestrator": "other", "template_text": (self.site / "t.jinja2").read_text(encoding="utf-8"),
                                  "csv_text": self.csv_text([("site-d", {})])})  # a template upload locks no site folder
        self.assertEqual(self.wait(elsewhere, "awaiting_approval", "failed", "rejected").state, "awaiting_approval")
        other.cancel(held.id)

    def test_reads_run_in_parallel(self):
        self.orch.together = threading.Barrier(2, timeout=10)  # each /validate waits until both runs are inside it
        tasks = self.manager()
        text = (self.site / "rendered" / "h1_preconfig.yml").read_text(encoding="utf-8")
        both = [tasks.create({"action": "validate_yaml", "orchestrator": "lab", "files": [text]}) for _ in range(2)]
        self.assertEqual([self.wait(task, "done", "failed").state for task in both], ["done", "done"], [t.result.text for t in both if t.result])

    def test_runs_on_one_appliance_queue_behind_each_other(self):
        release = threading.Event()
        self.orch.together = release  # the site check waits inside /validate
        tasks = self.manager()
        check = self.wait(tasks.create({"action": "validate", "build": "h1", "site": "t", "csv_text": self.csv_text([("q-1", {})])}), "running")
        preview = tasks.create({"action": "variables", "build": "h1", "pick": "top20"})
        listing = self.wait(tasks.create({"action": "list", "orchestrator": "lab"}), "done", "failed")
        time.sleep(0.3)
        self.assertEqual((check.state, preview.state, listing.state), ("running", "queued", "done"))
        release.set()
        self.assertEqual((self.wait(check, "done", "failed").state, self.wait(preview, "done", "failed").state), ("done", "done"))
        self.assertEqual([r["kind"] for r in preview.result.outputs], ["dry-run", "picker"])  # a preview writes nothing

    def test_an_upload_with_invalid_and_conflicting_rows_still_asks_for_the_ready_ones(self):
        tasks = self.manager()
        task = tasks.create({"action": "upload", "orchestrator": "lab", "build": "h1", "site": "t",
                             "csv_text": self.csv_text([("site-n", {}), ("taken", {}), ("site-bad", {"lan0_ip": "bad"})])})
        self.wait(task, "awaiting_approval", "failed", "rejected")
        self.assertEqual(task.view()["result"]["approval"]["counts"], {"ready": 1, "invalid": 1, "conflict": 1, "rows": 3})

    def builds(self, host_names, gate):
        """A TaskManager on a fresh output folder whose capture waits on gate, then reads the box named on the command line
        (--appliance) with the hostName host_names gives it ("" for a box without one)."""
        def slow(orchestrator, appliance, *args, **kwargs):
            gate.wait()
            folder = write_capture(Path(tempfile.mkdtemp(dir=str(self.tmp))) / "cap")  # a thread id can come again
            run = json.loads((folder / "run.json").read_text(encoding="utf-8"))
            run["appliance"]["hostName"] = {k.lower(): v for k, v in host_names.items()}[appliance.lower()]
            (folder / "run.json").write_text(json.dumps(run), encoding="utf-8")
            return folder
        patch = mock.patch.object(capture, "capture", side_effect=slow)
        patch.start()
        self.addCleanup(patch.stop)
        elsewhere = Path(tempfile.mkdtemp(dir=str(self.tmp)))  # a rebuild of h1 in the shared folder would move the shared build aside
        tasks = TaskManager(str(self.on), str(elsewhere), str(elsewhere / "work"))
        self.managers.append(tasks)
        return tasks

    def build_task(self, tasks, hostname, nepk):
        return tasks.create({"action": "build", "orchestrator": "lab", "hostname": hostname, "nepk": nepk})

    def second_queues(self, tasks, release, first, second):
        self.wait(first, "running")
        listing = self.wait(tasks.create({"action": "list", "orchestrator": "lab"}), "done", "failed")
        time.sleep(0.3)
        self.assertEqual((first.state, second.state, listing.state), ("running", "queued", "done"))
        release.set()
        self.assertEqual([self.wait(t, "done", "failed").state for t in (first, second)], ["done", "done"], second.result and second.result.text)

    @staticmethod
    def folder(task):
        return [Path(r["folder"]).name for r in task.result.outputs if r["kind"] == "build"]

    def test_one_box_sent_by_hostname_and_by_nepk_queues(self):
        release = threading.Event()
        tasks = self.builds({"h1": "h1", "9.NE": "h1"}, release)
        first, second = self.build_task(tasks, "h1", "9.NE"), self.build_task(tasks, "", "9.NE")
        self.second_queues(tasks, release, first, second)
        self.assertEqual((self.folder(first), self.folder(second)), (["h1"], ["h1"]))
        self.assertIn("reading 9.NE from lab", second.result.text)  # the CLI is given hostname or nepk, as the card sent them

    def test_a_box_without_a_hostname_builds_and_locks_on_its_nepk(self):
        release = threading.Event()
        tasks = self.builds({"9.NE": ""}, release)
        first, second = self.build_task(tasks, "", "9.NE"), self.build_task(tasks, " ", "9.ne")
        self.second_queues(tasks, release, first, second)
        self.assertEqual(self.folder(second), ["9_NE"])  # the CLI names the folder after the nePk
        self.assertIn("reading 9.NE from lab", first.result.text)
        for card in ({"hostname": " ", "nepk": None}, {"hostname": "h1"}, {"hostname": "h1", "nepk": " "}):
            with self.assertRaisesRegex(TaskError, "nepk is required, and hostname unless the appliance has none"):
                tasks.create(dict(card, action="build", orchestrator="lab"))

    def test_variables_on_a_build_folder_queue_behind_a_build_of_that_box(self):
        release = threading.Event()
        release.set()
        tasks = self.builds({"h1": "h1"}, release)
        self.assertEqual(self.wait(self.build_task(tasks, "h1", "9.NE"), "done", "failed").state, "done")
        self.assertEqual(tasks._keys(tasks.out_root / "h1"), ["appliance:h1", "appliance:lab:host:h1"])
        release.clear()
        build = self.build_task(tasks, "h1", "9.NE")
        self.second_queues(tasks, release, build, tasks.create({"action": "variables", "build": "h1"}))

    def test_two_different_boxes_build_in_parallel(self):
        tasks = self.builds({"h1": "h1", "h2": "h2"}, threading.Barrier(2, timeout=10))  # each capture waits until both are in it
        both = [self.build_task(tasks, "h1", "9.NE"), self.build_task(tasks, "h2", "10.NE")]
        self.assertEqual([self.wait(t, "done", "failed").state for t in both], ["done", "done"], [t.result.text for t in both if t.result])
        self.assertEqual([self.folder(t) for t in both], [["h1"], ["h2"]])

    # ------------------------------------------------------------ the approval gates, through the task layer

    def test_with_the_switch_off_nothing_is_sent(self):
        runs = self.tmp / "preconfig-output" / "runs"
        before = sorted(runs.iterdir()) if runs.is_dir() else []
        tasks = self.manager(self.dotenv("off.env", ENV + "other_gui_preconfig_uploads=on\n"))  # on for another Orchestrator only
        template = (self.site / "t.jinja2").read_text(encoding="utf-8")
        for request in ({"action": "upload", "orchestrator": "lab", "build": "h1", "site": "t", "csv_text": self.csv_text([("site-off", {})])},
                        {"action": "upload_yaml", "orchestrator": "lab", "files": ["applianceInfo:\n  hostname: h\n"]},
                        {"action": "make", "choice": "upload", "orchestrator": "lab", "template_text": template, "csv_text": self.csv_text([("site-off", {})])}):
            with self.assertRaises(TaskError) as refused:  # refused before anything runs: no task, no folder, nothing sent
                tasks.create(request)
            self.assertEqual((refused.exception.status, str(refused.exception)),
                             (403, "refused, nothing sent: GUI uploads are off for lab: set lab_gui_preconfig_uploads=on in .env to allow them"))
        self.assertEqual((tasks.list(), self.orch.calls, sorted(runs.iterdir()) if runs.is_dir() else []), ([], [], before))

    def test_a_wrong_or_other_nickname_is_rejected(self):
        tasks = self.manager()
        for typed in ("Lab", "other", ""):
            task = self.wait(self.upload(tasks, "site-w"), "awaiting_approval")
            tasks.approve(task.id, [typed])
            self.wait(task, "rejected", "done", "failed")
            self.assertEqual((task.state, task.message), ("rejected", "upload approval refused: the nickname typed is not lab; nothing was uploaded"), typed)
        self.assertEqual(self.orch.posts(), [])

    def test_an_unanswered_approval_expires(self):
        tasks = self.manager(approval_seconds=0.3)
        task = self.wait(self.upload(tasks, "site-e"), "awaiting_approval")
        self.assertEqual((self.wait(task, "expired").message, self.orch.posts()), ("no approval within 0.005 minutes; nothing was uploaded", []))
        with self.assertRaisesRegex(TaskError, "not awaiting approval"):
            tasks.approve(task.id, ["lab"])

    def test_an_approval_is_used_once(self):
        tasks = self.manager()
        task = self.wait(self.upload(tasks, "site-r"), "awaiting_approval")
        tasks.approve(task.id, ["lab"])
        with self.assertRaisesRegex(TaskError, "not awaiting approval"):
            tasks.approve(task.id, ["lab"])
        self.wait(task, "done")
        with self.assertRaises(TaskError) as refused:
            tasks.approve(task.id, ["lab"])
        self.assertEqual((refused.exception.status, len(self.orch.posts()), tasks.approvals._grants), (409, 1, {}))

    def test_an_orchestrator_that_changed_identity_since_the_check_gets_nothing(self):
        tasks = self.manager()
        task = self.wait(self.upload(tasks, "site-i"), "awaiting_approval")
        real = self.orch.get
        with mock.patch.object(self.orch, "get", lambda path, query=None: {"serialNumber": "OTHER-SERIAL"} if path == "/gmsserver/info" else real(path, query)):
            tasks.approve(task.id, ["lab"])
            self.wait(task, "rejected", "done", "failed")
        self.assertEqual((task.state, self.orch.posts()), ("rejected", []))
        self.assertIn("NOT APPROVED, nothing uploaded: the batch changed since it was approved", task.result.text)

    # ------------------------------------------------------------ requests, files and folders

    def test_imported_files_are_saved_0600_under_names_from_python(self):
        tasks = self.manager()
        template = (self.site / "t.jinja2").read_text(encoding="utf-8")
        task = self.wait(tasks.create({"action": "render", "template_text": template, "csv_text": self.csv_text([("r-1", {})]),
                                       "file_name": "../../outside.csv"}), "done", "failed")
        self.assertEqual((task.state, task.folder.parent), ("done", self.tmp / "preconfig-output" / "runs"))
        self.assertEqual(stat.S_IMODE(task.folder.stat().st_mode), 0o700)
        names = sorted(str(p.relative_to(task.folder)) for p in task.folder.rglob("*") if p.is_file())
        self.assertEqual(names, ["rendered/r-1.yml", "sites.csv", "template.jinja2"])
        self.assertEqual({stat.S_IMODE((task.folder / n).stat().st_mode) for n in names}, {0o600})
        self.assertEqual(task.result.files, [str(task.folder / "rendered" / "r-1.yml")])
        self.assertFalse((self.tmp / "outside.csv").exists())

    def test_site_folder_runs_write_only_into_their_own_folder_at_0600(self):
        def snapshot():
            return {str(p.relative_to(self.build)): (p.stat().st_mtime_ns, p.stat().st_size) for p in self.build.rglob("*")}
        before = snapshot()
        tasks = self.manager()
        check = self.wait(tasks.create({"action": "validate", "build": "h1", "site": "t", "csv_text": self.csv_text([("w-1", {}), ("w-2", {"site": "REJECT"})])}),
                          "done", "failed")
        sent = self.wait(self.upload(tasks, "w-3"), "awaiting_approval")
        tasks.approve(sent.id, ["lab"])
        self.wait(sent, "done", "failed", "rejected")
        self.assertEqual((check.state, check.result.counts, sent.state), ("failed", {"VALID": 1, "INVALID": 1}, "done"))
        self.assertEqual(snapshot(), before)  # nothing written, changed or removed under the build folder
        for task, written in ((check, ["rendered/w-1.yml", "sites.csv"]), (sent, ["rendered/w-3.yml", "sites.csv", "upload-report.json"])):
            files = sorted(str(p.relative_to(task.folder)) for p in task.folder.rglob("*") if p.is_file())
            self.assertEqual(files, written)
            self.assertEqual({stat.S_IMODE((task.folder / n).stat().st_mode) for n in files}, {0o600})
        self.assertEqual(check.result.files, [str(check.folder / "rendered" / "w-1.yml")])

    def test_a_site_folder_without_columns_json_is_refused(self):
        bare = self.build / "site-bare"
        bare.mkdir(exist_ok=True)
        (bare / "bare.jinja2").write_text("applianceInfo: {}\n", encoding="utf-8")
        tasks = self.manager()
        for request in ({"action": "validate", "build": "h1", "site": "bare", "csv_text": "x\n"},
                        {"action": "upload", "orchestrator": "lab", "build": "h1", "site": "bare", "csv_text": "x\n"}):
            with self.assertRaisesRegex(TaskError, r"site-bare has no template \(columns.json\): give a build folder or a site-<name> folder"):
                tasks.create(request)
        self.assertEqual((tasks.list(), self.orch.calls), ([], []))

    def test_a_hostname_or_nepk_that_is_not_a_plain_name_is_refused(self):
        tasks = self.manager()
        for card in ({"hostname": "../h1", "nepk": "9.NE"}, {"hostname": "h1", "nepk": "9/NE"}, {"hostname": ".h1", "nepk": "9.NE"},
                     {"hostname": "", "nepk": ".."}, {"hostname": "a..b", "nepk": "9.NE"}, {"hostname": "a\\b", "nepk": "9.NE"}, {"hostname": "h 1", "nepk": "9.NE"}):
            with self.assertRaisesRegex(TaskError, "is not an appliance hostname or nePk as the appliance list shows it"):
                tasks.create(dict(card, action="build", orchestrator="lab"))
        self.assertEqual((tasks.list(), self.orch.calls), ([], []))

    def test_once_the_gui_is_closing_no_upload_starts_and_one_sending_is_waited_for(self):
        tasks = self.manager()
        waiting = self.wait(self.upload(tasks, "site-z"), "awaiting_approval")
        release, real = threading.Event(), engine.upload_yaml

        def slow(*args, **kwargs):
            release.wait(10)
            return real(*args, **kwargs)
        text = (self.site / "rendered" / "h1_preconfig.yml").read_text(encoding="utf-8")
        with mock.patch.object(engine, "upload_yaml", slow):
            other = self.manager(self.dotenv("both-closing.env", ENV + "lab_gui_preconfig_uploads=on\nother_gui_preconfig_uploads=on\n"))
            sending = self.wait(other.create({"action": "upload_yaml", "orchestrator": "other", "files": [text]}), "uploading")
            closing = threading.Thread(target=other.close)
            closing.start()
            time.sleep(0.3)
            self.assertTrue(closing.is_alive())  # Ctrl+C waits for the upload that is sending
            late = other.create({"action": "upload_yaml", "orchestrator": "lab", "files": [text]})
            self.assertEqual((self.wait(late, "cancelled").message), "the GUI is closing; nothing was uploaded")
            release.set()
            closing.join(10)
            self.assertFalse(closing.is_alive())
            self.assertEqual(sending.state, "done")
        tasks.close()
        tasks.approve(waiting.id, ["lab"])  # approved after the GUI began closing
        self.assertEqual((self.wait(waiting, "cancelled").message, len(self.orch.posts())), ("the GUI is closing; nothing was uploaded", 1))

    def test_requests_are_checked_before_anything_runs(self):
        tasks = self.manager()
        runs = self.tmp / "preconfig-output" / "runs"
        before = sorted(runs.iterdir()) if runs.is_dir() else []
        for request, message in [
            ({"action": "apply"}, "unknown action"),
            ({"action": "list", "orchestrator": "nope"}, "unknown Orchestrator 'nope'; configured: lab, other"),
            ({"action": "list"}, "unknown Orchestrator ''"),
            ({"action": "variables", "build": "../h1"}, "build must name a build folder"),
            ({"action": "variables", "build": "h1.previous-1"}, "build must name a build folder"),
            ({"action": "variables", "build": "missing"}, "there is no build folder"),
            ({"action": "variables", "build": "h1", "pick": "top5"}, "pick must be"),
            ({"action": "variables", "build": "h1", "set": "a,b"}, "set must list"),
            ({"action": "validate", "build": "h1", "site": "../t", "csv_text": "x"}, "site must name a site folder"),
            ({"action": "validate", "build": "h1", "site": "t"}, "csv_text is required"),
            ({"action": "upload", "orchestrator": "lab", "template_text": "x"}, "csv_text is required"),
            ({"action": "validate_yaml", "orchestrator": "lab", "files": "x"}, "files must list"),
            ({"action": "build", "orchestrator": "lab", "hostname": "h1", "nepk": "9.NE", "validate_on": "nope"}, "unknown Orchestrator 'nope'"),
        ]:
            with self.assertRaisesRegex(TaskError, message):
                tasks.create(request)
        self.assertEqual((tasks.list(), self.orch.calls), ([], []))
        self.assertEqual(sorted(runs.iterdir()) if runs.is_dir() else [], before)  # a refused request leaves no folder

    def test_earlier_builds_list_their_site_types_and_skip_unfinished_folders(self):
        out = self.tmp / "preconfig-output"
        for name in (".building-h2", ".incomplete-h3", "h1.previous-20261030T000000Z"):
            (out / name).mkdir(exist_ok=True)
            (out / name / "build.json").write_text("{}", encoding="utf-8")
        tasks = self.manager()
        builds = tasks.builds()
        self.assertEqual(builds["folder"], str(out))
        self.assertEqual([(b["appliance"], b["orchestrator"], bool(b["built"])) for b in builds["builds"]], [("h1", "lab", True)])
        site = [s for s in builds["builds"][0]["site_types"] if s["name"] == "t"][0]
        self.assertEqual(site, {"name": "t", "template_url": "/api/builds/h1/t/template", "csv_url": "/api/builds/h1/t/csv"})
        self.assertEqual(tasks.site_file("h1", "t", "template"), self.site / "t.jinja2")
        self.assertEqual(tasks.site_file("h1", "t", "csv").parent, self.site)
        for args in (("h1", "../t", "csv"), ("h1", "t", "columns"), ("..", "t", "csv"), ("h1", "missing", "csv")):
            with self.assertRaises(TaskError):
                tasks.site_file(*args)

    def test_a_task_view_is_json_and_names_its_state(self):
        tasks = self.manager()
        task = self.wait(tasks.create({"action": "list", "orchestrator": "lab"}), "done", "failed")
        view = json.loads(dumps(task.view()))
        self.assertEqual((view["action"], view["state"], view["orchestrator"], view["result"]["status"]), ("list", "done", "lab", "done"))
        self.assertIn("h1", task.full_result()["text"])


if __name__ == "__main__":
    unittest.main()

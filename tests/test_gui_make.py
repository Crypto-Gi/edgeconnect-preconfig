"""Make the preconfig files and check or upload existing YAML files (docs/gui/BUILD_PLAN.md step 9): the GUI writes the same
bytes as the CLI for each choice, a template that matches no saved site folder still renders and says the per-cell checks did
not run, every upload gate holds (upload --yaml is one button, D9), and a secret CSV cell is in no response. Offline: the fake
Orchestrator of the engine tests."""
import copy
import csv
import http.client
import io
import json
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_gui_tasks  # noqa: E402,F401  guard-ok: sets the paths and the run log off, as the other GUI tests
from test_gui_server import ServerCase  # noqa: E402  guard-ok: the server tests' client
from test_gui_tasks import Fixture  # noqa: E402  guard-ok: the shared build, site type and fake Orchestrator
import cli  # noqa: E402  guard-ok: imported after the sys.path insert
import common  # noqa: E402  guard-ok: imported after the sys.path insert
from gui_tasks import TaskError  # noqa: E402  guard-ok: imported after the sys.path insert

EDITED = "\n# edited by hand\n"  # a template that is no longer byte-identical to the saved one


# What --substitute reads on the validator (synth.inventory): one object of each kind to map names onto
INVENTORY = {"/gms/interfaceLabels": {"lan": {"1": {"name": "LAN1", "active": True}}, "wan": {"1": {"name": "WAN1", "active": True}}},
             "/gms/group": [{"name": "G1", "subType": 1}], "/gms/overlays/config": [{"name": "O1"}], "/template/templateGroups": [{"name": "T1"}],
             "/vrf/config/segments": {"0": {"name": "Default"}}, "/zones": {"0": {"name": "Default"}}, "/cluster/profiles": []}


def with_inventory(case):
    real = case.orch.get
    patch = mock.patch.object(case.orch, "get", lambda path, query=None: copy.deepcopy(INVENTORY[path]) if path in INVENTORY else real(path, query))
    patch.start()
    case.addCleanup(patch.stop)


class MakeCase(Fixture):
    def setUp(self):
        super().setUp()
        self.template = (self.site / "t.jinja2").read_text(encoding="utf-8")
        with_inventory(self)

    def make(self, tasks, choice, rows, template=None, **extra):
        task = tasks.create(dict({"action": "make", "orchestrator": "lab", "choice": choice, "template_text": template or self.template,
                                  "csv_text": self.csv_text(rows)}, **extra))
        self.wait(task, "awaiting_approval", "done", "failed", "rejected")
        if task.state == "awaiting_approval":
            tasks.approve(task.id, ["lab"])
            self.wait(task, "done", "failed", "rejected")
        return task

    def cli(self, argv):
        """The same command in the terminal: cli.main with only the .env and folders of a run, its text kept."""
        out = io.StringIO()
        with common.running(common.RunContext(dotenv=str(self.on), out_dir=self.tmp / "cli", work_dir=self.tmp / "work")), common.captured_output(out):
            code = cli.main([str(a) for a in argv])
        return code, out.getvalue()

    @staticmethod
    def files(paths):
        return {Path(p).name: Path(p).read_bytes() for p in paths}

    def written(self, folder):
        return self.files(sorted((folder / "rendered").glob("*.yml")))


class MakeTests(MakeCase):
    ROWS = [("m-1", {}), ("m-2", {}), ("m-bad", {"lan0_ip": "bad"}), ("m-rej", {"site": "REJECT"}), ("taken", {})]

    def cli_copy(self):
        """The build folder copied outside preconfig-output, so the CLI's own validate and upload write their rendered/ there."""
        copy = self.tmp / "cli" / "h1"
        shutil.rmtree(str(copy), ignore_errors=True)
        shutil.copytree(str(self.build), str(copy))
        shutil.rmtree(str(copy / "site-t" / "rendered"), ignore_errors=True)
        return copy / "site-t"

    def test_each_choice_writes_the_cli_files_byte_for_byte(self):
        sheet, template = self.tmp / "cli-sites.csv", self.tmp / "cli-own.jinja2"
        sheet.write_bytes(self.csv_text(self.ROWS).encode("utf-8"))
        template.write_bytes((self.template + EDITED).encode("utf-8"))
        for matched in (True, False):
            for choice in ("create", "validate", "upload"):
                with self.subTest(matched=matched, choice=choice):
                    self.orch.existing[:] = [{"id": 1, "name": "taken", "serialNum": None}]
                    task = self.make(self.manager(), choice, self.ROWS, None if matched else self.template + EDITED)
                    self.assertEqual("every cell was checked" in task.note, matched, task.note)
                    gui = self.files(task.result.files)
                    self.orch.existing[:] = [{"id": 1, "name": "taken", "serialNum": None}]
                    out = self.tmp / "cli-out-{}-{}".format(matched, choice)
                    shutil.rmtree(str(out), ignore_errors=True)
                    if matched:
                        site = self.cli_copy()
                        argv = {"create": ["render", "--site-dir", site, "--csv", sheet, "--out", out / "rendered"],
                                "validate": ["validate", site, "--csv", sheet],
                                "upload": ["upload", site, "--csv", sheet, "--orchestrator", "lab", "--approve"]}[choice]
                        folder = out if choice == "create" else site
                    else:
                        argv = {"create": ["render", "--template", template, "--csv", sheet, "--out", out / "rendered"],
                                "validate": ["render", "--template", template, "--csv", sheet, "--out", out / "rendered", "--validate", "lab", "--substitute"],
                                "upload": ["upload", "--template", template, "--csv", sheet, "--out", out, "--orchestrator", "lab", "--approve"]}[choice]
                        folder = out
                    code, text = self.cli(argv)
                    self.assertEqual(gui, self.written(folder), text)
                    self.assertTrue(gui)
                    self.assertEqual(task.result.exit_code, code, task.result.text)
                    if choice == "upload":
                        self.assertEqual(task.state, "done" if code == 0 else "failed")
                        self.assertIn("CREATED", task.result.counts)
        # what each run did differently: the matched template's per-cell check, /validate, and the upload
        self.assertEqual(sorted(gui), ["m-1.yml", "m-2.yml", "m-bad.yml", "taken.yml"])  # unmatched upload: lan0_ip=bad is not caught

    def test_an_unmatched_template_renders_and_says_the_cell_checks_did_not_run(self):
        out = self.tmp / "preconfig-output"
        older = out / "h1.previous-20991231T000000Z"  # an earlier build and an earlier site type holding the edited template: never matched
        shutil.copytree(str(self.build), str(older))
        shutil.copytree(str(self.site), str(self.build / "site-t.previous-20991231T000000Z"))
        self.addCleanup(shutil.rmtree, str(older), True)
        self.addCleanup(shutil.rmtree, str(self.build / "site-t.previous-20991231T000000Z"), True)
        for folder in (older / "site-t", self.build / "site-t.previous-20991231T000000Z"):
            (folder / "t.jinja2").write_text(self.template + EDITED, encoding="utf-8")
        tasks = self.manager()
        own = self.make(tasks, "create", [("u-1", {"lan0_ip": "bad"})], self.template + EDITED)
        self.assertEqual((own.state, own.result.counts), ("done", {"VALID": 1}))
        self.assertIn("matches no site folder saved in {}, so the per-cell checks".format(out), own.note)
        self.assertIn("did not run", own.view()["note"])
        saved = self.make(tasks, "create", [("u-1", {"lan0_ip": "bad"})])
        self.assertEqual((saved.state, saved.result.counts), ("failed", {"INVALID": 1}))
        self.assertEqual(saved.note, "The template is the one saved in h1/site-t: every cell was checked against its columns.json.")
        table = tasks.rows(saved.id, {"result": "invalid"})
        self.assertEqual([(r["item"], r["result"], r["column"], r["value"]) for r in table["rows"]], [("u-1", "invalid", "lan0_ip", "bad")])
        self.assertIn("lan0_ip", table["rows"][0]["fix"])

    def test_a_saved_template_replaced_while_the_run_waits_is_not_used(self):
        """A rebuild of the box that holds its lock may replace site-t while a make task waits: the task then renders the person's
        own template, never the new saved one, and says so."""
        saved = self.site / "t.jinja2"
        self.addCleanup(saved.write_bytes, saved.read_bytes())
        for choice in ("create", "upload"):
            with self.subTest(choice):
                saved.write_bytes(self.template.encode("utf-8"))
                tasks = self.manager()
                lock = tasks._locks.setdefault("appliance:h1", threading.Lock())
                lock.acquire()  # a run on h1 holds the appliance lock
                task = tasks.create({"action": "make", "orchestrator": "lab", "choice": choice, "template_text": self.template,
                                     "csv_text": self.csv_text([("swap-" + choice, {})])})
                time.sleep(0.3)
                self.assertEqual(task.state, "queued")
                saved.write_bytes((self.template + "\n# replaced by a rebuild\n").encode("utf-8"))
                lock.release()
                self.wait(task, "awaiting_approval", "done", "failed", "rejected")
                if task.state == "awaiting_approval":
                    tasks.approve(task.id, ["lab"])
                    self.wait(task, "done", "failed", "rejected")
                self.assertEqual(task.state, "done", task.result.text)
                for path in task.result.files:
                    self.assertNotIn("replaced by a rebuild", Path(path).read_text(encoding="utf-8"))
                self.assertIn("changed while this run waited", task.note)
                self.assertIn("did not run", task.note)

    def test_the_row_table_is_paged_and_filtered_by_python(self):
        tasks = self.manager()
        task = self.make(tasks, "validate", [("p-{}".format(n), {}) for n in range(30)] + [("p-bad", {"lan0_ip": "bad"}), ("p-rej", {"site": "REJECT"})])
        page = tasks.rows(task.id, {"size": "25", "page": "2"})
        self.assertEqual((page["total"], page["matching"], page["pages"], len(page["rows"]), page["facets"]["result"]), (32, 32, 2, 7, ["invalid", "written"]))
        problems = tasks.rows(task.id, {"problems": "1"})
        self.assertEqual(sorted(r["item"] for r in problems["rows"]), ["p-bad", "p-rej"])
        self.assertIn("Orchestrator /validate rejected", [r for r in problems["rows"] if r["item"] == "p-rej"][0]["fix"])
        self.assertEqual(tasks.rows(task.id, {"q": "p-29"})["matching"], 1)

    def test_the_upload_shows_n_of_m_and_needs_the_nickname(self):
        tasks = self.manager()
        rows = [("n-1", {}), ("n-2", {}), ("taken", {}), ("n-bad", {"lan0_ip": "bad"})]
        task = tasks.create({"action": "make", "orchestrator": "lab", "choice": "upload", "template_text": self.template, "csv_text": self.csv_text(rows)})
        self.wait(task, "awaiting_approval")
        self.assertEqual((task.view()["approval"]["counts"], task.view()["approval"]["gates"][0]["expected"], self.orch.posts()), ({"ready": 2, "rows": 4}, "lab", []))
        self.assertEqual(sorted(Path(f).name for f in task.result.files), ["n-1.yml", "n-2.yml", "taken.yml"])  # downloadable before the upload
        tasks.approve(task.id, ["LAB"])
        self.assertEqual((self.wait(task, "rejected").message, self.orch.posts()), ("upload approval refused: the nickname typed is not lab; nothing was uploaded", []))

    def test_requests_are_refused_before_anything_runs(self):
        tasks = self.manager()
        for request, message in (({"choice": "apply"}, "choice must be create, validate or upload"),
                                 ({"choice": "create", "template_text": ""}, "template_text is required"),
                                 ({"choice": "create", "csv_text": None}, "csv_text is required"),
                                 ({"choice": "validate", "validate_on": "other"}, r"/validate runs only on a validator \(EC_VALIDATORS: lab\)"),
                                 ({"choice": "validate", "template_text": self.template + EDITED, "orchestrator": "other"}, "choose one to validate on"),
                                 ({"choice": "upload", "orchestrator": ""}, "unknown Orchestrator ''")):
            with self.subTest(message), self.assertRaisesRegex(TaskError, message):
                tasks.create(dict({"action": "make", "orchestrator": "lab", "template_text": self.template, "csv_text": self.csv_text([("x", {})])}, **request))
        self.assertEqual((tasks.list(), self.orch.calls), ([], []))

    def test_downloads_hold_only_the_files_this_run_wrote(self):
        tasks = self.manager()
        task = self.make(tasks, "create", [("d-1", {}), ("d-2", {}), ("d-bad", {"lan0_ip": "bad"})])
        (task.folder / "rendered" / "stray.yml").write_text("older: file\n", encoding="utf-8")
        names = zipfile.ZipFile(io.BytesIO(tasks.written_zip(task.id))).namelist()
        self.assertEqual(names, ["d-1.yml", "d-2.yml"])
        self.assertEqual(tasks.written_file(task.id, "d-1.yml").read_bytes(), (task.folder / "rendered" / "d-1.yml").read_bytes())
        for name in ("stray.yml", "sites.csv", "template.jinja2", "../d-1.yml", ""):
            with self.subTest(name), self.assertRaises(TaskError):
                tasks.written_file(task.id, name)


class YamlUploadTests(MakeCase):
    """upload --yaml in the GUI is one button (D9, decision 0017): no typed nickname, but only where uploads are switched on."""

    def setUp(self):
        super().setUp()
        self.text = (self.site / "rendered" / "h1_preconfig.yml").read_text(encoding="utf-8")

    def upload(self, tasks, orchestrator="lab", **extra):
        return tasks.create(dict({"action": "upload_yaml", "orchestrator": orchestrator, "files": [self.text]}, **extra))

    def test_one_button_uploads_only_the_chosen_file(self):
        tasks = self.manager()
        task = self.wait(self.upload(tasks, name="y-ok", serial=""), "done", "failed", "rejected", "awaiting_approval")
        self.assertEqual((task.state, task.result.counts, task.view()["approval"]), ("done", {"CREATED": 1}, None), task.result.text)
        self.assertEqual([e["state"] for e in task.events if "state" in e], ["queued", "uploading", "done"])
        self.assertEqual([e["name"] for e in self.orch.existing[1:]], ["y-ok"])  # the one file, under the name given
        row = tasks.rows(task.id, {})["rows"][0]
        self.assertEqual((row["item"], row["result"], row["file"]), ("y-ok", "uploaded", ""))  # it wrote no file: none is offered

    def test_only_an_orchestrator_with_uploads_on_gets_the_file(self):
        tasks = self.manager(self.dotenv("other-only.env", test_gui_tasks.ENV + "other_gui_preconfig_uploads=on\n"))
        with self.assertRaises(TaskError) as refused:
            self.upload(tasks, "lab", name="y-lab")
        self.assertEqual((refused.exception.status, tasks.list(), self.orch.calls), (403, [], []))
        self.assertIn("GUI uploads are off for lab", str(refused.exception))
        task = self.wait(self.upload(tasks, "other", name="y-other"), "done", "failed", "rejected")
        self.assertEqual((task.state, task.orchestrator, [e["name"] for e in self.orch.existing[1:]]), ("done", "other", ["y-other"]))


class SecretTests(ServerCase):
    """A CSV with a secret column, through the server: its values are in no JSON body, event, row table, zip listing or error."""

    SECRETS = ("Pa55-Hidden-Word", 'Pa"ss-Br0ken-77', "Rej3ct-Hidden-Value")

    def setUp(self):
        super().setUp()
        with_inventory(self)

    def test_a_secret_cell_is_shown_nowhere(self):
        template = "applianceInfo:\n  hostname: {{ data['hostname'] }}\n  site: {{ data['site'] }}\n  contact: \"{{ data['admin_password'] }}\"\n"
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerows([["preconfig_name", "hostname", "site", "admin_password"], ["s-1", "s-1", "S1", self.SECRETS[0]],
                          ["s-2", "s-2", "S1", self.SECRETS[1]], ["s-3", "s-3", "REJECT", self.SECRETS[2]]])
        streams = []
        for choice in ("create", "validate"):
            task = self.post_task({"action": "make", "orchestrator": "lab", "choice": choice, "template_text": template, "csv_text": out.getvalue()})
            streams.append(json.dumps(self.events(task)))
            self.assertEqual(task.result.counts, {"create": {"VALID": 2, "INVALID": 1}, "validate": {"VALID": 1, "INVALID": 2}}[choice])
            self.assertIn("did not run", task.note)  # no saved folder: secret-like headers mark the secret column
            for path in ("/api/tasks/{}", "/api/tasks/{}/result", "/api/tasks/{}/rows", "/api/tasks/{}/rows?problems=1", "/api/tasks", "/api/tasks/{}/file?name=nope.yml"):
                self.request("GET", path.format(task.id))
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
            connection.request("GET", "/api/tasks/{}/files.zip".format(task.id), headers={"Host": self.host, "Cookie": self.cookie})
            response = connection.getresponse()
            names = zipfile.ZipFile(io.BytesIO(response.read())).namelist()
            connection.close()
            self.assertEqual((response.status, response.headers["Content-Type"], names), (200, "application/zip", ["s-1.yml", "s-3.yml"] if choice == "create" else ["s-1.yml"]))
            if choice == "validate":  # /validate's error quotes the rejected document, secret and all
                self.assertIn("<SECRET:admin_password>", json.dumps(task.full_result()))
        self.bodies.append(self.request("POST", "/api/tasks", {"action": "make", "orchestrator": "lab", "choice": "nope", "template_text": template, "csv_text": out.getvalue()})[1])
        shown = "\n".join(self.bodies + streams)
        for secret in self.SECRETS:
            for part in (secret, secret.split('"')[-1]):
                self.assertNotIn(part, shown)


if __name__ == "__main__":
    import unittest
    unittest.main()

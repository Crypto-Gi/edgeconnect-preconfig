"""The GUI's engine (docs/gui/BUILD_PLAN.md step 3): one CLI command line through cli.main in this process, offline against the fake
Orchestrator, returned as a Result; the upload gates refuse before anything is sent."""
import base64
import csv
import dataclasses
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "gui"))
import capture  # noqa: E402  guard-ok: modules are imported after the sys.path insert, as in the other test modules
import cli  # noqa: E402  guard-ok: imported after the sys.path insert
import common  # noqa: E402  guard-ok: imported after the sys.path insert
import engine  # noqa: E402  guard-ok: imported after the sys.path insert
import upload  # noqa: E402  guard-ok: imported after the sys.path insert
import validate  # noqa: E402  guard-ok: imported after the sys.path insert
from approval import ApprovalError, GuiApprovals  # noqa: E402  guard-ok: imported after the sys.path insert
from test_golden import GOLDEN, Orchestrator, write_capture  # noqa: E402  guard-ok: the shared synthetic capture and fake Orchestrator
from test_run_output import in_threads  # noqa: E402  guard-ok: the shared thread runner

ENV = "orch_1=lab\nlab_base_url=https://orch.example.com\nlab_api_key=k\nEC_VALIDATORS=lab\n"


class Echoing(Orchestrator):
    """The fake Orchestrator, logging every call; a rejected /validate quotes the whole document back, as an error may quote a value."""

    def __init__(self):
        super().__init__()
        self.calls, self.together = [], None

    def get(self, path, query=None):
        self.calls.append(("GET", path))
        return super().get(path, query)

    def request(self, method, path, query=None, json_body=None, expected_status=(200,), expect_json=True, read_only=False):
        self.calls.append((method, path))
        if path == validate.PATH and self.together is not None:
            self.together.wait()  # both parallel runs are inside cli.main at once
        text = base64.b64decode(json_body["configData"]).decode("utf-8") if json_body and "configData" in json_body else ""
        if path == validate.PATH and "REJECT" in text:
            raise common.ValidationError("HTTP 400: invalid value in " + text, status=400, method=method, path=path)
        return super().request(method, path, query, json_body, expected_status, expect_json, read_only)

    def posts(self):
        return [call for call in self.calls if call == ("POST", "/gms/appliance/preconfiguration")]


class Unreadable:
    """The process stdin of a GUI: an engine run must never read it."""

    def __getattr__(self, name):
        raise AssertionError("the run read the process stdin ({})".format(name))


def shown(result):
    """Everything a Result can show."""
    return json.dumps(dataclasses.asdict(result), ensure_ascii=False)


class EngineTests(unittest.TestCase):
    """One build and one site type are shared; each case writes its own CSVs and .env files next to them."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.orch = Echoing()
        environ = {k: v for k, v in os.environ.items() if k.upper() != "EC_VALIDATORS" and not k.lower().endswith("_gui_preconfig_uploads")}  # the process environment wins over .env
        cls.patches = [mock.patch.dict(os.environ, environ, clear=True), mock.patch.object(sys, "stdin", Unreadable()),
                       mock.patch.object(capture, "capture", side_effect=lambda *a, **k: write_capture(common.work_dir() / "cap")),
                       mock.patch.object(cli, "client_for", return_value=cls.orch), mock.patch.object(validate, "client_for", return_value=cls.orch),
                       mock.patch.object(upload, "client_for", return_value=cls.orch)]
        for patch in cls.patches:
            patch.start()
        cls.on = cls.dotenv("on.env", ENV + "lab_gui_preconfig_uploads=on\n")
        cls.built = engine.build("lab", "h1", **cls.context(cls.on))
        cls.build = cls.tmp / "preconfig-output" / "h1"
        cls.picked = engine.variables(cls.build, "top10", name="t", yes=True, **cls.context(cls.on))
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

    @classmethod
    def context(cls, env):
        return {"dotenv": str(env), "out_dir": cls.tmp, "work_dir": cls.tmp / "work"}

    def setUp(self):
        self.orch.existing[:] = [{"id": 1, "name": "taken", "serialNum": None}]
        self.orch.calls[:] = []
        self.orch.together = None

    def sheet(self, name, rows, fields=None):
        path = self.tmp / name
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields or list(self.example))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def row(self, name, **cells):
        return {**self.example, "preconfig_name": name, "hostname": name, **cells}

    # ------------------------------------------------------------ Result fields

    def test_a_build_and_a_pick_return_their_recorder_results(self):
        self.assertEqual((self.built.exit_code, self.built.status, self.built.approval), (0, "done", None))
        self.assertEqual([r["kind"] for r in self.built.outputs], ["build"])
        self.assertEqual(self.built.outputs[0]["folder"], str(self.build))
        self.assertIn("reading h1 from lab (GET only) ...", self.built.text)
        self.assertEqual((self.built.issues, self.built.rows, self.built.counts, self.built.files), ([], [], {}, []))
        self.assertEqual([r["kind"] for r in self.picked.outputs], ["picker"])
        self.assertEqual(self.picked.outputs[0]["folder"], str(self.site))

    def test_an_upload_check_returns_issues_rows_counts_files_and_what_approving_takes(self):
        sheet = self.sheet("check.csv", [self.row("site-a"), self.row("taken"), self.row("site-c", lan0_ip="bad")])
        result = engine.upload(sheet, "lab", folder=self.site, **self.context(self.on))
        self.assertEqual((result.exit_code, result.status, self.orch.posts()), (1, "failed", []))
        rendered = self.site / "rendered"
        self.assertEqual([(r["row"], r["item"], r["status"], r["outcome"], r["file"]) for r in result.rows],
                         [(1, "site-a", "ready", "PLANNED", str(rendered / "site-a.yml")), (2, "taken", "conflict", "SKIPPED", str(rendered / "taken.yml")),
                          (3, "site-c", "invalid", "INVALID", None)])
        self.assertEqual(result.counts, {"PLANNED": 1, "SKIPPED": 1, "INVALID": 1})
        self.assertEqual(result.files, [str(rendered / "site-a.yml"), str(rendered / "taken.yml")])
        self.assertIn({"row": 2, "column": "preconfig_name", "value": "taken", "message": "preconfig_name 'taken' already exists on Orchestrator", "fix": ""}, result.issues)
        bad = [i for i in result.issues if i["row"] == 3]
        self.assertEqual([(i["column"], i["value"], i["fix"]) for i in bad], [("lan0_ip", "bad", "")])
        self.assertEqual(result.rows[2]["reason"], bad[0]["message"])
        self.assertIn("ROW  3 | INVALID  | site-c", result.text)
        self.assertIn("Approval required", result.text)
        fingerprint = [r["value"] for r in result.outputs if r["kind"] == "fingerprint"]
        self.assertEqual(result.approval, {"gates": [{"prompt": "Type the Orchestrator nickname lab to upload: ", "expected": "lab",
                                                      "refusal": "upload approval refused: the nickname typed is not lab"}],
                                           "fingerprint": fingerprint[0], "orchestrator": "lab"})

    def test_text_holds_stdout_and_stderr_and_the_exit_code_of_an_argparse_error(self):
        result = engine.run(["build", "--orchestrator", "lab"], **self.context(self.on))
        self.assertEqual((result.exit_code, result.status), (2, "failed"))
        self.assertIn("the following arguments are required: --appliance", result.text)

    def test_the_text_of_a_command_is_the_cli_text(self):
        golden = (GOLDEN / "list.txt").read_text(encoding="utf-8")
        stdout = golden.split("--- stdout\n", 1)[1].split("--- stderr\n", 1)[0]
        self.assertEqual(engine.list_appliances("lab", **self.context(self.on)).text, stdout)

    # ------------------------------------------------------------ outcomes

    def test_every_recorded_status_maps_to_the_run_log_word(self):
        expected = {"ok": "VALID", "failed": "INVALID", "invalid": "INVALID", "conflict": "SKIPPED", "ready": "PLANNED", "uploaded": "CREATED",
                    "upload-failed": "FAILED"}
        for status, outcome in expected.items():
            self.assertEqual(engine._row({"kind": "row", "index": 1, "preconfig_name": "x", "status": status, "file": None, "problems": []})["outcome"], outcome, status)
        files = {"passed": "VALID", "uploaded": "CREATED", "upload-failed": "FAILED", "validate failed": "INVALID", "refused: no such file": "INVALID",
                 "refused: not a preconfig YAML": "INVALID", "refused: secret placeholder": "INVALID", "skipped: name conflict": "SKIPPED",
                 "skipped: serial conflict": "SKIPPED"}
        for status, outcome in files.items():
            self.assertEqual(engine._row({"kind": "file", "name": "x", "status": status, "file": "x.yml", "detail": ""})["outcome"], outcome, status)

    def test_render_rows_are_valid_or_invalid(self):
        sheet = self.sheet("render.csv", [self.row("r-a"), self.row("r-b", lan0_ip="bad")])
        result = engine.render(self.site / "t.jinja2", sheet, self.tmp / "run-render", **self.context(self.on))
        self.assertEqual([(r["item"], r["outcome"]) for r in result.rows], [("r-a", "VALID"), ("r-b", "VALID")])  # no columns.json: no cell checks
        result = engine.render(self.site / "t.jinja2", self.sheet("render.csv", [self.row("r-a"), self.row("r-a")]), self.tmp / "run-render2", **self.context(self.on))
        self.assertEqual([(r["item"], r["outcome"]) for r in result.rows], [("r-a", "VALID"), ("r-a", "INVALID")])
        self.assertEqual(result.files, [str(self.tmp / "run-render2" / "r-a.yml")])

    def test_yaml_files_are_valid_skipped_invalid_or_created(self):
        folder = self.tmp / "yaml-files"
        folder.mkdir(exist_ok=True)
        good = (self.site / "rendered" / "h1_preconfig.yml")
        engine.validate(self.site, self.site / "sites.csv", **self.context(self.on))
        shutil.copy(good, folder / "fresh.yml")
        shutil.copy(good, folder / "taken.yml")
        (folder / "broken.yml").write_text("{{ data['x'] }}\n", encoding="utf-8")
        checked = engine.validate_yaml([folder], "lab", **self.context(self.on))
        self.assertEqual({r["item"]: r["outcome"] for r in checked.rows}, {"broken": "INVALID", "fresh": "VALID", "taken": "SKIPPED"})
        self.assertEqual((checked.files, checked.approval, self.orch.posts()), ([], None, []))
        sent = engine.upload_yaml([folder / "fresh.yml"], "lab", **self.context(self.on))
        self.assertEqual(([(r["item"], r["outcome"]) for r in sent.rows], sent.counts, sent.approval), ([("fresh", "CREATED")], {"CREATED": 1}, None))
        self.assertEqual(len(self.orch.posts()), 1)

    # ------------------------------------------------------------ approval

    def test_an_approved_upload_end_to_end(self):
        sheet = self.sheet("approved.csv", [self.row("site-x"), self.row("site-y")])
        checked = engine.upload(sheet, "lab", folder=self.site, **self.context(self.on))
        self.assertEqual((checked.status, checked.counts, self.orch.posts()), ("done", {"PLANNED": 2}, []))
        self.assertIn("checking (no upload without --approve)", checked.text)
        approvals = GuiApprovals()
        with self.assertRaisesRegex(ApprovalError, "nickname typed is not lab"):
            engine.approve(approvals, checked.approval, ["Lab"])
        approver = engine.approve(approvals, checked.approval, ["lab"])
        sent = engine.upload(sheet, "lab", folder=self.site, approver=approver, **self.context(self.on))
        self.assertEqual((sent.exit_code, sent.status, sent.approval), (0, "done", None))
        self.assertIn("uploading approved.csv on lab ...", sent.text)
        self.assertEqual(([(r["item"], r["outcome"]) for r in sent.rows], sent.counts), ([("site-x", "CREATED"), ("site-y", "CREATED")], {"CREATED": 2}))
        self.assertEqual([e["name"] for e in self.orch.existing[1:]], ["site-x", "site-y"])

    def test_the_approver_keeps_the_typed_orchestrator(self):
        approvals = GuiApprovals()
        request = {"gates": [{"prompt": "p", "expected": "lab", "refusal": "no"}], "fingerprint": "f" * 64, "orchestrator": "lab"}
        record = engine.approve(approvals, request, ["lab"], typed_orchestrator="lab (typed)")("f" * 64)
        self.assertEqual((record["plan_fingerprint"], record["typed"], record["typed_orchestrator"]), ("f" * 64, ["lab"], "lab (typed)"))

    def test_a_not_approved_upload_is_rejected(self):
        sheet = self.sheet("changed.csv", [self.row("site-m")])
        approver = engine.approve(GuiApprovals(), engine.upload(sheet, "lab", folder=self.site, **self.context(self.on)).approval, ["lab"])
        self.sheet("changed.csv", [self.row("site-m", hostname="site-m2")])
        result = engine.upload(sheet, "lab", folder=self.site, approver=approver, **self.context(self.on))
        self.assertEqual((result.exit_code, result.status, self.orch.posts()), (2, "rejected", []))
        self.assertIn("NOT APPROVED, nothing uploaded: the batch changed since it was approved", result.text)

    # ------------------------------------------------------------ gates: nothing is sent

    def test_a_with_approve_without_an_approver_is_refused(self):
        sheet = self.sheet("gate-a.csv", [self.row("site-g")])
        for flag in ("--approve", "--appr"):  # argparse takes an abbreviation, so the gate does too
            result = engine.run(["upload", str(self.site), "--csv", str(sheet), "--orchestrator", "lab", flag], **self.context(self.on))
            self.assertEqual((result.exit_code, result.status, result.approval, self.orch.calls), (2, "rejected", None, []), flag)
            self.assertIn("refused, nothing sent: an upload with --approve needs the GUI's approval", result.text)
        result = engine.run(["upload", "--yaml", str(self.site / "rendered"), "--orchestrator", "lab", "--approve"], **self.context(self.on))
        self.assertEqual((result.status, self.orch.calls), ("rejected", []))

    def test_b_uploads_need_the_switch_for_their_own_orchestrator(self):
        sheet = self.sheet("gate-b.csv", [self.row("site-h")])
        approver = engine.approve(GuiApprovals(), engine.upload(sheet, "lab", folder=self.site, **self.context(self.on)).approval, ["lab"])
        self.orch.calls[:] = []
        for name, text in (("off.env", ENV), ("other.env", ENV + "other_gui_preconfig_uploads=on\n"), ("no.env", ENV + "lab_gui_preconfig_uploads=yes\n")):
            off = self.context(self.dotenv(name, text))
            for result in (engine.upload(sheet, "lab", folder=self.site, **off), engine.upload(sheet, "lab", folder=self.site, approver=approver, **off),
                           engine.upload(sheet, "lab", template=self.site / "t.jinja2", out=self.tmp / "run-b", **off),
                           engine.upload_yaml([self.site / "rendered"], "lab", **off)):
                self.assertEqual((result.exit_code, result.status, result.approval), (2, "rejected", None), name)
                self.assertIn("refused, nothing sent: GUI uploads are off for lab: set lab_gui_preconfig_uploads=on in .env", result.text)
        self.assertEqual(self.orch.calls, [])  # not even the upload's own /validate or a GET (D20)
        result = engine.run(["upload", str(self.site), "--csv", str(sheet)], **self.context(self.on))  # the build's validator, not named
        self.assertEqual((result.status, self.orch.calls), ("rejected", []))
        self.assertIn("names its Orchestrator with --orchestrator", result.text)
        checked = engine.validate_yaml([self.site / "rendered"], "lab", **self.context(self.dotenv("off.env", ENV)))  # a check needs no switch
        self.assertEqual((checked.status, self.orch.posts()), ("done", []))

    def test_gate_variants_read_the_command_line_as_the_cli_does(self):
        sheet = self.sheet("variants.csv", [self.row("site-v")])
        off = self.context(self.dotenv("off.env", ENV))
        cases = [(["upload", "--site-dir", str(self.site), "--csv", str(sheet), "--orchestrator", "lab", "--approve"], self.context(self.on), "runs only the command lines it builds"),
                 (["upload", "--template", str(self.site / "t.jinja2"), "--csv", str(sheet), "--orchestrator", "lab", "--approve"], self.context(self.on), "needs the GUI's approval"),
                 (["upload", str(self.site), "--csv", str(sheet), "--orch", "lab"], off, "GUI uploads are off for lab"),
                 (["upload", str(self.site), "--csv", str(sheet), "--orchestrator", "LAB"], off, "GUI uploads are off for LAB: set lab_gui_preconfig_uploads=on"),
                 (["upload", str(self.site), "--csv", str(sheet), "--orchestrator", "lab", "--orchestrator", "other"], self.context(self.on), "GUI uploads are off for other")]
        for argv, context, why in cases:
            with self.subTest(argv[-2:]):
                result = engine.run(argv, **context)
                self.assertEqual((result.status, self.orch.calls), ("rejected", []))
                self.assertIn(why, result.text)
        checked = engine.run(["upload", str(self.site), "--csv", str(sheet), "--orchestrator", "LAB"], **self.context(self.on))  # the CLI's profile lab
        self.assertEqual((checked.status, checked.approval["orchestrator"], self.orch.posts()), ("done", "LAB", []))

    def test_an_upload_past_the_gates_without_an_approver_is_still_not_approved(self):
        sheet = self.sheet("past.csv", [self.row("site-p")])
        with mock.patch.object(engine, "_refusal", return_value=None):
            result = engine.run(["upload", str(self.site), "--csv", str(sheet), "--orchestrator", "lab", "--approve"], **self.context(self.on))
        self.assertEqual((result.exit_code, result.status, self.orch.posts()), (2, "rejected", []))
        self.assertIn("NOT APPROVED, nothing uploaded: no approval was given for this upload", result.text)

    def test_c_the_upload_helper_adds_approve_only_with_an_approver(self):
        sheet = self.sheet("gate-c.csv", [self.row("site-k")])
        with mock.patch.object(engine, "run", wraps=engine.run) as ran:
            checked = engine.upload(sheet, "lab", folder=self.site, **self.context(self.on))
            engine.upload(sheet, "lab", folder=self.site, approver=engine.approve(GuiApprovals(), checked.approval, ["lab"]), **self.context(self.on))
        self.assertNotIn("--approve", ran.call_args_list[0][0][0])
        self.assertEqual(ran.call_args_list[1][0][0][-1], "--approve")
        self.assertEqual(len(self.orch.posts()), 1)

    # ------------------------------------------------------------ secrets in text

    def test_a_secret_column_value_never_reaches_text_issues_rows_or_outputs(self):
        site = self.build / "site-secret"
        shutil.rmtree(str(site), ignore_errors=True)
        shutil.copytree(str(self.site), str(site))
        meta = json.loads((site / "columns.json").read_text(encoding="utf-8"))
        meta["columns"]["contact_name"]["secret"] = True
        (site / "columns.json").write_text(json.dumps(meta), encoding="utf-8")
        for secret in ('Hunter"2 Secret', "Hunter'2 <&> Secret", "Hunter\\2 été"):  # the template writes string cells with tojson
            with self.subTest(secret):
                sheet = self.sheet("secret.csv", [self.row("site-s", contact_name=secret, site="REJECT")])
                result = engine.validate(site, sheet, "lab", **self.context(self.on))
                self.assertEqual(result.counts, {"INVALID": 1})
                self.assertIn("<SECRET:contact_name>", result.text)
                self.assertIn("<SECRET:contact_name>", json.dumps(result.issues))
                self.assertNotIn("Hunter", shown(result))

    def test_secret_like_headers_count_without_a_columns_file(self):
        template = self.tmp / "own.jinja2"
        template.write_text("applianceInfo:\n  hostname: {{ data['hostname'] }}\n  site: {{ data['site'] }}\n  contact: {{ data['admin_password'] }}\n", encoding="utf-8")
        sheet = self.sheet("own.csv", [{"hostname": "own-1", "site": "REJECT", "admin_password": "Pa55-Word", "preconfig_name": "own-1"}],
                           ["hostname", "site", "admin_password", "preconfig_name"])
        result = engine.render(template, sheet, self.tmp / "run-own", validate_on="lab", **self.context(self.on))
        self.assertIn("<SECRET:admin_password>", result.text)
        self.assertNotIn("Pa55-Word", result.text + json.dumps(result.issues) + json.dumps(result.outputs))

    def test_a_yaml_file_secret_never_reaches_the_text(self):
        path = self.tmp / "with-key.yml"
        path.write_text("applianceInfo:\n  site: REJECT\nvrrp:\n  authKey: Sup3r-K3y\n", encoding="utf-8")
        result = engine.validate_yaml([path], "lab", **self.context(self.on))
        self.assertEqual(result.counts, {"INVALID": 1})
        self.assertNotIn("Sup3r-K3y", result.text + json.dumps(result.rows) + json.dumps(result.outputs))
        self.assertIn("<SECRET:$.vrrp.authKey>", result.text)

    def check_yaml(self, text, secret, *argv):
        """validate --yaml FILE (or argv + FILE): the secret appears nowhere in the Result."""
        path = self.tmp / "secret-file.yml"
        path.write_text(text, encoding="utf-8")
        result = engine.run(list(argv) + [str(path)] if argv else ["validate", "--yaml", str(path), "--orchestrator", "lab"], **self.context(self.on))
        self.assertNotIn(secret, shown(result))
        return result

    def test_a_numeric_yaml_secret_never_reaches_the_text(self):
        for text in ("applianceInfo:\n  site: REJECT\nvrrp:\n  authKey: 98765432\n", "applianceInfo: {site: REJECT}\nvrrp: {authKey: 98765432}\n"):
            with self.subTest(text):
                self.assertEqual(self.check_yaml(text, "98765432").counts, {"INVALID": 1})

    def test_a_secret_breaking_yaml_parsing_never_appears(self):
        for text, secret in (("vrrp:\n  authKey: *Sup3rK3y\n", "Sup3rK3y"), ('vrrp:\n  authKey: "Pa"ss-W0rd"\n', "ss-W0rd"),
                             ("vrrp: {authKey: *Fl0wK3y}\n", "Fl0wK3y")):  # no key: value line to scan: the parser's message is withheld
            with self.subTest(secret):
                result = self.check_yaml(text, secret)
                self.assertEqual([r["status"] for r in result.rows], ["refused: not a preconfig YAML"])
        template = self.tmp / "quoted.jinja2"
        template.write_text("applianceInfo:\n  hostname: {{ data['hostname'] }}\n  contact: \"{{ data['admin_password'] }}\"\n", encoding="utf-8")
        sheet = self.sheet("quoted.csv", [{"hostname": "q-1", "admin_password": 'Pa"ss-W0rd', "preconfig_name": "q-1"}], ["hostname", "admin_password", "preconfig_name"])
        result = engine.render(template, sheet, self.tmp / "run-quoted", **self.context(self.on))
        self.assertEqual(result.counts, {"INVALID": 1})
        self.assertNotIn("ss-W0rd", shown(result))

    def test_a_yaml_snippet_of_a_rendered_row_is_withheld(self):
        secret = 'abcdefghijklmnopqrstuvwxyz0123456789"ABCDEFGHIJ'  # the " is far into the line: PyYAML's snippet is cut from the left
        template = self.tmp / "quoted.jinja2"
        template.write_text("applianceInfo:\n  hostname: {{ data['hostname'] }}\n  contact: \"{{ data['admin_password'] }}\"\n", encoding="utf-8")
        sheet = self.sheet("long.csv", [{"hostname": "q-2", "admin_password": secret, "preconfig_name": "q-2"}], ["hostname", "admin_password", "preconfig_name"])
        result = engine.render(template, sheet, self.tmp / "run-long", **self.context(self.on))
        self.assertEqual(result.counts, {"INVALID": 1})
        for part in ("0123456789", "ABCDEFGHIJ"):
            self.assertNotIn(part, shown(result))
        self.assertRegex(result.text, r'line 3, column \d+ \(the source snippet is withheld: it may hold a secret\)\n')
        self.assertIn("(the source snippet is withheld: it may hold a secret)", result.issues[0]["message"])

    def test_the_stage_form_sending_raw_yaml_files_is_refused(self):
        path = self.tmp / "flow.yml"
        path.write_text('vrrp: {authKey: "Fl0w"K3y}\n', encoding="utf-8")
        result = engine.run(["validate", "--orchestrator", "lab", str(path)], **self.context(self.on))
        self.assertEqual((result.exit_code, result.status, self.orch.calls), (2, "rejected", []))
        self.assertIn("use validate --yaml or upload --yaml", result.text)

    def test_only_the_command_lines_the_helpers_build_run(self):
        sheet = self.sheet("shapes.csv", [self.row("site-z")])
        for argv in (["check", str(self.site), "--rows", str(sheet), "--validate", "lab"], ["translate", "--capture", str(self.tmp)],
                     ["capture", "--orchestrator", "lab", "--appliance", "h1"], ["biuld", "--orchestrator", "lab"], ["--help"], [],
                     ["validate", "--orchestrator", "lab", "--csv", str(sheet)], ["upload", "--site-dir", str(self.site), "--csv", str(sheet), "--orchestrator", "lab"],
                     ["render", "--site-dir", str(self.site), "--csv", str(sheet)], ["render", "--template", str(self.site / "t.jinja2")]):
            with self.subTest(argv[:2]):
                result = engine.run(argv, **self.context(self.on))
                self.assertEqual((result.exit_code, result.status, self.orch.calls), (2, "rejected", []))
                self.assertIn("refused, nothing sent: the GUI runs only the command lines it builds", result.text)
                self.assertFalse((self.site / "rendered" / "site-z.yml").exists())

    def test_files_named_without_yaml_are_scanned_too(self):
        other = self.tmp / "other.yml"
        other.write_text("applianceInfo:\n  site: S1\n", encoding="utf-8")
        result = self.check_yaml("applianceInfo:\n  site: REJECT\nvrrp:\n  authKey: Sup3r-K3y\n", "Sup3r-K3y", "validate", "--yaml", str(other), "--orchestrator", "lab")
        self.assertIn("validate failed                secret-file.yml", result.text)  # the stage's positional files join --yaml

    def test_a_secret_cut_by_the_cli_leaves_no_prefix(self):
        secret = "Sup3r-K3y-0123456789"
        body = "applianceInfo:\n  site: REJECT\n# {}\nvrrp:\n  authKey: {}\n"
        error = str(common.ValidationError("HTTP 400: invalid value in " + body.format("", secret), status=400, method="POST", path=validate.PATH))
        pad = "x" * (1000 - 8 - error.index(secret))  # validate_text keeps the first 1000 characters: 8 of the secret
        result = self.check_yaml(body.format(pad, secret), secret[:8])
        self.assertIn("<SECRET:$.vrrp.authKey>", result.text)

    # ------------------------------------------------------------ runs side by side, prompts

    def test_two_parallel_runs_with_their_own_env_see_only_their_own_text_and_files(self):
        self.orch.together = threading.Barrier(2, timeout=10)
        runs = {}
        for name in ("a", "b"):
            env = self.dotenv(name + ".env", ENV.replace("EC_VALIDATORS=lab", "EC_VALIDATORS=lab_" + name))
            runs[name] = (self.sheet("par-{}.csv".format(name), [self.row("par-" + name)]), self.tmp / ("run-par-" + name), self.context(env))
        results = dict(zip(runs, in_threads(*[lambda n=n: engine.render(self.site / "t.jinja2", runs[n][0], runs[n][1], validate_on="lab_" + n, **runs[n][2])
                                              for n in runs])))
        for name, other in (("a", "b"), ("b", "a")):
            result = results[name]
            self.assertEqual((result.exit_code, result.files), (0, [str(runs[name][1] / "par-{}.yml".format(name))]), result.text)
            self.assertIn("par-" + name, result.text)
            self.assertNotIn("par-" + other, result.text)
        self.orch.together = None
        refused = engine.render(self.site / "t.jinja2", runs["a"][0], runs["a"][1], validate_on="lab_b", **runs["a"][2])
        self.assertEqual(refused.exit_code, 2)
        self.assertIn("lab_b is not a validator Orchestrator (allowed: lab-a, lab_a)", refused.text)

    def test_a_prompt_reads_end_of_file_and_never_the_process_stdin(self):
        result = in_threads(lambda: engine.run(["variables", str(self.build)], **self.context(self.on)))[0]
        self.assertEqual((result.exit_code, result.status), (1, "failed"))
        self.assertTrue(result.text.endswith("\nnothing written\n"), result.text)


if __name__ == "__main__":
    unittest.main()

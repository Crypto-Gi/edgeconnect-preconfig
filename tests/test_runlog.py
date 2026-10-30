"""The run log (cli-requests R4): every command appends one line to reports/preconfig.jsonl in the automation tools' format,
which their reader (a copy in harnesses/automation_reader.py) reads back; mode 0600; a secret cell never reaches it."""
import csv
import hashlib
import json
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "harnesses"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "scripts"))
os.environ["EDGECONNECT_REPORT_DIR"] = ""  # each test names its own folder
import automation_reader  # noqa: E402  guard-ok: imported after the sys.path insert
import cli  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
import common  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
import runlog  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
import test_golden  # noqa: E402  guard-ok: the golden fixture (fake Orchestrator, one shared build)
from common import RunContext, running  # noqa: E402  guard-ok: scripts are imported after the sys.path insert

_FIXTURE = test_golden.Golden.__dict__


class RunLog(unittest.TestCase):
    setUpClass, tearDownClass = _FIXTURE["setUpClass"], _FIXTURE["tearDownClass"]
    setUp, files, run_cli, mask, built, site, sheet = (_FIXTURE[n] for n in ("setUp", "files", "run_cli", "mask", "built", "site", "sheet"))

    def logged(self, argv, folder="reports", context=None):
        """Run cli.main(argv) with the log in <tmp>/<folder>; returns the exit code and the run's log record."""
        log = self.tmp / folder / "preconfig.jsonl"
        before = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
        with mock.patch.dict(os.environ, {"EDGECONNECT_REPORT_DIR": str(self.tmp / folder)}):
            if context is None:
                code, self.out_text, self.err_text = self.run_cli(argv)
            else:
                with running(context):
                    code, self.out_text, self.err_text = self.run_cli(argv)
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), len(before) + 1, "one line per command")
        return code, json.loads(lines[-1])

    def test_a_user_session_reads_back_through_automations_reader(self):
        site = self.site()
        runs = [(["list", "--orchestrator", "lab"], 0, "SUCCESS", "no", {}),
                (["upload", str(site), "--csv", str(site / "sites.csv")], 0, "PLANNED", "no", {"PLANNED": 1}),
                (["upload", str(site), "--csv", str(site / "sites.csv"), "--approve"], 0, "SUCCESS", "yes", {"CREATED": 1}),
                (["validate", str(site), "--csv", str(self.sheet("bad.csv", lambda rows: rows + [dict(rows[0], hostname="h9", preconfig_name="h9", site="REJECT")]))],
                 1, "INVALID", "no", {"VALID": 1, "INVALID": 1})]
        records = []
        for argv, code, status, written, outcomes in runs:
            got, record = self.logged(argv, "session")
            records.append(record)
            self.assertEqual((got, record["status"], record["changes_written"], record["summary"]["row_outcomes"]), (code, status, written, outcomes), argv)
            self.assertEqual((record["workflow"], record["source"], record["exit_code"]), ("preconfig " + argv[0], "cli", code))
            fingerprint = record.pop("report_fingerprint")
            self.assertEqual(fingerprint, "sha256:" + hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest())
        log = self.tmp / "session" / "preconfig.jsonl"
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        summaries = automation_reader.summaries_from_file(str(log), last=0)
        self.assertEqual([s["rows"] for s in summaries], [r["summary"]["rows"] for r in records])
        bad = summaries[-1]["rows"]
        self.assertEqual([(r["row"], r["item"], r["outcome"]) for r in bad], [(1, "h1_preconfig", "VALID"), (2, "h9", "INVALID")])
        self.assertIn("applianceInfo.site: invalid value", bad[1]["reason"])
        text = automation_reader.render(summaries[-1])
        self.assertIn("RESULT       : INVALID", text)
        self.assertIn("Workflow     : preconfig validate", text)
        self.assertEqual(records[1]["summary"]["csv"]["data_rows"], 1)

    def test_a_secret_that_breaks_the_yaml_never_reaches_the_log(self):
        template = self.tmp / "s.jinja2"
        template.write_text('applianceInfo:\n  hostname: "{{ data[\'hostname\'] }}"\nnac:\n  key: "{{ data[\'nac_key\'] }}"\n', encoding="utf-8")
        meta = {"columns": {"hostname": {"path": "applianceInfo.hostname", "type": "str", "optional": False, "example": "h1"},
                            "nac_key": {"path": "nac.key", "type": "str", "optional": False, "example": "", "secret": True}}}
        (self.tmp / "s.json").write_text(json.dumps(meta), encoding="utf-8")
        for secret in ('ab"cd: ef', "p\\q\"r", 'x" password=zz9'):  # each breaks the YAML, so the parse error quotes it
            with self.subTest(secret=secret):
                sheet = self.tmp / "s.csv"
                with sheet.open("w", newline="", encoding="utf-8") as handle:
                    csv.writer(handle).writerows([["hostname", "nac_key"], ["h1", secret]])
                code, record = self.logged(["render", "--template", str(template), "--csv", str(sheet), "--columns", str(self.tmp / "s.json"), "--out", str(self.tmp / "s")], "secret")
                self.assertEqual(code, 1)
                self.assertIn(secret[:4], self.out_text + self.err_text)  # the printed text quotes it: the log must not
                text = (self.tmp / "secret" / "preconfig.jsonl").read_text(encoding="utf-8")
                for leak in {secret, repr(secret)[1:-1], json.dumps(secret)[1:-1], json.dumps(json.dumps(secret)[1:-1])[1:-1]}:
                    self.assertNotIn(leak, text)
                self.assertIn("<SECRET:nac_key>", json.dumps(record))

    def test_an_empty_folder_setting_turns_the_log_off(self):
        with mock.patch.dict(os.environ, {"EDGECONNECT_REPORT_DIR": ""}):
            self.assertEqual(self.run_cli(["list", "--orchestrator", "lab"])[0], 0)
        self.assertFalse((Path.cwd() / "reports").exists())

    def test_a_refused_approval_and_a_kept_approval_record(self):
        site = self.site("ap")

        def refuse(fingerprint):
            raise PermissionError("the rows changed")
        code, record = self.logged(["upload", str(site), "--csv", str(site / "sites.csv"), "--approve"], "approval", RunContext(approver=refuse, source="gui"))
        self.assertEqual((code, record["status"], record["changes_written"], record["source"]), (2, "REFUSED", "no", "gui"))
        self.assertEqual(record["summary"]["blocking_issues"], ["the rows changed"])
        approval = {"by": "someone", "time": "2026-10-30T07:00:00Z", "typed_orchestrator": "lab", "typed": []}
        code, record = self.logged(["upload", str(site), "--csv", str(site / "sites.csv"), "--approve"], "approval", RunContext(approver=lambda fingerprint: approval, source="gui"))
        self.assertEqual((code, record["status"], record["summary"]["approval"]), (0, "SUCCESS", approval))
        self.assertIn("Approved by  : someone at 2026-10-30T07:00:00Z in the GUI, typing 'lab'", automation_reader.render(record["summary"]))

    def test_a_command_that_raises_is_still_logged(self):
        def boom(argv):
            raise RuntimeError("unexpected")
        with mock.patch.dict(cli.VERBS, {"list": boom}), mock.patch.dict(os.environ, {"EDGECONNECT_REPORT_DIR": str(self.tmp / "raised")}):
            with self.assertRaises(RuntimeError):
                cli.main(["list", "--orchestrator", "lab"])
        record = json.loads((self.tmp / "raised" / "preconfig.jsonl").read_text(encoding="utf-8"))
        self.assertEqual((record["status"], record["exit_code"]), ("ERROR", 1))

    def test_a_log_that_cannot_be_written_leaves_the_result_alone(self):
        (self.tmp / "blocked").write_text("a file where the folder would be", encoding="utf-8")
        with mock.patch.dict(os.environ, {"EDGECONNECT_REPORT_DIR": str(self.tmp / "blocked")}):
            code, _, err = self.run_cli(["list", "--orchestrator", "lab"])
        self.assertEqual(code, 0)
        self.assertIn("warning: run log not written", err)

    def test_the_orchestrator_the_run_connects_to_is_named(self):
        folder = self.tmp / "env"
        folder.mkdir(exist_ok=True)
        (folder / ".env").write_text("orch_1=lab_e\nlab_e_base_url=https://lab-e.example.com\nlab_e_api_key=k\n", encoding="utf-8")
        with running(RunContext(dotenv=str(folder / ".env"))), mock.patch.dict(os.environ, {"EDGECONNECT_REPORT_DIR": str(folder)}):
            with runlog.logged(["list"]) as log:
                common.client_for("lab_e")
                log["code"] = 0
        record = json.loads((folder / "preconfig.jsonl").read_text(encoding="utf-8"))
        self.assertEqual((record["orchestrator"], record["orchestrator_url"]), ("lab_e", "https://lab-e.example.com/gms/rest"))
        self.assertNotIn('"k"', json.dumps(record))


if __name__ == "__main__":
    unittest.main()

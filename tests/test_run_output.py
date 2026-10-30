"""A run's printed lines, prompts and structured results (cli-requests R1, R3): quiet() and captured_output() capture only their own
run's lines, a prompt at the end of stdin ends the command instead of waiting, and the recorder keeps each command's results."""
import csv
import io
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
os.environ["EDGECONNECT_REPORT_DIR"] = ""  # a command logs to reports/ (runlog.py); the run-log tests set their own folder
import cli  # noqa: E402  guard-ok: scripts are imported after the sys.path insert, as in the other test modules
import common  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
import upload  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
import validate  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
from common import Recorder, RunContext, captured_output, running  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
from test_golden import Orchestrator, write_capture  # noqa: E402  guard-ok: the shared synthetic capture and fake Orchestrator
from test_run_context import dotenv  # noqa: E402  guard-ok: the shared .env writer


def no_env_validators():
    """The process environment wins over every .env (as in the CLI): leave EC_VALIDATORS out of it for these cases."""
    return mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k.upper() != "EC_VALIDATORS"}, clear=True)


def in_threads(*jobs):
    """Run each job in its own thread at the same time; returns their results, re-raising the first error."""
    results, errors = [None] * len(jobs), []

    def run(i, job):
        try:
            results[i] = job()
        except BaseException as error:  # reported by the caller
            errors.append(error)
    threads = [threading.Thread(target=run, args=(i, job)) for i, job in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    if any(t.is_alive() for t in threads):
        raise AssertionError("a run did not finish")
    if errors:
        raise errors[0]
    return results


class ParallelCapture(unittest.TestCase):
    def test_two_parallel_quiet_runs_each_capture_only_their_own_lines(self):
        together = threading.Barrier(2, timeout=10)

        def stage(name):
            for n in range(20):
                print("{} out {}".format(name, n))
                print("{} err {}".format(name, n), file=sys.stderr)
                if n % 5 == 0:
                    together.wait()  # interleave the two runs' writes
            return name

        first, second = in_threads(lambda: cli.quiet(stage, "a"), lambda: cli.quiet(stage, "b"))
        for (result, text), name, other in ((first, "a", "b"), (second, "b", "a")):
            self.assertEqual(result, name)
            self.assertEqual(text.count("\n"), 40)
            self.assertNotIn(other + " ", text)

    def test_a_capture_leaves_the_rest_of_the_process_printing_where_it_did(self):
        with tempfile.TemporaryDirectory(), mock.patch.object(sys, "stdout", io.StringIO()) as outside:
            mine = io.StringIO()
            with captured_output(mine):
                print("captured")
            print("not captured")
        self.assertEqual(mine.getvalue(), "captured\n")
        self.assertEqual(outside.getvalue(), "not captured\n")

    def test_two_runs_with_their_own_env_print_their_own_refusal(self):
        with tempfile.TemporaryDirectory() as tmp, no_env_validators():
            yml = Path(tmp) / "t.yml"
            yml.write_text('applianceInfo:\n  hostname: "t"\n', encoding="utf-8")

            def run(nick, other):
                out = io.StringIO()
                with running(RunContext(dotenv=dotenv(tmp, nick))), captured_output(out):
                    code = cli.main(["validate", "--yaml", str(yml), "--orchestrator", other])
                return code, out.getvalue()
            (code_a, text_a), (code_b, text_b) = in_threads(lambda: run("lab_a", "lab_b"), lambda: run("lab_b", "lab_a"))
        self.assertEqual((code_a, code_b), (2, 2))
        self.assertIn("lab_b is not a validator Orchestrator (allowed: lab-a, lab_a)", text_a)
        self.assertIn("lab_a is not a validator Orchestrator (allowed: lab-b, lab_b)", text_b)


class Fixture(unittest.TestCase):
    """A build folder from the synthetic capture, with a fake Orchestrator, for the cases below."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.orch = Orchestrator()
        self.orch.existing.append({"id": 1, "name": "taken", "serialNum": None})
        cap = write_capture(self.tmp / "cap")
        for target in (mock.patch("capture.capture", return_value=cap), mock.patch.object(cli, "client_for", return_value=self.orch),
                       mock.patch.object(validate, "client_for", return_value=self.orch), mock.patch.object(upload, "client_for", return_value=self.orch)):
            target.start()
            self.addCleanup(target.stop)
        self.recorder = Recorder()
        self.context = RunContext(validators={"lab"}, out_dir=self.tmp, work_dir=self.tmp / "work", recorder=self.recorder)

    def run_cli(self, argv, stdin=""):
        out = io.StringIO()
        with running(self.context), captured_output(out), mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            code = cli.main(argv)
        return code, out.getvalue()

    def results(self, kind):
        found = [r for r in self.recorder.results if r["kind"] == kind]
        del self.recorder.results[:]
        return found


class StdinAtEnd(Fixture):
    def test_every_verb_finishes_with_stdin_at_end_of_file(self):
        folder = str(self.tmp / "preconfig-output" / "h1")
        site = folder + "/site-s"
        yml = self.tmp / "t.yml"
        yml.write_text('applianceInfo:\n  hostname: "t"\n', encoding="utf-8")
        runs = [(["list", "--orchestrator", "lab"], 0), (["build", "--orchestrator", "lab", "--appliance", "h1"], 0),
                (["variables", folder], 1), (["variables", folder, "--pick", "top10", "--yes", "--name", "s"], 0),
                (["compare", folder, "h2"], 1), (["validate", site, "--csv", site + "/sites.csv"], 0), (["upload", site, "--csv", site + "/sites.csv"], 0),
                (["validate", "--yaml", str(yml), "--orchestrator", "lab"], 0), (["upload", "--yaml", str(yml), "--orchestrator", "lab"], 0)]
        for argv, expected in runs:
            with self.subTest(argv=argv[:2]):
                [(code, text)] = in_threads(lambda: self.run_cli(argv))
                self.assertEqual(code, expected, text)
                if expected == 1:
                    self.assertTrue(text.endswith("\nnothing written\n"), text)


class Recorded(Fixture):
    def test_each_command_keeps_its_structured_result(self):
        self.assertEqual(self.run_cli(["build", "--orchestrator", "lab", "--appliance", "h1"])[0], 0)
        [build] = self.results("build")
        folder = self.tmp / "preconfig-output" / "h1"
        self.assertEqual(build, {"kind": "build", "folder": str(folder), "verdict": "Complete for preconfig; 2 item(s) to check by hand.",
                                 "summary": str(folder / "summary.md"), "validator": "lab", "release": "9.7.1.42046"})

        self.assertEqual(self.run_cli(["variables", str(folder), "--pick", "top10", "--set", "deploymentInfo.deploymentMode", "--yes", "--name", "s"])[0], 0)
        [picker] = self.results("picker")
        site = folder / "site-s"
        self.assertEqual(picker["folder"], str(site))
        rows = {r["path"]: r for r in picker["rows"]}
        self.assertEqual(rows["applianceInfo.hostname"]["locked_why"], "hostname")
        self.assertTrue(rows["applianceInfo.site"]["locked"])
        mode = rows["deploymentInfo.deploymentMode"]
        self.assertEqual((mode["on"], mode["locked"], mode["value"]), (True, False, "router"))
        self.assertIn(mode["column"], picker["columns"])
        self.assertEqual(picker["columns"], sorted(picker["columns"], key=[r["column"] for r in picker["rows"] if r["on"]].index))
        with (site / "sites.csv").open(newline="", encoding="utf-8") as handle:
            self.assertEqual(set(next(csv.reader(handle))) - {"preconfig_name", "serial_number", "tag", "autoApply"} - set(picker["columns"]), set())

        sheet = self.tmp / "bad.csv"
        with (site / "sites.csv").open(newline="", encoding="utf-8") as handle:
            rows_in = list(csv.DictReader(handle))
        bad = dict(rows_in[0], hostname="h9", preconfig_name="h9", total_outbound_bandwidth="lots")
        with sheet.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows_in[0]))
            writer.writeheader()
            writer.writerows([rows_in[0], bad])
        self.assertEqual(self.run_cli(["validate", str(site), "--csv", str(sheet)])[0], 1)
        ok, failed = self.results("row")
        self.assertEqual((ok["index"], ok["status"], ok["problems"]), (1, "ok", []))
        self.assertEqual(ok["file"], str(site / "rendered" / "h1_preconfig.yml"))
        self.assertEqual((failed["index"], failed["preconfig_name"], failed["status"], failed["file"]), (2, "h9", "failed", None))
        self.assertIn({"row": 2, "column": "total_outbound_bandwidth", "value": "lots", "message": "total_outbound_bandwidth: 'lots' must be a whole number"}, failed["problems"])

        self.assertEqual(self.run_cli(["upload", str(site), "--csv", str(sheet), "--approve"])[0], 1)
        uploaded, invalid = self.results("row")
        self.assertEqual((uploaded["status"], uploaded["problems"]), ("uploaded", []))
        self.assertEqual(invalid["status"], "invalid")
        self.assertEqual(invalid["problems"][0]["column"], "total_outbound_bandwidth")

        taken = self.tmp / "taken.yml"
        taken.write_text('applianceInfo:\n  hostname: "h7"\n', encoding="utf-8")
        self.assertEqual(self.run_cli(["upload", "--yaml", str(taken), "--orchestrator", "lab"])[0], 0)
        [skipped] = self.results("file")
        self.assertEqual((skipped["name"], skipped["file"]), ("taken", str(taken)))
        self.assertTrue(skipped["status"].startswith("skipped"), skipped)

    def test_a_recorder_does_not_change_what_is_printed(self):
        with_recorder = self.run_cli(["build", "--orchestrator", "lab", "--appliance", "h1"])
        self.assertTrue(self.results("build"))
        self.context = RunContext(validators={"lab"}, out_dir=self.tmp / "plain", work_dir=self.tmp / "work")
        without = self.run_cli(["build", "--orchestrator", "lab", "--appliance", "h1"])
        self.assertEqual(without[0], with_recorder[0])
        self.assertEqual(without[1].replace(str(self.tmp / "plain"), str(self.tmp)), with_recorder[1])

    def test_a_secret_cell_is_never_recorded(self):
        # a template that does not hold the secret column: flow-through fails and its message names the cell
        template = self.tmp / "s.jinja2"
        template.write_text('applianceInfo:\n  hostname: "{{ data[\'hostname\'] }}"\nnac:\n  key: "fixed"\n', encoding="utf-8")
        meta = {"columns": {"hostname": {"path": "applianceInfo.hostname", "type": "str", "optional": False, "example": "h1"},
                            "nac_key": {"path": "nac.key", "type": "str", "optional": False, "example": "", "secret": True}}}
        (self.tmp / "columns.json").write_text(json.dumps(meta), encoding="utf-8")
        for secret in ("s3cr3t-value", "p\\q", "ab'c\"d", "abc "):  # repr escapes a backslash or both quotes; a trimmed value differs
            with self.subTest(secret=secret):
                sheet = self.tmp / "s.csv"
                with sheet.open("w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerows([["hostname", "nac_key"], ["h1", secret]])
                code, _ = self.run_cli(["render", "--template", str(template), "--csv", str(sheet), "--columns", str(self.tmp / "columns.json"), "--out", str(self.tmp / "r")])
                self.assertEqual(code, 1)
                row = self.results("row")[-1]
                self.assertEqual([(p["column"], p["value"]) for p in row["problems"]], [("nac_key", "<SECRET>")], row)
                recorded = "\n".join(map(str, [*row.values(), *(v for problem in row["problems"] for v in problem.values())]))
                for leak in (secret, secret.strip(), repr(secret)[1:-1], json.dumps(secret)[1:-1]):
                    self.assertNotIn(leak, recorded)
                self.assertIn("<SECRET>", row["problems"][0]["message"])

    def test_problem_data(self):
        problem = common.Problem("secret column k is empty", "k", "<SECRET>")
        self.assertEqual(problem, "secret column k is empty")
        self.assertEqual(common.problem_data(3, problem, {"k"}), {"row": 3, "column": "k", "value": "<SECRET>", "message": "secret column k is empty"})
        self.assertEqual(common.problem_data(3, "plain"), {"row": 3, "column": None, "value": None, "message": "plain"})
        self.assertEqual(common.problem_data(3, common.Problem("k: 'x1' bad", "k", "x1"), {"k"})["message"], "k: <SECRET> (a secret column's message is not recorded)")


class RecordedUploads(Fixture):
    def site(self):
        self.run_cli(["build", "--orchestrator", "lab", "--appliance", "h1"])
        self.run_cli(["variables", str(self.tmp / "preconfig-output" / "h1"), "--pick", "top10", "--yes", "--name", "s"])
        del self.recorder.results[:]
        return self.tmp / "preconfig-output" / "h1" / "site-s"

    def test_a_failed_upload_records_why(self):
        site = self.site()
        real = self.orch.request

        def refuse(method, path, **kw):
            if method == "POST" and path == "/gms/appliance/preconfiguration":
                raise common.ServerError("HTTP 500: busy", status=500)
            return real(method, path, **kw)
        with mock.patch.object(self.orch, "request", side_effect=refuse):
            self.assertEqual(self.run_cli(["upload", str(site), "--csv", str(site / "sites.csv"), "--approve"])[0], 1)
        [row] = self.results("row")
        self.assertEqual(row["status"], "upload-failed")
        self.assertEqual([p["message"] for p in row["problems"]], ["upload failed: HTTP 500: busy"])

    def test_rows_before_a_stopped_validation_are_recorded(self):
        site = self.site()
        with (site / "sites.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        sheet = self.tmp / "two.csv"
        with sheet.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows([rows[0], dict(rows[0], hostname="h9", preconfig_name="h9")])
        real, calls = self.orch.request, []

        def stop_second(method, path, **kw):
            if path == validate.PATH:
                calls.append(path)
                if len(calls) == 2:
                    raise common.AuthenticationError("HTTP 401", status=401)
            return real(method, path, **kw)
        with mock.patch.object(self.orch, "request", side_effect=stop_second):
            self.assertEqual(self.run_cli(["upload", str(site), "--csv", str(sheet)])[0], 2)
        [row] = self.results("row")
        self.assertEqual((row["index"], row["status"]), (1, "ready"))
        self.assertTrue(Path(row["file"]).is_file())


class DryRunThenSave(Fixture):
    def test_a_dry_run_writes_nothing_asks_nothing_calls_no_orchestrator_and_save_gives_its_columns(self):
        self.assertEqual(self.run_cli(["build", "--orchestrator", "lab", "--appliance", "h1"])[0], 0)
        folder = self.tmp / "preconfig-output" / "h1"
        flags = ["--pick", "top10", "--set", "deploymentInfo.deploymentMode,template_groups", "--fix", "template_groups"]
        del self.recorder.results[:]
        before = sorted((str(p), p.stat().st_mtime_ns) for p in self.tmp.rglob("*"))
        reports = self.tmp / "reports"
        with mock.patch.object(self.orch, "get", side_effect=AssertionError("GET during a dry run")), \
                mock.patch.object(self.orch, "request", side_effect=AssertionError("request during a dry run")), \
                mock.patch.dict(os.environ, {"EDGECONNECT_REPORT_DIR": str(reports)}):
            code, dry_text = self.run_cli(["variables", str(folder)] + flags + ["--dry-run"])  # stdin at its end: a question would end it with exit 1
        self.assertEqual(code, 0, dry_text)
        self.assertEqual(sorted((str(p), p.stat().st_mtime_ns) for p in self.tmp.rglob("*")), before)  # no file, not even the run log's
        [dry] = self.results("picker")
        rows = {r["path"]: r for r in dry["rows"]}
        self.assertTrue(rows["deploymentInfo.deploymentMode"]["on"])
        self.assertFalse(rows["templateGroups.groups"]["on"])  # --fix undid --set
        self.assertEqual(rows["applianceInfo.hostname"]["lines"], [2, 2])

        with mock.patch.dict(os.environ, {"EDGECONNECT_REPORT_DIR": str(reports)}):
            code, saved_text = self.run_cli(["variables", str(folder)] + flags + ["--name", "s", "--yes"])
        self.assertEqual(code, 0, saved_text)
        [saved] = self.results("picker")
        self.assertEqual(saved["columns"], dry["columns"])
        self.assertTrue(saved_text.startswith(dry_text))  # the same column line, then what Save wrote
        self.assertTrue((reports / "preconfig.jsonl").is_file())  # the Save is logged as before
        with (folder / "site-s" / "sites.csv").open(newline="", encoding="utf-8") as handle:
            self.assertEqual(set(next(csv.reader(handle))) - {"preconfig_name", "serial_number", "tag", "autoApply"}, set(dry["columns"]))


class ParallelBuilds(unittest.TestCase):
    def test_two_builds_and_a_render_in_parallel_each_get_only_their_own_lines(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(tmp), True)
        orch = Orchestrator()
        cap = write_capture(tmp / "cap")
        sheet = tmp / "r.csv"
        sheet.write_text("hostname\nr1\n", encoding="utf-8")
        template = tmp / "r.jinja2"
        template.write_text('applianceInfo:\n  hostname: "{{ data[\'hostname\'] }}"\n', encoding="utf-8")
        together = threading.Barrier(3, timeout=20)

        def run(name, argv):
            out = io.StringIO()
            with running(RunContext(validators={"lab"}, out_dir=tmp / name, work_dir=tmp / "work")), captured_output(out):
                together.wait()
                return cli.main(argv), out.getvalue()
        with mock.patch("capture.capture", return_value=cap), mock.patch.object(validate, "client_for", return_value=orch):
            a, b, r = in_threads(lambda: run("a", ["build", "--orchestrator", "lab", "--appliance", "h1"]),
                                 lambda: run("b", ["build", "--orchestrator", "lab", "--appliance", "h1"]),
                                 lambda: run("r", ["render", "--template", str(template), "--csv", str(sheet)]))
        self.assertEqual((a[0], b[0], r[0]), (0, 0, 0), (a, b, r))
        for (code, text), mine, others in ((a, "a", "br"), (b, "b", "ar"), (r, "r", "ab")):
            self.assertIn(str(tmp / mine / "preconfig-output"), text)
            for other in others:
                self.assertNotIn(str(tmp / other / "preconfig-output"), text)
        self.assertEqual(a[1].count("reading h1 from lab"), 1)
        self.assertNotIn("reading", r[1])

if __name__ == "__main__":
    unittest.main()

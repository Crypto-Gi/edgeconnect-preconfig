"""A run's printed lines, prompts and structured results (cli-requests R1, R3): quiet() and captured_output() capture only their own
run's lines, a prompt at the end of stdin ends the command instead of waiting, and the recorder keeps each command's results."""
import csv
import io
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "scripts"))
import cli  # noqa: E402  guard-ok: scripts are imported after the sys.path insert, as in the other test modules
import common  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
import upload  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
import validate  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
from common import Recorder, RunContext, captured_output, running  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
from test_golden import Orchestrator, write_capture  # noqa: E402  guard-ok: the shared synthetic capture and fake Orchestrator
from test_run_context import dotenv  # noqa: E402  guard-ok: the shared .env writer


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
        with tempfile.TemporaryDirectory() as tmp:
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

    def test_no_recorder_keeps_nothing_and_prints_the_same(self):
        self.context.recorder = None
        code, text = self.run_cli(["build", "--orchestrator", "lab", "--appliance", "h1"])
        self.assertEqual(code, 0)
        self.assertEqual(self.recorder.results, [])
        self.assertIn("Saved {}".format(self.tmp / "preconfig-output" / "h1"), text)

    def test_a_secret_value_is_never_recorded(self):
        problem = common.Problem("secret column k is empty", "k", "<SECRET>")
        self.assertEqual(problem, "secret column k is empty")
        self.assertEqual(common.problem_data(3, problem), {"row": 3, "column": "k", "value": "<SECRET>", "message": "secret column k is empty"})
        self.assertEqual(common.problem_data(3, "plain"), {"row": 3, "column": None, "value": None, "message": "plain"})


if __name__ == "__main__":
    unittest.main()

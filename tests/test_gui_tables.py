"""The run log tables (docs/gui/BUILD_PLAN.md step 7), as the automation GUI's test_gui_tables.py: the runs the CLI and the GUI log
in reports/preconfig.jsonl, one run's rows and its CSV exactly as the log holds them, the approval record of an upload, paging on
the server that stays fast over 10,000 runs, and the routes under the server's security (GET, cookie, validated run IDs)."""
import contextlib
import csv
import io
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_gui_server import ServerCase  # noqa: E402  guard-ok: the server tests' fixture (cookie, Host, fake Orchestrator)
from test_gui_tasks import Fixture  # noqa: E402  guard-ok: the shared build, site type and fake Orchestrator
import cli  # noqa: E402  guard-ok: imported after the sys.path insert
import common  # noqa: E402  guard-ok: imported after the sys.path insert
import gui_server  # noqa: E402  guard-ok: imported after the sys.path insert
import gui_tables  # noqa: E402  guard-ok: imported after the sys.path insert

ALL = {"size": "200"}
FORMULA = ("=", "+", "-", "@", "\t", "\r")


def logged_rows(log):
    """{run_id: its rows} straight from the log file, without gui_tables."""
    return {record["summary"]["run_id"]: record["summary"]["rows"] for record in map(json.loads, log.read_text(encoding="utf-8").splitlines())}


def as_csv_cells(rows):
    """The log's rows as a spreadsheet must read them: text, and a formula start behind a leading '."""
    return [{key: ("'" + text if text.startswith(FORMULA) else text) for key, text in ((key, str(row[key])) for key in gui_tables.ROW_FIELDS)} for row in rows]


class RunLogTests(Fixture):
    def setUp(self):
        super().setUp()
        self.reports = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.reports), True)
        patch = mock.patch.dict(common.os.environ, {"EDGECONNECT_REPORT_DIR": str(self.reports)})
        patch.start()
        self.addCleanup(patch.stop)

    def log_runs(self):
        """A GUI upload (its check and its approved run), a GUI validate with a bad row, and the same validate from the CLI."""
        tasks = self.manager()
        sent = self.wait(self.upload(tasks, "r-1"), "awaiting_approval")
        tasks.approve(sent.id, ["lab"])
        self.wait(sent, "done", "failed", "rejected")
        rows = [("r-2", {}), ("=HYPERLINK(1)", {"site": "REJECT"})]  # a name a spreadsheet would run as a formula
        check = self.wait(tasks.create({"action": "validate", "build": "h1", "site": "t", "csv_text": self.csv_text(rows)}), "done", "failed")
        self.assertEqual((sent.state, check.state), ("done", "failed"))
        sheet = self.tmp / "cli.csv"
        sheet.write_text(self.csv_text(rows), encoding="utf-8")
        with common.running(common.RunContext(dotenv=str(self.on), out_dir=self.tmp, work_dir=self.tmp / "work")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["validate", str(self.site), "--csv", str(sheet)]), 1)
        return self.reports / "preconfig.jsonl"

    def test_each_run_shows_as_the_log_records_it_with_its_rows_and_their_csv(self):
        log = self.log_runs()
        records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        table = gui_tables.RunLog(self.reports)
        runs, unreadable = table.runs()
        self.assertEqual((len(runs), unreadable), (len(records), 0))
        self.assertEqual(sorted((run["workflow"], run["source"], run["status"]) for run in runs), [  # the GUI's validate task runs render --site-dir
            ("preconfig render", "gui", "INVALID"), ("preconfig upload", "gui", "PLANNED"), ("preconfig upload", "gui", "SUCCESS"), ("preconfig validate", "cli", "INVALID")])
        rows = logged_rows(log)
        for record in records:
            summary = record["summary"]
            run = next(run for run in runs if run["run_id"] == summary["run_id"])
            for key in ("timestamp", "orchestrator", "workflow", "status", "changes_written", "duration_seconds", "source", "exit_code"):
                self.assertEqual(run[key], summary[key], key)
            self.assertEqual(run["row_outcomes"], ", ".join("{} {}".format(n, outcome) for outcome, n in sorted(summary["row_outcomes"].items())))
            detail = gui_tables.run_detail(table.find(summary["run_id"]), ALL)
            self.assertEqual([{key: row[key] for key in gui_tables.ROW_FIELDS} for row in detail["rows"]["rows"]], rows[summary["run_id"]])
            self.assertEqual((detail["run"]["counts"], detail["run"]["next_step"], detail["run"].get("approval")), (summary["counts"], summary["next_step"], summary.get("approval")))
            exported = list(csv.DictReader(io.StringIO(gui_tables.to_csv(gui_tables.select(table.find(summary["run_id"])["_outcomes"], {}, ("outcome",))[0], gui_tables.ROW_FIELDS).decode("utf-8"))))
            self.assertEqual(exported, as_csv_cells(rows[summary["run_id"]]), "the row CSV is the run log's rows")
        bad = next(run for run in runs if run["source"] == "cli")
        self.assertEqual((bad["tone"], bad["row_outcomes"]), ("bad", "1 INVALID, 1 VALID"))
        self.assertIn("'=HYPERLINK(1)", gui_tables.to_csv(table.find(bad["run_id"])["_outcomes"], gui_tables.ROW_FIELDS).decode("utf-8"))
        approved = next(run for run in runs if run["status"] == "SUCCESS")
        approval = gui_tables.run_detail(table.find(approved["run_id"]), {})["run"]["approval"]
        self.assertEqual((approval["orchestrator"], approval["typed"], len(approval["plan_fingerprint"])), ("lab", ["lab"], 64))

    def test_a_damaged_line_is_counted_and_the_rest_still_shown(self):
        log = self.log_runs()
        with log.open("a", encoding="utf-8") as handle:
            handle.write('{"half a record\n[1]\n{"summary": {"rows": [1]}}\n')
        runs, unreadable = gui_tables.RunLog(self.reports).runs()
        self.assertEqual((len(runs), unreadable), (4, 3))
        self.assertEqual(gui_tables.RunLog(self.reports / "missing").runs(), ([], 0))
        self.assertEqual(gui_tables.RunLog(None).runs(), ([], 0))

    def test_only_preconfigs_own_log_is_read(self):
        (self.reports / "firewall.jsonl").write_text(json.dumps({"summary": {"run_id": "run-other", "rows": []}}) + "\n", encoding="utf-8")
        self.assertEqual(gui_tables.RunLog(self.reports).runs(), ([], 0))


def rows(count):
    return [dict(name="row-{:05d}".format(number), size=number % 7, kind=("a", "b", "c")[number % 3], _problem=number % 10 == 0, _text="row-{:05d} kind-{}".format(number, ("a", "b", "c")[number % 3])) for number in range(count)]


class TableTests(unittest.TestCase):
    def test_search_filter_problems_sort_and_page(self):
        data = rows(1000)
        result = gui_tables.page(data, {"q": "KIND-B", "kind": "b", "problems": "1", "sort": "size", "order": "desc", "size": "25", "page": "2"}, ("kind",))
        self.assertEqual((result["total"], result["matching"], result["pages"], result["page"], len(result["rows"])), (1000, 33, 2, 2, 8))
        self.assertEqual(result["facets"], {"kind": ["a", "b", "c"]})
        self.assertNotIn("_text", result["rows"][0])
        sizes = [row["size"] for row in gui_tables.select(data, {"q": "kind-b", "problems": "1", "sort": "size", "order": "desc"}, ("kind",))[0]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_bad_paging_values_fall_back_and_unknown_sort_columns_are_ignored(self):
        result = gui_tables.page(rows(120), {"size": "7", "page": "99", "sort": "__class__"}, default_sort="name")
        self.assertEqual((result["size"], result["page"], result["sort"], result["rows"][0]["name"]), (50, 3, "name", "row-00100"))
        self.assertEqual(gui_tables.page(rows(3), {"page": "x"})["page"], 1)

    def test_digits_that_are_not_decimal_sort_as_text(self):
        data = [dict(id=value, _problem=False, _text="") for value in ("\u00b2", "10", "9")]
        self.assertEqual([row["id"] for row in gui_tables.select(data, {"sort": "id"})[0]], ["9", "10", "\u00b2"])

    def test_a_run_id_the_server_would_refuse_gets_a_line_id_that_opens(self):
        with tempfile.TemporaryDirectory() as directory:
            ids = ("run-ok_1", "run-../x", "run-\u00e9", "run-" + "a" * 80)
            (Path(directory) / "preconfig.jsonl").write_text("".join(json.dumps({"summary": {"run_id": run_id, "rows": []}}) + "\n" for run_id in ids), encoding="utf-8")
            log = gui_tables.RunLog(Path(directory))
            listed = sorted(run["run_id"] for run in log.runs()[0])
            self.assertEqual(listed, ["preconfig-line-2", "preconfig-line-3", "preconfig-line-4", "run-ok_1"])
            for run_id in listed:
                self.assertTrue(gui_tables.RUN_ID.match(run_id) and log.find(run_id), run_id)

    def test_numbers_and_numeric_text_sort_as_numbers_and_blanks_go_last(self):
        data = [dict(id=value, _problem=False, _text="") for value in ("10", "9", "", "100", None)]
        self.assertEqual([row["id"] for row in gui_tables.select(data, {"sort": "id"})[0]], ["9", "10", "100", "", None])
        self.assertEqual([row["id"] for row in gui_tables.select(data, {"sort": "id", "order": "desc"})[0]], ["100", "10", "9", "", None])

    def test_no_csv_cell_can_run_as_a_formula(self):
        text = gui_tables.to_csv([{"row": 1, "item": value, "scope": "", "outcome": "INVALID", "reason": "-1"} for value in ("=1+1", "+1", "@SUM(1)", "\tx", "\rx", "ok")], gui_tables.ROW_FIELDS)
        cells = [row["item"] for row in csv.DictReader(io.StringIO(text.decode("utf-8"), newline=""))]
        self.assertEqual(cells, ["'=1+1", "'+1", "'@SUM(1)", "'\tx", "'\rx", "ok"])
        self.assertNotIn(",-1", text.decode("utf-8"))

    def test_10000_runs_stay_responsive(self):
        """The browser only receives one page; loading, searching and sorting 10,000 logged runs stays fast on the server."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preconfig.jsonl"
            run_rows = [{"row": number, "item": "site-{}".format(number), "scope": "site-{}.yml".format(number), "outcome": "CREATED", "reason": ""} for number in range(1, 11)]
            with path.open("w") as handle:
                for number in range(10000):
                    summary = {"run_id": "run-{:012d}".format(number), "timestamp": "2026-10-26T{:02d}:{:02d}:{:02d}+00:00".format(number // 3600 % 24, number // 60 % 60, number % 60), "workflow": "preconfig upload", "status": "SUCCESS" if number % 50 else "PARTIAL", "orchestrator": ("lab", "lab2")[number % 2], "changes_written": "yes", "exit_code": 0, "duration_seconds": 1.5, "source": "cli", "row_outcomes": {"CREATED": 10}, "rows": run_rows, "counts": [], "blocking_issues": []}
                    handle.write(json.dumps({"status": summary["status"], "summary": summary, "outputs": []}) + "\n")
            log = gui_tables.RunLog(Path(directory))
            started = time.monotonic()
            runs = log.runs()[0]
            loaded = time.monotonic() - started
            started = time.monotonic()
            result = gui_tables.page(runs, {"q": "run-0000000012", "orchestrator": "lab", "sort": "status", "size": "50"}, gui_tables.RUN_FILTERS, "timestamp", True)
            problems = gui_tables.page(runs, {"problems": "1"}, gui_tables.RUN_FILTERS, "timestamp", True)
            queried = time.monotonic() - started
            with mock.patch.object(gui_tables, "_read", wraps=gui_tables._read) as read:
                self.assertEqual(len(log.runs()[0]), 10000)
                read.assert_not_called()  # an unchanged log is not read again
        self.assertEqual((len(runs), result["matching"], len(result["rows"]), problems["matching"]), (10000, 50, 50, 200))
        self.assertLess(loaded, 15, "first load of 10,000 runs")
        self.assertLess(queried, 1, "two queries over 10,000 runs")


class RouteTests(ServerCase):
    APPROVAL = {"by": "someone", "time": "2026-10-30T10:00:00+00:00", "plan_fingerprint": "sha256:abc", "orchestrator": "lab", "typed_orchestrator": "lab", "typed": ["lab"]}

    def setUp(self):
        super().setUp()
        self.reports = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.reports), True)
        run_rows = [{"row": 1, "item": "=cmd|' /C calc'!A0", "scope": "a.yml", "outcome": "CREATED", "reason": ""}, {"row": 2, "item": "b", "scope": "b.yml", "outcome": "FAILED", "reason": "refused"}]
        lines = []
        for number, (source, approval) in enumerate((("gui", self.APPROVAL), ("cli", None))):
            summary = {"run_id": "run-{:012d}".format(number), "timestamp": "2026-10-30T10:0{}:00+00:00".format(number), "workflow": "preconfig upload", "status": "PARTIAL",
                       "orchestrator": "lab", "changes_written": "possibly; see the result details", "exit_code": 1, "duration_seconds": 2.0, "source": source,
                       "row_outcomes": {"CREATED": 1, "FAILED": 1}, "rows": run_rows, "counts": ["1 CREATED", "1 FAILED"], "blocking_issues": [], "command": "edgeconnect-preconfig upload x"}
            if approval:
                summary["approval"] = approval
            lines.append(json.dumps({"summary": summary}))
        (self.reports / "preconfig.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.server.runlog = gui_tables.RunLog(self.reports)

    def test_the_server_reads_the_run_log_folder_it_was_given(self):
        servers = [gui_server.GuiServer(self.manager(), 0, folder) for folder in (str(self.reports), None)]
        for server in servers:
            self.addCleanup(server.server_close)
        self.assertEqual([server.runlog.directory for server in servers], [self.reports, None])

    def test_runs_and_a_run_page_and_their_csvs(self):
        response, text = self.request("GET", "/api/runs?source=gui&size=25")
        data = json.loads(text)
        self.assertEqual((response.status, data["total"], data["matching"], data["folder"], data["facets"]["source"]), (200, 2, 1, str(self.reports), ["cli", "gui"]))
        self.assertEqual(data["rows"][0]["run_id"], "run-000000000000")
        response, text = self.request("GET", "/api/runs.csv?problems=1")
        self.assertEqual((response.status, response.headers["Content-Type"], response.headers["Content-Disposition"]), (200, "text/csv; charset=utf-8", 'attachment; filename="runs.csv"'))
        self.assertEqual(text.splitlines()[0], ",".join(gui_tables.RUN_FIELDS + ("exit_code",)))
        self.assertEqual(len(text.splitlines()), 3)
        response, text = self.request("GET", "/api/runs/run-000000000000?outcome=FAILED")
        detail = json.loads(text)
        self.assertEqual((response.status, detail["run"]["approval"], [row["item"] for row in detail["rows"]["rows"]]), (200, self.APPROVAL, ["b"]))
        self.assertNotIn("approval", json.loads(self.request("GET", "/api/runs/run-000000000001")[1])["run"])
        response, text = self.request("GET", "/api/runs/run-000000000000/rows.csv")
        self.assertEqual((response.status, response.headers["Content-Disposition"]), (200, 'attachment; filename="run-000000000000-rows.csv"'))
        self.assertEqual(list(csv.DictReader(io.StringIO(text))), as_csv_cells(json.loads((self.reports / "preconfig.jsonl").read_text().splitlines()[0])["summary"]["rows"]))

    def test_run_ids_are_checked_and_nothing_here_takes_a_post_or_skips_the_cookie(self):
        for path in ("/api/runs/", "/api/runs/..%2F..%2Fetc%2Fpasswd", "/api/runs/../tasks", "/api/runs/run-1.jsonl", "/api/runs/run-000000000000/revert",
                     "/api/runs/run-000000000000/rows.csv/x", "/api/runs/" + "a" * 81, "/api/runs/run-unknown", "/api/runs/run-000000000000/x.csv"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0].status, 404)
        self.assertEqual(self.request("POST", "/api/runs", {})[0].status, 404)
        for path in ("/api/runs", "/api/runs.csv", "/api/runs/run-000000000000", "/api/runs/run-000000000000/rows.csv"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, cookie=False)[0].status, 401)
                self.assertEqual(self.request("GET", path, host="evil.example:{}".format(self.port))[0].status, 421)

    def test_secrets_in_a_hand_made_line_never_reach_a_page_or_a_csv(self):
        row = {"row": 1, "item": "api_key=SEKRET", "scope": "s.yml", "outcome": "FAILED", "reason": "refused: Authorization: Bearer abc123", "password": "plain"}
        summary = {"run_id": "run-secret", "timestamp": "2026-10-30T11:00:00+00:00", "workflow": "preconfig upload", "status": "FAILED", "password": "plain",
                   "command": "edgeconnect-preconfig upload x api_key=SEKRET", "blocking_issues": ["Authorization: Bearer abc123"], "rows": [row]}
        with (self.reports / "preconfig.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"summary": summary}) + "\n")
        for path in ("/api/runs/run-secret", "/api/runs/run-secret/rows.csv", "/api/runs?q=run-secret", "/api/runs.csv?q=run-secret"):
            with self.subTest(path=path):
                response, text = self.request("GET", path)
                self.assertEqual(response.status, 200)
                self.assertIn("<REDACTED>" if "rows.csv" in path else "run-secret", text)  # the row CSV names no run
                for leak in ("plain", "SEKRET", "abc123"):
                    self.assertNotIn(leak, text)
        detail = json.loads(self.request("GET", "/api/runs/run-secret")[1])
        self.assertEqual((detail["run"]["password"], detail["rows"]["rows"][0]["item"]), ("<REDACTED>", "api_key=<REDACTED>"))

    def test_with_the_run_log_off_the_table_says_so(self):
        self.server.runlog = gui_tables.RunLog(None)
        data = json.loads(self.request("GET", "/api/runs")[1])
        self.assertEqual((data["total"], data["folder"]), (0, None))


if __name__ == "__main__":
    unittest.main()

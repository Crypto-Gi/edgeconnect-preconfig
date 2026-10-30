"""Build and "choose what changes per site" (docs/gui/BUILD_PLAN.md step 8) through the task layer and the server: the preview
writes nothing and calls no Orchestrator, Save gives the CLI's own files byte for byte, --fix undoes --set, a locked row is
refused with its reason, the YAML viewer never shows a secret, and a build streams its capture's request log."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_gui_tasks import ENV, Fixture  # noqa: E402  guard-ok: sets the paths and the run log off; the shared build h1 and fake Orchestrator
from test_gui_server import ServerCase  # noqa: E402  guard-ok: the server tests' request helper
from test_golden import write_capture  # noqa: E402  guard-ok: the shared synthetic capture
import capture  # noqa: E402  guard-ok: imported after the sys.path insert
import cli  # noqa: E402  guard-ok: imported after the sys.path insert
import common  # noqa: E402  guard-ok: imported after the sys.path insert
import validate  # noqa: E402  guard-ok: imported after the sys.path insert
from gui_tasks import TaskManager  # noqa: E402  guard-ok: imported after the sys.path insert


def files(folder):
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def picker(task):
    return [r for r in task.result.outputs if r["kind"] == "picker"][0]


class PickTests(Fixture):
    def preview(self, tasks, **request):
        return self.wait(tasks.create(dict({"action": "variables", "build": "h1", "pick": "top10"}, **request)), "done", "failed")

    def free(self):
        """Paths of rows a person may flip, from a Top 10 preview: some off (to tick) and some on (to untick)."""
        rows = picker(self.preview(self.manager()))["rows"]
        free = [r for r in rows if not r["locked"] and r["follows"] is None]
        return [r["path"] for r in free if not r["on"]], [r["path"] for r in free if r["on"]]

    def test_save_with_top10_and_extras_gives_the_files_of_the_same_cli_command_byte_for_byte(self):
        off, on = self.free()
        extras, unticks = off[:2], on[:1]
        self.assertEqual(len(extras), 2)
        task = self.wait(self.manager().create({"action": "variables", "build": "h1", "pick": "top10", "set": extras, "fix": unticks, "save": True, "name": "g"}),
                         "done", "failed")
        self.assertEqual(task.state, "done", task.result.text)
        site = self.build / "site-g"
        self.assertEqual(picker(task)["folder"], str(site))
        by_gui = files(site)
        self.assertIn("g.jinja2", by_gui)
        self.assertIn("sites.csv", by_gui)
        argv = ["variables", str(self.build), "--pick", "top10", "--set", ",".join(extras)] + (["--fix", ",".join(unticks)] if unticks else []) + ["--name", "g", "--yes"]
        context = common.RunContext(recorder=common.Recorder(), dotenv=str(self.on), out_dir=self.tmp, work_dir=self.tmp / "work")
        with common.running(context), contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(out), mock.patch.object(sys, "stdin", io.StringIO()):
            code = cli.main(argv)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(files(site), by_gui)  # the CLI's site-g (the GUI's was kept as site-g.previous-...)
        self.assertEqual(len(list(self.build.glob("site-g.previous-*"))), 1)

    def test_fix_undoes_set_and_a_locked_row_is_refused_with_its_reason(self):
        tasks = self.manager()
        (path,), plain = self.free()[0][:1], picker(self.preview(tasks))
        ticked = picker(self.preview(tasks, set=[path]))
        self.assertTrue({r["path"]: r for r in ticked["rows"]}[path]["on"])
        undone = picker(self.preview(tasks, set=[path], fix=[path]))
        self.assertEqual((undone["columns"], {r["path"]: r for r in undone["rows"]}[path]["on"]), (plain["columns"], False))
        refused = self.preview(tasks, fix=["applianceInfo.hostname"])
        self.assertEqual((refused.state, [r["kind"] for r in refused.result.outputs]), ("failed", ["dry-run"]))
        self.assertIn("error: --fix: applianceInfo.hostname cannot be fixed: always per site, hostname", refused.result.text)
        site = self.preview(tasks, fix=["applianceInfo.site"])
        self.assertIn("cannot be fixed: always per site, deployment-page value (docs/decisions/0006)", site.result.text)

    def test_a_preview_writes_nothing_and_calls_no_orchestrator(self):
        before = sorted((str(p), p.stat().st_mtime_ns) for p in self.tmp.rglob("*"))
        off, on = self.free()
        task = self.preview(self.manager(), pick="top20", set=off[:3], fix=on[:1])
        self.assertEqual(task.state, "done", task.result.text)
        self.assertIsNone(picker(task)["folder"])
        self.assertEqual(sorted((str(p), p.stat().st_mtime_ns) for p in self.tmp.rglob("*")), before)
        self.assertEqual(self.orch.calls, [])

    def test_save_validates_only_on_a_validator(self):
        tasks = self.manager(self.dotenv("none.env", ENV.replace("EC_VALIDATORS=lab", "EC_VALIDATORS=other")))
        task = self.wait(tasks.create({"action": "variables", "build": "h1", "pick": "top10", "save": True, "name": "nv"}), "done", "failed")
        self.assertEqual(task.state, "failed")
        self.assertIn("lab is not a validator Orchestrator", task.result.text)
        self.assertEqual((self.orch.calls, (self.build / "site-nv").exists()), ([], False))

    def test_the_yaml_viewer_and_the_summary_never_show_a_secret(self):
        build = self.tmp / "preconfig-output" / "h9"
        shutil.copytree(str(self.build), str(build), ignore=shutil.ignore_patterns("site-*"))
        with (build / "reference.yaml").open("a", encoding="utf-8") as handle:
            handle.write("nac:\n  server:\n  - host: 10.0.0.5\n    key: Rad-Key-33\nbanners:\n  adminPassword: Adm1n-Pw-77\n  note: <SECRET:$.x.y>\n")
        with (build / "summary.md").open("a", encoding="utf-8") as handle:
            handle.write("- quoted: Adm1n-Pw-77 and <SECRET:$.x.y>\n")
        tasks = self.manager()
        shown = tasks.build_text("h9", "reference")
        raw = (build / "reference.yaml").read_text(encoding="utf-8")
        for secret in ("Rad-Key-33", "Adm1n-Pw-77", "<SECRET:"):
            self.assertNotIn(secret, shown["text"])
        self.assertEqual(len(shown["text"].splitlines()), len(raw.splitlines()))  # line for line, so the highlights land
        self.assertEqual(shown["text"].splitlines()[raw.splitlines().index("    key: Rad-Key-33")], "    key: <SECRET>")
        summary = tasks.build_text("h9", "summary")
        self.assertIn("Verdict:", summary["text"])
        self.assertTrue(summary["text"].endswith("- quoted: <SECRET> and <SECRET>\n"))
        for bad in (("h9", "columns"), ("../h9", "reference"), ("missing", "summary")):
            with self.assertRaises(Exception):
                tasks.build_text(*bad)

    def test_a_build_streams_its_capture_request_log(self):
        def reading(orchestrator, appliance, role, pause, out_root, *args, **kwargs):
            other = out_root / ".incomplete-{}-b-20261030T000000Z-1".format(appliance) / "requests.jsonl"  # another box: h1-b
            other.parent.mkdir(parents=True)
            other.write_text(json.dumps({"method": "GET", "path": "/not/this/box", "status": "ok"}) + "\n", encoding="utf-8")
            os.utime(str(other), (time.time() + 60, time.time() + 60))  # the newest log there, yet not this box's
            log = out_root / ".incomplete-{}-20261030T000000Z-1".format(appliance) / "requests.jsonl"
            log.parent.mkdir(parents=True)
            for path in ("/gms/versions", "/appliance", "/reachability/gms"):
                with log.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"method": "GET", "path": path, "status": "ok", "message": "never shown"}) + "\n")
                time.sleep(0.3)
            shutil.rmtree(str(log.parent))
            return write_capture(Path(tempfile.mkdtemp(dir=str(self.tmp))) / "cap")
        patch = mock.patch.object(capture, "capture", side_effect=reading)
        patch.start()
        self.addCleanup(patch.stop)
        elsewhere = Path(tempfile.mkdtemp(dir=str(self.tmp)))
        tasks = TaskManager(str(self.on), str(elsewhere), str(elsewhere / "work"))
        task = self.wait(tasks.create({"action": "build", "orchestrator": "lab", "hostname": "h1", "nepk": "9.NE"}), "done", "failed")
        self.assertEqual(task.state, "done", task.result.text)
        progress = [(e["done"], e["progress"]) for e in task.events if "progress" in e]
        self.assertEqual(progress, [(1, "GET /gms/versions ok"), (2, "GET /appliance ok"), (3, "GET /reachability/gms ok")])
        self.assertLess([i for i, e in enumerate(task.events) if "progress" in e][-1], [i for i, e in enumerate(task.events) if e.get("state") == "done"][0])

    def test_list_gives_the_appliance_rows_the_build_card_sends(self):
        task = self.wait(self.manager().create({"action": "list", "orchestrator": "lab", "search": "h"}), "done", "failed")
        found = [r for r in task.full_result()["outputs"] if r["kind"] == "appliance"]  # build.js reads these (R8)
        self.assertEqual([(a["hostname"], a["nepk"], a["reachable"]) for a in found], [("h1", "9.NE", "yes"), ("h2", "8.NE", "NO")])
        self.assertEqual(self.manager().builds()["builds"][0]["site_type"], "h1")


class PickServerTests(ServerCase):
    def test_the_viewer_texts_are_served_as_json_and_only_for_a_build(self):
        response, text = self.request("GET", "/api/builds/h1/reference")
        self.assertEqual(response.status, 200, text)
        body = json.loads(text)
        self.assertEqual(body["text"], (self.build / "reference.yaml").read_text(encoding="utf-8"))  # no secret in this one
        self.assertEqual(self.request("GET", "/api/builds/h1/summary")[0].status, 200)
        for path in ("/api/builds/h1/columns", "/api/builds/..%2Fh1/reference", "/api/builds/missing/summary"):
            self.assertIn(self.request("GET", path)[0].status, (400, 404), path)
        self.assertEqual(self.request("GET", "/api/builds/h1/reference", cookie=False)[0].status, 401)


if __name__ == "__main__":
    unittest.main()

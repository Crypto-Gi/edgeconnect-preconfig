"""The upload approval hook (cli-requests R2): a fingerprint of what would be sent, recorded on every run with ready rows,
and the run's approver called with it just before the first upload; an approver that raises means nothing is sent."""
import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "scripts"))
import common  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
import upload  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
from common import Recorder, RunContext, running  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
from generate import generate  # noqa: E402  guard-ok: scripts are imported after the sys.path insert


class Client:
    """An Orchestrator with preconfigs `existing`, a serial from /gmsserver/info and a base URL; counts upload POSTs."""

    def __init__(self, serial="00-00-00-00-00-00", url="https://orch.example.com/gms/rest", existing=()):
        self.write_requests, self.posts, self.serial, self.existing = 0, [], serial, list(existing)
        self.config = SimpleNamespace(base_url=url)

    def get(self, path, query=None):
        if path == "/gmsserver/info":
            return {"serialNumber": self.serial}
        if path == "/gms/appliance/preconfiguration":
            return list(self.existing)
        raise AssertionError("unexpected GET " + path)

    def request(self, method, path, query=None, json_body=None, expected_status=(200,), expect_json=True, read_only=False):
        if path == "/gms/appliance/preconfiguration" and method == "POST":
            self.posts.append(json_body)
            self.existing.append({"id": len(self.posts), "name": json_body["name"]})
            return {"id": len(self.posts)}
        if path.endswith("/validate") and read_only:
            return b""
        raise AssertionError("unexpected {} {}".format(method, path))


class Fingerprint(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        meta = generate({"applianceInfo": {"hostname": "br-1", "site": "s1"}}, self.tmp, "br", decide=lambda p, v, s: p == "applianceInfo.hostname")
        self.columns = meta["columns"]
        self.sheet(["br-2", "br-3"])

    def sheet(self, names, hostname=None):
        with (self.tmp / "rows.csv").open("w", newline="", encoding="utf-8") as handle:
            w = csv.DictWriter(handle, fieldnames=["hostname", "preconfig_name", "serial_number"])
            w.writeheader()
            w.writerows({"hostname": hostname or n, "preconfig_name": n, "serial_number": ""} for n in names)

    def run_upload(self, client, approve=False, approver=None):
        recorder, err = Recorder(), io.StringIO()
        argv = ["--template", str(self.tmp / "br.jinja2"), "--csv", str(self.tmp / "rows.csv"), "--columns", str(self.tmp / "columns.json"),
                "--out", str(self.tmp / "out"), "--orchestrator", "lab"] + (["--approve"] if approve else [])
        with mock.patch.object(upload, "client_for", return_value=client), running(RunContext(recorder=recorder, approver=approver)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = upload.main(argv)
        prints = [r["value"] for r in recorder.results if r["kind"] == "fingerprint"]
        return code, (prints[0] if prints else None), err.getvalue()

    def fingerprint(self, client=None):
        code, value, _ = self.run_upload(client or Client())
        self.assertEqual(code, 0)
        self.assertRegex(value or "", "^[0-9a-f]{64}$")
        return value

    def test_the_same_inputs_give_the_same_fingerprint(self):
        self.assertEqual(self.fingerprint(), self.fingerprint())

    def test_each_input_changes_it(self):
        base = self.fingerprint()
        self.sheet(["br-2", "br-3"], hostname="other")  # a CSV cell
        self.assertNotEqual(self.fingerprint(), base)
        self.sheet(["br-2", "br-3"])
        self.assertEqual(self.fingerprint(), base)

        template = self.tmp / "br.jinja2"
        original = template.read_bytes()
        template.write_bytes(original + b"\n")  # a template byte
        self.assertNotEqual(self.fingerprint(), base)
        template.write_bytes(original)

        columns = self.tmp / "columns.json"
        original = columns.read_bytes()
        columns.write_bytes(original + b"\n")  # columns.json bytes
        self.assertNotEqual(self.fingerprint(), base)
        columns.write_bytes(original)

        self.assertNotEqual(self.fingerprint(Client(existing=[{"id": 1, "name": "br-3"}])), base)  # br-3 conflicts: one ready row fewer
        self.assertNotEqual(self.fingerprint(Client(serial="SN-1")), base)  # another Orchestrator serial
        self.assertNotEqual(self.fingerprint(Client(url="https://other.example.com/gms/rest")), base)  # another Orchestrator address
        self.assertEqual(self.fingerprint(Client(serial="")), base)  # the placeholder counts as no serial

    def test_no_ready_row_records_no_fingerprint(self):
        client = Client(existing=[{"id": 1, "name": "br-2"}, {"id": 2, "name": "br-3"}])
        self.assertEqual(self.run_upload(client)[1], None)

    def test_the_approver_gets_the_fingerprint_the_check_run_showed_then_rows_are_sent(self):
        shown = self.fingerprint()
        seen, client = [], Client()
        code, recorded, _ = self.run_upload(client, approve=True, approver=seen.append)
        self.assertEqual((code, seen, recorded), (0, [shown], shown))
        self.assertEqual([p["name"] for p in client.posts], ["br-2", "br-3"])

    def test_an_approver_that_raises_sends_nothing(self):
        def refuse(fingerprint):
            raise PermissionError("fingerprint changed\nsince the check")
        client = Client()
        code, _, err = self.run_upload(client, approve=True, approver=refuse)
        self.assertEqual((code, client.posts, client.write_requests), (2, [], 0))
        self.assertEqual(err, "NOT APPROVED, nothing uploaded: fingerprint changed since the check\n")
        self.assertFalse((self.tmp / "out" / "upload-report.json").exists())

    def test_without_an_approver_approve_uploads_as_before(self):
        client = Client()
        self.assertEqual(self.run_upload(client, approve=True)[0], 0)
        self.assertEqual(len(client.posts), 2)
        self.assertEqual(json.loads((self.tmp / "out" / "upload-report.json").read_text())["summary"]["uploaded"], 2)

    def test_an_unreadable_server_info_still_gives_a_fingerprint(self):
        client = Client()
        client.get = lambda path, query=None: (_ for _ in ()).throw(common.EdgeConnectError("HTTP 404")) if path == "/gmsserver/info" else []
        self.assertEqual(self.fingerprint(client), self.fingerprint())  # the serial counts as none, as with the placeholder


if __name__ == "__main__":
    unittest.main()

"""The preconfigs verb (cli-requests R7): lists the preconfigs on an Orchestrator and prints one as redacted YAML, GET only.

A secret never reaches stdout, stderr, the recorder or the run log, also one whose " breaks the YAML, and configData is never
listed or recorded."""
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "scripts"))
os.environ["EDGECONNECT_REPORT_DIR"] = ""  # each test names its own folder
import cli  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
from common import Recorder, RunContext, running  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
from test_golden import PRECONFIGS, SECRETS  # noqa: E402  guard-ok: the golden fixture's preconfigs


class Client:
    """Answers the preconfig list; every request it is asked to send is kept with its method."""

    def __init__(self, writes=0):
        self.sent, self.write_requests = [], writes
        self.config = SimpleNamespace(base_url="https://orch.example.com/gms/rest")

    def get(self, path, query=None):
        self.sent.append(("GET", path))
        if path != "/gms/appliance/preconfiguration":
            raise AssertionError("unexpected GET " + path)
        return copy.deepcopy(PRECONFIGS)

    def request(self, method, path, **kw):
        self.sent.append((method.upper(), path))
        raise AssertionError("unexpected {} {}".format(method, path))


class Preconfigs(unittest.TestCase):
    def run_cli(self, argv, client=None):
        """cli.main with a recorder and the run log in a temp folder; returns code, stdout, stderr, the results and the log line."""
        self.client = client or Client()
        recorder = Recorder()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"EDGECONNECT_REPORT_DIR": tmp}), \
                mock.patch.object(cli, "client_for", return_value=self.client), running(RunContext(recorder=recorder)), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main(argv)
            log = (Path(tmp) / "preconfig.jsonl").read_text(encoding="utf-8")
        self.assertEqual({m for m, _ in self.client.sent}, {"GET"}, "GET only")
        return code, out.getvalue(), err.getvalue(), recorder.results, log

    def assertNoSecret(self, *texts):
        for text in texts:
            for secret in SECRETS + tuple(p["configData"] for p in PRECONFIGS if p.get("configData")):
                self.assertNotIn(secret, text)

    def test_the_list_shows_and_records_the_rows_without_config_data(self):
        code, out, err, results, log = self.run_cli(["preconfigs", "--orchestrator", "lab"])
        self.assertEqual((code, err), (0, ""))
        for name in ("br-1", "br-2", "br-3", "br-4", "001BBC000001", "east", "1761782400000"):
            self.assertIn(name, out)
        rows = [r for r in results if r["kind"] == "preconfig"]
        self.assertEqual(rows[0], {"kind": "preconfig", "name": "br-1", "serial": "001BBC000001", "tag": "east", "last_change": 1761782400000, "id": 7})
        self.assertEqual([r["id"] for r in rows], [7, 8, 9, 10])
        self.assertNoSecret(out, json.dumps(results), log)
        self.assertEqual(self.client.sent, [("GET", "/gms/appliance/preconfiguration")])

    def test_show_prints_the_yaml_with_every_secret_redacted(self):
        for wanted in ("br-1", "7"):
            with self.subTest(wanted=wanted):
                code, out, err, results, log = self.run_cli(["preconfigs", "--orchestrator", "lab", "--show", wanted])
                self.assertEqual((code, err), (0, ""))
                self.assertIn("hostname: br-1", out)
                for path in ("applianceInfo.adminPassword", "ikeConfig.presharedKey", "nac.server[0].key"):
                    self.assertIn("<SECRET:$.{}>".format(path), out)
                self.assertIn("password: <SECRET:text>", out)  # the banner's free text
                shown = [r for r in results if r["kind"] == "preconfig-yaml"]
                self.assertEqual(len(shown), 1)
                self.assertIn(shown[0]["text"], out)
                self.assertNoSecret(out, json.dumps(results), log)

    def test_a_yaml_that_does_not_parse_prints_where_never_its_text(self):
        for wanted in ("br-2", "8"):
            with self.subTest(wanted=wanted):
                code, out, err, results, log = self.run_cli(["preconfigs", "--orchestrator", "lab", "--show", wanted])
                self.assertEqual((code, out), (2, ""))
                self.assertIn("does not parse (line 3, column 26)", err)
                self.assertNotIn("adminPassword", err)
                self.assertNoSecret(err, json.dumps(results), log)

    def test_a_preconfig_it_cannot_show_is_one_error_line(self):
        for wanted, why in (("nope", "0 matches by name or id"), ("br-3", "holds no configData"), ("br-4", "not base64")):
            with self.subTest(wanted=wanted):
                code, out, err, _, _ = self.run_cli(["preconfigs", "--orchestrator", "lab", "--show", wanted])
                self.assertEqual((code, out, err.count("\n")), (2, "", 1))
                self.assertIn(why, err)

    def test_a_write_counted_on_the_client_stops_it(self):
        code, out, err, _, _ = self.run_cli(["preconfigs", "--orchestrator", "lab"], Client(writes=1))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("read-only", err)


if __name__ == "__main__":
    unittest.main()

"""The preconfigs verb (cli-requests R7): lists the preconfigs on an Orchestrator and prints one as redacted YAML, GET only.

A secret never reaches stdout, stderr, the recorder or the run log, also one whose " breaks the YAML, and configData is never
listed or recorded."""
import base64
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
from common import Recorder, RunContext, redact_secrets, running  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
from test_golden import PRECONFIGS, SECRETS  # noqa: E402  guard-ok: the golden fixture's preconfigs


def stored(ident, name, text):
    return {"id": ident, "name": name, "configData": base64.b64encode(text.encode()).decode()}


class Client:
    """Answers the preconfig list; any other call (request, post, ...) is only kept, so the GET-only assertion names it."""

    def __init__(self, writes=0, items=None):
        self.sent, self.write_requests, self.items = [], writes, PRECONFIGS if items is None else items
        self.config = SimpleNamespace(base_url="https://orch.example.com/gms/rest")

    def get(self, path, query=None):
        self.sent.append(("GET", path))
        return copy.deepcopy(self.items) if path == "/gms/appliance/preconfiguration" else None

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *args, **kw: self.sent.append((name, args))


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
        self.assertEqual({m for m, _ in self.client.sent}, {"GET"}, "GET only: {}".format(self.client.sent))
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

    def show(self, text, items=None, wanted="x"):
        return self.run_cli(["preconfigs", "--orchestrator", "lab", "--show", wanted], Client(items=items or [stored(1, "x", text)]))

    def test_every_value_under_a_secret_key_is_redacted(self):
        cases = {"adminPassword: 2024-01-15": "2024", "presharedKey: 2001-12-14 21:59:43.10": "2001", "password: !!binary aHVudGVyMg==": "aHVudGVyMg",
                 "password: !!set {hunter2: null}": "hunter2", "password: true": "true", "presharedKey: {value: hunter2}": "hunter2",
                 "password: [hunter2, 12]": "hunter2", "password: 1234": "1234"}
        for line, secret in cases.items():
            with self.subTest(line=line):
                code, out, err, results, log = self.show("applianceInfo:\n  hostname: br-9\n  " + line + "\n")
                self.assertEqual((code, err), (0, ""))
                key = line.split(":")[0]
                self.assertIn("{}: <SECRET:$.applianceInfo.{}>".format(key, key), out)
                for text in (out.split("\n", 1)[1], json.dumps([r for r in results if r["kind"] == "preconfig-yaml"])):
                    self.assertNotIn(secret, text)
        code, out, _, _, _ = self.show("applianceInfo:\n  password: null\n")
        self.assertIn("password: null", out)  # nothing there, nothing to hide

    def test_a_tag_that_hides_a_mapping_never_shows_its_secret(self):
        for line in ("x: !!omap [{password: hunter2}]", "x: !!pairs [{password: hunter2}]", 'x: !!set {"password: hunter2": null}'):
            with self.subTest(line=line):
                code, out, err, results, log = self.show(line + "\n")
                self.assertEqual((code, err), (0, ""))
                self.assertIn("<SECRET:$.x", out)  # the whole set, or each pair of the list
                self.assertNotIn("hunter2", out + json.dumps(results) + log)

    def test_captures_redact_as_before(self):
        value = {"password": True, "md5": 5, "token": "", "psk": {"note": "x"}, "secret": ["s1"], "when": (1, 2)}
        self.assertEqual(redact_secrets(value), ({"password": True, "md5": 5, "token": "", "psk": {"note": "x"}, "secret": ["<SECRET:$.secret[0]>"], "when": (1, 2)},
                                                 ["$.secret[0]"]))

    def test_a_yaml_with_an_alias_is_not_shown(self):
        """An alias copies a value onto another key, so the walk would redact it under the secret key and print it under the other."""
        refusal = "error: preconfig x (id 1): its YAML uses aliases (*name), which can copy a secret onto a key that is not secret; it is not shown\n"
        for text, secret in (("applianceInfo:\n  adminPassword: &p hunter2\n  hostname: *p\n", "hunter2"),
                             ("a: &n 123456\nikeConfig:\n  presharedKey: *n\n", "123456"),
                             ("base: &b {password: hunter2}\nother:\n  <<: *b\n", "hunter2"),  # a merge key is an alias too
                             ("a: &a [*a]\n", None)):  # one that refers to itself
            with self.subTest(text=text):
                code, out, err, results, log = self.show(text)
                self.assertEqual((code, out, err), (2, "", refusal))
                self.assertEqual([r for r in results if r["kind"] == "preconfig-yaml"], [])
                if secret:
                    self.assertNotIn(secret, out + err + json.dumps(results) + log)
        code, out, err, _, _ = self.show("a: " + "{b: " * 450 + "1" + "}" * 450 + "\n")  # parses, but too deep to walk or dump
        self.assertEqual((code, out, err), (2, "", "error: preconfig x (id 1): its YAML nests too deep, so it is not shown\n"))
        code, out, _, _, _ = self.show("applianceInfo:\n  hostname: '*p and &p are text here'\n")
        self.assertEqual(code, 0)  # an asterisk inside a quoted value is not an alias
        self.assertIn("*p and &p are text here", out)

    def test_an_id_wins_over_a_name_and_a_shared_name_lists_its_ids(self):
        items = [stored(1, "2", "applianceInfo:\n  hostname: by-name\n"), stored(2, "x", "applianceInfo:\n  hostname: by-id\n"),
                 stored(3, "x", "applianceInfo:\n  hostname: other\n")]
        code, out, _, _, _ = self.show("", items, "2")
        self.assertEqual(code, 0)
        self.assertIn("hostname: by-id", out)
        code, out, err, _, _ = self.show("", items, "x")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, "error: preconfig 'x': 2 preconfigs have that name on lab (ids 2, 3): give the id\n")

    def test_a_write_counted_on_the_client_stops_it(self):
        code, out, err, _, _ = self.run_cli(["preconfigs", "--orchestrator", "lab"], Client(writes=1))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("read-only", err)


if __name__ == "__main__":
    unittest.main()

"""upload --yaml and validate --yaml: existing preconfig files go through the same checks as a CSV row (docs/decisions/0017)."""
import base64
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "scripts"))
import upload  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
import validate  # noqa: E402  guard-ok: scripts are imported after the sys.path insert

GOOD = "applianceInfo:\n  hostname: br-1\n"
ROUTE = ("applianceInfo:\n  hostname: br-2\nsegmentLocalRoutes:\n- segment: corp\n  routes:\n  - routeIpSubnet: 10.1.0.0/16\n    nextHop: 0.0.0.0\n    interfaceName: lan1\n"
         "  - routeIpSubnet: 10.2.0.0/16\n    nextHop: 0.0.0.0\n    interfaceName: default\n")


class Client:
    def __init__(self, existing=(), reject=None):
        self.existing, self.reject, self.posted, self.validated, self.write_requests = list(existing), reject, [], [], 0

    def get(self, path, query=None):
        return self.existing

    def request(self, method, path, query=None, json_body=None, expected_status=(200,), expect_json=True, read_only=False):
        if path == "/gms/appliance/preconfiguration/validate":
            text = base64.b64decode(json_body["configData"]).decode()
            self.validated.append(text)
            if self.reject and self.reject in text:
                raise type("ValidationError", (Exception,), {"status": 400})("rejected by Orchestrator")
            return b""
        if path == "/gms/appliance/preconfiguration" and method == "POST":
            self.posted.append(json_body)
            self.existing.append({"name": json_body["name"], "serialNum": json_body.get("serialNum")})
            return {"id": len(self.posted)}
        raise AssertionError("unexpected call {} {}".format(method, path))


class YamlFiles(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def file(self, name, text):
        (self.dir / name).write_text(text, encoding="utf-8")
        return str(self.dir / name)

    def run_upload(self, argv, client):
        with mock.patch.object(upload, "client_for", return_value=client), contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                code = upload.main(argv + ["--orchestrator", "lab"])
            except SystemExit as done:
                code = done.code
        return code, out.getvalue() + err.getvalue()

    def run_validate(self, argv, client):
        with mock.patch.object(validate, "validator", return_value=client), contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                code = validate.main(argv + ["--orchestrator", "lab"])
            except SystemExit as done:
                code = done.code
        return code, out.getvalue() + err.getvalue()

    def test_validate_yaml_is_a_dry_run(self):
        client = Client()
        code, out = self.run_validate(["--yaml", self.file("br-1.yml", GOOD)], client)
        self.assertEqual((code, client.validated, client.posted, client.write_requests), (0, [GOOD], [], 0))
        self.assertIn("passed", out)

    def test_upload_yaml_uploads_without_approve_and_never_applies(self):
        client = Client()
        code, out = self.run_upload(["--yaml", self.file("br-1.yml", GOOD)], client)
        self.assertEqual(code, 0, out)
        self.assertEqual(client.posted, [{"name": "br-1", "configData": base64.b64encode(GOOD.encode()).decode(), "autoApply": False,
                                          "comment": "uploaded via building-edgeconnect-preconfig-templates"}])
        self.assertRegex(out, r"uploaded +br-1\.yml")
        self.assertIn("verified", out)
        _, _ = self.run_upload(["--yaml", self.file("br-9.yml", GOOD.replace("br-1", "br-9")), "--approve"], Client())  # accepted, changes nothing

    def test_folder_and_several_files_with_one_bad_and_one_conflict(self):
        folder = self.dir / "batch"
        folder.mkdir()
        (folder / "b.yaml").write_text(GOOD.replace("br-1", "b"))
        (folder / "a.yml").write_text(GOOD.replace("br-1", "a"))
        (folder / "notes.txt").write_text("x")
        (folder / "sub").mkdir()
        (folder / "sub" / "c.yml").write_text(GOOD)  # not recursive
        bad = self.file("bad.yml", "applianceInfo: [unclosed\n")
        empty = self.file("empty.yml", "")
        listed = self.file("list.yml", "- a\n- b\n")
        jinja = self.file("tpl.yml", "applianceInfo:\n  hostname: {{ data['hostname'] }}\n")
        taken = self.file("taken.yml", GOOD.replace("br-1", "taken"))
        rejected = self.file("nope.yml", GOOD.replace("br-1", "nope"))
        client = Client(existing=[{"name": "taken"}], reject="nope")
        code, out = self.run_upload(["--yaml", str(folder), bad, empty, listed, jinja, taken, rejected], client)
        self.assertEqual(code, 1)
        self.assertEqual([p["name"] for p in client.posted], ["a", "b"])
        for name, outcome in (("bad.yml", "refused: not a preconfig YAML"), ("empty.yml", "refused: not a preconfig YAML"), ("list.yml", "refused: not a preconfig YAML"),
                              ("tpl.yml", "refused: not a preconfig YAML"), ("taken.yml", "skipped: name conflict"), ("nope.yml", "validate failed")):
            self.assertRegex(out, r"{} +{}".format(outcome, name.replace(".", r"\.")))
        self.assertIn("rejected by Orchestrator", out)
        self.assertIn("holds Jinja {{ }}: a template, not a rendered preconfig", out)
        again = self.dir / "again"
        again.mkdir()
        (again / "a.yml").write_text(GOOD.replace("br-1", "a2"))  # same name as batch/a.yml, uploaded earlier in this run
        client = Client()
        code, out = self.run_upload(["--yaml", str(folder), str(again)], client)
        self.assertEqual([p["name"] for p in client.posted], ["a", "b"])
        self.assertRegex(out, r"skipped: name conflict +a\.yml +a: 'a' already exists on Orchestrator or earlier in this batch")
        self.assertNotIn("c.yml", out)

    def test_secret_placeholders_are_named_and_refused(self):
        client = Client()
        text = GOOD + "nac:\n  server:\n  - host: 192.0.2.5\n    key: <SECRET:not-captured>\n"
        code, out = self.run_upload(["--yaml", self.file("br-1.yml", text)], client)
        self.assertEqual((code, client.posted, client.validated), (1, [], []))
        self.assertIn("refused: secret placeholder", out)
        self.assertIn("fill these in: nac.server[0].key", out)

    def test_name_and_serial(self):
        client = Client(existing=[{"name": "other", "serialNum": "001BBC462D3D"}])
        code, out = self.run_upload(["--yaml", self.file("br-1.yml", GOOD), "--name", "Store-7", "--serial", "00-1b-bc-46-2d-3e"], client)
        self.assertEqual(code, 0, out)
        self.assertEqual((client.posted[0]["name"], client.posted[0]["serialNum"]), ("Store-7", "001BBC462D3E"))
        code, out = self.run_upload(["--yaml", self.file("br-2.yml", GOOD), "--serial", "001BBC462D3D"], client)
        self.assertEqual(len(client.posted), 1)
        self.assertIn("skipped: serial conflict", out)
        for bad in (["--name", "x"], ["--serial", "001BBC462D3D"]):
            code, out = self.run_upload(["--yaml", self.file("a.yml", GOOD), self.file("b.yml", GOOD)] + bad, Client())
            self.assertEqual(code, 2, bad)
            self.assertIn("exactly one file", out)
        code, out = self.run_upload(["--yaml", self.file("c.yml", GOOD), "--serial", "xyz"], Client())
        self.assertEqual(code, 2)
        self.assertIn("12 hexadecimal", out)

    def test_csv_and_yaml_are_exclusive(self):
        self.assertEqual(self.run_upload(["--yaml", self.file("a.yml", GOOD), "--csv", "x.csv"], Client())[0], 2)
        self.assertEqual(self.run_upload([], Client())[0], 2)

    def test_interface_route_with_next_hop_zero_warns_but_uploads(self):
        client = Client()
        code, out = self.run_upload(["--yaml", self.file("br-2.yml", ROUTE)], client)
        self.assertEqual((code, len(client.posted)), (0, 1))
        self.assertIn("WARN br-2.yml: route 10.1.0.0/16 (segment corp) has next hop 0.0.0.0 and interface lan1: passes /validate but ECOS 9.7+ rejects the whole routes step "
                      "at apply; remove the interface - see docs/decisions/0016", out)
        self.assertNotIn("10.2.0.0/16", out)


if __name__ == "__main__":
    unittest.main()

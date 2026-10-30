"""Golden outputs: what each command prints, pinned byte for byte, so later changes (run context, recorder, GUI hooks) keep the CLI text the same.

Every case runs cli.main offline: the synthetic capture of test_translate_robust stands in for the appliance read, and a fake Orchestrator
answers GET /gms/versions, /appliance and the preconfig list, POST /validate and the preconfig upload. The temp folder, timestamps and
the toolkit version are masked. compare is pinned with its own fixes (1.3.4), not here.

To accept a deliberate change of output: EDGECONNECT_UPDATE_GOLDEN=1 python3 -m unittest tests.test_golden, then review the diff of tests/golden/.
"""
import base64
import contextlib
import copy
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "scripts"))
import cli  # noqa: E402  guard-ok: scripts are imported after the sys.path insert, as in the other test modules
import common  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
import upload  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
import validate  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
from test_translate_robust import DATA  # noqa: E402  guard-ok: the shared synthetic capture

GOLDEN = Path(__file__).resolve().parent / "golden"
UPDATE = os.environ.get("EDGECONNECT_UPDATE_GOLDEN") == "1"
RELEASE = "9.7.1.42046"
APPLIANCES = [{"hostName": "h1", "nePk": "9.NE", "model": "EC-V", "softwareVersion": "9.7.1.0", "site": "S1", "state": 1},
              {"hostName": "h2", "nePk": "8.NE", "model": "EC-XS", "softwareVersion": "9.6.2.1", "site": "S1", "state": 2}]


class Orchestrator:
    """The lab Orchestrator: reads, /validate (rejects a document holding REJECT) and preconfig uploads, never an apply."""

    def __init__(self):
        self.write_requests, self.existing = 0, []
        self.config = SimpleNamespace(base_url="https://orch.example.com/gms/rest")

    def get(self, path, query=None):
        answers = {"/gms/versions": {"current": RELEASE}, "/appliance": APPLIANCES, "/gms/appliance/preconfiguration": self.existing}
        if path not in answers:
            raise AssertionError("unexpected GET " + path)
        return copy.deepcopy(answers[path])

    def request(self, method, path, query=None, json_body=None, expected_status=(200,), expect_json=True, read_only=False):
        if path == validate.PATH and read_only:
            if "REJECT" in base64.b64decode(json_body["configData"]).decode("utf-8"):
                raise common.ValidationError("HTTP 400: applianceInfo.site: invalid value", status=400, method=method, path=path)
            return b""
        if path == "/gms/appliance/preconfiguration" and method == "POST":
            self.existing.append({"id": len(self.existing) + 1, "name": json_body["name"], "serialNum": json_body.get("serialNum")})
            return {"id": len(self.existing)}
        raise AssertionError("unexpected {} {}".format(method, path))


def write_capture(folder):
    """A capture folder as capture.capture() leaves it: the index, run.json and one file per read."""
    (folder / "capture").mkdir(parents=True)
    index = []
    for ident, data in DATA.items():
        name = "capture/{}.live.json".format(ident)
        (folder / name).write_text(json.dumps({"label": "live", "data": data}), encoding="utf-8")
        index.append({"id": ident, "label": "live", "status": "ok", "file": name})
    (folder / "capture-index.json").write_text(json.dumps(index), encoding="utf-8")
    run = {"stage": "capture", "orchestrator": "lab", "release": RELEASE, "role": "reference", "reachable": True, "started": "20261030T000000Z",
           "appliance": {"nePk": "9.NE", "hostName": "h1", "model": "EC-V", "softwareVersion": "9.7.1.0", "state": 1}, "finished": "20261030T000001Z",
           "counts": {"ok": len(index), "error": 0, "unavailable": 0}, "write_requests": 0, "secret_paths": {}}
    (folder / "run.json").write_text(json.dumps(run), encoding="utf-8")
    return folder


class Golden(unittest.TestCase):
    """One build is shared: the cases that need a site folder run on it in the order a user would."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.orch = Orchestrator()
        cls.cap = write_capture(cls.tmp / "cap")
        cls.out = cls.tmp / "out"
        cls.cwd = os.getcwd()
        os.chdir(str(cls.tmp))  # stage defaults (./preconfig-output) land in the temp folder
        cls.patches = [mock.patch.object(cli, "VALIDATORS", {"lab"}), mock.patch.object(validate, "VALIDATORS", {"lab"}),
                       mock.patch.object(cli, "client_for", return_value=cls.orch), mock.patch.object(validate, "client_for", return_value=cls.orch),
                       mock.patch.object(upload, "client_for", return_value=cls.orch), mock.patch("capture.capture", return_value=cls.cap)]
        for p in cls.patches:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        os.chdir(cls.cwd)
        shutil.rmtree(str(cls.tmp), ignore_errors=True)

    def setUp(self):
        self.orch.existing[:] = [{"id": 1, "name": "taken", "serialNum": None}]  # each case sees only the one preconfig already there

    def run_cli(self, argv, answers=None):
        """cli.main(argv) with stdout and stderr captured; answers are the lines typed at the prompts (stdin)."""
        typed = io.StringIO("".join(a + "\n" for a in answers or []))
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err, mock.patch.object(sys, "stdin", typed):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def mask(self, text):
        for path in sorted({str(self.tmp), str(self.tmp.resolve())}, key=len, reverse=True):  # macOS: /var is /private/var
            text = text.replace(path, "<tmp>")
        text = re.sub(r"\d{8}T\d{6}Z", "<stamp>", text)
        return text.replace(common.toolkit_version(), "<toolkit>")

    def check(self, name, argv, answers=None, code=0):
        got, out, err = self.run_cli(argv, answers)
        typed = "".join("\n> typed: " + (a or "(Enter)") for a in answers or [])
        text = self.mask("$ edgeconnect-preconfig {}{}\nexit {}\n--- stdout\n{}--- stderr\n{}".format(" ".join(argv), typed, got, out, err))
        path = GOLDEN / (name + ".txt")
        if UPDATE:
            GOLDEN.mkdir(exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.assertEqual(got, code, text)
        self.assertTrue(path.is_file(), "no golden {}: run with EDGECONNECT_UPDATE_GOLDEN=1".format(path.name))
        self.assertEqual(text, path.read_text(encoding="utf-8"), "output of {} changed; if meant, regenerate with EDGECONNECT_UPDATE_GOLDEN=1".format(name))

    def built(self):
        if not (self.out / "h1" / "build.json").is_file():
            self.run_cli(["build", "--orchestrator", "lab", "--appliance", "h1", "--out", str(self.out)])
        return self.out / "h1"

    def site(self, name="t10"):
        folder = self.built() / ("site-" + name)
        if not folder.is_dir():
            self.run_cli(["variables", str(self.built()), "--pick", "top10", "--yes", "--name", name])
        return folder

    def sheet(self, name, change):
        """A copy of sites.csv with change(rows) applied, for the rows a user would add."""
        import csv
        source = self.site() / "sites.csv"
        with source.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        fields = list(rows[0])
        rows = change(rows)
        target = self.tmp / name
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return target

    # ------------------------------------------------------------ list

    def test_list(self):
        self.check("list", ["list", "--orchestrator", "lab"])

    def test_list_search(self):
        self.check("list-search", ["list", "--orchestrator", "lab", "--search", "xs"])

    def test_list_search_no_match(self):
        self.check("list-search-none", ["list", "--orchestrator", "lab", "--search", "nothing"])

    # ------------------------------------------------------------ build

    def test_build(self):
        shutil.rmtree(str(self.out), ignore_errors=True)
        self.check("build", ["build", "--orchestrator", "lab", "--appliance", "h1", "--out", str(self.out)])

    def test_build_again_keeps_the_earlier_one(self):
        self.built()
        self.check("build-again", ["build", "--orchestrator", "lab", "--appliance", "h1", "--out", str(self.out)])

    def test_build_on_a_non_validator(self):
        self.check("build-not-validator", ["build", "--orchestrator", "fieldref", "--appliance", "h1", "--out", str(self.out)], code=2)

    # ------------------------------------------------------------ variables

    def test_variables_top10(self):
        self.check("variables-top10", ["variables", str(self.built()), "--pick", "top10", "--yes", "--name", "g10"])

    def test_variables_top20(self):
        self.check("variables-top20", ["variables", str(self.built()), "--pick", "top20", "--yes", "--name", "g20"])

    def test_variables_all(self):
        self.check("variables-all", ["variables", str(self.built()), "--pick", "all", "--yes", "--name", "gall"])

    def test_variables_set(self):
        self.check("variables-set", ["variables", str(self.built()), "--pick", "top10", "--set", "deploymentInfo.deploymentMode", "--yes", "--name", "gset"])

    def test_variables_set_unknown(self):
        self.check("variables-set-unknown", ["variables", str(self.built()), "--pick", "top10", "--set", "no_such_setting", "--yes"], code=2)

    def test_variables_asked(self):
        # Top 10, open the locked list, flip setting 1, Enter, default name, yes
        self.check("variables-asked", ["variables", str(self.built())], answers=["1", "l", "1", "", "gask", "y"])

    def test_variables_quit(self):
        self.check("variables-quit", ["variables", str(self.built())], answers=["2", "q"], code=1)

    # ------------------------------------------------------------ validate, upload (CSV)

    def test_validate_csv(self):
        site = self.site()
        self.check("validate-csv", ["validate", str(site), "--csv", str(site / "sites.csv")])

    def test_validate_csv_with_a_rejected_row(self):
        site = self.site()
        sheet = self.sheet("rejected.csv", lambda rows: rows + [dict(rows[0], hostname="h9", preconfig_name="h9", site="REJECT")])
        self.check("validate-csv-rejected", ["validate", str(site), "--csv", str(sheet)], code=1)

    def test_upload_csv_without_approve(self):
        site = self.site()
        self.check("upload-csv-check", ["upload", str(site), "--csv", str(site / "sites.csv")])

    def test_upload_csv_skips_a_name_already_there(self):
        site = self.site()
        sheet = self.sheet("taken.csv", lambda rows: [dict(rows[0], preconfig_name="taken")])
        self.check("upload-csv-conflict", ["upload", str(site), "--csv", str(sheet), "--approve"])

    def test_upload_csv_with_approve(self):
        site = self.site("up")
        self.check("upload-csv-approve", ["upload", str(site), "--csv", str(site / "sites.csv"), "--approve"])

    # ------------------------------------------------------------ validate --yaml, upload --yaml

    def yaml_files(self):
        folder = self.tmp / "yaml"
        if not (folder / "taken.yml").is_file():
            folder.mkdir(exist_ok=True)
            reference = (self.built() / "reference.yaml").read_text(encoding="utf-8")
            (folder / "good.yml").write_text(re.sub(r"<SECRET:[^>]*>", "s3cret", reference), encoding="utf-8")
            (folder / "rejected.yml").write_text('applianceInfo:\n  hostname: "h8"\n  site: "REJECT"\n', encoding="utf-8")
            (folder / "taken.yml").write_text('applianceInfo:\n  hostname: "h7"\n', encoding="utf-8")
        return folder

    def test_validate_yaml(self):
        self.check("validate-yaml", ["validate", "--yaml", str(self.yaml_files()), "--orchestrator", "lab"], code=1)

    def test_upload_yaml(self):
        self.check("upload-yaml", ["upload", "--yaml", str(self.yaml_files() / "good.yml"), "--orchestrator", "lab", "--name", "golden-upload"])

    def test_validate_yaml_on_a_non_validator(self):
        self.check("validate-yaml-not-validator", ["validate", "--yaml", str(self.yaml_files()), "--orchestrator", "fieldref"], code=2)

    # ------------------------------------------------------------ stage forms

    def test_render_stage(self):
        site = self.site()
        self.check("render-stage", ["render", "--template", str(site / "t10.jinja2"), "--csv", str(site / "sites.csv"), "--out", str(self.tmp / "rendered")])

    def test_upload_stage(self):
        site = self.site()
        self.check("upload-stage", ["upload", "--template", str(site / "t10.jinja2"), "--csv", str(site / "sites.csv"), "--orchestrator", "lab",
                                    "--out", str(self.tmp / "upload-stage")])

    # ------------------------------------------------------------ help and version

    def test_help(self):
        self.check("help", ["--help"])

    def test_no_command(self):
        self.check("no-command", [], code=2)

    def test_unknown_command(self):
        self.check("unknown-command", ["nope"], code=2)


if __name__ == "__main__":
    unittest.main()

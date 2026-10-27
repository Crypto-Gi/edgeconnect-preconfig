import base64
import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "scripts"))
from common import ServerError, ValidationError, changed_paths, normalize_serial, parse_yaml, resolve_output_filename  # noqa: E402
from generate import generate  # noqa: E402
from options import extract  # noqa: E402
from render import flow_through, headers, main as render_main, read_rows, render_row  # noqa: E402
from upload import run_batch  # noqa: E402

REFERENCE = {
    "applianceInfo": {"hostname": "lab-a", "site": "site 1: main", "zip": None, "location": {"zipCode": "55401"}},
    "templateGroups": {"groups": ["G1", "G2"]},
    "deploymentInfo": {
        "totalOutboundBandwidth": 200000, "serverPerSegment": False, "ratio": 1.5, "target": "1:1", "empty": [],
        "deploymentInterfaces": [
            {"interfaceName": "lan0", "ipAddressMask": "192.0.2.1/24", "dnsServers": ["8.8.8.8", "8.8.4.4"], "password": "x"},
            {"interfaceName": "wan0", "ipAddressMask": None, "password": None},
        ],
    },
}


class RoundTrip(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.meta = generate(REFERENCE, self.out, "t")
        self.template = self.out / "t.jinja2"
        self.row = read_rows(self.out / "example.csv")[0]

    def test_example_row_reproduces_reference_except_secrets(self):
        doc = parse_yaml(render_row(self.template, self.row))
        self.assertEqual(changed_paths(REFERENCE, doc), ["deploymentInfo.deploymentInterfaces[0].password"])
        self.assertEqual(doc["deploymentInfo"]["target"], "1:1")  # YAML 1.1 would read 61
        self.assertIs(doc["deploymentInfo"]["serverPerSegment"], False)
        self.assertEqual(doc["applianceInfo"]["location"]["zipCode"], "55401")  # string stays a string

    def test_headers_match_columns_and_runner_fields(self):
        self.assertEqual(set(headers(self.template.read_text())), set(self.meta["columns"]))
        self.assertEqual(set(self.row), set(self.meta["columns"]) | {"preconfig_name", "serial_number"})
        self.assertEqual(self.row["preconfig_name"], "lab-a_preconfig")
        self.assertEqual(self.row["serial_number"], "")

    def test_filename_resolution_duplicate_detection_and_serial_normalization(self):
        self.assertEqual(resolve_output_filename({"preconfig_name": "Store-010-Primary"}), ("Store-010-Primary", "Store-010-Primary.yml"))
        self.assertEqual(resolve_output_filename({"filename": "Store-010-Primary.yml"}), ("Store-010-Primary", "Store-010-Primary.yml"))
        self.assertEqual(resolve_output_filename({"hostname": "br-01"}), ("br-01_preconfig", "br-01_preconfig.yml"))
        with self.assertRaises(ValueError):
            resolve_output_filename({"preconfig_name": "../outside"})
        self.assertEqual(normalize_serial("00-1b-bc-46-2d-3d"), "001BBC462D3D")
        self.assertEqual(normalize_serial("00:1B:BC:46:2D:3E"), "001BBC462D3E")
        self.assertIsNone(normalize_serial(""))
        with self.assertRaises(ValueError):
            normalize_serial("NOT-A-SERIAL")

    def test_partial_batch_validation_and_conflict_skipping_upload(self):
        # Build a 3-row CSV: Row 1 (Store-01, valid, already on Orchestrator), Row 2 (Store-02, invalid serial), Row 3 (Store-03, valid, new)
        batch_csv = self.out / "batch.csv"
        fieldnames = list(self.row.keys())
        r1 = dict(self.row, preconfig_name="Store-01", hostname="store-01", serial_number="00-1B-BC-00-00-01")
        r2 = dict(self.row, preconfig_name="Store-02", hostname="store-02", serial_number="BAD-SERIAL")
        r3 = dict(self.row, preconfig_name="Store-03", hostname="store-03", serial_number="00-1b-bc-46-2d-3d")
        with batch_csv.open("w", newline="", encoding="utf-8") as handle:
            w = csv.DictWriter(handle, fieldnames=fieldnames)
            w.writeheader()
            w.writerows([r1, r2, r3])

        # 1. render_main writes only valid rows (Store-01.yml and Store-03.yml) and never writes Store-02.yml
        rdir = self.out / "rendered_cli"
        rc = render_main(["--template", str(self.template), "--csv", str(batch_csv), "--out", str(rdir), "--columns", str(self.out / "columns.json")])
        self.assertEqual(rc, 1)
        self.assertEqual(sorted(p.name for p in rdir.glob("*.yml")), ["Store-01.yml", "Store-03.yml"])

        # 2. upload.run_batch: without approve=True, 0 uploads; with approve=True, skips Store-01 (conflict) and Store-02 (invalid), uploads Store-03
        class FakeUploadClient:
            def __init__(self):
                self.write_requests = 0
                self.store = [{"id": 10, "name": "Store-01", "serialNum": "001BBC000001"}]
                self.uploaded = []

            def get(self, path, query=None):
                return list(self.store)

            def request(self, method, path, query=None, json_body=None, expected_status=(200,), expect_json=True, read_only=False):
                if not read_only and method != "GET":
                    self.write_requests += 1
                if path == "/gms/appliance/preconfiguration/validate":
                    return b""
                if path == "/gms/appliance/preconfiguration" and method == "POST":
                    entry = {"id": 99, "name": json_body["name"], "serialNum": json_body.get("serialNum")}
                    self.store.append(entry)
                    self.uploaded.append(json_body)
                    return entry
                return b""

        client = FakeUploadClient()
        uout = self.out / "upload_out"
        rep_dry = run_batch(self.template, batch_csv, uout, client, self.meta["columns"], approve=False)
        self.assertEqual(rep_dry["summary"]["uploaded"], 0)
        self.assertEqual(client.write_requests, 0)
        self.assertEqual(sorted(p.name for p in (uout / "rendered").glob("*.yml")), ["Store-01.yml", "Store-03.yml"])

        rep_up = run_batch(self.template, batch_csv, uout, client, self.meta["columns"], approve=True)
        self.assertEqual(rep_up["summary"]["valid_yaml_written"], 2)
        self.assertEqual(rep_up["summary"]["invalid_skipped"], 1)
        self.assertEqual(rep_up["summary"]["conflict_skipped"], 1)
        self.assertEqual(rep_up["summary"]["uploaded"], 1)
        self.assertEqual(len(client.uploaded), 1)
        self.assertEqual((client.uploaded[0]["name"], client.uploaded[0]["serialNum"]), ("Store-03", "001BBC462D3D"))

    def _batch(self, names):
        path = self.out / "b.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            w = csv.DictWriter(handle, fieldnames=list(self.row.keys()))
            w.writeheader()
            w.writerows(dict(self.row, preconfig_name=n, hostname=n.lower(), serial_number="") for n in names)
        return path

    def test_validate_http_500_on_one_row_does_not_stop_the_batch(self):
        # /validate answers HTTP 500 for some documents (e.g. nac with an empty sub-list); that is one row's failure
        class Client:
            write_requests = 0

            def get(self, path, query=None):
                return []

            def request(self, method, path, query=None, json_body=None, expected_status=(200,), expect_json=True, read_only=False):
                if b"bad-1" in base64.b64decode(json_body["configData"]):
                    raise ServerError("HTTP 500: java.lang.NullPointerException", status=500)
                return b""

        rep = run_batch(self.template, self._batch(["BAD-1", "Good-2"]), self.out / "u500", Client(), self.meta["columns"])
        self.assertEqual([r["status"] for r in rep["rows"]], ["invalid", "ready"])
        self.assertIn("HTTP 500", rep["rows"][0]["detail"])
        self.assertEqual([p.name for p in (self.out / "u500" / "rendered").glob("*.yml")], ["Good-2.yml"])

    def test_failed_upload_is_reported_and_remaining_rows_still_upload(self):
        class Client:
            write_requests = 0
            store = []

            def get(self, path, query=None):
                return list(self.store)

            def request(self, method, path, query=None, json_body=None, expected_status=(200,), expect_json=True, read_only=False):
                if method == "POST" and path == "/gms/appliance/preconfiguration":
                    if json_body["name"] == "Up-1":
                        raise ValidationError("HTTP 400: Preconfiguration with name Up-1 already exists", status=400)
                    self.store.append({"id": 7, "name": json_body["name"]})
                    return {"id": 7}
                return b""

        out = self.out / "ufail"
        rep = run_batch(self.template, self._batch(["Up-1", "Up-2"]), out, Client(), self.meta["columns"], approve=True)
        self.assertEqual([r["status"] for r in rep["rows"]], ["upload-failed", "uploaded"])
        self.assertEqual((rep["summary"]["uploaded"], rep["summary"]["upload_failed"]), (1, 1))
        self.assertIn("already exists", rep["rows"][0]["detail"])
        self.assertTrue((out / "upload-report.json").is_file())

    def test_secret_is_a_column_and_example_leaves_it_empty(self):
        col = self.meta["columns"]["deploymentInfo.deploymentInterfaces.0.password"]
        self.assertTrue(col["secret"] and col["had_value"])
        self.assertEqual(self.row["deploymentInfo.deploymentInterfaces.0.password"], "")

    def test_changing_one_cell_changes_one_field_and_flows_through(self):
        row = dict(self.row, **{"deploymentInfo.totalOutboundBandwidth": "300000", "template_groups": "G1,G9"})
        doc = parse_yaml(render_row(self.template, row))
        self.assertEqual(flow_through(doc, self.meta["columns"], row), [])
        self.assertEqual(doc["templateGroups"]["groups"], ["G1", "G9"])
        base = parse_yaml(render_row(self.template, self.row))
        self.assertEqual(changed_paths(base, doc), ["deploymentInfo.totalOutboundBandwidth", "templateGroups.groups[1]"])

    def test_blank_optional_cell_renders_null_and_missing_column_fails(self):
        doc = parse_yaml(render_row(self.template, self.row))
        self.assertIsNone(doc["deploymentInfo"]["deploymentInterfaces"][1]["ipAddressMask"])
        row = dict(self.row)
        del row["hostname"]
        with self.assertRaises(ValueError):
            render_row(self.template, row)

    def test_variables_policy_keeps_the_rest_literal(self):
        out = Path(tempfile.mkdtemp())
        meta = generate(REFERENCE, out, "p", variables=[r"hostname$", r"totalOutboundBandwidth$"])
        self.assertEqual(sorted(meta["columns"]), ["deploymentInfo.deploymentInterfaces.0.password", "deploymentInfo.deploymentInterfaces.1.password", "deploymentInfo.totalOutboundBandwidth", "hostname", "template_groups"])  # secrets are always columns
        doc = parse_yaml(render_row(out / "p.jinja2", read_rows(out / "example.csv")[0]))
        self.assertEqual(doc["deploymentInfo"]["deploymentInterfaces"][0]["ipAddressMask"], "192.0.2.1/24")


class Options(unittest.TestCase):
    def test_enum_range_and_exclusive_range(self):
        text = "\n".join(["  # mode - firewall mode", "  #        Value:[All,stateful,harden]", "  # bw - bandwidth", "  #   Value: integer[0-100,000,000, exclusive]",
                          "  # on - flag", "  #   Value: [true, false], optional", "  # skip - doc", "  #   Value: [1-65535]", "section:", "  mode: All"])
        found = {o["key"]: o for o in extract(text)}
        self.assertEqual(found["mode"]["enum"], ["All", "stateful", "harden"])
        self.assertEqual((found["bw"]["range"], found["bw"]["exclusive"]), ([0, 100000000], True))
        self.assertEqual(found["on"]["enum"], [True, False])
        self.assertNotIn("skip", found)
        self.assertEqual(found["mode"]["section"], "section")


class Yaml(unittest.TestCase):
    def test_core_schema_scalars(self):
        doc = parse_yaml("a: 1:1\nb: yes\nc: 12\nd: true\ne: 1.5\nf: ~\ng: 2000::01/64\nh: 'x'")
        self.assertEqual(doc, {"a": "1:1", "b": "yes", "c": 12, "d": True, "e": 1.5, "f": None, "g": "2000::01/64", "h": "x"})


if __name__ == "__main__":
    unittest.main()

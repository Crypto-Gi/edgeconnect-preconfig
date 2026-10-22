import csv
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "building-edgeconnect-preconfig-templates" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import upload  # noqa: E402
from generate import generate  # noqa: E402

APPLY = re.compile(r"preconfiguration/apply|autoApply[\"']?\]?\s*[:=](?!=)\s*(?![\s]|False\b)")  # any autoApply value other than a literal False


class NoApply(unittest.TestCase):
    """The toolkit cannot apply a preconfig, not even later through Orchestrator's autoApply (docs/decisions/0008)."""

    def test_the_scan_catches_every_way_to_turn_autoapply_on(self):
        for line in ('"autoApply": True', 'payload["autoApply"] = flag', "body['autoApply']=args.auto", '"autoApply": bool(x)', 'POST /gms/appliance/preconfiguration/apply'):
            self.assertTrue(APPLY.search(line), line)
        for line in ('"autoApply": False,', 'payload["autoApply"] == False'):
            self.assertFalse(APPLY.search(line), line)

    def test_no_shipped_or_test_code_calls_apply(self):
        found = [(p.relative_to(ROOT), n) for p in list(SCRIPTS.glob("*.py")) + list((ROOT / "tests").rglob("*.py")) if p.name != "test_no_apply.py"
                 for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if APPLY.search(line.split("#")[0])]
        self.assertEqual(found, [])

    def test_upload_has_no_auto_apply_and_always_sends_false(self):
        with self.assertRaises(SystemExit):
            upload.main(["--site-dir", ".", "--csv", "x.csv", "--orchestrator", "lab", "--auto-apply"])

        class Client:
            write_requests = 0
            posted = []

            def get(self, path, query=None):
                return []

            def request(self, method, path, query=None, json_body=None, expected_status=(200,), expect_json=True, read_only=False):
                if path == "/gms/appliance/preconfiguration" and method == "POST":
                    self.posted.append(json_body)
                    return {"id": 1}
                return b""

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            meta = generate({"applianceInfo": {"hostname": "br-1"}}, out, "br", decide=lambda p, v, s: p == "applianceInfo.hostname")
            with (out / "rows.csv").open("w", newline="") as handle:
                w = csv.DictWriter(handle, fieldnames=["hostname", "preconfig_name", "serial_number"])
                w.writeheader()
                w.writerow({"hostname": "br-2", "preconfig_name": "br-2", "serial_number": ""})
            client = Client()
            upload.run_batch(out / "br.jinja2", out / "rows.csv", out, client, meta["columns"], approve=True)
        self.assertEqual([p["autoApply"] for p in client.posted], [False])


if __name__ == "__main__":
    unittest.main()

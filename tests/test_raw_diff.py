import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "harnesses"))
import raw_diff  # noqa: E402


def make_capture(root: Path, nepk: str, host: str, files: dict) -> Path:
    folder = root / nepk
    (folder / "capture").mkdir(parents=True)
    (folder / "run.json").write_text(json.dumps({"appliance": {"nePk": nepk, "hostName": host}}))
    for name, (query, data) in files.items():
        (folder / "capture" / (name + ".json")).write_text(json.dumps({"query": query, "data": data}))
    return folder


class RawDiffTest(unittest.TestCase):
    def test_mapped_identifiers_match_and_real_gaps_show(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = make_capture(root, "1.NE", "site-a", {
                "deployment.cached": ({"nePk": "1.NE"}, {"modeIfs": [
                    {"ifName": "lan0", "applianceIPs": [{"ip": "192.0.2.1", "mask": 24}, {"ip": "192.0.2.10", "mask": 24}]},
                    {"ifName": "wan0", "applianceIPs": [{"ip": "203.0.113.1"}]}]}),
                "vrrp.cached": ({"nePk": "1.NE"}, [{"groupId": 1, "vip": "192.0.2.254"}]),
                "overlay-association.orchestrator": ({}, {"1": ["1.NE", "0.NE"], "2": ["1.NE"]}),
                "running-config.backup": ({"nePk": "1.NE"}, {"runningConfig": "## generated\nsystem hostname site-a\nip route 0.0.0.0/0 192.0.2.10\n"}),
            })
            b = make_capture(root, "4.NE", "site-b", {
                # list order differs, .10 maps on its own and must not be touched by the .1 rule
                "deployment.cached": ({"nePk": "4.NE"}, {"modeIfs": [
                    {"ifName": "wan0", "applianceIPs": [{"ip": "203.0.113.1"}]},
                    {"ifName": "lan0", "applianceIPs": [{"ip": "198.51.100.1", "mask": 24}, {"ip": "198.51.100.10", "mask": 24}]}]}),
                "vrrp.cached": ({"nePk": "4.NE"}, []),
                "overlay-association.orchestrator": ({}, {"1": ["0.NE", "1.NE", "4.NE"]}),
                "running-config.backup": ({"nePk": "4.NE"}, {"runningConfig": "## other\nsystem hostname site-b\nip route 0.0.0.0/0 198.51.100.10\n"}),
            })
            report = raw_diff.compare(a, b, [("192.0.2.1", "198.51.100.1"), ("192.0.2.10", "198.51.100.10"), ("192.0.2.254", "198.51.100.254")], [])
            self.assertEqual(report["endpoints"]["deployment.cached"]["diffs"], [])
            self.assertEqual([d["path"] for d in report["endpoints"]["vrrp.cached"]["diffs"]], ["vrrp.cached:[0].groupId", "vrrp.cached:[0].vip"])
            self.assertEqual(report["membership"]["overlay-association.orchestrator"]["only_source"], ["2[]"])
            self.assertEqual(report["running_config"]["only_source"], [])
            self.assertEqual(report["running_config"]["matched"], 2)
            self.assertEqual(raw_diff.Mapper([("203.0.113.1", "X")]).text("203.0.113.1/24 203.0.113.15 1203.0.113.1"), "X/24 203.0.113.15 1203.0.113.1")


if __name__ == "__main__":
    unittest.main()

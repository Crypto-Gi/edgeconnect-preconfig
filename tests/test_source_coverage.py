import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "harnesses"))
import source_coverage as sc  # noqa: E402  guard-ok: imported after the sys.path insert

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "compare" / "captures"


class SourceCoverageTest(unittest.TestCase):
    def test_a_leaf_is_taken_only_when_the_converter_reads_it(self):
        seen = sc.Seen()
        d = sc.wrap({"a": 1, "b": {"c": 2, "d": 3}, "l": [{"x": 1, "y": 2}], "routes": {"10.0.0.0/24": {"advert": True, "metric": 5}}, "e": {}}, "", seen)
        d["a"], d["b"].get("c"), [i["x"] for i in d["l"]]
        for _, r in d["routes"].items():
            r["metric"]
        self.assertEqual(seen.leaves - seen.taken, {"b.d", "l[].y", "routes.*.advert", "e"})
        list(d["b"])  # listing keys (ctx.unmapped) is not a take
        self.assertIn("b.d", seen.leaves - seen.taken)
        dict(d["b"]), d.get("e")  # a whole copy takes every key
        self.assertEqual(seen.leaves - seen.taken, {"l[].y", "routes.*.advert"})

    def test_a_key_path_never_shows_a_name_the_converter_walks(self):
        seen = sc.Seen()
        d = sc.wrap({"peers": {"branch-a": {"metric": 1, "priority": 2}}, "sites": {"store-x": {"a": 1}, "store-y": {"a": 2}}}, "", seen)
        for _, peer in d["peers"].items():  # one entry: only the walk shows its key is a name
            peer["priority"]
        self.assertEqual(seen.untaken(), {"peers.*.metric", "sites.*.a"})  # two records with the same fields: a record map

    def test_ignore_paths_are_literal_but_for_the_wildcard(self):
        self.assertTrue(sc.glob("modeIfs[].applianceIPs[].version", "modeIfs[].applianceIPs[].version"))
        self.assertFalse(sc.glob("modeIfs[].version", "modeIfs1.version"))
        self.assertTrue(sc.glob("routes.*", "routes.*.advert"))

    def test_the_fixture_corpus_has_no_untraced_leaf(self):
        rules = sc.ignore_rules()
        self.assertTrue(all(r.get("why") for r in rules))  # every ignore entry gives its reason
        catalogue = {e["id"]: e for e in sc.load_yaml(sc.CATALOGUE)["endpoints"]}
        for name in ("base", "relay", "zone"):
            self.assertEqual(sc.untraced(FIXTURES / name, "9.7.1.42046", rules, catalogue), ({}, None), name)

    def capture_with_ipsla(self, op):
        folder = Path(tempfile.mkdtemp()) / "cap"
        shutil.copytree(FIXTURES / "base", folder)
        rule = {"enable": True, "interval": 10, "comment": "", "routeLabelId": 0, "operation": {"OPR_1": op}, "upAction": "def-alarm", "downAction": "alarm"}
        (folder / "capture" / "ipsla-managers.live.json").write_text(json.dumps({"data": {"MGR_1": rule}}))
        index = json.loads((folder / "capture-index.json").read_text()) + [{"id": "ipsla-managers", "label": "live", "status": "ok", "file": "capture/ipsla-managers.live.json"}]
        (folder / "capture-index.json").write_text(json.dumps(index))
        return sc.untraced(folder, "9.7.1.42046", sc.ignore_rules(), {e["id"]: e for e in sc.load_yaml(sc.CATALOGUE)["endpoints"]})

    def test_a_known_field_the_mapper_skips_is_reported(self):
        # lab 2026-11-01: force-dns on an http monitor was dropped with no ledger line; the oracle names such a field
        http = {"type": "http-monitor", "http-monitor-url": "www.example.com", "port-name": "wan0", "force-dns": "true"}
        self.assertEqual(self.capture_with_ipsla(http), ({}, None))  # the http monitor takes it now
        port = {"type": "port-monitor", "port-name": "wan0", "force-dns": "true"}  # the interface monitor never reads it
        self.assertEqual(self.capture_with_ipsla(port), ({"ipsla-managers": ["*.operation.*.force-dns"]}, None))


if __name__ == "__main__":
    unittest.main()

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "building-edgeconnect-preconfig-templates" / "scripts"))
from common import parse_yaml  # noqa: E402
from decisions import is_site_specific, list_mode, make_decide, propose  # noqa: E402
from generate import generate  # noqa: E402
from mappers import Ctx, m_loopbackInterface, m_ospfSystemConfig  # noqa: E402
from render import read_rows, render_row  # noqa: E402

REFERENCE = {
    "applianceInfo": {"hostname": "a", "site": "s1"},
    "templateGroups": {"groups": ["G1", "G2"]},
    "nac": {"enable": True, "server": [{"host": "192.0.2.5", "authport": 1812, "key": "k", "name": "default"}]},
    "bgpSystemConfig": {"enable": True, "asn": 65001, "routerId": "192.0.2.1", "enableGracefulRestart": False},
}


class Decisions(unittest.TestCase):
    def build(self, decisions):
        out = Path(tempfile.mkdtemp())
        meta = generate(REFERENCE, out, "t", decide=make_decide(decisions),
                        list_fixed={"templateGroups.groups"} if list_mode(decisions, "templateGroups") == "fixed" else set())
        return out, meta

    def test_site_specific_picks_addresses_and_ids_only(self):
        _, meta = self.build({"sections": {"bgpSystemConfig": "site-specific", "templateGroups": "variable", "nac": "fixed"}})
        cols = set(meta["columns"])
        self.assertIn("bgpSystemConfig.asn", cols)
        self.assertIn("bgpSystemConfig.routerId", cols)
        self.assertNotIn("bgpSystemConfig.enable", cols)
        self.assertIn("template_groups", cols)

    def test_all_mode_variablises_everything_fixed_mode_nothing_but_secrets(self):
        _, meta = self.build({"sections": {"bgpSystemConfig": "all", "nac": "fixed", "templateGroups": "variable"}})
        self.assertIn("bgpSystemConfig.enableGracefulRestart", meta["columns"])
        self.assertNotIn("nac.enable", meta["columns"])
        self.assertTrue(meta["columns"]["nac.server.0.key"]["secret"])  # a secret is always a column, even in a fixed section
        self.assertNotIn("nac.server.0.host", meta["columns"])

    def test_template_groups_can_be_fixed_literal(self):
        out, meta = self.build({"sections": {"templateGroups": "fixed"}})
        self.assertNotIn("template_groups", meta["columns"])
        doc = parse_yaml(render_row(out / "t.jinja2", read_rows(out / "example.csv")[0]))
        self.assertEqual(doc["templateGroups"]["groups"], ["G1", "G2"])

    def test_extra_overrides(self):
        _, meta = self.build({"sections": {"bgpSystemConfig": "fixed"}, "extra_variable": [r"bgpSystemConfig\.enable$"], "extra_fixed": [r"routerId$"]})
        self.assertIn("bgpSystemConfig.enable", meta["columns"])
        self.assertNotIn("bgpSystemConfig.routerId", meta["columns"])

    def test_proposal_counts(self):
        p = propose(REFERENCE, {"nac.server[0].key"})
        self.assertEqual(p["templateGroups"]["proposed"], "variable")
        self.assertEqual(p["bgpSystemConfig"]["proposed"], "site-specific")
        self.assertEqual(p["nac"]["secrets"], 1)
        self.assertTrue(is_site_specific("x.nextHop", "x")[0])
        self.assertFalse(is_site_specific("x.enable", True)[0])


class FakeCap:
    def __init__(self, data):
        self.run = {"appliance": {"nePk": "9.NE"}}
        self._d = data

    def data(self, ident, prefer="live"):
        return self._d.get(ident)


class Mappers(unittest.TestCase):
    def test_loopback_skips_orchestrator_managed_and_resolves_names(self):
        dep = {"sysConfig": {"ifLabels": {"lan": [{"id": "4", "name": "Voice"}], "wan": []}, "vrfs": [{"id": "0", "name": "Default"}], "vrfZonesMap": {"0": {"1": {"id": 1, "name": "INSIDE"}}}, "roles": [{"id": "0", "name": "default"}]}}
        loop = {"lo1": {"admin": True, "gms_marked": False, "ipaddr": "192.0.2.1", "label": "4", "nmask": 32, "role_id": 0, "vrf_id": 0, "zone": 1},
                "lo2": {"admin": False, "gms_marked": True, "ipaddr": "192.0.2.2", "label": "", "nmask": 32, "role_id": 0, "vrf_id": 0, "zone": 0}}
        notes = []
        out = m_loopbackInterface(Ctx(FakeCap({"deployment": dep, "loopbacks": loop}), notes))
        self.assertEqual(out, {"loopbacks": [{"interfaceId": "lo1", "adminStatus": "Up", "ipAddressMask": "192.0.2.1/32", "interfaceLabel": "Voice", "zone": "INSIDE", "role": None, "segment": "Default"}]})
        self.assertEqual([n["disposition"] for n in notes], ["orchestrator-managed", "mapped"])

    def test_ospf_reports_unmapped_source_keys_and_omits_when_disabled(self):
        notes = []
        system = {"enable": True, "routerId": "192.0.2.1", "redistMapToOSPF": "m", "opaque_enable": True, "brandNewKey": 1}
        out = m_ospfSystemConfig(Ctx(FakeCap({"ospf-system": system, "ospf-areas": {"0": {"0.0.0.0": {"area_type": 0, "redist": True, "summary": True, "translator_role": 0}}}, "ospf-interfaces": {}}), notes))
        self.assertEqual(out["areas"][0]["areaId"], "0.0.0.0")
        self.assertTrue(any(n["disposition"] == "unmapped-source-key" and "brandNewKey" in n["note"] for n in notes))
        notes = []
        self.assertIsNone(m_ospfSystemConfig(Ctx(FakeCap({"ospf-system": {"enable": False}, "ospf-interfaces": {}}), notes)))
        self.assertEqual(notes[0]["disposition"], "default-omitted")


if __name__ == "__main__":
    unittest.main()

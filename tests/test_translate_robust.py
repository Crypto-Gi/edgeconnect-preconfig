"""A malformed or unexpected capture read never fails the whole translation: the read is ledgered and everything else is written.

Each case breaks one read of a small synthetic capture the way a fuzzer on real captures did (wrong type, null item, missing key) and checks that
build() still returns, that the read is ledgered as not-readable (whole read unusable) or toolkit-error (one item of an unexpected shape),
and that the rest of the preconfig is still written."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "scripts"))
from mappers import Ctx  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
from translate import build  # noqa: E402  guard-ok: imported after the sys.path insert

RA = {"hoplimit": 64, "linkmtu": 1500, "managedflag": False, "maxinterval": 600, "mininterval": 200, "otherconfigflag": True, "reachabletime": 30000,
      "retranstimer": 1000, "routerdeflifetime": 1800, "routerpreference": "high", "sendadv": True}
WAN = {"vlan": 0, "label": "1", "wanSide": True, "vrf": 0, "zone": 0, "harden": 1, "behindNAT": "none", "maxBW": {"outbound": 50000, "inbound": 50000},
       "version": 4, "ip": "198.51.100.10", "mask": 24, "wanNexthop": "198.51.100.1"}
SRV = {"prefix": "10.0.0.0/24", "ipStart": "10.0.0.10", "ipEnd": "10.0.0.200", "gw": [], "dns": [], "netbiosNodeType": "B", "maxLease": 86400, "defaultLease": 3600}
LAN = {"vlan": 0, "label": "4", "wanSide": False, "vrf": 0, "zone": 0, "ip": "10.0.0.1", "mask": 24, "dhcpd": {"type": "server", "server": SRV}}
LAN6 = {"vlan": 90, "label": "", "wanSide": False, "vrf": 0, "zone": 0, "ip": "2001:db8:1::1", "mask": 64, "version": 6, "ra": RA}
DATA = {
    "appliance": {"hostName": "h1", "groupId": "1.Network", "site": "S1", "networkRole": "0"},
    "groups": [{"id": "1.Network", "name": "Net"}],
    "extra-info": {"location": {"address": "1 Example Road"}, "contact": {"name": "ops"}, "overlaySettings": {"isUserDefinedIPSecUDPPort": True, "ipsecUdpPort": 12000}},
    "cluster-profiles": [{"id": 1, "name": "P1"}], "cluster-profile-mapping": {"1": ["S1"]},
    "region-associations": {"9.NE": {"regionId": 2, "regionName": "East"}},
    "template-association": {"templateIds": ["G1"]}, "template-groups": [{"name": "G1", "selectedTemplateNames": ["nac"]}],
    "overlay-config": [{"id": 1, "name": "Mesh"}], "overlay-association": {"1": ["9.NE"]},
    "running-config": {"runningConfig": "interface pass-through max-bandwidth 5000\n"},
    "deployment": {"sysConfig": {"mode": "router", "maxBW": 100000, "haIf": "lan1",
                                 "ifLabels": {"lan": [{"id": "4", "name": "Voice"}], "wan": [{"id": "1", "name": "INET"}]},
                                 "vrfs": [{"id": "0", "name": "Default"}], "vrfZonesMap": {"0": {"a": {"id": 0, "name": "Trusted"}}}, "roles": [{"id": "0", "name": "default"}]},
                   "modeIfs": [{"ifName": "wan0", "applianceIPs": [WAN]}, {"ifName": "lan0", "applianceIPs": [LAN, LAN6]}, {"ifName": "lan1", "applianceIPs": [dict(WAN, vlan=4000)]}]},
    "ha-groups": {"g1": {"subnet": "169.254.1.0/24", "mask": 30, "vlanStart": 4000, "appliances": [{"nePk": "9.NE"}, {"nePk": "8.NE"}]}},
}


class FakeCap:
    folder = "synthetic"

    def __init__(self, data):
        self.run = {"appliance": {"nePk": "9.NE"}}
        self._d = data

    def data(self, ident, prefer="live"):
        return copy.deepcopy(self._d.get(ident))


def translate(change=None):
    data = copy.deepcopy(DATA)
    if change:
        change(data)
    ledger = []
    return build(FakeCap(data), ledger), ledger


def lines(ledger, disposition):
    return [e for e in ledger if e["disposition"] == disposition]


def interfaces(out):
    return [i["interfaceName"] for i in out["deploymentInfo"]["deploymentInterfaces"]]


class CleanFixture(unittest.TestCase):
    def test_the_fixture_translates_without_errors(self):
        out, ledger = translate()
        self.assertEqual(out["applianceInfo"]["group"], "Net")
        self.assertEqual((out["applianceInfo"]["clusterProfile"], out["applianceInfo"]["region"]), ("P1", "East"))
        self.assertEqual(out["templateGroups"], {"groups": ["G1"]})
        self.assertEqual(interfaces(out), ["lan0", "lan0.90", "wan0"])
        di = out["deploymentInfo"]
        self.assertEqual((len(di["dhcpInfo"]), len(di["raInfo"]), di["haConfig"]["haInterface"]), (1, 1, "lan1"))
        self.assertEqual(di["passThroughShapedTraffic"], {"outboundMaxBandwidth": 5000})
        self.assertEqual(lines(ledger, "toolkit-error"), [])
        self.assertEqual([e["item"] for e in lines(ledger, "not-readable")], ["availabilityTimeSettings", "subnets"])  # neither is captured here


class MalformedReads(unittest.TestCase):
    def assertLedgered(self, ledger, disposition, item):
        self.assertTrue(any(e["disposition"] == disposition and e["item"].startswith(item) for e in ledger),
                        "no {} line for {}: {}".format(disposition, item, [(e["item"], e["disposition"]) for e in ledger if e["disposition"] not in ("mapped", "context")]))

    def test_a_group_item_of_another_shape_is_skipped(self):
        out, ledger = translate(lambda d: d["groups"].insert(0, "x"))
        self.assertEqual(out["applianceInfo"]["group"], "Net")
        self.assertLedgered(ledger, "toolkit-error", "groups")

    def test_a_groups_read_of_another_shape_keeps_the_group_id(self):
        out, ledger = translate(lambda d: d.update(groups=7))
        self.assertEqual(out["applianceInfo"]["group"], "1.Network")
        self.assertLedgered(ledger, "not-readable", "groups")

    def test_an_unusable_template_association_writes_no_template_groups(self):
        for bad in (7, "G1", [1], None):
            with self.subTest(templateIds=bad):
                out, ledger = translate(lambda d: d["template-association"].update(templateIds=bad) if bad != [1] else d.update({"template-association": bad}))
                self.assertNotIn("templateGroups", out)  # groups replace on apply: an unread association is not "no groups"
                self.assertLedgered(ledger, "not-readable", "template-association")
                self.assertIn("deploymentInfo", out)

    def test_a_template_group_of_another_shape_is_skipped(self):
        for bad in ("x", {"selectedTemplateNames": ["nac"]}, {"name": "G1", "selectedTemplateNames": 7}):
            with self.subTest(group=bad):
                out, ledger = translate(lambda d: d["template-groups"].insert(0, bad))
                self.assertEqual(out["templateGroups"], {"groups": ["G1"]})
                self.assertTrue(any(e["item"] == "template nac" for e in ledger))  # the well-formed group still provides its section
                self.assertLedgered(ledger, "toolkit-error", "template-groups[0]")

    def test_a_running_config_that_is_not_text_is_not_readable(self):
        for bad in ({"runningConfig": 7}, {"runningConfig": ["x"]}, "x"):
            with self.subTest(read=bad):
                out, ledger = translate(lambda d: d.update({"running-config": bad}))
                self.assertNotIn("passThroughShapedTraffic", out["deploymentInfo"])
                self.assertLedgered(ledger, "not-readable", "running-config")
                self.assertEqual(interfaces(out), ["lan0", "lan0.90", "wan0"])

    def test_appliance_info_reads_of_another_shape(self):
        cases = {
            "site missing": (lambda d: d["appliance"].pop("site"), "toolkit-error", "appliance.site", "site"),
            "groupId missing": (lambda d: d["appliance"].pop("groupId"), "toolkit-error", "appliance.groupId", "group"),
            "networkRole not a number": (lambda d: d["appliance"].update(networkRole="x"), "toolkit-error", "appliance.networkRole", "networkRole"),
            "cluster mapping a list": (lambda d: d.update({"cluster-profile-mapping": [1]}), "toolkit-error", "cluster-profile", "clusterProfile"),
            "cluster profile without name": (lambda d: d.update({"cluster-profiles": [{"id": 1}]}), "toolkit-error", "cluster-profile", "clusterProfile"),
            "region associations a list": (lambda d: d.update({"region-associations": [1]}), "toolkit-error", "region-associations", "region"),
            "region without name": (lambda d: d.update({"region-associations": {"9.NE": {"regionId": 2}}}), "toolkit-error", "region-associations", "region"),
            "location a string": (lambda d: d["extra-info"].update(location="x"), "toolkit-error", "extra-info.location", "location"),
            "contact a number": (lambda d: d["extra-info"].update(contact=7), "toolkit-error", "extra-info.contact", "contact"),
            "extra-info a list": (lambda d: d.update({"extra-info": [1]}), "not-readable", "extra-info", "location"),
        }
        for name, (change, disposition, item, missing) in cases.items():
            with self.subTest(name):
                out, ledger = translate(change)
                self.assertEqual(out["applianceInfo"]["hostname"], "h1")
                self.assertNotIn(missing, out["applianceInfo"])
                self.assertLedgered(ledger, disposition, item)
                self.assertEqual(interfaces(out), ["lan0", "lan0.90", "wan0"])

    def test_deployment_scalars_of_another_shape(self):
        cases = {
            "maxBW not a number": (lambda d: d["deployment"]["sysConfig"].update(maxBW="x"), "deployment.sysConfig.maxBW", "totalOutboundBandwidth"),
            "ipsec port not a number": (lambda d: d["extra-info"]["overlaySettings"].update(ipsecUdpPort="x"), "extra-info.overlaySettings", "ipsecUdpPort"),
            "overlay settings a string": (lambda d: d["extra-info"].update(overlaySettings="x"), "extra-info.overlaySettings", "ipsecUdpPort"),
        }
        for name, (change, item, missing) in cases.items():
            with self.subTest(name):
                out, ledger = translate(change)
                self.assertNotIn(missing, out["deploymentInfo"])
                self.assertLedgered(ledger, "toolkit-error", item)
                self.assertEqual(interfaces(out), ["lan0", "lan0.90", "wan0"])

    def test_overlays_of_another_shape(self):
        out, ledger = translate(lambda d: d["overlay-config"].insert(0, {"name": "NoId"}))
        self.assertEqual(out["businessIntentOverlays"], {"overlays": ["Mesh"]})
        self.assertLedgered(ledger, "toolkit-error", "overlay-config")
        out, ledger = translate(lambda d: d.update({"overlay-association": [1]}))
        self.assertNotIn("businessIntentOverlays", out)  # overlays replace on apply: an unread association is not "no overlays"
        self.assertLedgered(ledger, "not-readable", "overlay-association")

    def test_ha_groups_of_another_shape_write_no_ha_config(self):
        cases = {
            "member not an object": lambda d: d["ha-groups"]["g1"]["appliances"].insert(0, "x"),
            "pool missing": lambda d: d["ha-groups"]["g1"].pop("subnet"),
            "peer link without bandwidth": lambda d: d["deployment"]["modeIfs"][2]["applianceIPs"][0].pop("maxBW"),
        }
        for name, change in cases.items():
            with self.subTest(name):
                out, ledger = translate(change)
                self.assertNotIn("haConfig", out["deploymentInfo"])
                self.assertLedgered(ledger, "toolkit-error", "ha-groups")
                self.assertEqual(interfaces(out), ["lan0", "lan0.90", "wan0"])
        out, ledger = translate(lambda d: d.update({"ha-groups": {"g0": "x", **d["ha-groups"]}}))  # another group of another shape: skipped
        self.assertEqual(out["deploymentInfo"]["haConfig"]["haIpPool"], "169.254.1.0/24")
        self.assertLedgered(ledger, "toolkit-error", "ha-groups[g0]")
        out, ledger = translate(lambda d: d.update({"ha-groups": [1]}))
        self.assertNotIn("haConfig", out["deploymentInfo"])
        self.assertLedgered(ledger, "not-readable", "ha-groups")

    def test_sys_config_lists_of_another_shape(self):
        cases = {
            "vrf item": (lambda d: d["deployment"]["sysConfig"]["vrfs"].insert(0, "x"), "toolkit-error", "deployment.sysConfig.vrfs"),
            "label side": (lambda d: d["deployment"]["sysConfig"]["ifLabels"].update(lan="x"), "not-readable", "deployment.sysConfig.ifLabels.lan"),
            "labels a list": (lambda d: d["deployment"]["sysConfig"].update(ifLabels=[1]), "not-readable", "deployment.sysConfig.ifLabels"),
            "zones a list": (lambda d: d["deployment"]["sysConfig"].update(vrfZonesMap=[1]), "not-readable", "deployment.sysConfig.vrfZonesMap"),
            "zone item": (lambda d: d["deployment"]["sysConfig"]["vrfZonesMap"]["0"].update(b=None), "toolkit-error", "deployment.sysConfig.vrfZonesMap"),
            "role item": (lambda d: d["deployment"]["sysConfig"]["roles"].insert(0, None), "toolkit-error", "deployment.sysConfig.roles"),
        }
        for name, (change, disposition, item) in cases.items():
            with self.subTest(name):
                data = copy.deepcopy(DATA)
                change(data)
                notes = []
                Ctx(FakeCap(data), notes)  # the mappers' own copy of the maps
                self.assertLedgered(notes, disposition, item)
                out, ledger = translate(change)
                self.assertLedgered(ledger, disposition, item)
                self.assertEqual(interfaces(out), ["lan0", "lan0.90", "wan0"])
                self.assertEqual(out["deploymentInfo"]["deploymentInterfaces"][0]["segment"], "Default")

    def test_an_interface_of_another_shape_is_skipped(self):
        out, ledger = translate(lambda d: d["deployment"]["modeIfs"][0]["applianceIPs"][0].update(harden=9))
        self.assertEqual(interfaces(out), ["lan0", "lan0.90"])
        self.assertLedgered(ledger, "toolkit-error", "deployment.modeIfs[wan0]")
        out, ledger = translate(lambda d: d["deployment"]["modeIfs"].insert(0, {"ifName": "lan9", "applianceIPs": 7}))
        self.assertEqual(interfaces(out), ["lan0", "lan0.90", "wan0"])
        self.assertLedgered(ledger, "toolkit-error", "deployment.modeIfs[lan9]")

    def test_dhcp_and_ra_blocks_of_another_shape_drop_only_that_entry(self):
        out, ledger = translate(lambda d: d["deployment"]["modeIfs"][1]["applianceIPs"][0]["dhcpd"]["server"].pop("prefix"))
        self.assertEqual(interfaces(out), ["lan0", "lan0.90", "wan0"])
        self.assertNotIn("dhcpInfo", out["deploymentInfo"])
        self.assertLedgered(ledger, "toolkit-error", "deployment.modeIfs[lan0].dhcpd")
        out, ledger = translate(lambda d: d["deployment"]["modeIfs"][1]["applianceIPs"][1]["ra"].update(prefix_id={"x": {}}))
        self.assertEqual(interfaces(out), ["lan0", "lan0.90", "wan0"])
        self.assertNotIn("raInfo", out["deploymentInfo"])
        self.assertLedgered(ledger, "toolkit-error", "deployment.modeIfs[lan0.90].ra")
        out, ledger = translate(lambda d: d["deployment"].update(dhcpFailover=[1]))
        self.assertEqual(len(out["deploymentInfo"]["dhcpInfo"]), 1)
        self.assertLedgered(ledger, "not-readable", "deployment.dhcpFailover")

    def test_unreadable_mode_interfaces_are_not_invented(self):
        for bad in (None, 7, "x"):
            with self.subTest(modeIfs=bad):
                out, ledger = translate(lambda d: d["deployment"].update(modeIfs=bad))
                self.assertNotIn("deploymentInfo", out)  # interfaces replace on apply: never written without them
                self.assertLedgered(ledger, "not-readable", "deployment.modeIfs")
                self.assertEqual(out["applianceInfo"]["group"], "Net")
                self.assertEqual(out["templateGroups"], {"groups": ["G1"]})

    def test_unusable_appliance_or_deployment_is_still_nothing_to_convert(self):
        from translate import NothingToConvert
        for change in (lambda d: d.update(appliance=[1]), lambda d: d.update(deployment="x"), lambda d: d["deployment"].update(sysConfig="x")):
            with self.assertRaises(NothingToConvert):
                translate(change)


if __name__ == "__main__":
    unittest.main()

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
    "appliances-all": [{"nePk": "8.NE", "serial": "00-11-22", "hostName": "h2"}],
    "security-maps": {"map1": {"12_13": {"prio": {"1000": {"gms_marked": False}}}}},
    "admin-distance": {"ebgp": 20},
}


class FakeCap:
    folder = "synthetic"

    def __init__(self, data, failed=()):
        self.run = {"appliance": {"nePk": "9.NE"}}
        self._d = {k: v for k, v in data.items() if k not in failed}
        # the capture index: every read made, and the ones that failed (HTTP 500) with no usable copy
        self.index = [{"id": k, "label": "live", "status": "ok"} for k in self._d] + [{"id": k, "label": "live", "status": "error", "http": 500} for k in failed]

    def data(self, ident, prefer="live"):
        return copy.deepcopy(self._d.get(ident))


def translate(change=None, failed=()):
    data = copy.deepcopy(DATA)
    if change:
        change(data)
    ledger = []
    return build(FakeCap(data, failed), ledger), ledger


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

    def test_a_groups_read_of_another_shape_writes_no_group(self):
        out, ledger = translate(lambda d: d.update(groups=7))
        self.assertNotIn("group", out["applianceInfo"])  # the raw id "1.Network" would name a group that does not exist
        self.assertLedgered(ledger, "not-readable", "groups")

    def test_an_unusable_template_association_writes_no_template_groups(self):
        for bad in (7, "G1", [1], None):
            with self.subTest(templateIds=bad):
                out, ledger = translate(lambda d: d["template-association"].update(templateIds=bad) if bad != [1] else d.update({"template-association": bad}))
                self.assertNotIn("templateGroups", out)  # groups replace on apply: an unread association is not "no groups"
                self.assertLedgered(ledger, "not-readable", "template-association")
                self.assertIn("deploymentInfo", out)

    def test_an_unread_association_writes_no_section_a_group_may_provide(self):
        out, ledger = translate(lambda d: d["template-association"].update(templateIds=7))
        self.assertTrue(any(e["item"] == "template nac" and e["disposition"] == "not-readable" and e["target"] == "nac" for e in ledger))

    def test_unreadable_field_templates_name_only_their_sections(self):
        def change(d):
            d["template-groups"][0].update(selectedTemplateNames=["nac", "vrrp"], templates=["x"])
        out, ledger = translate(change)
        self.assertEqual([e["target"] for e in ledger if e["item"] == "template-groups templates"], ["vrrp"])

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
            "cluster mapping a list": (lambda d: d.update({"cluster-profile-mapping": [1]}), "not-readable", "cluster-profile", "clusterProfile"),
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
        out, ledger = translate(lambda d: d["deployment"]["modeIfs"][0]["applianceIPs"][0].update(harden=9))  # an own WAN link not read
        self.assertNotIn("haConfig", out["deploymentInfo"])  # its haInterfaceOrder would miss the link
        self.assertLedgered(ledger, "toolkit-error", "ha-groups")
        for where in (0, 2):  # an own WAN link's or a peer link's label that does not resolve
            with self.subTest(label_of=where):
                out, ledger = translate(lambda d: d["deployment"]["modeIfs"][where]["applianceIPs"][0].update(label="9"))
                self.assertNotIn("haConfig", out["deploymentInfo"])
                self.assertLedgered(ledger, "toolkit-error", "ha-groups")
        out, ledger = translate(lambda d: d.update({"ha-groups": {}}))  # read fine, no group lists the appliance: as before, no line
        self.assertNotIn("haConfig", out["deploymentInfo"])
        self.assertFalse(any(e["item"] == "ha-groups" and e["disposition"] == "not-readable" for e in ledger))
        out, ledger = translate(failed=("ha-groups",))
        self.assertNotIn("haConfig", out["deploymentInfo"])
        self.assertLedgered(ledger, "not-readable", "ha-groups")
        out, ledger = translate(lambda d: d.update({"ha-groups": {"g0": "x", **d["ha-groups"]}}))  # another group of another shape: skipped
        self.assertEqual(out["deploymentInfo"]["haConfig"]["haIpPool"], "169.254.1.0/24")
        self.assertLedgered(ledger, "toolkit-error", "ha-groups[g0]")
        out, ledger = translate(lambda d: d.update({"ha-groups": [1]}))
        self.assertNotIn("haConfig", out["deploymentInfo"])
        self.assertLedgered(ledger, "not-readable", "ha-groups")

    def test_a_sys_config_map_item_of_another_shape_makes_the_map_unreadable(self):
        cases = {  # one id without a name would be written as null wherever it is used
            "vrf item": (lambda d: d["deployment"]["sysConfig"]["vrfs"].insert(0, "x"), "deployment.sysConfig.vrfs"),
            "zone item": (lambda d: d["deployment"]["sysConfig"]["vrfZonesMap"]["0"].update(b=None), "deployment.sysConfig.vrfZonesMap"),
            "zone without name": (lambda d: d["deployment"]["sysConfig"]["vrfZonesMap"]["0"]["a"].pop("name"), "deployment.sysConfig.vrfZonesMap"),
            "label without name": (lambda d: d["deployment"]["sysConfig"]["ifLabels"]["lan"][0].pop("name"), "deployment.sysConfig.ifLabels"),
            "role item": (lambda d: d["deployment"]["sysConfig"]["roles"].insert(0, None), "deployment.sysConfig.roles"),
        }
        for name, (change, item) in cases.items():
            with self.subTest(name):
                data = copy.deepcopy(DATA)
                change(data)
                notes = []
                Ctx(FakeCap(data), notes)  # the mappers' own copy of the maps
                self.assertLedgered(notes, "toolkit-error", item)
                out, ledger = translate(change)
                self.assertLedgered(ledger, "toolkit-error", item)
                self.assertNotIn("deploymentInfo", out)
                self.assertTrue(any(e["disposition"] == "not-readable" and e["target"] == "deploymentInfo" for e in ledger))
                self.assertFalse(any(e["item"] == "deployment.modeIfs" and e["disposition"] == "mapped" for e in ledger))

    def test_an_unreadable_sys_config_map_writes_no_deployment_info(self):
        cases = {
            "vrfs an object": (lambda d: d["deployment"]["sysConfig"].update(vrfs={"x": 1}), "deployment.sysConfig.vrfs"),
            "vrfs a string": (lambda d: d["deployment"]["sysConfig"].update(vrfs="x"), "deployment.sysConfig.vrfs"),
            "label side": (lambda d: d["deployment"]["sysConfig"]["ifLabels"].update(lan="x"), "deployment.sysConfig.ifLabels.lan"),
            "labels a list": (lambda d: d["deployment"]["sysConfig"].update(ifLabels=[1]), "deployment.sysConfig.ifLabels"),
            "zones a list": (lambda d: d["deployment"]["sysConfig"].update(vrfZonesMap=[1]), "deployment.sysConfig.vrfZonesMap"),
            "roles an object": (lambda d: d["deployment"]["sysConfig"].update(roles={"x": 1}), "deployment.sysConfig.roles"),
        }
        for name, (change, item) in cases.items():
            with self.subTest(name):
                data = copy.deepcopy(DATA)
                change(data)
                notes = []
                Ctx(FakeCap(data), notes)
                self.assertLedgered(notes, "not-readable", item)
                out, ledger = translate(change)
                self.assertNotIn("deploymentInfo", out)  # interfaces replace on apply: not written with names missing or empty
                self.assertTrue(any(e["disposition"] == "not-readable" and e["target"] == "deploymentInfo" for e in ledger))
                self.assertFalse(any(e["item"] == "deployment.modeIfs" and e["disposition"] == "mapped" for e in ledger))
                self.assertEqual(out["applianceInfo"]["group"], "Net")

    def test_unreadable_segment_names_are_not_reported_as_no_segments(self):
        def change(d):
            d["deployment"]["sysConfig"].update(vrfs={"x": 1})
            d["system-info"] = {"auto_subnet": {"vrf": {"0": {}, "1": {}}}}
        out, ledger = translate(change)
        self.assertNotIn("segmentLocalRoutes", out)
        self.assertFalse(any(e["disposition"] == "default-omitted" and e["target"].startswith("segmentLocalRoutes") for e in ledger))
        self.assertTrue(any(e["disposition"] == "not-readable" and e["target"] == "segmentLocalRoutes" for e in ledger))

    def test_no_interface_read_writes_no_deployment_info(self):
        def change(d):
            for m in d["deployment"]["modeIfs"]:
                for a in m["applianceIPs"]:
                    a["vrf"] = 5  # no such segment
        out, ledger = translate(change)
        self.assertNotIn("deploymentInfo", out)
        self.assertLedgered(ledger, "not-readable", "deployment.modeIfs")
        self.assertFalse(any(e["item"] == "deployment.modeIfs" and e["disposition"] == "mapped" for e in ledger))

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
        def failover(d):
            d["deployment"]["dhcpFailover"] = {"lan0": {"role": "primary"}}  # no my_ip
            d["deployment"]["modeIfs"][1]["applianceIPs"][0]["dhcpd"]["server"]["failover"] = True
        out, ledger = translate(failover)
        self.assertNotIn("dhcpInfo", out["deploymentInfo"])
        self.assertLedgered(ledger, "toolkit-error", "deployment.modeIfs[lan0].dhcpd")
        self.assertFalse(any(e["item"] == "deployment.dhcpFailover" for e in ledger))  # not "no DHCP server there has failover enabled"
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


ROUTE = {"prefix": "10.9.0.0/16", "nextHop": "10.0.0.254", "configured": True, "local": True, "metric": 1, "advert": True, "advert_bgp": False, "advert_ospf": False, "zone_id": 65534, "label": 0}
NAC = {"nacEnable": True, "dot1x": {"1": {"a": 1}}, "macAuthProfile": {"1": {"a": 1}}, "server": {"1": {"address": "192.0.2.5", "source_ip_interface": "INET"}},
       "serverGroup": {"1": {"name": "g", "server": "s"}}, "aaaProfile": {"1": {"a": 1}}, "applyPolicies": {"if": {"lan0": {"auth_type": 1, "profile": "p"}}}}


class NamesAndOwnershipNotGuessed(unittest.TestCase):
    """Second review: outputs that were still built from reads that could not be used."""
    assertLedgered = MalformedReads.assertLedgered

    def test_every_mode_interface_entry_failing_writes_no_deployment_info(self):
        def all_null(d):
            for m in d["deployment"]["modeIfs"]:
                m["applianceIPs"] = None
        for name, change in (("applianceIPs null", all_null), ("entries not objects", lambda d: d["deployment"].update(modeIfs=[None, "x"]))):
            with self.subTest(name):
                out, ledger = translate(change)
                self.assertNotIn("deploymentInfo", out)
                self.assertLedgered(ledger, "not-readable", "deployment.modeIfs")
                self.assertFalse(any(e["disposition"] == "mapped" and e["target"].startswith("deploymentInfo") for e in ledger))

    def test_a_dropped_deployment_info_has_no_mapped_lines(self):
        out, ledger = translate(lambda d: d["deployment"]["sysConfig"].update(vrfs="x"))
        self.assertNotIn("deploymentInfo", out)
        self.assertFalse(any(e["disposition"] in ("mapped", "default-omitted") and e["target"].startswith("deploymentInfo") for e in ledger))

    def test_nac_server_label_is_not_guessed_when_labels_cannot_be_read(self):
        def nac(d, bad):
            d["template-groups"][0].update(selectedTemplateNames=[])
            d["nac"] = NAC
            if bad:
                d["deployment"]["sysConfig"]["ifLabels"].update(wan="x")
        out, _ = translate(lambda d: nac(d, False))
        self.assertEqual(out["nac"]["server"][0]["source_ip_interface"], "INET")
        out, ledger = translate(lambda d: nac(d, True))
        self.assertNotEqual(((out.get("nac") or {}).get("server") or [{}])[0].get("source_ip_interface"), "any")
        self.assertFalse(any(e["disposition"] == "unresolved" and e["item"].startswith("nac.server") for e in ledger))  # not "has no interface label"
        self.assertTrue(any(e["disposition"] == "not-readable" and e["target"] == "nac" for e in ledger))

    def test_vxlan_zones_are_not_guessed_when_segment_names_cannot_be_read(self):
        def vxlan(d, bad):
            d["deployment"]["sysConfig"]["vrfZonesMap"] = {"0": {"a": {"id": 0, "name": "Trusted"}, "b": {"id": 3, "name": "DefZ3"}}, "5": {"c": {"id": 3, "name": "Blue"}}}
            d["deployment"]["sysConfig"]["vrfs"] = "x" if bad else [{"id": "0", "name": "Default"}, {"id": "5", "name": "Seg5"}]
            d["running-config"] = {"runningConfig": "vxlan vtep-interface lo1\nvxlan segment Seg5 vni 5005\nvxlan segment Seg5 zone 3\n"}
        out, _ = translate(lambda d: vxlan(d, False))
        self.assertEqual(out["vxlan"]["vniVrfMaps"][0]["zone"], "Blue")
        out, ledger = translate(lambda d: vxlan(d, True))
        self.assertNotIn("vxlan", out)
        self.assertTrue(any(e["disposition"] == "not-readable" and e["target"] == "vxlan" for e in ledger))

    def test_a_failover_server_that_fails_early_is_not_reported_as_without_failover(self):
        def change(d):
            d["deployment"]["dhcpFailover"] = {"lan0": {"role": "primary", "my_ip": "10.0.0.1", "my_port": 647, "peer_ip": "10.0.0.2", "peer_port": 647}}
            srv = d["deployment"]["modeIfs"][1]["applianceIPs"][0]["dhcpd"]["server"]
            srv["failover"] = True
            del srv["ipEnd"]
        out, ledger = translate(change)
        self.assertLedgered(ledger, "toolkit-error", "deployment.modeIfs[lan0].dhcpd")
        self.assertFalse(any(e["item"] == "deployment.dhcpFailover" for e in ledger))

    def test_unreadable_route_templates_leave_their_sections_out(self):
        def change(d):
            d["template-groups"][0].update(selectedTemplateNames=["routes"], templates=["x"])
            d["subnets"] = {"subnets": {"entries": [{"state": dict(ROUTE)}]}}
        out, ledger = translate(lambda d: d.update(subnets={"subnets": {"entries": [{"state": dict(ROUTE)}]}}))
        self.assertIn("localRoutes", out)  # the fixture's route is written without a template
        out, ledger = translate(change)
        self.assertNotIn("localRoutes", out)  # the routes template may set some of its fields
        self.assertEqual([e["target"] for e in ledger if e["item"] == "template-groups templates"], ["localRoutes, segmentLocalRoutes"])

class SubnetSharingRead(unittest.TestCase):
    """A failed /systemInfo/system read is not "no subnet-sharing settings": nothing is written from it and nothing claims defaults."""

    def run_with(self, system_info, failed=False):
        data = copy.deepcopy(DATA)
        data["template-groups"][0].update(selectedTemplateNames=[])
        data["subnets"] = {"subnets": {"entries": [{"state": dict(ROUTE)}]}}
        data["subnets-all-vrfs"] = {"subnets": {"0": {"entries": [{"state": dict(ROUTE)}]}}}
        if system_info is not None:
            data["system-info"] = system_info
        cap = FakeCap(data)
        cap.index = [{"id": "system-info", "label": "live", "status": "error" if failed else "ok"}]
        ledger = []
        return build(cap, ledger), ledger

    def test_a_readable_sharing_read_is_carried(self):
        out, ledger = self.run_with({"auto_subnet": {"vrf": {"0": {"add_local_lan": True, "add_local_metric": 55}}}})
        self.assertEqual((out["localRoutes"]["advertiseLocalLanSubnets"], out["localRoutes"]["localMetric"]), (True, 55))
        self.assertTrue(any(e["item"] == "system-info.auto_subnet" and e["disposition"] == "default-omitted" for e in ledger))

    def test_a_failed_or_malformed_sharing_read_writes_routes_only_and_says_so(self):
        for name, (info, failed) in {"read failed": (None, True), "not an object": ("x", False), "auto_subnet not an object": ({"auto_subnet": 7}, False)}.items():
            with self.subTest(name):
                out, ledger = self.run_with(info, failed)
                self.assertEqual(len(out["localRoutes"]["routes"]), 1)
                self.assertFalse(set(out["localRoutes"]) - {"routes"})  # no sharing flag is guessed
                self.assertTrue(any(e["item"] == "system-info" and e["disposition"] == "not-readable" and "localRoutes" in e["target"] for e in ledger))
                self.assertFalse(any(e["disposition"] == "default-omitted" and (e["item"].startswith("system-info") or e["target"] == "segmentLocalRoutes") for e in ledger))
                self.assertTrue(any(e["disposition"] == "not-readable" and e["target"].startswith("segmentLocalRoutes") for e in ledger))

class FailedReads(unittest.TestCase):
    """Third review: a read the capture made but could not complete (no usable copy) is not-readable, never an empty value."""
    assertLedgered = MalformedReads.assertLedgered

    def test_each_failed_read_writes_nothing_from_it(self):
        cases = {  # read -> (where the value would be, ledger item)
            "template-association": (lambda out: "templateGroups" in out, "template-association"),
            "overlay-config": (lambda out: "businessIntentOverlays" in out, "overlay-config"),
            "overlay-association": (lambda out: "businessIntentOverlays" in out, "overlay-association"),
            "extra-info": (lambda out: {"location", "contact"} & set(out["applianceInfo"]) or "ipsecUdpPort" in out["deploymentInfo"], "extra-info"),
            "running-config": (lambda out: "passThroughShapedTraffic" in out["deploymentInfo"], "running-config"),
            "groups": (lambda out: "group" in out["applianceInfo"], "groups"),
            "cluster-profiles": (lambda out: "clusterProfile" in out["applianceInfo"], "cluster-profile"),
            "cluster-profile-mapping": (lambda out: "clusterProfile" in out["applianceInfo"], "cluster-profile"),
            "region-associations": (lambda out: "region" in out["applianceInfo"], "region-associations"),
            "ha-groups": (lambda out: "haConfig" in out["deploymentInfo"], "ha-groups"),
            "appliances-all": (lambda out: {"haPeerSerial", "haPeerHostname"} & set(out["deploymentInfo"]["haConfig"]), "appliances-all"),
            "security-maps": (lambda out: False, "security-maps"),
            "admin-distance": (lambda out: False, "admin-distance"),
        }
        out, ledger = translate()
        self.assertEqual(out["deploymentInfo"]["haConfig"]["haPeerSerial"], "001122")
        for ident, (written, item) in cases.items():
            with self.subTest(ident):
                out, ledger = translate(failed=(ident,))
                self.assertFalse(written(out))
                self.assertLedgered(ledger, "not-readable", item)
                self.assertFalse(any(e["disposition"] in ("mapped", "default-omitted") and e["item"].startswith(ident) for e in ledger))
                self.assertEqual(interfaces(out), ["lan0", "lan0.90", "wan0"])

    def test_names_from_reads_of_another_shape_are_not_written_as_ids(self):
        for ident, field in (("groups", "group"), ("cluster-profiles", "clusterProfile"), ("cluster-profile-mapping", "clusterProfile")):
            with self.subTest(ident):
                out, ledger = translate(lambda d: d.update({ident: "x"}))
                self.assertNotIn(field, out["applianceInfo"])  # not the raw id, not silently dropped
                self.assertTrue(any(e["disposition"] == "not-readable" and e["item"].startswith(ident.split("-mapping")[0]) for e in ledger))

    def test_no_stored_backup_is_not_a_default(self):
        out, ledger = translate(lambda d: d.update({"running-config": []}))
        self.assertNotIn("passThroughShapedTraffic", out["deploymentInfo"])
        self.assertFalse(any(e["item"] == "running-config" and e["disposition"] == "default-omitted" for e in ledger))
        self.assertTrue(any(e["item"] == "running-config" and e["disposition"] == "not-readable" for e in ledger))

    def test_a_failover_server_without_failover_settings_is_ledgered(self):
        def change(d):
            d["deployment"]["dhcpFailover"] = {}
            d["deployment"]["modeIfs"][1]["applianceIPs"][0]["dhcpd"]["server"]["failover"] = True
        out, ledger = translate(change)
        self.assertNotIn("dhcpHA", out["deploymentInfo"]["dhcpInfo"][0])
        self.assertTrue(any(e["item"] == "deployment.dhcpFailover" and e["disposition"] == "unresolved" for e in ledger))

    def test_an_unknown_dhcp_entry_counts_as_possibly_wanting_failover(self):
        def base(d):
            d["deployment"]["dhcpFailover"] = {"lan0": {"role": "primary", "my_ip": "10.0.0.1", "my_port": 647, "peer_ip": "10.0.0.2", "peer_port": 647}}
        for name, bad in (("dhcpd x", lambda a: a.update(dhcpd="x")), ("vlan x", lambda a: a.update(vlan="x")), ("entry null", None)):
            with self.subTest(name):
                def change(d):
                    base(d)
                    ips = d["deployment"]["modeIfs"][1]["applianceIPs"]
                    if bad is None:
                        ips[0] = None
                    else:
                        bad(ips[0])
                out, ledger = translate(change)
                self.assertFalse(any(e["item"] == "deployment.dhcpFailover" and e["disposition"] == "runtime" for e in ledger))

    def test_wrong_blame_is_not_added(self):
        out, ledger = translate(lambda d: d["deployment"]["sysConfig"].update(vrfs="x"))
        self.assertEqual([e["item"] for e in ledger if e["disposition"] == "toolkit-error"], [])
        def nac_modeifs_null(d):
            d["template-groups"][0].update(selectedTemplateNames=[])
            d["nac"] = NAC
            d["deployment"]["modeIfs"] = None
        out, ledger = translate(nac_modeifs_null)
        self.assertNotIn("nac", out)
        self.assertEqual([e["item"] for e in ledger if e["disposition"] == "toolkit-error"], [])
        self.assertTrue(any(e["disposition"] == "not-readable" and e["target"] == "nac" for e in ledger))

    def test_a_dangling_label_or_zone_id_is_ledgered(self):
        for name, change in (("label", lambda d: d["deployment"]["modeIfs"][1]["applianceIPs"][0].update(label="77")),
                             ("zone", lambda d: d["deployment"]["modeIfs"][1]["applianceIPs"][0].update(zone=77))):
            with self.subTest(name):
                out, ledger = translate(change)
                self.assertEqual(interfaces(out), ["lan0", "lan0.90", "wan0"])
                self.assertTrue(any(e["disposition"] == "unresolved" and "77" in e["note"] for e in ledger))

    def test_vxlan_maps_are_not_called_vtep_less_when_the_backup_is_unreadable(self):
        for name, cfg in (("not an object", "x"), ("text not a string", {"runningConfig": 5}), ("no stored backup", [])):
            with self.subTest(name):
                out, ledger = translate(lambda d: d.update({"vxlan-vni-map": {"0": {"vni": 1001}}, "running-config": cfg}), failed=("vxlan",))
                self.assertNotIn("vxlan", out)
                self.assertFalse(any(e["disposition"] == "outside-preconfig" and e["target"] == "vxlan" for e in ledger))
                self.assertTrue(any(e["disposition"] == "not-readable" and e["target"] == "vxlan" for e in ledger))

    def test_a_null_or_odd_sharing_read_is_not_no_settings(self):
        for name, info in (("ok but null", None), ("auto_subnet a list", {"auto_subnet": []})):
            with self.subTest(name):
                data = copy.deepcopy(DATA)
                data["subnets"] = {"subnets": {"entries": [{"state": dict(ROUTE)}]}}
                data["system-info"] = info
                cap = FakeCap(data)
                cap.index.append({"id": "system-info", "label": "live", "status": "ok"})
                ledger = []
                build(cap, ledger)
                self.assertTrue(any(e["item"] == "system-info" and e["disposition"] == "not-readable" for e in ledger))

if __name__ == "__main__":
    unittest.main()

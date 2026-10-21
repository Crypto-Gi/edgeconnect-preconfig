import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "harnesses"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "building-edgeconnect-preconfig-templates" / "scripts"))
from apply_and_diff import GuardrailError, check_guardrails, compare_intended_vs_actual  # noqa: E402
from common import parse_yaml  # noqa: E402
from mappers import Ctx, m_bgpSystemConfig, m_branchNatMapsConfig, m_dhcpRemoteAddressPoolConfig, m_flowRedirection, m_ipAllowList, m_multicast, m_nac, m_peerPriority, m_segmentBgpSystems, m_tunnelConfiguration, m_vrrp, m_vxlan, m_managementIp, m_ecLicensing, m_managementRoutes, m_secureWebServicesConfig, passthrough_max_bandwidth  # noqa: E402
from synth import substitute  # noqa: E402
from mappers import m_idsIpsConfig, m_interfaceConfig, m_segmentOspfConfig  # noqa: E402
from suite import load_constraints  # noqa: E402
from translate import HARDEN, build, drop_template_fields, local_firewall_rules, prune, template_fields  # noqa: E402
import copy  # noqa: E402


class FakeCap:
    def __init__(self, data):
        self.run = {"appliance": {"nePk": "9.NE"}}
        self._d = data

    def data(self, ident, prefer="live"):
        return self._d.get(ident)


DEP = {"sysConfig": {"ifLabels": {"lan": [{"id": "4", "name": "Voice"}], "wan": []}, "vrfs": [{"id": "0", "name": "Default"}], "vrfZonesMap": {}, "roles": [{"id": "0", "name": "default"}]},
       "modeIfs": [{"ifName": "lan0", "applianceIPs": [{"vlan": 0, "label": "4"}]}]}


def ctx(data):
    notes = []
    return Ctx(FakeCap(data), notes), notes


class MapperRules(unittest.TestCase):
    def test_peer_priority_uses_weight_not_metric(self):
        c, _ = ctx({"peer-priority": {"peerlist": {"B": {"peer_metric": 0, "peer_weight": 5}, "A": {"peer_metric": 0, "peer_weight": 10}}}})
        self.assertEqual(m_peerPriority(c), {"peerPriorities": [{"peerName": "A", "priority": 10}, {"peerName": "B", "priority": 5}]})

    def test_vrrp_drops_runtime_state(self):
        c, notes = ctx({"vrrp": [{"groupId": 1, "interface": "lan0", "enable": "Up", "vipaddr": "192.0.2.1", "adv_timer": 100, "priority": 101, "tracker": False, "version": 3, "preempt": True,
                                  "auth": "", "desc": "", "pkt_trace": False, "holddown": 10, "mode": "master", "uptime": "1 day", "vmac": "00:00:5e:00:01:01"}]})
        out = m_vrrp(c)
        self.assertNotIn("mode", out["vrrpEntries"][0])
        self.assertNotIn("authenticationString", out["vrrpEntries"][0])
        self.assertFalse(any(n["disposition"] == "unmapped-source-key" for n in notes))

    def test_nac_with_an_empty_list_is_not_written(self):
        src = {"nacEnable": False, "server": {"1": {"self": 1, "address": "192.0.2.5", "authport": 1812, "key": "", "name": "default", "source_ip_interface": "any"}},
               "serverGroup": {"1": {"self": 1, "name": "default", "server": {"1": {"self": 1, "name": "default"}}}}, "aaaProfile": {"1": {"self": 1, "name": "default", "da_server": ""}},
               "dot1x": {"1": {"self": 1, "name": "default"}}, "macAuthProfile": {"1": {"self": 1, "name": "default"}}, "applyPolicies": {"if": {"lan0": {"profile": "none", "auth_type": 0}}}}
        # every interface trusted (no AAA profile): written without a profile, which /validate accepts and "none" it rejects
        self.assertEqual(m_nac(ctx({"nac": src, "deployment": DEP})[0])["applyPolicies"], [{"id": 1, "auth_interface": "Voice", "auth_type": "trusted"}])
        c, notes = ctx({"nac": dict(src, dot1x={}), "deployment": DEP})
        self.assertIsNone(m_nac(c))
        self.assertTrue(any(n["disposition"] == "unresolved" and "dot1x" in n["note"] for n in notes))
        src["applyPolicies"] = {"if": {"lan0": {"profile": "default", "auth_type": 1}}}
        out = m_nac(ctx({"nac": src, "deployment": DEP})[0])
        self.assertEqual(out["server"][0]["host"], "192.0.2.5")
        self.assertEqual(out["server"][0]["key"], "<SECRET:not-captured>")
        self.assertEqual(out["applyPolicies"][0]["auth_interface"], "Voice")
        src["serverGroup"]["1"]["server"] = {"2": {"self": 2, "name": "radius2"}, "10": {"self": 10, "name": "backup"}, "1": {"self": 1, "name": "default"}}
        self.assertEqual(m_nac(ctx({"nac": src, "deployment": DEP})[0])["serverGroup"][0]["server"], "default, radius2, backup")  # /validate rejects "a,b"

    def test_multicast_drops_stale_pim_entries_and_reads_list_shape(self):
        data = {"deployment": DEP, "multicast-enable": [{"vrf_id": 0, "enable": True}],
                "multicast-config": [{"vrf_id": 0, "rp": "192.0.2.9", "pim": {"lan0": {"dr_priority": 1, "enable": True, "hello_interval": 30, "join_prune_interval": 30},
                                                                                "GHOST": {"dr_priority": 1, "enable": True, "hello_interval": 30, "join_prune_interval": 30}}, "igmp": {}, "filter_name": None}]}
        c, notes = ctx(data)
        out = m_multicast(c)
        self.assertEqual([p["interfaceName"] for p in out[0]["pim"]], ["lan0"])
        self.assertEqual(out[0]["enable"], "true")
        self.assertTrue(any("GHOST" in n["note"] for n in notes))
        self.assertIsNone(m_multicast(ctx({"multicast-config": {"pim0": {}}})[0]))

    def test_managed_tunnels_are_skipped_and_user_tunnel_maps(self):
        user = {"alias": "T1", "mode": "ipsec_ip", "adminStatus": "up", "sourceIpAddress": "192.0.2.1", "destIpAddress": "198.51.100.1", "natMode": "none", "peerName": "", "max_bw_auto": True, "max_bw": 0,
                "presharedkey": "<SECRET:x>", "ike_aalg": "sha1", "ike_ealg": "default", "dhgroup": "14", "ike_lifetime": 360, "dpd_delay": 10, "dpd_retry": 3, "id_type": "address", "id_str": "a", "exchange_mode": "main",
                "ike_version": 2, "ike_id_local": "", "ike_id_remote": "", "ike_prf": "auto", "ah": "sha1", "esp": "", "ipsecAntiReplayWindow": "8192", "lifetime": 360, "lifebytes": 0, "pfs": True, "pfsgroup": "14",
                "ipsec_Suite_b": "none", "gmsMarked": False}
        c, notes = ctx({"passthrough-tunnels": {"a": dict(user), "b": {"gmsMarked": True}}})
        out = m_tunnelConfiguration(c)
        self.assertEqual(len(out["tunnels"]), 1)
        t = out["tunnels"][0]
        self.assertEqual((t["tunnelEncap"], t["ikeConfig"]["diffHellManGroup"], t["ipsecConfig"]["ipsecEncryptAlgorithm"], t["ipsecConfig"]["secrecyGroup"]), ("ipsec", 14, "auto", 14))  # esp "" is how auto is stored
        self.assertEqual(notes[0]["disposition"], "orchestrator-managed")
        off = m_tunnelConfiguration(ctx({"passthrough-tunnels": {"a": dict(user, pfs=False)}})[0])
        self.assertEqual(off["tunnels"][0]["ipsecConfig"]["secrecyGroup"], "disable")
        self.assertEqual(m_tunnelConfiguration(ctx({"passthrough-tunnels": {"a": dict(user, ipsecAntiReplayWindow="disable")}})[0])["tunnels"][0]["ipsecConfig"]["enableIpSecArcWindow"], 0)  # ECOS stores 0 as "disable"
        c, notes = ctx({"passthrough-tunnels": {"a": dict(user, ipsecAntiReplayWindow="odd"), "b": dict(user, alias="T2")}})
        self.assertEqual([t["tunnelName"] for t in m_tunnelConfiguration(c)["tunnels"]], ["T2"])
        self.assertTrue(any(n["disposition"] == "unresolved" and "T1" in n["note"] for n in notes))
        self.assertEqual(m_tunnelConfiguration(ctx({"passthrough-tunnels": {"a": dict(user, esp="null")}})[0])["tunnels"][0]["ipsecConfig"]["ipsecEncryptAlgorithm"], "none")
        for esp in ("null", ""):
            gmac = m_tunnelConfiguration(ctx({"passthrough-tunnels": {"a": dict(user, ah="aes128gmac", esp=esp)}})[0])
            self.assertEqual(gmac["tunnels"][0]["ipsecConfig"]["ipsecEncryptAlgorithm"], "null")

    def test_secure_web_services_kept_when_a_value_is_not_default(self):
        url = {"hasURLClassification": False, "incrementalUpdate": False, "queryOnMiss": False, "urlReputation": 1}
        ip = {"hasIPReputation": False, "incrementalUpdate": False}
        src = lambda u: {"secure-web-services": {"secureWebServicesConfig": {"hasSecureWebServicesConfig": False, "urlClassificationConfig": u, "ipReputationConfig": ip}}}
        self.assertIsNone(m_secureWebServicesConfig(ctx(src(url))[0]))
        self.assertEqual(m_secureWebServicesConfig(ctx(src(dict(url, urlReputation=4)))[0])["urlClassificationConfig"]["urlReputation"], 4)

    def test_cli_sections(self):
        self.assertEqual(passthrough_max_bandwidth(["hostname x", "interface pass-through max-bandwidth 50000"]), 50000)
        self.assertIsNone(passthrough_max_bandwidth(["hostname x"]))
        backup = {"fileName": "b", "backupTime": "1", "runningConfig": "ip mgmt-ip 192.0.2.7\nsystem eclicense boost bandwidth 50000\nsystem eclicense boost enable\n"}
        self.assertEqual(m_managementIp(ctx({"running-config": backup})[0]), {"ipAddress": "192.0.2.7"})
        self.assertEqual(m_ecLicensing(ctx({"running-config": backup})[0]), {"useDefaultAccount": True, "boost": 50000})
        self.assertIsNone(m_managementIp(ctx({"running-config": {"runningConfig": "hostname x"}})[0]))
        cfg = {"runningConfig": "ip default-gateway 192.0.2.1 mgmt0 1\nip route 203.0.113.0 /24 192.0.2.253 mgmt0 10 0.0.0.0\nip route 203.0.113.0 /24 192.0.2.253 mgmt0 10 0.0.0.0\nip route 198.51.100.0 /24 192.0.2.9 lan0 1\n"}
        self.assertEqual(m_managementRoutes(ctx({"running-config": cfg})[0])["routes"],
                         [{"routeIpSubnet": "0.0.0.0/0", "nextHop": "192.0.2.1", "interfaceName": "mgmt0", "metric": 1, "sourceIp": None},
                          {"routeIpSubnet": "203.0.113.0/24", "nextHop": "192.0.2.253", "interfaceName": "mgmt0", "metric": 10, "sourceIp": "0.0.0.0"}])


class Substitution(unittest.TestCase):
    INV = {"groups": ["G"], "overlays": ["O1", "O2"], "templateGroups": ["T"], "lan": ["Voice", "Data"], "wan": ["MPLS1"], "segments": ["Default", "S1"], "zones": ["Default", "Z1"]}

    def test_names_map_one_to_one_by_kind_and_values_stay(self):
        doc = {"applianceInfo": {"hostname": "h", "group": "X", "region": "R"}, "templateGroups": {"groups": ["a", "b"]}, "businessIntentOverlays": {"overlays": ["a", "b", "c"]},
               "deploymentInfo": {"deploymentInterfaces": [{"interfaceName": "lan0", "interfaceType": "lan", "interfaceLabel": "L1", "segment": "Corp", "zone": "Z", "role": "Employee", "ipAddressMask": "192.0.2.1/24"},
                                                         {"interfaceName": "lan1", "interfaceType": "lan", "interfaceLabel": "L2", "segment": "Default"},
                                                         {"interfaceName": "wan0", "interfaceType": "wan", "interfaceLabel": "W1"}]}, "appExpressGroups": ["g"]}
        notes = []
        out = substitute(doc, self.INV, notes)
        i = out["deploymentInfo"]["deploymentInterfaces"]
        self.assertEqual([x["interfaceLabel"] for x in i], ["Voice", "Data", "MPLS1"])
        self.assertEqual((i[0]["segment"], i[1]["segment"], i[0]["role"], i[0]["ipAddressMask"]), ("S1", "Default", "default", "192.0.2.1/24"))
        self.assertIsNone(out["applianceInfo"]["region"])
        self.assertNotIn("appExpressGroups", out)
        self.assertTrue(notes)
        self.assertEqual(doc["applianceInfo"]["group"], "X")  # input untouched


class Release(unittest.TestCase):
    def test_prune_removes_gated_paths_and_ledgers_once(self):
        ledger = []
        out = prune({"a": {"keep": 1, "gated": 2}, "l": [{"gated": 1, "x": 2}, {"gated": 3}]}, {"a.gated", "l.gated"}, ledger)
        self.assertEqual(out, {"a": {"keep": 1}, "l": [{"x": 2}, {}]})
        self.assertEqual([e["target"] for e in ledger], ["a.gated", "l.gated"])


class ApplyGuardrailAndDiff(unittest.TestCase):
    def test_guardrails_enforce_vlab_wan0_dhcp_100m_and_no_mgmt_ip(self):
        valid = {"deploymentInfo": {"deploymentInterfaces": [{"interfaceName": "wan0.4000", "addressingMode": "dhcpv4", "inboundMaxBandwidth": 100000, "outboundMaxBandwidth": 100000}]}}
        check_guardrails("vlab", valid)
        with self.assertRaises(GuardrailError):
            check_guardrails("prod", valid)
        with self.assertRaises(GuardrailError):
            check_guardrails("vlab", {"managementIp": {"ipAddress": "192.0.2.1"}})
        with self.assertRaises(GuardrailError):
            check_guardrails("vlab", {"deploymentInfo": {"deploymentInterfaces": [{"interfaceName": "wan0.4000", "addressingMode": "static", "inboundMaxBandwidth": 100000, "outboundMaxBandwidth": 100000}]}})
        with self.assertRaises(GuardrailError):
            check_guardrails("vlab", {"deploymentInfo": {"deploymentInterfaces": [{"interfaceName": "wan0.4000", "addressingMode": "dhcpv4", "inboundMaxBandwidth": 50000, "outboundMaxBandwidth": 100000}]}})

    def test_compare_intended_vs_actual(self):
        intended = {"applianceInfo": {"hostname": "h1", "region": None}, "bgpSystemConfig": {"asn": 65001, "neighbors": [{"password": "secret"}]}}
        actual = {"applianceInfo": {"hostname": "h1"}, "bgpSystemConfig": {"asn": 65002, "neighbors": [{"password": "<SECRET:$.x>"}]}}
        diff = compare_intended_vs_actual(intended, actual)
        self.assertEqual(diff["mismatch_count"], 1)
        self.assertEqual(diff["mismatches"][0]["path"], "bgpSystemConfig.asn")
        self.assertEqual(diff["missing_count"], 0)

    def test_compare_reads_single_segment_intent_from_segment_default(self):
        intended = {"bgpSystemConfig": {"asn": 65001, "enable": True}}
        actual = {"segmentBgpSystems": [{"segment": "corp", "asn": 65009}, {"segment": "Default", "asn": 65001, "enable": True}]}
        diff = compare_intended_vs_actual(intended, actual)
        self.assertEqual((diff["matched_count"], diff["mismatch_count"], diff["missing_count"]), (2, 0, 0))

    def test_compare_aligns_reordered_lists_by_identity_key(self):
        intended = {"deploymentInfo": {"deploymentInterfaces": [{"interfaceName": "lan0", "zone": "a"}, {"interfaceName": "lan0.90", "zone": "b"},
                                                                {"interfaceName": "wan1", "zone": "c"}]}}
        actual = {"deploymentInfo": {"deploymentInterfaces": [{"interfaceName": "wan1", "zone": "c"}, {"interfaceName": "lan0", "zone": "a"},
                                                              {"interfaceName": "wan0.4000", "zone": "d"}]}}
        diff = compare_intended_vs_actual(intended, actual)
        self.assertEqual(diff["mismatch_count"], 0)
        self.assertEqual([m["path"] for m in diff["missing_in_actual"]], ["deploymentInfo.deploymentInterfaces[1].interfaceName", "deploymentInfo.deploymentInterfaces[1].zone"])
        self.assertEqual([e["actual"] for e in diff["extra_in_same_sections"]], ["wan0.4000", "d"])

    def test_vlab_discovered_fixes(self):
        self.assertEqual(HARDEN, {0: "All", 1: "harden", 2: "stateful", 3: "statefulSNAT"})
        # managementIp omitted when mgmt0 is DHCP
        backup = {"fileName": "b", "backupTime": "1", "runningConfig": "ip mgmt-ip 192.0.2.187\n"}
        self.assertIsNone(m_managementIp(ctx({"running-config": backup, "deployment": {"mgmtIfData": {"mgmt0": {"dhcp": True}}}})[0]))
        # ipAllowList includes gmsMarked entries pushed by Orchestrator
        allow = m_ipAllowList(ctx({"ip-allow-list": {"enable": True, "ip": {"192.0.2.0/24": {"prefixLength": 24, "comment": "lab", "protocol": ["https", "ssh"], "gmsMarked": True}}}})[0])
        self.assertEqual(allow["ips"][0]["ip"], "192.0.2.0")
        # branchNatMapsConfig reads /nat/maps
        nat = m_branchNatMapsConfig(ctx({"deployment": DEP, "branch-nat-maps": {"map1": {"prio": {"10": {"dir": "outbound", "enable": True, "comment": "c",
            "match": {"intf": "any", "segment": "0", "proto": "tcp", "src_subnet": "192.0.2.0/24", "dst_subnet": "10.0.0.0/16", "dst_port": "1-10"},
            "set": {"trans_src": "198.51.100.0/24", "trans_dst": "10.0.0.0/16", "trans_dport": "11-20"}}}}}})[0])
        self.assertEqual(nat["branchNatRules"][0]["translateSourceIpSubnet"], "198.51.100.0/24")
        # Multi-segment BGP omits bgpSystemConfig and puts Default + non-Default inside segmentBgpSystems
        dep2 = {"sysConfig": {"ifLabels": {"lan": [], "wan": []}, "vrfs": [{"id": "0", "name": "Default"}, {"id": "1", "name": "corp"}], "vrfZonesMap": {}, "roles": []}}
        bgp_all = {"0": {"enable": True, "asn": 65001, "rtr_id": "192.0.2.1", "graceful_restart_en": True, "max_restart_time": 120, "stale_path_time": 150, "remote_as_path_advertise": True},
                   "1": {"enable": True, "asn": 65101, "rtr_id": "203.0.113.1", "graceful_restart_en": True, "max_restart_time": 120, "stale_path_time": 150, "remote_as_path_advertise": True}}
        data = {"deployment": dep2, "bgp-system": bgp_all["0"], "bgp-neighbors": {}, "bgp-system-all-vrfs": bgp_all, "bgp-neighbors-all-vrfs": {"0": {}, "1": {}}}
        self.assertIsNone(m_bgpSystemConfig(ctx(data)[0]))
        segs = m_segmentBgpSystems(ctx(data)[0])
        self.assertEqual([s["segment"] for s in segs], ["Default", "corp"])

    def test_round2_97_and_edge_case_fixes(self):
        # Leading-zero numeric string (NJ ZIP 08034) does not crash octal parser
        self.assertEqual(parse_yaml("zipCode: 08034\ncount: 10\n"), {"zipCode": "08034", "count": 10})
        # dhcpRemoteAddressPoolConfig skips local interface DHCP pools and keeps only remote pools
        pools = [{"interface": "lan0", "subnet": "10.0.0.0/24", "scopeName": "lan0-10", "ipRange": [], "gatewayIp": "10.0.0.1", "dnsServers": [], "ntpServers": [], "netbiosNameServers": [], "netbiosNodeType": "B", "maxLeaseTime": 600, "defaultLeaseTime": 60},
                 {"interface": None, "subnet": "192.168.8.0/24", "scopeName": "GUEST", "ipRange": [], "gatewayIp": "192.168.8.1", "dnsServers": [], "ntpServers": [], "netbiosNameServers": [], "netbiosNodeType": "B", "maxLeaseTime": 600, "defaultLeaseTime": 60}]
        self.assertEqual(m_dhcpRemoteAddressPoolConfig(ctx({"dhcp-remote-subnets": pools})[0])["dhcpAddressMask"], "192.168.8.0/24")
        # multicast igmpEnable reads seg['igmp'][k]['enable']
        mc = m_multicast(ctx({"deployment": DEP, "multicast-enable": [{"vrf_id": 0, "enable": True}],
                              "multicast-config": [{"vrf_id": 0, "rp": "192.0.2.9", "pim": {"lan0": {"dr_priority": 1, "enable": True, "hello_interval": 30, "join_prune_interval": 30}},
                                                    "igmp": {"lan0": {"enable": False}}, "filter_name": None}]})[0])
        self.assertFalse(mc[0]["pim"][0]["igmpEnable"])
        # vxlan and flowRedirection read live JSON endpoints
        vx = m_vxlan(ctx({"deployment": DEP, "vxlan": {"port": 4789, "vtepIf": "lo1"}, "vxlan-vni-map": {"0": {"vni": 1001, "zoneId": 0, "role": 0}}})[0])
        self.assertEqual((vx["vtepSrcInterface"], vx["vniVrfMaps"][0]["vni"]), ("lo1", 1001))
        fr = m_flowRedirection(ctx({"cluster-state": {"S1": {"siteApplianceInterfaceIps": {"9.NE": {"state": {"flow_redir": True, "wait_time": 10, "interface": "wan1", "peerIPStatusList": [{"peerIP": "192.0.2.1"}]}}}}}})[0])
        self.assertEqual((fr["interfaceName"], fr["peers"][0]["peerIp"]), ("wan1", "192.0.2.1"))
        # running-config fallback: no wait-time line means the ECOS default 50, a line overrides it
        cfg = "cluster interface wan1\ncluster peer 192.0.2.1\nflow-redirection enable"
        self.assertEqual(m_flowRedirection(ctx({"running-config": {"runningConfig": cfg}})[0])["waitTime"], 50)
        self.assertEqual(m_flowRedirection(ctx({"running-config": {"runningConfig": cfg + "\nflow-redirection wait-time 20"}})[0])["waitTime"], 20)


class AuditRegressions(unittest.TestCase):
    """Payload shapes from older/newer ECOS releases and lab runs that crashed a mapper, aborted translate, or wrote a wrong value."""

    def _build(self, ips, ifname="lan0", ledger=None, **extra):
        dep = copy.deepcopy(DEP)
        dep["sysConfig"]["ifLabels"]["wan"] = [{"id": "1", "name": "INET"}]
        dep["modeIfs"] = [{"ifName": ifname, "applianceIPs": ips}]
        data = dict({"appliance": {"hostName": "h1", "groupId": "1.Network", "site": "S1", "networkRole": "0"}, "deployment": dep}, **extra)
        cap = FakeCap(data)
        cap.folder = "fake"
        return build(cap, [] if ledger is None else ledger)

    def test_local_firewall_rules_are_ledgered_not_written(self):
        rule = {"gms_marked": False, "match": {"acl": "a"}, "set": {"action": "allow"}}
        maps = {"map1": {"12_13": {"prio": {"1000": rule, "1010": rule, "65535": rule, "1020": dict(rule, gms_marked=True)}}, "13_12": {"prio": {"1500": rule}}}}
        text = local_firewall_rules(FakeCap({"security-maps": maps}))
        self.assertTrue(text.startswith("3 firewall rule(s) and 1 zone-pair default rule(s)"))
        self.assertIsNone(local_firewall_rules(FakeCap({"security-maps": {"map1": {"12_13": {"prio": {"1020": dict(rule, gms_marked=True)}}}}})))
        self.assertIsNone(local_firewall_rules(FakeCap({})))
        ledger = []
        wan = {"vlan": 0, "label": "1", "wanSide": True, "vrf": 0, "zone": 0, "harden": 1, "behindNAT": "none", "maxBW": {"outbound": 50000}, "intf_mode": "static_slaac",
               "version": 4, "ip": "198.51.100.10", "mask": 24, "wanNexthop": "198.51.100.1"}
        doc = self._build([wan], "wan0", ledger, **{"security-maps": maps})
        self.assertEqual([e["disposition"] for e in ledger if e["item"] == "security-maps"], ["unresolved"])
        self.assertFalse(any("ecurity" in k or "irewall" in k for k in doc))

    def test_admin_distance_and_sse_interfaces_are_ledgered_not_dropped(self):
        distance = {"ebgp": 16, "ibgp": 200, "ospf": 110}
        sse = {"admin": True, "interface": "veth20000", "ip_addr": "192.0.2.6", "mask": 31}
        ledger = []
        self._build([], ledger=ledger, **{"admin-distance": distance, "sse-interfaces": sse})
        got = {e["item"]: e for e in ledger if e["item"] in ("admin-distance", "sse-interfaces")}
        self.assertEqual(got["admin-distance"]["disposition"], "unresolved")
        self.assertIn("ebgp 16", got["admin-distance"]["note"])
        self.assertEqual(got["sse-interfaces"]["disposition"], "orchestrator-managed")
        self.assertIn("veth20000", got["sse-interfaces"]["note"])
        ledger = []
        groups = [{"name": "G1", "selectedTemplateNames": ["adminDistance"], "templates": []}]
        self._build([], ledger=ledger, **{"admin-distance": distance, "template-association": {"templateIds": ["G1"]}, "template-groups": groups})
        self.assertEqual([e["disposition"] for e in ledger if e["item"] == "admin-distance"], ["template-group"])

    def test_template_fields_owns_only_fields_a_selected_associated_template_sets(self):
        routes = {"name": "routes", "value": {"ecmp_en": True, "auto_subnet": {"vrf": {"0": {"add_local_lan": True, "add_local_metric": 50, "local_communities": "", "add_local_communities": "none"},
                                                                                   "82": {"local_asn_filter": "true"}}}}}
        vrrp = {"name": "vrrp", "value": {"admin": "Up", "advTimer": "", "priority": "", "preempt": False, "auth": "x"}}
        bgp = {"name": "bgp", "value": {"vrf": {"0": {"graceful_restart_en": "none", "max_restart_time": 120, "route_target_template": {"import": "65000:1", "export": ""}}}}}
        groups = [{"name": "G1", "selectedTemplateNames": ["routes", "vrrp"], "templates": [routes, vrrp, bgp]},
                  {"name": "G2", "selectedTemplateNames": ["bgp"], "templates": [bgp]},
                  {"name": "G3", "selectedTemplateNames": ["vrrp"], "templates": [dict(vrrp, value={"priority": 200})]}]
        owned = template_fields(FakeCap({"template-association": {"templateIds": ["G1", "G2"]}, "template-groups": groups}))
        self.assertEqual(owned["routes"], {"0": {"advertiseLocalLanSubnets", "localMetric"}, "82": {"filterRoutesWithLocalASN"}})
        self.assertEqual(owned["vrrp"], {None: {"admin", "preemption", "authenticationString"}})
        self.assertEqual(owned["bgp"], {"0": {"maxRestartTime", "importRouteTarget"}})

    def test_drop_template_fields_keeps_what_no_template_sets(self):
        out = {"vrrp": {"vrrpEntries": [{"groupId": 1, "interfaceName": "lan0", "virtualIpAddress": "192.0.2.1", "admin": "up", "priority": 110}]},
               "localRoutes": {"advertiseLocalLanSubnets": True, "localMetric": 50, "routes": []},
               "segmentBgpSystems": [{"segment": "Default", "asn": 65001, "maxRestartTime": 120, "neighbors": []}, {"segment": "Guest", "asn": 65001, "maxRestartTime": 90, "neighbors": []}]}
        notes = []
        owned = {"vrrp": {None: {"admin"}}, "routes": {"0": {"advertiseLocalLanSubnets", "localMetric"}}, "bgp": {"0": {"maxRestartTime"}}}
        drop_template_fields(out, owned, {"0": "Default", "82": "Guest"}, lambda *a: notes.append(a))
        self.assertEqual(out["vrrp"]["vrrpEntries"][0], {"groupId": 1, "interfaceName": "lan0", "virtualIpAddress": "192.0.2.1", "priority": 110})
        self.assertNotIn("localRoutes", out)
        self.assertNotIn("maxRestartTime", out["segmentBgpSystems"][0])
        self.assertEqual(out["segmentBgpSystems"][1]["maxRestartTime"], 90)
        self.assertIn(("template bgp", "unresolved"), [n[:2] for n in notes])

    def test_vrrp_template_no_longer_drops_vrrp_groups(self):
        vrrp_src = [{"groupId": 1, "interface": "lan0", "enable": True, "vipaddr": "192.0.2.1", "adv_timer": 1, "priority": 110, "tracker": "", "version": 2,
                     "preempt": True, "auth": "", "desc": "", "pkt_trace": False, "holddown": 10}]
        groups = [{"name": "G1", "selectedTemplateNames": ["vrrp"], "templates": [{"name": "vrrp", "value": {"admin": "Up", "advTimer": "", "priority": "", "preempt": False}}]}]
        wan = {"vlan": 0, "label": "1", "wanSide": True, "vrf": 0, "zone": 0, "harden": 1, "behindNAT": "none", "maxBW": {"outbound": 50000}, "intf_mode": "static_slaac",
               "version": 4, "ip": "198.51.100.10", "mask": 24, "wanNexthop": "198.51.100.1"}
        doc = self._build([wan], "wan0", **{"vrrp": vrrp_src, "template-association": {"templateIds": ["G1"]}, "template-groups": groups})
        entry = doc["vrrp"]["vrrpEntries"][0]
        self.assertEqual((entry["groupId"], entry["priority"]), (1, 110))
        self.assertNotIn("admin", entry)
        self.assertNotIn("preemption", entry)

    def test_bgp_neighbor_without_evpn_key_before_ecos_9_4_1(self):
        nb = {"192.0.2.2": {"remote_as": 65002, "type": "PE-router", "enable": True, "hold": 180, "ka": 60, "lcl_interface": "", "next_hop_self": False,
                            "password": "", "store_received_routes": False, "bfd_desired": False, "directly_connected": True}}
        sys0 = {"enable": True, "asn": 65001, "rtr_id": "10.0.0.1"}
        out = m_bgpSystemConfig(ctx({"deployment": DEP, "bgp-system": sys0, "bgp-neighbors": nb, "bgp-system-all-vrfs": {"0": sys0}, "bgp-neighbors-all-vrfs": {"0": nb}})[0])
        self.assertIs(out["neighbors"][0]["enableEVPN"], False)

    def test_null_sysconfig_lists_do_not_crash_ctx(self):
        for key in ("roles", "vrfs", "vrfZonesMap", "ifLabels"):
            dep = copy.deepcopy(DEP)
            dep["sysConfig"][key] = None
            Ctx(FakeCap({"deployment": dep}), [])

    def test_ids_mode_comes_from_ids_state_and_is_never_guessed(self):
        profiles = {"Default_S5": ["9.NE"]}
        c, notes = ctx({"ids-profile-state": profiles})
        self.assertIsNone(m_idsIpsConfig(c))  # in no /ids/state list: writing mode Off would disable IDS/IPS on apply
        self.assertEqual(notes[-1]["disposition"], "unresolved")
        self.assertEqual(m_idsIpsConfig(ctx({"ids-profile-state": profiles, "ids-state": {"disabled": ["1.NE"], "ips_inline": ["9.NE"]}})[0]), {"mode": "IPS-Inline", "profile": "Default_S5"})
        self.assertEqual(m_idsIpsConfig(ctx({"ids-profile-state": profiles, "ids-state": {"disabled": ["9.NE"]}})[0])["mode"], "Off")

    def test_string_placeholder_vnis_and_bad_interface_speed(self):
        vx = m_vxlan(ctx({"deployment": DEP, "vxlan": {"port": 4789, "vtepIf": "lo1"}, "vxlan-vni-map": {"0": {"vni": "1001"}, "1": {"vni": "0"}, "2": {"vni": "-1"}}})[0])
        self.assertEqual([m["vni"] for m in vx["vniVrfMaps"]], [1001])
        info = [{"ifname": "wan1", "ifSpeed": "", "ifDuplex": "full", "admin": False}, {"ifname": "wan2", "ifSpeed": "1000", "ifDuplex": "full", "admin": False}]
        self.assertEqual([i["interfaceName"] for i in m_interfaceConfig(ctx({"interface-state": {"ifInfo": info}})[0])["interfaces"]], ["wan2"])

    def test_segment_ospf_bfd_loss_is_ledgered(self):
        iface = {"area": "0.0.0.0", "cost": 1, "priority": 1, "adminStatus": True, "helloInterval": 10, "deadInterval": 40, "transmitDelay": 1, "retransmitInterval": 5,
                 "authType": "None", "authKey": "", "md5Key": 0, "md5Password": "", "comment": "", "bfdDesired": True}
        sysd, area = {"enable": True, "routerId": "1.1.1.1"}, {"0.0.0.0": {"area_type": 0, "redist": False, "summary": False, "translator_role": 0}}
        c, notes = ctx({"deployment": DEP, "ospf-areas": {"0": area, "1": area}, "ospf-system-all-vrfs": {"0": sysd, "1": sysd}, "ospf-interfaces-all-vrfs": {"0": {"lan0": iface}, "1": {}}})
        m_segmentOspfConfig(c)
        self.assertTrue(any("BFD" in n["note"] for n in notes))

    def test_nac_source_ip_interface_without_label_becomes_any(self):
        dep = copy.deepcopy(DEP)
        dep["modeIfs"] = [{"ifName": "lan0", "applianceIPs": [{"vlan": 0, "label": "4"}, {"vlan": 71, "label": ""}]}]
        nac = {"nacEnable": True, "server": {"1": {"address": "192.0.2.5", "authport": 1812, "key": "", "name": "default", "source_ip_interface": "lan0.71"},
                                              "2": {"address": "192.0.2.6", "authport": 1812, "key": "", "name": "s2", "source_ip_interface": "lo1"}},
               "serverGroup": {"1": {"name": "g", "server": "default"}}, "aaaProfile": {"1": {"name": "p"}}, "dot1x": {"1": {"name": "d"}}, "macAuthProfile": {"1": {"name": "m"}},
               "applyPolicies": {"if": {"lan0": {"profile": "p", "auth_type": 1}, "lan0.71": {"profile": "p", "auth_type": 1}, "lan0.72": {"profile": "dot1X", "auth_type": 1}, "vti1": {"profile": "none"}}}}
        out = m_nac(ctx({"deployment": dep, "nac": nac, "loopbacks": {"lo99": {"label": ""}}})[0])
        self.assertEqual([s["source_ip_interface"] for s in out["server"]], ["any", "any"])
        # lan0.71 has no label and lan0.72 no longer exists: only lan0 (label Voice) stays, renumbered from 1
        self.assertEqual([(p["id"], p["auth_interface"]) for p in out["applyPolicies"]], [(1, "Voice")])
        dep["sysConfig"] = dict(DEP["sysConfig"], ifLabels={"lan": [{"id": "4", "name": "Voice"}, {"id": "5", "name": "Data"}], "wan": [{"id": "1", "name": "INETB"}]})
        dep["modeIfs"] = [{"ifName": "lan0", "applianceIPs": [{"vlan": 0, "label": "4"}, {"vlan": 10, "label": "4"}]}, {"ifName": "lan1", "applianceIPs": [{"vlan": 0, "label": "5"}]},
                          {"ifName": "wan1", "applianceIPs": [{"vlan": 100, "label": "1", "wanSide": True}]}]
        nac["applyPolicies"] = {"if": {"lan0": {"profile": "p", "auth_type": 3}, "lan0.10": {"profile": "none", "auth_type": 0}, "lan1": {"profile": "none", "auth_type": 0}, "wan1.100": {"profile": "none", "auth_type": 0}}}
        c, notes = ctx({"deployment": dep, "nac": nac})
        # the WAN entry is left out, and lan0.10 (same label as lan0, different policy) is ledgered
        self.assertEqual(m_nac(c)["applyPolicies"], [{"id": 1, "auth_interface": "Voice", "profile": "p", "auth_type": "mac"}, {"id": 2, "auth_interface": "Data", "auth_type": "trusted"}])
        self.assertTrue(any("lan0.10" in n["item"] and n["disposition"] == "unresolved" for n in notes))
        # the appliance keeps a trusted entry on the RADIUS server's source interface too; /validate rejects that label in both places
        dep["modeIfs"] = [{"ifName": "lan0", "applianceIPs": [{"vlan": 71, "label": "5"}, {"vlan": 72, "label": "4"}, {"vlan": 73, "label": "5"}]}]
        nac["server"] = {"1": dict(nac["server"]["1"], source_ip_interface="lan0.71")}
        nac["applyPolicies"] = {"if": {"lan0.71": {"profile": "none", "auth_type": 0}, "lan0.72": {"profile": "default", "auth_type": 0}, "lan0.73": {"profile": "none", "auth_type": 0}}}
        out = m_nac(ctx({"deployment": dep, "nac": nac})[0])
        self.assertEqual((out["server"][0]["source_ip_interface"], out["applyPolicies"]), ("Data", [{"id": 1, "auth_interface": "Voice", "auth_type": "trusted"}]))
        nac["applyPolicies"] = {"if": {"lan0.71": {"profile": "none", "auth_type": 0}}}
        self.assertIsNone(m_nac(ctx({"deployment": dep, "nac": nac})[0]))

    def test_translate_dhcp_relay_gateway_dual_stack_and_cluster_profile(self):
        relay = {"vlan": 0, "label": "4", "wanSide": False, "ip": "10.0.0.1", "mask": 24, "vrf": 0, "zone": 0,
                 "dhcpd": {"type": "relay", "relay": {"dhcpserver": ["10.9.9.9"], "option82": False, "option82_policy": "append"}}}  # ECOS < 9.5.1: no sub-option keys
        self.assertFalse(self._build([relay])["deploymentInfo"]["dhcpInfo"][0]["option82SubOptions150"])
        srv = {"prefix": "10.0.0.0/24", "ipStart": "10.0.0.10", "ipEnd": "10.0.0.200", "gw": [], "dns": [], "netbiosNodeType": "B", "maxLease": 86400, "defaultLease": 3600}
        server = dict(relay, dhcpd={"type": "server", "server": srv})
        self.assertIsNone(self._build([server])["deploymentInfo"]["dhcpInfo"][0]["gatewayIpAddress"])
        wan = {"vlan": 0, "label": "1", "wanSide": True, "vrf": 0, "zone": 0, "harden": 1, "behindNAT": "none", "maxBW": {"outbound": 50000}, "intf_mode": "static_slaac"}
        ifs = self._build([dict(wan, version=4, ip="198.51.100.10", mask=24, wanNexthop="198.51.100.1"), dict(wan, version=6, ip="", mask=0, wanNexthop="")], "wan0")["deploymentInfo"]["deploymentInterfaces"]
        self.assertEqual([(i["addressingMode"], i["ipAddressMask"]) for i in ifs], [("static", "198.51.100.10/24"), ("slaac", None)])
        out = self._build([], **{"cluster-profiles": [{"id": 3, "name": "HA-Pair"}], "cluster-profile-mapping": {"3": ["S1"]}})
        self.assertEqual(out["applianceInfo"]["clusterProfile"], "HA-Pair")

    def test_branch_nat_without_deployment_info_is_rejected_before_validate(self):
        from validate import apply_time_error, validate_text

        class Client:
            write_requests = 0
            calls = 0

            def request(self, *a, **k):
                Client.calls += 1
                return b""
        nat = "branchNatMapsConfig:\n  branchNatRules:\n  - priorityLevel: 90\n"
        verdict = validate_text(Client(), nat)
        self.assertEqual((verdict["accepted"], Client.calls), (False, 0))
        self.assertIn("deploymentInfo", verdict["error"])
        self.assertTrue(validate_text(Client(), "deploymentInfo:\n  deploymentMode: inline-router\n" + nat)["accepted"])
        self.assertIn("placeholder", validate_text(Client(), "ikeConfig:\n  presharedKey: '<SECRET:$.psk>'\n")["error"])
        ike = "tunnelConfiguration:\n  tunnels:\n  - tunnelName: t1\n    ikeConfig: {{ikeVersion: {}, localIkeIdentifier: {}, remoteIkeIdentifier: 192.0.2.2}}\n"
        self.assertIn("IKEv2", validate_text(Client(), ike.format(2, "null"))["error"])  # /validate answers HTTP 500
        self.assertIsNone(apply_time_error(ike.format(1, "null")))
        self.assertIsNone(apply_time_error(ike.format(2, "192.0.2.1")))
        psk = "tunnelConfiguration:\n  tunnels:\n  - tunnelName: t2\n    tunnelEncap: ipsec\n    ikeConfig: {{ikeVersion: 1, presharedKey: '{}'}}\n"
        self.assertIn("presharedKey", apply_time_error(psk.format("")))  # render and /validate accept it, apply fails
        self.assertIsNone(apply_time_error(psk.format("Test-Only-Key-1")))
        noencap = "tunnelConfiguration:\n  tunnels:\n  - tunnelName: t3\n    tunnelEncap: no_encap\n    remoteIp: {}\n"
        self.assertIn("remoteIp", apply_time_error(noencap.format("null")))
        self.assertIsNone(apply_time_error(noencap.format("198.51.100.9")))
        natrule = "deploymentInfo:\n  deploymentMode: inline-router\nbranchNatMapsConfig:\n  branchNatRules:\n  - priorityLevel: 10\n    interfaceName: {}\n"
        self.assertIn("VLAN subinterface", apply_time_error(natrule.format("lan1.55")))
        self.assertIsNone(apply_time_error(natrule.format("lan0")))
        self.assertIn("WAN interface", apply_time_error(natrule.format("wan1")))
        self.assertIsNone(apply_time_error(natrule.format("any")))
        self.assertEqual(Client.calls, 1)

    def test_local_routes_carry_subnet_sharing_flags_without_routes(self):
        from mappers import m_localRoutes
        vrf0 = {"self": 0, "add_local_asn": True, "add_local_communities": True, "add_local_lan": True, "add_local_metric": 55, "add_local_wan": False,
                "local_asn_filter": True, "local_communities": "100:101", "rtmap_to_subshare": "default_rtmap_to_subsh"}
        c, notes = ctx({"deployment": DEP, "system-info": {"auto_subnet": {"vrf": {"0": vrf0}}}, "subnets": {"subnets": {"entries": []}}})
        self.assertEqual(m_localRoutes(c), {"advertiseLocalLanSubnets": True, "advertiseLocalWanSubnets": False, "localMetric": 55, "localCommunities": "100:101",
                                            "filterRoutesWithLocalASN": True, "redistToSDwanFabricRouteMap": "default_rtmap_to_subsh", "routes": []})
        self.assertIn("add_local_asn", " ".join(n["note"] for n in notes if n["disposition"] == "unresolved"))  # no preconfig field exists for it
        self.assertNotIn("add_local_asn", " ".join(n["note"] for n in notes if n["disposition"] == "unmapped-source-key"))
        c, _ = ctx({"deployment": DEP, "system-info": {"auto_subnet": {"vrf": {"0": dict(vrf0, add_local_communities=False)}}}, "subnets": {"subnets": {"entries": []}}})
        self.assertEqual(m_localRoutes(c)["localCommunities"], "")  # communities are kept but not added
        c, _ = ctx({"deployment": DEP, "subnets": {"subnets": {"entries": []}}})
        self.assertIsNone(m_localRoutes(c))
        route = {"configured": True, "local": True, "automatic": False, "prefix": "198.51.100.0/24", "nextHop": "", "ifName": "lan0.72", "metric": 50, "advert": True,
                 "advert_bgp": False, "advert_ospf": False, "zone_id": "65534", "label": "0"}
        c, notes = ctx({"deployment": DEP, "subnets": {"subnets": {"entries": [{"state": route}, {"state": dict(route, prefix="203.0.113.0/24", nextHop="192.0.2.1")}]}}})
        self.assertEqual([r["routeIpSubnet"] for r in m_localRoutes(c)["routes"]], ["203.0.113.0/24"])  # interface-only route fails at apply
        self.assertTrue(any(n["disposition"] == "unresolved" and "198.51.100.0/24" in n["note"] for n in notes))

    def test_routes_take_advert_flags_and_comment_from_the_configured_table(self):
        from mappers import m_localRoutes
        route = {"configured": True, "local": True, "automatic": False, "prefix": "203.0.113.0/24", "nextHop": "192.0.2.250", "ifName": "lan0.201", "metric": 50, "advert": True,
                 "advert_bgp": False, "advert_ospf": False, "zone_id": "65534", "label": "0"}
        configured = {"prefix": {"203.0.113.0/24": {"advert": True, "advert_bgp": True, "advert_ospf": False, "nhop": {"192.0.2.250": {"interface": {"lan0.201": {"comment": "printers", "dir": "FROM_LAN"}}}}}}}
        c, _ = ctx({"deployment": DEP, "subnets": {"subnets": {"entries": [{"state": route}]}}, "static-routes-configured": configured})
        r = m_localRoutes(c)["routes"][0]
        self.assertEqual((r["advertiseToBgp"], r["comment"], r["tag"]), (True, "printers", "FROM_LAN"))  # /subnets reports advert_bgp false and has no comment
        c, _ = ctx({"deployment": DEP, "subnets": {"subnets": {"entries": [{"state": route}]}}})
        self.assertNotIn("comment", m_localRoutes(c)["routes"][0])

    def test_next_hop_zero_route_is_written_from_its_config(self):
        from mappers import m_localRoutes
        st = {"configured": True, "local": True, "automatic": False, "prefix": "0.0.0.0/0", "nextHop": "", "ifName": "", "metric": 50, "advert": True,
              "advert_bgp": False, "advert_ospf": False, "zone_id": "65534", "label": "0"}
        conf = {"prefix": "0.0.0.0/0", "nextHop": "0.0.0.0", "ifName": "default", "comment": "", "tag": "ANY", "gms_marked": False}
        configured = {"prefix": {"0.0.0.0/0": {"advert": True, "nhop": {"0.0.0.0": {"interface": {"default": {"dir": "ANY", "comment": "hub default"}}}}}}}
        c, _ = ctx({"deployment": DEP, "subnets": {"subnets": {"entries": [{"state": st, "config": conf}]}}, "static-routes-configured": configured})
        r = m_localRoutes(c)["routes"][0]
        self.assertEqual((r["nextHop"], r["interfaceName"], r["advertise"], r["comment"], r["tag"]), ("0.0.0.0", None, True, "hub default", "ANY"))
        for entry in ({"state": st, "config": dict(conf, gms_marked=True)}, {"state": st}):  # Orchestrator-made, or no config to read
            c, _ = ctx({"deployment": DEP, "subnets": {"subnets": {"entries": [entry]}}})
            self.assertIsNone(m_localRoutes(c))

    def test_default_interface_route_is_not_pinned_to_the_interface_in_use(self):
        from mappers import m_localRoutes
        st = {"configured": True, "local": True, "automatic": False, "prefix": "198.51.100.0/24", "nextHop": "192.0.2.1", "ifName": "wan1", "metric": 50, "advert": False,
              "advert_bgp": False, "advert_ospf": False, "zone_id": "65534", "label": "0"}
        c, _ = ctx({"deployment": DEP, "subnets": {"subnets": {"entries": [{"state": st, "config": {"nextHop": "192.0.2.1", "ifName": "default"}}]}}})
        self.assertIsNone(m_localRoutes(c)["routes"][0]["interfaceName"])
        c, _ = ctx({"deployment": DEP, "subnets": {"subnets": {"entries": [{"state": st, "config": {"nextHop": "192.0.2.1", "ifName": "lan0"}}]}}})
        self.assertEqual(m_localRoutes(c)["routes"][0]["interfaceName"], "lan0")
        c, _ = ctx({"deployment": DEP, "subnets": {"subnets": {"entries": [{"state": st, "config": {"nextHop": "192.0.2.1", "ifName": "lan0", "gms_marked": True}}]}}})
        self.assertIsNone(m_localRoutes(c))  # an Orchestrator-made route with a real next hop (third-party cloud integration) is not a static route
        dep = dict(DEP, sysConfig=dict(DEP["sysConfig"], vrfZonesMap={"0": {"1": {"id": 1, "name": "WAN"}}}))
        entry = {"state": dict(st, zone_id="1"), "config": {"nextHop": "192.0.2.1", "ifName": "default", "zone_id": 65534}}
        c, _ = ctx({"deployment": dep, "subnets": {"subnets": {"entries": [entry]}}})
        self.assertIsNone(m_localRoutes(c)["routes"][0]["zone"])  # the zone of wan1 is not the route's zone
        c, _ = ctx({"deployment": dep, "subnets": {"subnets": {"entries": [{"state": entry["state"]}]}}})
        self.assertEqual(m_localRoutes(c)["routes"][0]["zone"], "WAN")  # no config: the state's zone

    def test_disabled_segment_instances_are_ledgered_not_silent(self):
        dep = {"sysConfig": dict(DEP["sysConfig"], vrfs=[{"id": "0", "name": "Default"}, {"id": "1", "name": "corp"}, {"id": "2", "name": "guest"}]), "modeIfs": DEP["modeIfs"]}
        system = {"enable": True, "asn": 65001, "rtr_id": "192.0.2.1", "route_target": {}}
        c, notes = ctx({"deployment": dep, "bgp-system-all-vrfs": {"0": system, "1": dict(system, asn=65101), "2": dict(system, enable=False)},
                        "bgp-neighbors-all-vrfs": {}})
        self.assertEqual([v["segment"] for v in m_segmentBgpSystems(c)], ["Default", "corp"])
        self.assertTrue(any(n["item"] == "bgp-system-all-vrfs[2]" and "guest" in n["note"] for n in notes))

    def test_disabled_ospf_segment_is_ledgered_when_no_segment_runs_ospf(self):
        from mappers import m_segmentOspfConfig
        dep = {"sysConfig": dict(DEP["sysConfig"], vrfs=[{"id": "0", "name": "Default"}, {"id": "1", "name": "corp"}]), "modeIfs": DEP["modeIfs"]}
        c, notes = ctx({"deployment": dep, "ospf-system-all-vrfs": {"0": {"enable": True}, "1": {"enable": False, "routerId": "192.0.2.1"}}})
        self.assertIsNone(m_segmentOspfConfig(c))
        self.assertTrue(any(n["item"] == "ospf-system-all-vrfs[1]" and "corp" in n["note"] for n in notes))

    def test_segment_routes_take_comment_from_route_config_not_the_default_table(self):
        from mappers import m_segmentLocalRoutes
        dep = {"sysConfig": dict(DEP["sysConfig"], vrfs=[{"id": "0", "name": "Default"}, {"id": "1", "name": "corp"}]), "modeIfs": DEP["modeIfs"]}
        st = {"configured": True, "local": True, "automatic": False, "prefix": "203.0.113.0/24", "nextHop": "192.0.2.250", "ifName": "lan0.203", "metric": 50, "advert": True,
              "advert_bgp": False, "advert_ospf": False, "zone_id": "65534", "label": "0"}
        configured = {"prefix": {"203.0.113.0/24": {"advert_bgp": True, "nhop": {"192.0.2.250": {"interface": {"lan0.203": {"comment": "default route"}}}}}}}  # same prefix, Default segment
        c, notes = ctx({"deployment": dep, "static-routes-configured": configured,
                        "subnets-all-vrfs": {"subnets": {"1": {"entries": [{"state": st, "config": {"comment": "corp app", "tag": "FROM_WAN"}}]}}}})
        r = m_segmentLocalRoutes(c)[0]["routes"][0]
        self.assertEqual((r["advertiseToBgp"], r["comment"], r["tag"]), (False, "corp app", "FROM_WAN"))
        self.assertEqual(len([n for n in notes if n["item"] == "subnets-all-vrfs[1]"]), 1)  # route_list runs twice here

    def test_segment_without_routes_keeps_its_subnet_sharing(self):
        from mappers import m_localRoutes, m_segmentLocalRoutes
        dep = {"sysConfig": dict(DEP["sysConfig"], vrfs=[{"id": "0", "name": "Default"}, {"id": "1", "name": "corp"}]), "modeIfs": DEP["modeIfs"]}
        sharing = {"add_local_lan": True, "add_local_wan": False, "add_local_metric": 50, "local_asn_filter": True, "rtmap_to_subshare": "to_fabric"}
        st = {"configured": True, "local": True, "automatic": False, "prefix": "203.0.113.0/24", "nextHop": "192.0.2.250", "ifName": "lan0", "metric": 50, "advert": True,
              "advert_bgp": False, "advert_ospf": False, "zone_id": "65534", "label": "0"}
        c, _ = ctx({"deployment": dep, "system-info": {"auto_subnet": {"vrf": {"0": sharing, "1": sharing}}},
                    "subnets": {"subnets": {"entries": [{"state": st}]}}, "subnets-all-vrfs": {"subnets": {"0": {"entries": [{"state": st}]}}}})
        self.assertIsNone(m_localRoutes(c))  # corp has settings of its own, so every segment goes in segmentLocalRoutes
        out = {e["segment"]: e for e in m_segmentLocalRoutes(c)}
        self.assertEqual(len(out["Default"]["routes"]), 1)
        self.assertEqual((out["corp"]["routes"], out["corp"]["advertiseLocalLanSubnets"], out["corp"]["filterRoutesWithLocalASN"], out["corp"]["redistToSDwanFabricRouteMap"]),
                         ([], True, True, "to_fabric"))

    def test_default_valued_segment_is_left_out_and_no_route_map_is_noted(self):
        from mappers import m_segmentLocalRoutes
        dep = {"sysConfig": dict(DEP["sysConfig"], vrfs=[{"id": "0", "name": "Default"}, {"id": "1", "name": "corp"}, {"id": "2", "name": "iot"}]), "modeIfs": DEP["modeIfs"]}
        plain = {"add_local_lan": False, "add_local_wan": False, "add_local_metric": 50, "local_asn_filter": False, "rtmap_to_subshare": ""}
        c, notes = ctx({"deployment": dep, "system-info": {"auto_subnet": {"vrf": {"0": plain, "1": dict(plain, add_local_lan=True), "2": plain}}}, "subnets-all-vrfs": {"subnets": {}}})
        out = {e["segment"]: e for e in m_segmentLocalRoutes(c)}
        self.assertEqual(sorted(out), ["Default", "corp"])  # writing iot would make Orchestrator add default_rtmap_to_subsh
        self.assertNotIn("redistToSDwanFabricRouteMap", out["corp"])  # omitted, null and "" all become default_rtmap_to_subsh; never "none"
        self.assertTrue(any("default_rtmap_to_subsh" in n["note"] and n["target"].startswith("segmentLocalRoutes") for n in notes))
        trimmed = {"segmentLocalRoutes": [dict(e) for e in out.values()]}
        drop_template_fields(trimmed, {"routes": {"1": {"advertiseLocalLanSubnets"}}}, {"0": "Default", "1": "corp"}, lambda *a: None)
        self.assertEqual([e["segment"] for e in trimmed["segmentLocalRoutes"]], ["Default"])  # corp has only default values left once the template's field is out

    def test_http_ip_sla_keeps_proxy_and_user_agent(self):
        from mappers import m_ipSlaRule
        op = {"type": "http-monitor", "http-monitor-url": "www.example.com", "port-name": "wan1", "http-timeout": "30", "proxyAddress": "192.0.2.9", "proxyPort": "3128", "user-agent": "Mozilla/5.0"}
        c, _ = ctx({"deployment": DEP, "ipsla-managers": {"MGR_1": {"enable": True, "interval": 10, "comment": "", "routeLabelId": 0, "operation": {"OPR_1": op}}}})
        m = m_ipSlaRule(c)["ruleEntries"][0]["ipslaMonitor"]
        self.assertEqual((m["proxyAddress"], m["proxyPort"], m["userAgent"]), ("192.0.2.9", 3128, "Mozilla/5.0"))
        rule = {"enable": True, "interval": "10", "comment": "", "routeLabelId": 0, "upAction": "def-subnet", "operation": {"OPR_2": {"type": "port-monitor", "port-name": "wan1"}},
                "downAction": {"DN_ACTION_subnet-adv_3": {"type": "subnet-adv", "port-name": "lan0.201"}}}
        c2, notes = ctx({"deployment": DEP, "ipsla-managers": {"MGR_2": rule}})
        r = m_ipSlaRule(c2)["ruleEntries"][0]
        self.assertEqual((r["downAction"], r["upAction"]), ({"downActionType": "advertiseSubnet", "interfaceName": "lan0.201"}, {"upActionType": "defaultSubnetAction"}))
        rule["downAction"] = {"DN_ACTION_vrrp-prio_dec_7": {"dec": "20", "type": "vrrp-prio", "port-name": "lan0.202"}}
        rule["upAction"] = {"UP_ACTION_vrrp-prio_inc_8": {"type": "vrrp-prio", "port-name": "lan0.202", "inc": "20"}}
        r = m_ipSlaRule(ctx({"deployment": DEP, "ipsla-managers": {"MGR_2": rule}})[0])["ruleEntries"][0]
        self.assertEqual((r["downAction"], r["upAction"]), ({"downActionType": "decreaseVRRPPriority", "interfaceName": "lan0.202", "priority": 20},
                                                            {"upActionType": "increaseVRRPPriority", "interfaceName": "lan0.202", "priority": 20}))
        for dn, want in (({"type": "subnet-share"}, {"downActionType": "disableSubnetSharing"}),
                         ({"type": "subnet-rmv", "port-name": "lan0.204"}, {"downActionType": "removeAutoSubnet", "interfaceName": "lan0.204"})):
            rule["downAction"] = {"DN_ACTION_x_1": dn}
            self.assertEqual(m_ipSlaRule(ctx({"deployment": DEP, "ipsla-managers": {"MGR_2": rule}})[0])["ruleEntries"][0]["downAction"], want)
        rule["downAction"] = {"DN_ACTION_x_1": {"type": "made-up"}}
        c2, notes = ctx({"deployment": DEP, "ipsla-managers": {"MGR_2": rule}})
        m_ipSlaRule(c2)
        self.assertTrue(any("made-up" in n["note"] for n in notes))  # an unmapped action is ledgered, not silently an alarm
        ping = {"type": "ip-monitor", "ip-monitor-ip": "192.0.2.1", "src-port-name": "wan1", "force-dns": "true", "metric-combination": "metric-and"}
        dep = dict(DEP, modeIfs=[{"ifName": "wan1", "applianceIPs": [{"vlan": 0, "ip": "198.51.100.11", "label": ""}]}])
        tunnels = {"t1": {"alias": "pt_gre", "sourceIpAddress": "198.51.100.11"}, "t2": {"alias": "other", "sourceIpAddress": "198.51.100.99"}}
        data = {"deployment": dep, "passthrough-tunnels": tunnels, "ipsla-managers": {"MGR_3": dict(rule, operation={"OPR_1": ping})}}
        m = m_ipSlaRule(ctx(data)[0])["ruleEntries"][0]["ipslaMonitor"]
        self.assertEqual((m["sourceInterfaceName"], m["tunnelName"], m["forceDnsThroughProbePath"], m["metricCombination"]), ("wan1", "pt_gre", True, "metric-and"))
        tunnels["t2"]["sourceIpAddress"] = "198.51.100.11"  # two candidate tunnels: the rule is not guessed
        self.assertIsNone(m_ipSlaRule(ctx(data)[0]))
        del op["proxyAddress"], op["user-agent"]
        self.assertFalse({"proxyAddress", "proxyPort", "userAgent"} & set(m_ipSlaRule(c)["ruleEntries"][0]["ipslaMonitor"]))

    def test_peer_priority_list_template_owns_the_peer_priority_section(self):
        from translate import selected_templates
        cap = FakeCap({"template-association": {"templateIds": ["g"]}, "template-groups": [{"name": "g", "selectedTemplateNames": ["peerPriorityList", "dns"]}]})
        self.assertEqual(selected_templates(cap), {"peerPriority", "dns"})

    def test_overlay_hub_membership_is_ledgered(self):
        cap = FakeCap({"appliance": {"hostName": "h1", "groupId": "1.Network", "site": "S1", "networkRole": "1"}, "deployment": dict(DEP, modeIfs=[]),
                       "overlay-config": [{"id": 1, "name": "Mesh", "topology": {"hubs": []}}, {"id": 4, "name": "Default", "topology": {"hubs": ["9.NE", "2.NE"]}}],
                       "overlay-association": {"1": ["9.NE"], "4": ["9.NE"]}})
        cap.folder = "fake"
        ledger: list = []
        self.assertEqual(build(cap, ledger)["businessIntentOverlays"], {"overlays": ["Mesh", "Default"]})
        self.assertEqual([e["note"] for e in ledger if e["item"] == "overlay-config.topology.hubs"][0][:26], "hub in overlays Default: h")

    def test_ra_info_keeps_prefixes_and_skips_ipv4(self):
        ra = {"self": "lan0.90", "hoplimit": 64, "linkmtu": 1500, "managedflag": False, "maxinterval": 600, "mininterval": 200, "otherconfigflag": True, "reachabletime": 30000,
              "retranstimer": 1000, "routerdeflifetime": 1800, "routerpreference": "high", "sendadv": True,
              "prefix_id": {"3": {"self": 3, "autonomous": False, "onlink": True, "prefix": "2001:db8:3::/64", "prefixvalidlifetime": 0, "prfrdlifetime": 0},
                            "1": {"self": 1, "autonomous": True, "onlink": True, "prefix": "2001:db8:1::/64", "prefixvalidlifetime": 86400, "prfrdlifetime": 14400}}}
        lan = {"label": "", "wanSide": False, "mask": 64, "vrf": 0, "zone": 0, "version": 6, "ra": ra}
        di = self._build([dict(lan, vlan=90, ip="2001:db8:1::1"), dict(lan, vlan=91, ip="10.0.0.1", mask=24, version=4)])["deploymentInfo"]
        self.assertEqual([r["raInterfaceName"] for r in di["raInfo"]], ["lan0.90"])
        self.assertEqual([(p["id"], p["prefix"], p["prefixvalidlifetime"]) for p in di["raInfo"][0]["prefix_id"]], [(1, "2001:db8:1::/64", 86400), (3, "2001:db8:3::/64", 0)])

    def test_dhcp_failover_maps_to_dhcp_ha_once_per_physical_interface(self):
        srv = {"prefix": "10.0.0.0/24", "ipStart": "10.0.0.10", "ipEnd": "10.0.0.200", "gw": [], "dns": [], "netbiosNodeType": "B", "maxLease": 86400, "defaultLease": 3600, "failover": True}
        lan = {"label": "", "wanSide": False, "ip": "10.0.0.1", "mask": 24, "vrf": 0, "zone": 0, "dhcpd": {"type": "server", "server": srv}}
        dep = copy.deepcopy(DEP)
        dep["modeIfs"] = [{"ifName": "lan0", "applianceIPs": [dict(lan, vlan=71), dict(lan, vlan=72)]},
                          {"ifName": "lan1", "applianceIPs": [dict(lan, dhcpd={"type": "server", "server": dict(srv, failover=False)})]}]
        fo = {"load_bal_max": 5, "max_resp_delay": 10, "max_unack_updates": 10, "mclt": 3600, "my_ip": "10.0.0.1", "my_port": 647, "peer_ip": "10.0.0.2", "peer_port": 647, "role": "primary", "split": 128}
        dep["dhcpFailover"] = {"lan0": dict(fo, dhcp_fo_rel_name="fo-lan0"), "lan1": fo}  # lan1: stale settings, its server has failover off
        cap = FakeCap({"appliance": {"hostName": "h1", "groupId": "1.Network", "site": "S1", "networkRole": "0"}, "deployment": dep})
        cap.folder = "fake"
        ledger: list = []
        dhcp = build(cap, ledger)["deploymentInfo"]["dhcpInfo"]
        self.assertEqual([("dhcpHA" in d) for d in dhcp], [True, False, False])
        self.assertEqual(dhcp[0]["dhcpHA"], {"dhcpFailoverRole": "primary", "ipAddress": "10.0.0.1", "ipAddressPort": 647, "peerIpAddress": "10.0.0.2", "peerIpAddressPort": 647,
                                             "mclt": 3600, "split": 128, "maxResponseDelay": 10, "maxUnackedUpdates": 10, "loadBalanceMaxSeconds": 5, "foRelationshipName": "fo-lan0"})
        self.assertEqual(sorted(e["disposition"] for e in ledger if e["item"] == "deployment.dhcpFailover"), ["mapped", "runtime"])

    def test_corpus_skips_partial_only_captures(self):
        import json, tempfile
        from corpus import latest
        root = Path(tempfile.mkdtemp())
        for stamp, only in (("20261017T0800Z", None), ("20261017T0900Z", ["ids-state"]), ("20261016T0700Z", None)):
            (root / stamp).mkdir()
            (root / stamp / "run.json").write_text(json.dumps({"only": only}))
        self.assertEqual(latest(root).name, "20261017T0800Z")
        (root / "20261017T1000Z").mkdir()  # newer but taken while the appliance was unreachable
        (root / "20261017T1000Z" / "run.json").write_text(json.dumps({"only": None, "reachable": False}))
        self.assertEqual(latest(root).name, "20261017T0800Z")

    def test_branch_nat_zero_address_and_pool_reference(self):
        maps = {"map1": {"prio": {"10": {"dir": "outbound", "enable": True, "match": {"intf": "lan0", "segment": "0", "proto": "any", "src_subnet": "0.0.0.0/0", "dst_subnet": "198.51.100.0/24"},
                                         "set": {"trans_src": "41"}}}}}
        pools = {"7": {"dir": "outbound", "name": "p7", "pat": 1, "segment": "0", "subnet": "203.0.113.7/32", "comment": ""},
                 "41": {"dir": "outbound", "name": "p41", "pat": 1, "segment": "0", "subnet": "203.0.113.41/32", "comment": ""}}
        rule = m_branchNatMapsConfig(ctx({"deployment": DEP, "branch-nat-maps": maps, "nat-pools": pools})[0])["branchNatRules"][0]
        self.assertEqual((rule["sourceIpSubnet"], rule["translateNatPoolID"]), ("any", 1))  # pool 41 is the second natPools entry
        self.assertNotIn("translateSourceIpSubnet", rule)
        # names only ({"map1": "map1"}): no rules, nothing written, the ledger names the map
        c, notes = ctx({"deployment": DEP, "branch-nat-maps": {"map1": "map1"}})
        self.assertIsNone(m_branchNatMapsConfig(c))
        nat_notes = [n for n in notes if n["item"] == "branch-nat-maps"]
        self.assertEqual([n["disposition"] for n in nat_notes], ["default-omitted"])
        self.assertIn("map1", nat_notes[0]["note"])
        c, notes = ctx({"deployment": DEP, "branch-nat-maps": {}})
        self.assertIsNone(m_branchNatMapsConfig(c))
        self.assertEqual([n["disposition"] for n in notes if n["item"] == "branch-nat-maps"], ["default-omitted"])

    def test_branch_nat_reads_live_rules_when_cached_lists_names_only(self):
        # seen on 9.6.2.1 and 9.7.1 right after an apply: Orchestrator's cached /nat/maps lags the appliance
        import json
        import tempfile
        from translate import Capture
        rule = {"dir": "outbound", "enable": True, "match": {"intf": "any", "segment": "0", "proto": "any", "src_subnet": "192.0.2.0/24"}, "set": {"trans_src": "198.51.100.0/24"}}
        copies = {"cached": {"map1": "map1"}, "live": {"map1": {"prio": {"10": rule}}}}
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "capture").mkdir()
            (folder / "run.json").write_text(json.dumps({"appliance": {"nePk": "2.NE"}}))
            index = [{"id": "deployment", "label": "live", "status": "ok", "file": "capture/deployment.json"}]
            (folder / "capture/deployment.json").write_text(json.dumps({"data": DEP}))
            for label, data in copies.items():  # cached first, as capture.py writes the index
                index.append({"id": "branch-nat-maps", "label": label, "status": "ok", "file": "capture/branch-nat-maps.{}.json".format(label)})
                (folder / "capture/branch-nat-maps.{}.json".format(label)).write_text(json.dumps({"data": data}))
            (folder / "capture-index.json").write_text(json.dumps(index))
            notes = []
            nat = m_branchNatMapsConfig(Ctx(Capture(folder), notes))
        self.assertEqual([(r["priorityLevel"], r["translateSourceIpSubnet"]) for r in nat["branchNatRules"]], [(10, "198.51.100.0/24")])
        self.assertEqual([n["disposition"] for n in notes if n["item"] == "branch-nat-maps"], ["mapped"])

    def test_ids_profile_is_substituted_by_signature_family(self):
        inv = {"segments": ["Default"], "zones": ["Default"], "lan": ["L"], "wan": ["W"], "groups": ["g"], "templateGroups": ["t"], "overlays": ["o"],
               "idsProfiles": ["Default", "Default_S5", "Default_S7"]}
        notes = []
        self.assertEqual(substitute({"idsIpsConfig": {"mode": "IPS-Performant", "profile": "Custom_S7"}}, inv, notes)["idsIpsConfig"]["profile"], "Default_S7")
        self.assertEqual(substitute({"idsIpsConfig": {"mode": "Off", "profile": "Default_S5"}}, inv)["idsIpsConfig"]["profile"], "Default_S5")
        self.assertTrue(notes)

    def test_suite_loads_the_shipped_constraints(self):
        self.assertTrue(load_constraints())


if __name__ == "__main__":
    unittest.main()

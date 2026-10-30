#!/usr/bin/env python3
"""Writes the synthetic captures under tests/fixtures/compare/captures/: a base site and one partner per scenario.

Every name and address is made up (example names, 192.0.2.0/24 and 198.51.100.0/24). Run it after changing a capture below,
then `EDGECONNECT_UPDATE_GOLDEN=1 python -m unittest tests.test_compare_golden` and review the golden diff.
"""
import copy
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRV = {"prefix": "192.0.2.0/25", "ipStart": "192.0.2.10", "ipEnd": "192.0.2.100", "gw": ["192.0.2.1"], "dns": ["198.51.100.53"], "netbiosNodeType": "B",
       "maxLease": 86400, "defaultLease": 3600}
WAN = {"vlan": 0, "label": "1", "wanSide": True, "vrf": 0, "zone": 0, "harden": 1, "behindNAT": "none", "maxBW": {"outbound": 50000, "inbound": 50000},
       "version": 4, "ip": "198.51.100.10", "mask": 25, "wanNexthop": "198.51.100.1"}
LAN = {"vlan": 0, "label": "4", "wanSide": False, "vrf": 0, "zone": 0, "ip": "192.0.2.1", "mask": 25, "dhcpd": {"type": "server", "server": SRV}}
ROUTE = {"configured": True, "local": True, "automatic": False, "prefix": "198.51.100.128/25", "nextHop": "192.0.2.126", "ifName": "lan0", "metric": 50,
         "advert": True, "advert_bgp": False, "advert_ospf": False, "zone_id": "65534", "label": "0"}
BASE = {
    "appliance": {"hostName": "branch-a", "groupId": "1.Network", "site": "Example-A", "networkRole": "0"},
    "groups": [{"id": "1.Network", "name": "Branches"}],
    "extra-info": {"location": {"address": "1 Example Road"}, "contact": {"name": "ops"}},
    "deployment": {"sysConfig": {"mode": "router", "maxBW": 100000,
                                 "ifLabels": {"lan": [{"id": "4", "name": "Data"}], "wan": [{"id": "1", "name": "INET"}]},
                                 "vrfs": [{"id": "0", "name": "Default"}],
                                 "vrfZonesMap": {"0": {"a": {"id": 0, "name": "Trusted"}, "b": {"id": 1, "name": "Guest"}}},
                                 "roles": [{"id": "0", "name": "default"}]},
                   "modeIfs": [{"ifName": "wan0", "applianceIPs": [WAN]}, {"ifName": "lan0", "applianceIPs": [LAN]}]},
    "subnets": {"subnets": {"entries": [{"state": ROUTE}, {"state": dict(ROUTE, prefix="192.0.2.192/26")}]}},  # both via 192.0.2.126
}


def lan(doc):
    return doc["deployment"]["modeIfs"][1]["applianceIPs"][0]


def partner(host, site, wan_ip, change=None):
    doc = copy.deepcopy(BASE)
    doc["appliance"].update(hostName=host, site=site)
    doc["deployment"]["modeIfs"][0]["applianceIPs"][0]["ip"] = wan_ip
    if change:
        change(doc)
    return doc


def scalars(doc):
    doc["deployment"]["modeIfs"][0]["applianceIPs"][0]["maxBW"] = {"outbound": 20000, "inbound": 20000}
    doc["subnets"]["subnets"]["entries"][0]["state"]["metric"] = 60


def zone(doc):
    lan(doc)["zone"] = 1  # Guest where the base has Trusted


def extra_route(doc):
    doc["subnets"]["subnets"]["entries"].append({"state": dict(ROUTE, prefix="198.51.100.192/26", nextHop="192.0.2.125")})


def no_route(doc):
    doc["subnets"]["subnets"]["entries"] = []


def links(doc):
    lan(doc)["ip"] = "192.0.2.2"  # the DHCP gateway stays 192.0.2.1 (a VRRP-style address)
    routes = doc["subnets"]["subnets"]["entries"]
    routes[0]["state"]["nextHop"], routes[1]["state"]["nextHop"] = "192.0.2.124", "192.0.2.125"


def relay(doc):
    lan(doc)["dhcpd"] = {"type": "relay", "relay": {"dhcpserver": ["198.51.100.67"], "option82": False, "option82_policy": "append"}}


CAPTURES = {
    "base": BASE,
    "identical": partner("branch-b", "Example-B", "198.51.100.20"),
    "scalars": partner("branch-b", "Example-B", "198.51.100.20", scalars),
    "zone": partner("branch-b", "Example-B", "198.51.100.20", zone),
    "partner-only": partner("branch-b", "Example-B", "198.51.100.20", extra_route),
    "base-only": partner("branch-b", "Example-B", "198.51.100.20", no_route),
    "links": partner("branch-b", "Example-B", "198.51.100.20", links),
    "relay": partner("branch-b", "Example-B", "198.51.100.20", relay),
    "broken": partner("branch-b", "Example-B", "198.51.100.20"),  # its capture-index.json is left out below
}


def write(name, data):
    folder = HERE / "captures" / name
    shutil.rmtree(folder, ignore_errors=True)
    (folder / "capture").mkdir(parents=True)
    nepk = "{}.NE".format(list(CAPTURES).index(name) + 1)
    (folder / "run.json").write_text(json.dumps({"orchestrator": "example", "release": "9.7.1.42046",
                                                 "appliance": {"nePk": nepk, "hostName": data["appliance"]["hostName"]}}, indent=1) + "\n")
    index = []
    for ident, value in data.items():
        (folder / "capture" / "{}.live.json".format(ident)).write_text(json.dumps({"data": value}, indent=1) + "\n")
        index.append({"id": ident, "label": "live", "status": "ok", "file": "capture/{}.live.json".format(ident)})
    if name != "broken":
        (folder / "capture-index.json").write_text(json.dumps(index, indent=1) + "\n")


if __name__ == "__main__":
    for name, data in CAPTURES.items():
        write(name, data)

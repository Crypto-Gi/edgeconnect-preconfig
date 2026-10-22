import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "building-edgeconnect-preconfig-templates" / "scripts"))
from common import changed_paths, parse_yaml  # noqa: E402
from generate import generate  # noqa: E402
from render import flow_through, read_rows, render_row  # noqa: E402
from variables import classify, read_sheet, write_sheet  # noqa: E402

REFERENCE = {
    "applianceInfo": {"hostname": "br-1", "group": "Branches", "site": "Plano", "region": None, "networkRole": "non-hub", "location": {"city": "Plano", "zipCode": "75024"}},
    "templateGroups": {"groups": ["G1", "G2"]},
    "bgpSystemConfig": {"routerId": "192.0.2.1"},  # a host-only leaf that comes before the interface address it equals
    "deploymentInfo": {
        "totalOutboundBandwidth": 200000,
        "deploymentInterfaces": [
            {"interfaceName": "lan0", "ipAddressMask": "192.0.2.1/24", "dhcpServer": {"startIpAddress": "192.0.2.10", "gatewayIpAddress": "192.0.2.1", "dnsServers": ["198.51.100.53"]}},
            {"interfaceName": "wan0", "ipAddressMask": None, "outboundMaxBandwidth": 5000},
            {"interfaceName": "wan1", "ipAddressMask": "203.0.113.2/30", "nextHop": "203.0.113.1"},
        ],
    },
    "localRoutes": {"staticRoutes": [{"routeIpSubnet": "0.0.0.0/0", "nextHop": "203.0.113.1", "interfaceName": "wan1"}]},
    "radius": {"server": [{"host": "198.51.100.7", "key": "secret"}]},
}


def sheet_rows():
    return {r["path"]: r for r in classify(REFERENCE, {"radius.server[0].key"})}


class Compare(unittest.TestCase):
    def test_other_sites_decide(self):
        import copy
        other = copy.deepcopy(REFERENCE)
        other["deploymentInfo"]["deploymentInterfaces"][1]["outboundMaxBandwidth"] = 9000
        other["localRoutes"]["staticRoutes"][0]["routeIpSubnet"] = "198.51.100.0/24"  # differs: a variable
        other["radius"]["server"].append({"host": "198.51.100.8", "key": "x"})  # only at the other site: not in this sheet
        third = copy.deepcopy(REFERENCE)
        del third["radius"]["server"][0]["host"]  # this site lacks a field: a different site type
        rows = {r["path"]: r for r in classify(REFERENCE, {"radius.server[0].key"}, others=[other, third])}
        route = rows["localRoutes.staticRoutes[0].routeIpSubnet"]
        self.assertEqual((route["decision"], route["reason"]), ("variable", "differs between the sites you gave"))
        self.assertEqual(rows["localRoutes.staticRoutes[0].interfaceName"]["reason"], "same at every site you gave")
        self.assertEqual(rows["deploymentInfo.totalOutboundBandwidth"]["decision"], "variable")  # equal everywhere, but the Deployment page is always per site
        self.assertEqual(rows["deploymentInfo.deploymentInterfaces[0].interfaceName"]["decision"], "fixed")  # layout stays fixed
        self.assertEqual(rows["deploymentInfo.deploymentInterfaces[0].ipAddressMask"]["decision"], "variable")  # own address stays a column
        other["templateGroups"]["groups"] = ["G1", "G3"]
        other["applianceInfo"]["group"] = "Other"
        again = {r["path"]: r for r in classify(REFERENCE, {"radius.server[0].key"}, others=[other, third])}
        self.assertEqual(again["templateGroups.groups"]["decision"], "variable")  # the sites differ, so the default "fixed" gives way
        self.assertEqual(again["templateGroups.groups"]["column"], "template_groups")
        self.assertEqual(again["applianceInfo.group"]["decision"], "variable")
        self.assertEqual(rows["templateGroups.groups"]["decision"], "fixed")
        host = rows["radius.server[0].host"]
        self.assertEqual(host["decision"], "fixed")
        self.assertTrue(host["flag"])
        self.assertIn("different site type", host["reason"])


class Classify(unittest.TestCase):
    def test_proposal_and_reasons(self):
        rows = sheet_rows()
        def at(path):
            return rows[path]["decision"], rows[path]["column"]
        self.assertEqual(at("applianceInfo.hostname"), ("variable", "hostname"))
        self.assertEqual(at("applianceInfo.site")[0], "variable")
        self.assertEqual(at("applianceInfo.group")[0], "variable")  # appliance info is always per site (decision 0006)
        self.assertEqual(at("applianceInfo.location.city"), ("variable", "city"))
        self.assertEqual(at("templateGroups.groups"), ("fixed", "template_groups"))
        self.assertEqual(at("deploymentInfo.deploymentInterfaces[0].ipAddressMask"), ("variable", "lan0_ip"))
        self.assertEqual(at("deploymentInfo.deploymentInterfaces[0].dhcpServer.gatewayIpAddress"), ("variable", "lan0_ip"))
        self.assertEqual(rows["deploymentInfo.deploymentInterfaces[0].dhcpServer.gatewayIpAddress"]["linked_to"], "lan0_ip")
        self.assertEqual(at("deploymentInfo.deploymentInterfaces[0].dhcpServer.startIpAddress")[0], "variable")
        self.assertEqual(at("deploymentInfo.deploymentInterfaces[1].outboundMaxBandwidth")[0], "variable")
        self.assertEqual(at("deploymentInfo.deploymentInterfaces[1].ipAddressMask")[0], "fixed")  # blank in the source
        self.assertEqual(at("deploymentInfo.deploymentInterfaces[0].interfaceName")[0], "fixed")
        self.assertEqual(at("localRoutes.staticRoutes[0].routeIpSubnet")[0], "fixed")  # default route
        self.assertEqual(at("deploymentInfo.deploymentInterfaces[2].nextHop"), ("variable", "wan1_next_hop"))
        self.assertEqual(rows["localRoutes.staticRoutes[0].nextHop"]["linked_to"], "wan1_next_hop")  # same address, typed once
        self.assertEqual(at("radius.server[0].key")[0], "variable")
        self.assertEqual(rows["radius.server[0].key"]["value"], "")  # a secret never reaches the sheet
        dns = rows["deploymentInfo.deploymentInterfaces[0].dhcpServer.dnsServers[0]"]
        self.assertEqual((dns["decision"], dns["flag"]), ("variable", False))  # on the Deployment page: per site, no question
        self.assertEqual(rows["radius.server[0].host"]["decision"], "fixed")

    def test_names_are_unique_and_valid(self):
        cols = [r["column"] for r in classify(REFERENCE, set()) if r["decision"] == "variable"]
        self.assertEqual(len(cols), len(set(cols)) + sum(1 for r in classify(REFERENCE, set()) if r["linked_to"]))


class RoundTrip(unittest.TestCase):
    def build(self, edit=None):
        out = Path(tempfile.mkdtemp())
        write_sheet(classify(REFERENCE, {"radius.server[0].key"}), out / "variables.csv")
        if edit:
            with (out / "variables.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            for r in rows:
                edit(r)
            with (out / "variables.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        chosen = read_sheet(out / "variables.csv", REFERENCE, {"radius.server[0].key"})
        meta = generate(REFERENCE, out, "t", names=chosen["names"], decide=lambda p, v, s: p in chosen["names"], list_fixed=chosen["list_fixed"], host_paths=chosen["host_paths"])
        return out, meta

    def test_example_row_reproduces_reference_and_linked_cells_follow(self):
        out, meta = self.build()
        row = read_rows(out / "example.csv")[0]
        doc = parse_yaml(render_row(out / "t.jinja2", row))
        self.assertEqual(changed_paths(REFERENCE, doc), ["radius.server[0].key"])
        self.assertEqual(flow_through(doc, meta["columns"], row), [])
        self.assertIn("data['lan0_ip'].split('/')[0] | tojson", (out / "t.jinja2").read_text())
        row["lan0_ip"] = "192.0.2.65/24"
        moved = parse_yaml(render_row(out / "t.jinja2", row))
        info = moved["deploymentInfo"]["deploymentInterfaces"][0]
        self.assertEqual((info["ipAddressMask"], info["dhcpServer"]["gatewayIpAddress"]), ("192.0.2.65/24", "192.0.2.65"))
        self.assertEqual(meta["columns"]["lan0_ip"]["path"], "deploymentInfo.deploymentInterfaces[0].ipAddressMask")
        self.assertEqual(sorted(meta["columns"]["lan0_ip"]["also_at"]), ["bgpSystemConfig.routerId", "deploymentInfo.deploymentInterfaces[0].dhcpServer.gatewayIpAddress"])

    def test_owner_can_flip_a_field_and_rename_a_column(self):
        def edit(r):
            if r["path"].endswith("dnsServers[0]"):
                r["decision"], r["column"] = "variable", "lan0_dns"
            if r["path"] == "applianceInfo.group":
                r["decision"], r["column"] = "variable", "group"
        out, meta = self.build(edit)
        self.assertIn("lan0_dns", meta["columns"])
        self.assertIn("group", meta["columns"])

    def test_bad_edits_are_named(self):
        for edit, message in (
            (lambda r: r.update(decision="maybe") if r["path"] == "applianceInfo.site" else None, "must be variable or fixed"),
            (lambda r: r.update(column="9 lives") if r["path"] == "applianceInfo.site" else None, "column '9 lives'"),
            (lambda r: r.update(decision="variable", column="lan0_ip") if r["path"] == "radius.server[0].host" else None, "whose source values differ"),
        ):
            with self.assertRaises(ValueError) as caught:
                self.build(edit)
            self.assertIn(message, str(caught.exception))

    def test_sheet_from_another_reference_is_refused(self):
        out, _ = self.build()
        other = {"applianceInfo": {"hostname": "x"}}
        with self.assertRaises(ValueError) as caught:
            read_sheet(out / "variables.csv", other, set())
        self.assertIn("not in this reference", str(caught.exception))


class MutationCheck(unittest.TestCase):
    def test_value_repeated_at_linked_paths_is_one_column(self):
        import json
        from check import run
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            (site / "t.jinja2").write_text("applianceInfo:\n  hostname: {{ data['hostname'] | tojson }}\nrules:\n- ip: {{ data['ip'] | tojson }}\n- ip: {{ data['ip'] | tojson }}\n")
            col = {"type": "string", "secret": False, "had_value": True}
            (site / "columns.json").write_text(json.dumps({"site_type": "t", "runner_columns": [], "columns": {
                "hostname": dict(col, path="applianceInfo.hostname"), "ip": dict(col, path="rules[0].ip", also_at=["rules[1].ip"])}}))
            for name in ("t.csv", "example.csv"):
                (site / name).write_text("hostname,ip\nbr-1,192.0.2.9\n")
            checks = run(site, "t", None, None)["checks"]
            self.assertTrue(checks["5 mutation: one column changes only its field"]["ok"], checks["5 mutation: one column changes only its field"])


if __name__ == "__main__":
    unittest.main()

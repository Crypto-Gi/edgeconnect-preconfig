import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "scripts"))
from cells import cell_problems, option_limits, subnet_problems  # noqa: E402
from generate import generate  # noqa: E402
from render import flow_through, main as render_main, read_rows, render_row  # noqa: E402
from common import parse_yaml  # noqa: E402
from upload import main as upload_main  # noqa: E402

REFERENCE = {
    "applianceInfo": {"hostname": "br-1", "site": "Plano"},
    "deploymentInfo": {
        "totalOutboundBandwidth": 200000,
        "deploymentInterfaces": [
            {"interfaceName": "lan0", "ipAddressMask": "192.0.2.1/24", "dhcpServer": {"startIpAddress": "192.0.2.10", "gatewayIpAddress": "192.0.2.1"}},
            {"interfaceName": "wan0", "ipAddressMask": None, "interfaceType": "wan"},
        ],
    },
}
VARIABLE = {"applianceInfo.hostname": "hostname", "applianceInfo.site": "site", "deploymentInfo.totalOutboundBandwidth": "bw", "deploymentInfo.deploymentInterfaces[0].ipAddressMask": "lan0_ip",
            "deploymentInfo.deploymentInterfaces[0].dhcpServer.startIpAddress": "lan0_start", "deploymentInfo.deploymentInterfaces[0].dhcpServer.gatewayIpAddress": "lan0_ip"}
HOST = {"deploymentInfo.deploymentInterfaces[0].dhcpServer.gatewayIpAddress"}


class AddressOrWord(unittest.TestCase):
    def test_behind_nat_may_be_auto_or_none(self):
        columns = {"nat": {"path": "deploymentInfo.deploymentInterfaces[1].behindNat", "format": "ip", "type": "str", "optional": False, "example": "203.0.113.9", "secret": False}}
        for cell in ("auto", "none", "203.0.113.10"):
            self.assertEqual(cell_problems(columns, {"nat": cell}, {}), [], cell)
        self.assertIn("nat", cell_problems(columns, {"nat": "203.0.113.300"}, {})[0])
        self.assertIn("auto", cell_problems(columns, {"nat": "x"}, {})[0])  # the hint names the words


class AddressingMode(unittest.TestCase):
    """The interface address and next hop are required for a static row and blank for a DHCP one, whichever site the template came from."""

    def build(self, mode, address, hop):
        reference = {"applianceInfo": {"hostname": "br-1"}, "deploymentInfo": {"deploymentInterfaces": [
            {"interfaceName": "wan0", "interfaceType": "wan", "addressingMode": mode, "ipAddressMask": address, "nextHop": hop}]}}
        names = {"applianceInfo.hostname": "hostname", "deploymentInfo.deploymentInterfaces[0].addressingMode": "wan0_mode",
                 "deploymentInfo.deploymentInterfaces[0].ipAddressMask": "wan0_ip", "deploymentInfo.deploymentInterfaces[0].nextHop": "wan0_next_hop"}
        out = Path(tempfile.mkdtemp())
        meta = generate(reference, out, "t", names=names, decide=lambda p, v, s: p in names, release="9.7.1.42046")
        return out, meta["columns"]

    def check(self, columns, **row):
        return cell_problems(columns, dict({"hostname": "br-2"}, **row), {}) + subnet_problems(columns, dict({"hostname": "br-2"}, **row))

    def test_static_template_dhcp_row(self):
        out, columns = self.build("static", "198.51.100.2/30", "198.51.100.1")
        self.assertEqual(columns["wan0_ip"]["mode_column"], "wan0_mode")
        self.assertEqual(self.check(columns, wan0_mode="dhcpv4", wan0_ip="", wan0_next_hop=""), [])
        self.assertIn("must be blank", self.check(columns, wan0_mode="dhcpv4", wan0_ip="198.51.100.6/30", wan0_next_hop="")[0])
        self.assertIn("wan0_ip: blank", self.check(columns, wan0_mode="static", wan0_ip="", wan0_next_hop="198.51.100.1")[0])
        doc = parse_yaml(render_row(out / "t.jinja2", {"hostname": "br-2", "wan0_mode": "dhcpv4", "wan0_ip": "", "wan0_next_hop": ""}))
        wan = doc["deploymentInfo"]["deploymentInterfaces"][0]
        self.assertEqual((wan["ipAddressMask"], wan["nextHop"]), (None, None))  # null, as translate writes a DHCP interface
        self.assertEqual(flow_through(doc, columns, {"hostname": "br-2", "wan0_mode": "dhcpv4", "wan0_ip": "", "wan0_next_hop": ""}), [])

    def test_dhcp_template_static_row(self):
        _, columns = self.build("dhcpv4", None, None)
        self.assertEqual(columns["wan0_ip"]["format"], "ip/prefix")  # from the field, not from the template site's null
        self.assertEqual(self.check(columns, wan0_mode="static", wan0_ip="198.51.100.2/30", wan0_next_hop="198.51.100.1"), [])
        self.assertIn("not an address", self.check(columns, wan0_mode="static", wan0_ip="198.51.100.300/30", wan0_next_hop="198.51.100.1")[0])
        self.assertIn("wan0_ip: blank", self.check(columns, wan0_mode="static", wan0_ip="", wan0_next_hop="198.51.100.1")[0])
        self.assertEqual(self.check(columns, wan0_mode="dhcpv4", wan0_ip="", wan0_next_hop=""), [])

    def test_fixed_mode(self):
        _, columns = self.build("static", "198.51.100.2/30", "198.51.100.1")
        del columns["wan0_mode"]
        for col in ("wan0_ip", "wan0_next_hop"):
            columns[col].pop("mode_column")
            columns[col]["mode"] = "static"
        self.assertIn("wan0_ip: blank", self.check(columns, wan0_ip="", wan0_next_hop="198.51.100.1")[0])


class Cells(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.meta = generate(REFERENCE, self.out, "t", names=VARIABLE, decide=lambda p, v, s: p in VARIABLE, host_paths=HOST, release="9.7.1.42046")
        self.columns = self.meta["columns"]
        self.limits = option_limits("9.7.1.42046")
        self.row = read_rows(self.out / "example.csv")[0]

    def problems(self, **edit):
        row = dict(self.row, **edit)
        return cell_problems(self.columns, row, self.limits) + subnet_problems(self.columns, row)

    def test_columns_md_says_what_each_csv_is_for(self):
        head = (self.out / "columns.md").read_text().split("\n\n")[1]
        self.assertEqual(head, "- `t.csv`: empty sheet (headers only) for new sites.\n"
                               "- `example.csv`: this appliance's own values, secrets blank - copy it to start your sites.csv.")

    def test_example_row_passes_and_columns_know_their_format(self):
        self.assertEqual(self.problems(), [])
        self.assertEqual(self.columns["lan0_ip"]["format"], "ip/prefix")
        self.assertEqual(self.columns["lan0_start"]["format"], "ip")

    def test_typos_are_named_by_column(self):
        self.assertIn("lan0_ip: '192.0.2.300/24' is not an address with prefix length", self.problems(lan0_ip="192.0.2.300/24")[0])
        self.assertIn("lan0_ip", self.problems(lan0_ip="192.0.2.1")[0])  # prefix length missing
        self.assertIn("lan0_start", self.problems(lan0_start="192.0.2.10/24")[0])  # prefix length where none belongs
        self.assertIn("leading or trailing spaces", self.problems(site=" Plano")[0])
        self.assertIn("blank", self.problems(lan0_ip="")[0])
        self.assertIn("whole number", self.problems(bw="2e5")[0])
        self.assertIn("outside 1 to 100000000", self.problems(bw="0")[0])
        self.assertEqual(self.problems(bw="200000"), [])  # the reference's own value always passes

    def test_address_must_sit_in_its_interface_subnet(self):
        self.assertEqual(self.problems(lan0_ip="198.51.100.1/24", lan0_start="198.51.100.10"), [])
        message = self.problems(lan0_start="198.51.100.10")
        self.assertEqual(len(message), 1)
        self.assertIn("lan0_start: 198.51.100.10 is not inside the subnet of lan0_ip", message[0])

    def test_an_empty_secret_is_refused_only_where_orchestrator_rejects_it(self):
        ref = dict(REFERENCE, nac={"server": [{"id": 1, "host": "192.0.2.5", "key": "<SECRET:nac.server.1.key>"}]},
                   ospfSystemConfig={"interfaces": [{"interfaceName": "lan0", "authenticationKey": "<SECRET:ospf.lan0.authKey>"}]})
        meta = generate(ref, Path(tempfile.mkdtemp()), "t", names=VARIABLE, decide=lambda p, v, s: p in VARIABLE or s, host_paths=HOST, release="9.7.1.42046")
        key = next(n for n, c in meta["columns"].items() if c["path"] == "nac.server[0].key")
        ospf = next(n for n, c in meta["columns"].items() if c["path"] == "ospfSystemConfig.interfaces[0].authenticationKey")
        self.assertTrue(meta["columns"][key]["secret"] and meta["columns"][ospf]["secret"])
        row = dict(self.row, **{key: "", ospf: ""})
        self.assertEqual(cell_problems(meta["columns"], row, self.limits),
                         ["secret column {} is empty: fill it in the CSV (the toolkit never stores secrets)".format(key)])
        self.assertEqual(cell_problems(meta["columns"], dict(row, **{key: "s3cret", ospf: " pass phrase "}), self.limits), [])  # a secret is typed as is, never format-checked

    def test_render_and_upload_cli(self):
        sites = self.out / "sites.csv"
        with sites.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(self.row))
            writer.writeheader()
            writer.writerow(dict(self.row, preconfig_name="ok"))
            writer.writerow(dict(self.row, preconfig_name="bad", lan0_start="198.51.100.10"))
        self.assertEqual(render_main(["--site-dir", str(self.out), "--csv", str(sites)]), 1)
        self.assertTrue((self.out / "rendered" / "ok.yml").is_file())
        self.assertFalse((self.out / "rendered" / "bad.yml").exists())
        with self.assertRaises(SystemExit):  # upload needs an explicit --csv
            upload_main(["--site-dir", str(self.out), "--orchestrator", "x"])


if __name__ == "__main__":
    unittest.main()

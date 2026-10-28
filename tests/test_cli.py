import contextlib
import copy
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "toolkit" / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "harnesses"))
import cli  # noqa: E402
from generate import generate  # noqa: E402
from rank_variables import rank  # noqa: E402
from render import headers  # noqa: E402
from variables import classify, read_sheet, setting_key, write_sheet  # noqa: E402

REFERENCE = {
    "applianceInfo": {"hostname": "br-1", "site": "Plano", "location": {"city": "Plano"}},
    "deploymentInfo": {
        "totalOutboundBandwidth": 200000,
        "deploymentInterfaces": [
            {"interfaceName": "lan0", "ipAddressMask": "192.0.2.1/24"},
            {"interfaceName": "lan0.71", "ipAddressMask": "198.51.100.1/24"},
            {"interfaceName": "wan0", "ipAddressMask": None, "outboundMaxBandwidth": 5000},
        ],
        "dhcpInfo": [{"dhcpInterfaceName": "lan0", "dhcpType": "server", "gatewayIpAddress": "192.0.2.1", "dnsServers": ["203.0.113.53"]}],
    },
    "localRoutes": {"staticRoutes": [{"routeIpSubnet": "0.0.0.0/0", "nextHop": "203.0.113.1"}, {"routeIpSubnet": "203.0.113.64/26", "nextHop": "192.0.2.254"}]},
    "bgpSystemConfig": {"asn": 65001, "routerId": "203.0.113.9", "neighbors": [{"peerIpAddress": "192.0.2.254"}]},
    "radius": {"server": [{"host": "203.0.113.7", "key": "<SECRET:radius>"}]},
}
SECRETS = {"radius.server[0].key"}
RANKING = {  # setting kind -> rank, as references/variable-ranking.json holds it
    "applianceInfo.location.city": {"differs_pct": 99, "appliances": 60},  # locked anyway: appliance info is always per site
    "bgpSystemConfig.routerId": {"differs_pct": 100, "appliances": 17},
    "bgpSystemConfig.asn": {"differs_pct": 96, "appliances": 17},
    "deploymentInfo.deploymentInterfaces[lan].interfaceName": {"differs_pct": 100, "appliances": 7},  # layout: never pre-ticked
}


def sheet_rows(reference=REFERENCE, others=None):
    return [{k: cli.cell(v) for k, v in r.items()} for r in classify(reference, SECRETS, None, others)]


def by_path(rows):
    return {r["path"]: r for r in rows}


def scripted(*answers):
    it = iter(answers)
    return lambda prompt: next(it)


class SettingKey(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(setting_key(REFERENCE, "deploymentInfo.deploymentInterfaces[1].ipAddressMask"), "deploymentInfo.deploymentInterfaces[lan.vlan].ipAddressMask")
        self.assertEqual(setting_key(REFERENCE, "deploymentInfo.dhcpInfo[0].dnsServers[0]"), "deploymentInfo.dhcpInfo[lan].dnsServers[#]")
        self.assertEqual(setting_key(REFERENCE, "localRoutes.staticRoutes[0].nextHop"), "localRoutes.staticRoutes[#].nextHop")


class Ranking(unittest.TestCase):
    def test_only_pairs_of_the_same_design_count(self):
        a = {"p": ("k", 1), "q": ("q", "same")}
        refs = [("d1", a), ("d1", {"p": ("k", 2), "q": ("q", "same")}), ("d2", {"p": ("k", 3), "q": ("q", "other")})]
        rows = {r["setting"]: r for r in rank(refs, min_appliances=1, min_pairs=1)}
        self.assertEqual((rows["k"]["differs_pct"], rows["k"]["pairs"], rows["k"]["appliances"]), (100, 1, 3))
        self.assertEqual(rows["q"]["differs_pct"], 0)  # differs only across designs, which is not compared


class PickerChoice(unittest.TestCase):
    def picker(self):
        return cli.Picker(REFERENCE, sheet_rows(), RANKING)

    def test_top_ticks_ranked_and_own_addresses_only(self):
        p = self.picker()
        p.start_top(1)
        on = {path for path, v in p.on.items() if v}
        self.assertIn("bgpSystemConfig.routerId", on)  # the top ranked
        self.assertNotIn("bgpSystemConfig.asn", on)  # ranked, but below the Top 1
        self.assertIn("localRoutes.staticRoutes[1].nextHop", on)  # inside this site's own subnet, always offered
        for locked in ("applianceInfo.hostname", "radius.server[0].key", "applianceInfo.location.city", "deploymentInfo.totalOutboundBandwidth",
                       "deploymentInfo.dhcpInfo[0].dnsServers[0]"):
            self.assertNotIn(locked, p.on)  # locked rows are not choices
        self.assertIn("deploymentInfo.deploymentInterfaces[0].interfaceName", p.on)  # layout: fixed, but can be flipped
        self.assertFalse(p.on["deploymentInfo.deploymentInterfaces[0].interfaceName"])
        p.start_top(20)
        self.assertFalse(p.on["deploymentInfo.deploymentInterfaces[0].interfaceName"])  # ranked high, but layout stays fixed unless flipped
        sheet = by_path(p.sheet(REFERENCE))
        self.assertEqual(sheet["deploymentInfo.dhcpInfo[0].dnsServers[0]"]["decision"], "variable")  # Deployment page, no question

    def test_follower_takes_its_primary_decision(self):
        p = self.picker()
        p.start_top(10)
        primary = next(r for r in p.free if r["path"] == "localRoutes.staticRoutes[1].nextHop")
        follower = "bgpSystemConfig.neighbors[0].peerIpAddress"  # the same address: typed once
        p.flip([primary["n"]])  # now fixed
        sheet = by_path(p.sheet(REFERENCE))
        self.assertEqual(sheet[follower]["decision"], "fixed")
        p.flip([primary["n"]])
        sheet = by_path(p.sheet(REFERENCE))
        self.assertEqual((sheet[follower]["decision"], sheet[follower]["column"]), ("variable", sheet[primary["path"]]["column"]))
        gateway = sheet["deploymentInfo.dhcpInfo[0].gatewayIpAddress"]  # follows the lan0 address, which is locked
        self.assertEqual((gateway["decision"], gateway["column"]), ("variable", sheet["deploymentInfo.deploymentInterfaces[0].ipAddressMask"]["column"]))

    def test_sheet_builds_a_template_with_the_chosen_columns(self):
        p = self.picker()
        p.start_top(10)
        self.assertEqual(p.set_by_name(["bgpSystemConfig.asn", "nonsense"]), ["nonsense"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "variables.csv"
            write_sheet(p.sheet(REFERENCE), path)
            chosen = read_sheet(path, REFERENCE, SECRETS)
            generate(REFERENCE, Path(tmp), "br", names=chosen["names"], decide=lambda path, value, secret: path in chosen["names"],
                     list_fixed=chosen["list_fixed"], host_paths=chosen["host_paths"])
            found = headers((Path(tmp) / "br.jinja2").read_text())
        dns = chosen["names"]["deploymentInfo.dhcpInfo[0].dnsServers[0]"]
        self.assertTrue(dns.startswith("lan0"))  # dhcp items are named by their interface
        for column in (dns, "hostname", "city", chosen["names"]["deploymentInfo.totalOutboundBandwidth"], chosen["names"]["bgpSystemConfig.asn"]):
            self.assertIn(column, found)
        self.assertNotIn("deploymentInfo.localRoutes", chosen["names"])

    def test_tick_list_commands(self):
        p = self.picker()
        p.start_top(10)
        asn = next(r for r in p.free if r["path"] == "bgpSystemConfig.asn")
        before = p.on[asn["path"]]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            done = cli.interact(p, "top10", scripted("asn", str(asn["n"]), "a", "s1", "l", "zzz", ""))
        self.assertTrue(done)
        self.assertEqual(p.on[asn["path"]], not before)
        self.assertIn("Always per site (locked)", out.getvalue())
        self.assertIn("nothing matches", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(cli.interact(p, "all", scripted("q")))


class CompareRows(unittest.TestCase):
    def test_each_site_gets_its_own_row(self):
        other = copy.deepcopy(REFERENCE)
        other["applianceInfo"]["hostname"] = "br-2"
        other["deploymentInfo"]["deploymentInterfaces"][0]["ipAddressMask"] = "198.51.100.129/25"
        other["deploymentInfo"]["dhcpInfo"][0]["gatewayIpAddress"] = "198.51.100.129"
        other["radius"]["server"][0]["key"] = "real-secret-at-br-2"
        rows = sheet_rows(others=[other])
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            path = site / "variables.csv"
            write_sheet(rows, path)
            chosen = read_sheet(path, REFERENCE, SECRETS)
            generate(REFERENCE, site, "br", names=chosen["names"], decide=lambda p, v, s: p in chosen["names"], list_fixed=chosen["list_fixed"], host_paths=chosen["host_paths"])
            from variables import sheet_items
            cli.write_rows(site, "br", [{"hostname": "br-2", "items": dict(sheet_items(other))}])
            with (site / "sites.csv").open(newline="") as handle:
                first, second = list(csv.DictReader(handle))
        lan = chosen["names"]["deploymentInfo.deploymentInterfaces[0].ipAddressMask"]
        self.assertEqual((first["hostname"], first[lan]), ("br-1", "192.0.2.1/24"))
        self.assertEqual((second["hostname"], second[lan], second["preconfig_name"]), ("br-2", "198.51.100.129/25", "br-2_preconfig"))
        secret = chosen["names"]["radius.server[0].key"]
        self.assertEqual((second[secret], second["serial_number"]), ("", ""))  # never copied: typed in by a person


class TopAndAll(unittest.TestCase):
    def test_top_n_never_ticks_template_groups_or_overlays(self):
        ref = copy.deepcopy(REFERENCE)
        ref["templateGroups"] = {"groups": ["G1", "G2"]}
        ref["businessIntentOverlays"] = {"overlays": ["O1"]}
        ranking = dict(RANKING, **{"templateGroups.groups": {"differs_pct": 97, "appliances": 60}, "businessIntentOverlays.overlays": {"differs_pct": 99, "appliances": 60}})
        p = cli.Picker(ref, sheet_rows(ref), ranking)
        p.start_top(20)
        self.assertEqual((p.on["templateGroups.groups"], p.on["businessIntentOverlays.overlays"]), (False, False))

    def test_pick_all_yes_ticks_every_setting(self):
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "build.json").write_text(json.dumps({"site_type": "br", "orchestrator": "lab", "validator": "lab", "capture": "c", "target_release": "9.7.1.42046", "appliance": {}}))
            (folder / "reference.yaml").write_text(yaml.safe_dump(REFERENCE))
            write_sheet(sheet_rows(), folder / "variables.csv")
            with mock.patch.object(cli, "build_site", return_value=folder) as built, mock.patch.object(cli, "ensure_sites_csv"), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_variables([str(folder), "--pick", "all", "--yes"]), 0)
        sheet = by_path(built.call_args.args[3])
        fixed = sorted(p for p, r in sheet.items() if r["decision"] == "fixed")
        self.assertEqual(fixed, [])


class CompareLayout(unittest.TestCase):
    def test_a_site_with_another_interface_layout_gets_no_row(self):
        other = copy.deepcopy(REFERENCE)
        other["applianceInfo"]["hostname"] = "br-2"
        other["deploymentInfo"]["deploymentInterfaces"][2] = {"interfaceName": "wan1", "ipAddressMask": "203.0.113.2/24", "outboundMaxBandwidth": 5000}
        self.assertEqual(cli.layout_differences(REFERENCE, other), ["deploymentInterfaces[2]: wan0 vs wan1"])
        self.assertEqual(cli.layout_differences(REFERENCE, copy.deepcopy(REFERENCE)), [])
        per_site = copy.deepcopy(REFERENCE)
        per_site["tunnels"] = [{"tunnelName": "zs-br1", "mtu": 1400}]
        theirs = copy.deepcopy(per_site)
        theirs["tunnels"][0]["tunnelName"] = "zs-br2"
        self.assertEqual(cli.layout_differences(per_site, theirs), [])  # per-site tunnel names are values, not a different layout
        renamed = copy.deepcopy(REFERENCE)
        renamed["deploymentInfo"]["deploymentInterfaces"][2]["interfaceName"] = "uplink"  # any other name, not only lanN/wanN
        self.assertEqual(cli.layout_differences(REFERENCE, renamed), ["deploymentInterfaces[2]: wan0 vs uplink"])

    def test_when_every_other_site_is_left_out_the_csv_still_exists(self):
        import yaml
        other = copy.deepcopy(REFERENCE)
        other["applianceInfo"]["hostname"] = "br-2"
        other["deploymentInfo"]["deploymentInterfaces"][2]["interfaceName"] = "wan1"
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "build.json").write_text(json.dumps({"site_type": "br", "orchestrator": "lab", "validator": "lab", "capture": "c", "target_release": "9.7.1.42046", "appliance": {"hostName": "br-1"}}))
            (folder / "reference.yaml").write_text(yaml.safe_dump(REFERENCE))
            site = folder / "site-br"
            site.mkdir()
            (site / "example.csv").write_text("hostname\nbr-1\n")
            seen = {}
            with mock.patch("capture.capture", return_value=folder), mock.patch("translate.Capture"), mock.patch("translate.build", return_value=other), \
                    mock.patch.object(cli, "build_site", return_value=site), \
                    mock.patch.object(cli, "validate_rows", side_effect=lambda target, csv_path, info, nick: seen.setdefault("exists", csv_path.is_file()) and 0), \
                    contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_compare([str(folder), "br-2", "--yes"])
        self.assertTrue(seen["exists"])

    def test_an_offline_site_without_cache_is_one_line(self):
        from translate import NothingToConvert
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "build.json").write_text(json.dumps({"site_type": "br", "orchestrator": "lab", "validator": "lab", "capture": "c", "target_release": "9.7.1.42046", "appliance": {}}))
            with mock.patch("capture.capture", return_value=folder), mock.patch("translate.Capture"), \
                    mock.patch("translate.build", side_effect=NothingToConvert("capture 'x' lacks appliance or deployment.sysConfig data")), \
                    contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["compare", str(folder), "br-2"]), 2)
        self.assertEqual(err.getvalue(), "error: capture 'x' lacks appliance or deployment.sysConfig data\n")


class ListLogin(unittest.TestCase):
    def client(self, versions_error=None):
        client = mock.Mock(write_requests=0, config=mock.Mock(base_url="https://orch.example.com/gms/rest"))
        def get(path, query=None):
            if path == "/gms/versions":
                if versions_error:
                    raise versions_error
                return {"current": "9.7.1.42046"}
            return [{"hostName": "br-1", "nePk": "1.NE", "model": "EC-V", "softwareVersion": "9.7.1", "site": "s", "state": 1}]
        client.get.side_effect = get
        return client

    def test_a_refused_release_read_still_lists(self):
        from common import PermissionDeniedError
        with mock.patch.object(cli, "client_for", return_value=self.client(PermissionDeniedError("403", status=403))), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.cmd_list(["--orchestrator", "lab"]), 0)
        self.assertIn("release unknown", out.getvalue())
        self.assertIn("br-1", out.getvalue())

    def test_a_bad_key_never_says_login_ok(self):
        from common import AuthenticationError
        with mock.patch.object(cli, "client_for", return_value=self.client(AuthenticationError("401", status=401))), contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(AuthenticationError):
                cli.cmd_list(["--orchestrator", "lab"])
        self.assertNotIn("login OK", out.getvalue())


class Dispatch(unittest.TestCase):
    def test_stage_names_still_work(self):
        with mock.patch.object(cli, "run_stage", return_value=0) as stage:
            cli.main(["validate", "--orchestrator", "lab", "a.yml"])
            cli.main(["cache-check", "x"])
        self.assertEqual([c.args[0] for c in stage.call_args_list], ["validate", "cache-check"])

    def test_verbs_and_unknown_names(self):
        with mock.patch.dict(cli.VERBS, {"validate": mock.Mock(return_value=0)}):
            cli.main(["validate", "folder", "--csv", "s.csv"])
            cli.VERBS["validate"].assert_called_once_with(["folder", "--csv", "s.csv"])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.run_stage("common", []), 2)  # a library, not a command
            self.assertEqual(cli.run_stage("nope", []), 2)
        self.assertIn("cache-check", cli.stages())
        self.assertNotIn("compare", cli.stages())  # the old stage name is now the compare verb

    def test_a_refused_validator_is_one_line_not_a_traceback(self):
        from validate import Stop
        with mock.patch.dict(cli.VERBS, {"build": mock.Mock(side_effect=Stop("PermissionDeniedError (HTTP 403) from /validate"))}), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(["build"]), 2)
        self.assertEqual(err.getvalue(), "error: PermissionDeniedError (HTTP 403) from /validate\n")

    def test_load_build_from_a_site_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "build.json").write_text(json.dumps({"orchestrator": "lab"}))
            (Path(tmp) / "site-br").mkdir()
            self.assertEqual(cli.load_build(Path(tmp) / "site-br")["orchestrator"], "lab")
            with self.assertRaises(cli.Fail):
                cli.load_build(Path(tmp) / "site-br" / "deeper")


class Build(unittest.TestCase):
    def fake_capture(self, tmp):
        cap = Path(tmp) / "cap"
        cap.mkdir()
        (cap / "run.json").write_text(json.dumps({"release": "9.7.1.42046", "appliance": {"nePk": "1.NE", "hostName": "Br 1"}}))
        return mock.patch("capture.capture", return_value=cap)

    def test_refuses_a_non_validator_before_reading_anything(self):
        with mock.patch.object(cli, "VALIDATORS", {"lab"}), mock.patch("capture.capture") as cap:
            with self.assertRaises(cli.Fail) as stop:
                cli.cmd_build(["--orchestrator", "fieldref", "--appliance", "x"])
        self.assertIn("EC_VALIDATORS", str(stop.exception))
        cap.assert_not_called()

    def test_nothing_is_kept_when_validation_fails(self):
        with tempfile.TemporaryDirectory() as tmp, self.fake_capture(tmp), mock.patch.object(cli, "VALIDATORS", {"lab"}), \
                mock.patch.object(cli, "translate_into", return_value="verdict: Complete for preconfig."), \
                mock.patch.object(cli, "checked", return_value=["10 validation recorded: rejected"]), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(cli.Fail):
                cli.cmd_build(["--orchestrator", "lab", "--appliance", "Br 1", "--out", str(Path(tmp) / "out")])
            self.assertEqual(list((Path(tmp) / "out").glob("*")), [])

    def test_a_validated_build_is_kept_and_an_earlier_one_renamed(self):
        def translate_into(out, capture, site, release, extra):
            out.mkdir(parents=True)
            (out / "reference.yaml").write_text("applianceInfo: {}\n")
            return "verdict: Complete for preconfig."
        with tempfile.TemporaryDirectory() as tmp, self.fake_capture(tmp), mock.patch.object(cli, "VALIDATORS", {"lab"}), \
                mock.patch.object(cli, "translate_into", side_effect=translate_into), mock.patch.object(cli, "checked", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            out = Path(tmp) / "out"
            (out / "Br_1").mkdir(parents=True)
            self.assertEqual(cli.cmd_build(["--orchestrator", "lab", "--appliance", "Br 1", "--out", str(out)]), 0)
            info = json.loads((out / "Br_1" / "build.json").read_text())
            self.assertEqual((info["orchestrator"], info["validator"], info["site_type"]), ("lab", "lab", "br_1"))
            self.assertEqual(len(list(out.glob("Br_1.previous-*"))), 1)
            self.assertEqual(list(out.glob(".building-*")), [])

    def test_target_ecos_reaches_translate_and_build_json(self):
        calls = []

        def translate_into(out, capture, site, release, extra):
            calls.append(extra)
            out.mkdir(parents=True)
            return "verdict: Complete for preconfig."
        with tempfile.TemporaryDirectory() as tmp, self.fake_capture(tmp), mock.patch.object(cli, "VALIDATORS", {"lab"}), \
                mock.patch.object(cli, "translate_into", side_effect=translate_into), mock.patch.object(cli, "checked", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            out = Path(tmp) / "out"
            cli.cmd_build(["--orchestrator", "lab", "--appliance", "Br 1", "--out", str(out), "--target-ecos", "9.6.2.1"])
            self.assertEqual(json.loads((out / "Br_1" / "build.json").read_text())["target_ecos"], "9.6.2.1")
            cli.cmd_build(["--orchestrator", "lab", "--appliance", "Br 1", "--out", str(out)])
        self.assertEqual(calls, [["--target-ecos", "9.6.2.1"], []])


if __name__ == "__main__":
    unittest.main()

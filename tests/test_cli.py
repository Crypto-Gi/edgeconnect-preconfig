import contextlib
import copy
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "toolkit" / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "harnesses"))
os.environ["EDGECONNECT_REPORT_DIR"] = ""  # a command logs to reports/ (runlog.py); the run-log tests set their own folder
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


class FixAndDryRun(unittest.TestCase):
    """variables --fix and --dry-run (cli-requests R5) on a build folder of REFERENCE: the dry run asks nothing, builds nothing, calls no Orchestrator."""

    def setUp(self):
        import yaml
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.folder), True)
        (self.folder / "build.json").write_text(json.dumps({"site_type": "br", "orchestrator": "lab", "validator": "lab", "capture": "c", "target_release": "9.7.1.42046", "appliance": {}}))
        (self.folder / "reference.yaml").write_text(yaml.safe_dump(REFERENCE, sort_keys=False))
        write_sheet(sheet_rows(), self.folder / "variables.csv")
        for target in (mock.patch("variables.load_ranking", return_value=RANKING), mock.patch.object(cli, "client_for", side_effect=AssertionError("Orchestrator called")),
                       mock.patch.object(cli, "build_site", side_effect=AssertionError("site built"))):
            target.start()
            self.addCleanup(target.stop)

    def run_cli(self, *flags):
        """cli.main on the folder with an empty stdin (a question ends the run with exit 1); returns code, stdout, stderr and the recorded picker."""
        from common import Recorder, RunContext, running
        recorder = Recorder()
        with running(RunContext(recorder=recorder)), contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err, \
                mock.patch.object(sys, "stdin", io.StringIO()):
            try:
                code = cli.main(["variables", str(self.folder)] + list(flags))
            except SystemExit as stop:  # argparse: an unknown flag
                code = stop.code
        pickers = [r for r in recorder.results if r["kind"] == "picker"]
        return code, out.getvalue(), err.getvalue(), pickers[0] if pickers else None

    def test_fix_undoes_set(self):
        host = "radius.server[0].host"  # not in the Top 10
        _, _, _, plain = self.run_cli("--pick", "top10", "--dry-run")
        code, _, err, ticked = self.run_cli("--pick", "top10", "--set", host, "--dry-run")
        self.assertEqual(code, 0, err)
        column = by_path(ticked["rows"])[host]["column"]
        self.assertIn(column, ticked["columns"])
        for fix in (host, column):  # the path, or the column name the dry run showed
            with self.subTest(fix=fix):
                code, _, err, undone = self.run_cli("--pick", "top10", "--set", host, "--fix", fix, "--dry-run")
                self.assertEqual(code, 0, err)
                self.assertFalse(by_path(undone["rows"])[host]["on"])
                self.assertEqual(undone["columns"], plain["columns"])
        self.assertFalse(by_path(plain["rows"])[host]["on"])
        router = "bgpSystemConfig.routerId"  # ticked by the Top 10: --fix unticks it too
        self.assertTrue(by_path(plain["rows"])[router]["on"])
        self.assertFalse(by_path(self.run_cli("--pick", "top10", "--fix", router, "--dry-run")[3]["rows"])[router]["on"])

    def test_a_locked_row_is_refused_with_its_reason(self):
        for name, why in (("hostname", "hostname"), ("radius.server[0].key", "secret"), ("site", "deployment-page value (docs/decisions/0006)"),
                          ("deploymentInfo.dhcpInfo[0].gatewayIpAddress", "deployment-page value (docs/decisions/0006)")):
            with self.subTest(name=name):
                code, out, err, picker = self.run_cli("--pick", "top10", "--fix", name, "--dry-run")
                self.assertEqual((code, out, picker), (2, "", None))
                self.assertEqual(err, "error: --fix: {} cannot be fixed: always per site, {}\n".format(name, why))

    def test_a_follower_is_refused_naming_the_row_it_follows(self):
        code, _, err, _ = self.run_cli("--pick", "top10", "--fix", "bgpSystemConfig.neighbors[0].peerIpAddress,no_such", "--dry-run")
        self.assertEqual(code, 2)
        self.assertEqual(err, "error: --fix: bgpSystemConfig.neighbors[0].peerIpAddress cannot be fixed on its own: it follows "
                              "local_routes_static_routes_1_next_hop (localRoutes.staticRoutes[1].nextHop); no setting named no_such\n")
        code, _, err, picker = self.run_cli("--pick", "top10", "--fix", "local_routes_static_routes_1_next_hop", "--dry-run")  # the column both share: the row it follows
        self.assertEqual(code, 0, err)
        rows = by_path(picker["rows"])
        self.assertEqual((rows["localRoutes.staticRoutes[1].nextHop"]["on"], rows["bgpSystemConfig.neighbors[0].peerIpAddress"]["on"]), (False, False))

    def test_fix_takes_a_label_as_set_does_and_names_every_unknown_once(self):
        path = "deploymentInfo.deploymentInterfaces[0].interfaceName"
        label = "deploymentInterfaces[lan0].interfaceName"  # what the tick list shows
        _, _, err, ticked = self.run_cli("--pick", "top10", "--set", label, "--dry-run")
        self.assertTrue(by_path(ticked["rows"])[path]["on"], err)
        code, _, err, undone = self.run_cli("--pick", "top10", "--set", label, "--fix", label, "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertFalse(by_path(undone["rows"])[path]["on"])
        code, _, err, _ = self.run_cli("--pick", "top10", "--fix", "a,hostname,b", "--dry-run")
        self.assertEqual((code, err), (2, "error: --fix: hostname cannot be fixed: always per site, hostname; no setting named a, b\n"))

    def test_dry_run_prints_the_columns_and_records_where_each_value_is(self):
        before = sorted((str(p), p.stat().st_mtime_ns) for p in self.folder.rglob("*"))
        code, out, err, picker = self.run_cli("--pick", "top10", "--set", "bgpSystemConfig.asn", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted((str(p), p.stat().st_mtime_ns) for p in self.folder.rglob("*")), before)  # nothing written
        self.assertEqual(out, "\n{} columns: {}\n".format(len(picker["columns"]), ", ".join(picker["columns"])))  # the line finish() prints
        self.assertIsNone(picker["folder"])
        lines = (self.folder / "reference.yaml").read_text().splitlines()
        for row in picker["rows"]:
            with self.subTest(path=row["path"]):
                if not row["on"]:
                    self.assertIsNone(row["lines"])
                    continue
                first, last = row["lines"]
                self.assertLessEqual(first, last)
                held = "\n".join(lines[first - 1:last])
                if row["value"] not in ("<SECRET>", ""):
                    self.assertIn(row["value"], held)
        self.assertEqual(by_path(picker["rows"])["applianceInfo.hostname"]["lines"], [2, 2])

    def test_dry_run_needs_pick(self):
        code, _, err, _ = self.run_cli("--dry-run")
        self.assertEqual((code, err), (2, "error: --dry-run needs --pick top10, top20 or all\n"))


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
        self.assertEqual(err.getvalue(), "error: br-2: no appliance or deployment data could be read (offline with no Orchestrator cache?), so it cannot be compared\n")

    def broken(self, damage):
        """cmd_compare with a real translate.Capture on a damaged partner capture: (exit code, stderr)."""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "build.json").write_text(json.dumps({"site_type": "br", "orchestrator": "lab", "validator": "lab", "capture": "c", "target_release": "9.7.1.42046",
                                                            "appliance": {"hostName": "br-1", "nePk": "1.NE"}}))
            cap = folder / "cap-2026-10-30T000000Z"
            (cap / "capture").mkdir(parents=True)
            (cap / "run.json").write_text(json.dumps({"appliance": {"nePk": "2.NE"}}))
            (cap / "capture-index.json").write_text(json.dumps([{"id": "appliance", "label": "live", "status": "ok", "file": "capture/appliance.live.json"}]))
            (cap / "capture" / "appliance.live.json").write_text(json.dumps({"data": {"hostName": "br-2"}}))
            damage(cap)
            with mock.patch("capture.capture", return_value=cap), contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["compare", str(folder), "br-2"])
        return code, err.getvalue()

    def test_a_damaged_capture_is_one_line_naming_the_site(self):
        for damage, detail in ((lambda cap: (cap / "capture-index.json").unlink(), "FileNotFoundError: capture-index.json"),
                               (lambda cap: (cap / "capture" / "appliance.live.json").write_text("{not json"), "JSONDecodeError: ")):
            code, err = self.broken(damage)
            self.assertEqual(code, 2)
            self.assertTrue(err.startswith("error: br-2: its capture could not be read ({}".format(detail)), err)
            self.assertEqual(len(err.splitlines()), 1)
            self.assertNotIn("cap-2026", err)  # the site as typed, not the capture folder

    def test_the_base_appliance_is_refused_as_its_own_partner(self):
        for typed in ("br-1", "BR-1", "1.NE"):
            code, out, built = run_compare(REFERENCE, [], argv=(typed, "--yes"))
            self.assertEqual((code, built), (2, None))
            self.assertIn("error: {} is the appliance this build was read from".format(typed), out)
        code, out, built = run_compare(REFERENCE, [renamed()], nepk=lambda site: "1.NE")  # another name, the same appliance
        self.assertEqual((code, built), (2, None))
        self.assertIn("error: br-2 is the appliance this build was read from", out)


def zoned(zone="LAN", host="br-1"):
    ref = copy.deepcopy(REFERENCE)
    ref["applianceInfo"]["hostname"] = host
    ref["deploymentInfo"]["deploymentInterfaces"][0].update(zone=zone, interfaceLabel="Data")
    return ref


def run_compare(reference, others, argv=("--yes",), answers=(), nepk=lambda site: site + ".NE"):
    """cmd_compare on in-memory references (no capture, no translate, no Orchestrator): (exit code, output, build_site call or None)."""
    import yaml
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "build.json").write_text(json.dumps({"site_type": "br", "orchestrator": "lab", "validator": "lab", "capture": "c",
                                                        "target_release": "9.7.1.42046", "appliance": {"hostName": "br-1", "nePk": "1.NE"}}))
        (folder / "reference.yaml").write_text(yaml.safe_dump(reference))
        built = {}

        def build_site(folder, info, name, sheet, rows_for=None):
            built.update(sheet=by_path(sheet), rows_for=rows_for)
            return folder
        names = [o["applianceInfo"]["hostname"] for o in others]
        with mock.patch("capture.capture", side_effect=lambda nick, site, *a: site), \
                mock.patch("translate.Capture", side_effect=lambda c: mock.Mock(run={"appliance": {"nePk": nepk(c)}})), \
                mock.patch("translate.build", side_effect=[copy.deepcopy(o) for o in others]), \
                mock.patch("translate.prune", side_effect=lambda value, allowed, ledger: value), \
                mock.patch.object(cli, "build_site", side_effect=build_site), mock.patch.object(cli, "ensure_sites_csv"), \
                mock.patch.object(cli, "validate_rows", return_value=0), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(out):
            code = cli.main(["compare", tmp] + names + list(argv)) if not answers else cli.cmd_compare([tmp] + names + list(argv), scripted(*answers))
        return code, out.getvalue(), built or None


class CompareLayoutChoice(unittest.TestCase):
    """Zone, label, segment and firewall mode that differ between the compared sites are offered as columns (docs/decisions/0018)."""
    def test_a_differing_zone_is_a_ticked_choice_and_an_equal_one_stays_fixed(self):
        rows = by_path(sheet_rows(zoned(), others=[zoned("Guest", "br-2")]))
        zone = rows["deploymentInfo.deploymentInterfaces[0].zone"]
        self.assertEqual((zone["decision"], zone["column"]), ("variable", "lan0_zone"))
        self.assertEqual(rows["deploymentInfo.deploymentInterfaces[0].interfaceLabel"]["decision"], "fixed")  # equal at both sites
        p = cli.Picker(zoned(), list(rows.values()), {})
        self.assertTrue(p.on["deploymentInfo.deploymentInterfaces[0].zone"])  # a choice, ticked, not locked
        same = by_path(sheet_rows(zoned(), others=[zoned("LAN", "br-2")]))
        self.assertEqual(same["deploymentInfo.deploymentInterfaces[0].zone"]["decision"], "fixed")
        self.assertEqual(by_path(sheet_rows(zoned()))["deploymentInfo.deploymentInterfaces[0].zone"]["decision"], "fixed")  # one site: decision 0006

    def test_yes_makes_it_a_column(self):
        code, out, built = run_compare(zoned(), [zoned("Guest", "br-2")])
        self.assertEqual(code, 0)
        self.assertEqual(built["sheet"]["deploymentInfo.deploymentInterfaces[0].zone"]["decision"], "variable")
        self.assertNotIn("Warning", out)

    def test_unticking_keeps_it_fixed_with_a_warning(self):
        import yaml
        from generate import Builder
        base = yaml.safe_load(yaml.safe_dump(zoned()))  # what cmd_compare reads back from reference.yaml, so the numbers match its list
        probe = Builder()
        probe.find_secrets(base)
        rows = [{k: cli.cell(v) for k, v in r.items()} for r in classify(base, probe.secret_paths, None, [zoned("Guest", "br-2")])]
        number = next(r["n"] for r in cli.Picker(base, rows, {}).free if r["path"].endswith("[0].zone"))
        code, out, built = run_compare(zoned(), [zoned("Guest", "br-2")], argv=("--name", "br"), answers=("edit", str(number), "", "y"))
        self.assertEqual(code, 0)
        self.assertEqual(built["sheet"]["deploymentInfo.deploymentInterfaces[0].zone"]["decision"], "fixed")
        self.assertIn("Warning: deploymentInterfaces[lan0].zone stays fixed: br-2 gets br-1's value LAN", out)


def renamed(host="br-2", change=None):
    other = copy.deepcopy(REFERENCE)
    other["applianceInfo"]["hostname"] = host
    if change:
        change(other)
    return other


class CompareShape(unittest.TestCase):
    """A site with settings the base lacks, or without the base's settings, gets no sites.csv row and a Note naming it."""
    def rows_and_notes(self, *others):
        code, out, built = run_compare(REFERENCE, list(others))
        self.assertEqual(code, 0)
        return [r["hostname"] for r in built["rows_for"]], [line for line in out.splitlines() if line.startswith("Note:")]

    def test_an_equal_shape_gets_a_row(self):
        self.assertEqual(self.rows_and_notes(renamed()), (["br-2"], []))

    def test_a_partner_only_route_means_no_row(self):
        extra = renamed(change=lambda o: o["localRoutes"]["staticRoutes"].append({"routeIpSubnet": "198.51.100.0/25", "nextHop": "192.0.2.253"}))
        rows, notes = self.rows_and_notes(extra, renamed("br-3"))
        self.assertEqual(rows, ["br-3"])
        self.assertEqual(len(notes), 1)
        self.assertIn("br-2 has 2 setting(s) br-1 lacks: localRoutes.staticRoutes[2].nextHop, localRoutes.staticRoutes[2].routeIpSubnet", notes[0])
        self.assertIn("sites.csv gets no row for br-2", notes[0])

    def test_a_missing_base_setting_means_no_row_unless_it_is_blank(self):
        def drop(o):
            del o["bgpSystemConfig"]["neighbors"]
        rows, notes = self.rows_and_notes(renamed(change=drop))
        self.assertEqual(rows, [])
        self.assertIn("br-2 lacks 1 setting(s) br-1 has: bgpSystemConfig.neighbors[0].peerIpAddress", notes[0])

        def blank_wan(o):
            del o["deploymentInfo"]["deploymentInterfaces"][2]["ipAddressMask"]  # blank (None) at the base: nothing would be copied
        self.assertEqual(self.rows_and_notes(renamed(change=blank_wan)), (["br-2"], []))

    def test_a_value_that_differs_is_a_column_even_when_another_site_lacks_it(self):
        def hop(o):
            o["localRoutes"]["staticRoutes"][0]["nextHop"] = "203.0.113.2"

        def drop(o):
            del o["localRoutes"]["staticRoutes"][0]["nextHop"]
        code, out, built = run_compare(REFERENCE, [renamed(change=hop), renamed("br-3", drop)])
        self.assertEqual(built["sheet"]["localRoutes.staticRoutes[0].nextHop"]["decision"], "variable")  # br-2's own next hop, not br-1's
        self.assertEqual([r["hostname"] for r in built["rows_for"]], ["br-2"])  # br-3 lacks it: no row

    def test_a_site_without_a_row_decides_no_column(self):
        def other_design(o):
            o["deploymentInfo"]["deploymentInterfaces"][0].update(zone="Guest", interfaceLabel="Data")
            o["localRoutes"]["staticRoutes"].append({"routeIpSubnet": "198.51.100.0/25", "nextHop": "192.0.2.253"})
        code, out, built = run_compare(zoned(), [renamed(change=other_design), zoned(host="br-3")])
        self.assertEqual([r["hostname"] for r in built["rows_for"]], ["br-3"])
        self.assertEqual(built["sheet"]["deploymentInfo.deploymentInterfaces[0].zone"]["decision"], "fixed")  # differs only at br-2, which gets no row

    def test_dhcp_relay_against_server_is_named(self):
        def relay(o):
            o["deploymentInfo"]["dhcpInfo"][0] = {"dhcpInterfaceName": "lan0", "dhcpType": "relay", "dhcpProxyServers": ["192.0.2.50"]}
        rows, notes = self.rows_and_notes(renamed(change=relay))
        self.assertEqual(rows, [])
        self.assertIn("br-2 uses DHCP relay on lan0, br-1 uses server", notes[0])


class CompareEqualAddresses(unittest.TestCase):
    def test_an_in_subnet_address_equal_at_every_site_stays_fixed(self):
        hop, peer = "localRoutes.staticRoutes[1].nextHop", "bgpSystemConfig.neighbors[0].peerIpAddress"  # both 192.0.2.254, inside lan0's subnet
        same = by_path(sheet_rows(others=[renamed(), renamed("br-3")]))
        self.assertEqual([(same[p]["decision"], same[p]["reason"]) for p in (hop, peer)], [("fixed", "same at every site you gave")] * 2)
        self.assertEqual(same["deploymentInfo.deploymentInterfaces[0].ipAddressMask"]["decision"], "variable")  # its own address stays a column

        def dhcp_start(o):
            o["deploymentInfo"]["dhcpInfo"][0]["startIpAddress"] = "192.0.2.10"
        deployment = by_path(sheet_rows(renamed("br-1", dhcp_start), others=[renamed(change=dhcp_start)]))
        self.assertEqual(deployment["deploymentInfo.dhcpInfo[0].startIpAddress"]["decision"], "variable")  # Deployment page: always per site (0006)

        def moved(o):
            o["localRoutes"]["staticRoutes"][1]["nextHop"] = "192.0.2.253"
        differs = by_path(sheet_rows(others=[renamed(), renamed("br-3", moved)]))
        self.assertEqual(differs[hop]["decision"], "variable")
        self.assertEqual(by_path(sheet_rows())[hop]["decision"], "variable")  # one site: the address rule as before


class CompareLinks(unittest.TestCase):
    """A row linked to another column at this site follows it only when the link holds at every site given."""
    def test_a_gateway_that_stays_while_the_interface_moves_gets_its_own_column(self):
        def vrrp_style(o):
            o["deploymentInfo"]["deploymentInterfaces"][0]["ipAddressMask"] = "192.0.2.2/24"  # the gateway stays 192.0.2.1
        gw = by_path(sheet_rows(others=[renamed(change=vrrp_style)]))["deploymentInfo.dhcpInfo[0].gatewayIpAddress"]
        self.assertEqual((gw["decision"], gw["linked_to"]), ("variable", ""))
        self.assertNotEqual(gw["column"], "lan0_ip")
        held = by_path(sheet_rows(others=[renamed()]))["deploymentInfo.dhcpInfo[0].gatewayIpAddress"]
        self.assertEqual(held["linked_to"], "lan0_ip")  # the link holds at br-2: one column

    def test_two_equal_next_hops_that_differ_at_another_site_are_two_columns(self):
        def split(o):
            o["localRoutes"]["staticRoutes"][1]["nextHop"] = "192.0.2.253"
            o["bgpSystemConfig"]["neighbors"][0]["peerIpAddress"] = "192.0.2.252"
        rows = by_path(sheet_rows(others=[renamed(change=split)]))
        hop, peer = rows["localRoutes.staticRoutes[1].nextHop"], rows["bgpSystemConfig.neighbors[0].peerIpAddress"]
        self.assertEqual((peer["decision"], hop["decision"]), ("variable", "variable"))
        self.assertNotEqual(peer["column"], hop["column"])
        self.assertEqual("", hop["linked_to"] + peer["linked_to"])


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
        self.assertNotIn("cells", cli.stages())  # libraries without main() are not commands
        self.assertNotIn("synth", cli.stages())

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
        with mock.patch("common.VALIDATORS", {"lab"}), mock.patch("capture.capture") as cap:
            with self.assertRaises(cli.Fail) as stop:
                cli.cmd_build(["--orchestrator", "fieldref", "--appliance", "x"])
        self.assertIn("EC_VALIDATORS", str(stop.exception))
        cap.assert_not_called()

    def test_nothing_is_kept_when_validation_fails(self):
        with tempfile.TemporaryDirectory() as tmp, self.fake_capture(tmp), mock.patch("common.VALIDATORS", {"lab"}), \
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
        with tempfile.TemporaryDirectory() as tmp, self.fake_capture(tmp), mock.patch("common.VALIDATORS", {"lab"}), \
                mock.patch.object(cli, "translate_into", side_effect=translate_into), mock.patch.object(cli, "checked", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            out = Path(tmp) / "out"
            (out / "Br_1").mkdir(parents=True)
            self.assertEqual(cli.cmd_build(["--orchestrator", "lab", "--appliance", "Br 1", "--out", str(out)]), 0)
            info = json.loads((out / "Br_1" / "build.json").read_text())
            self.assertEqual((info["orchestrator"], info["validator"], info["site_type"]), ("lab", "lab", "br_1"))
            self.assertEqual(len(list(out.glob("Br_1.previous-*"))), 1)
            self.assertEqual(list(out.glob(".building-*")), [])

    def test_build_rejects_a_malformed_target_ecos_before_reading(self):
        with mock.patch("common.VALIDATORS", {"lab"}), mock.patch("capture.capture", side_effect=SystemExit("read")) as cap, contextlib.redirect_stderr(io.StringIO()) as err:
            for bad in ("abc", "", "latest", "9"):
                with self.assertRaises(SystemExit, msg=bad):
                    cli.cmd_build(["--orchestrator", "lab", "--appliance", "x", "--target-ecos", bad])
        cap.assert_not_called()
        self.assertIn("not an ECOS release", err.getvalue())

    INFO = {"orchestrator": "lab", "validator": "lab", "capture": "cap", "target_release": "9.7.1.42046"}
    ECOS_CASES = (({"target_ecos": "9.6.2.1"}, "9.6.2.1"), ({"target_ecos": None}, None), ({}, None))  # build.json: present, null, key absent

    def test_build_site_passes_target_ecos_from_build_json(self):
        for extra_info, ecos in self.ECOS_CASES:
            calls = []

            def translate_into(out, capture, site, release, extra):
                calls.append(extra)
                raise cli.Fail("stop")
            with self.subTest(ecos=ecos, info=extra_info), tempfile.TemporaryDirectory() as tmp, mock.patch.object(cli, "translate_into", side_effect=translate_into):
                with self.assertRaises(cli.Fail):
                    cli.build_site(Path(tmp), dict(self.INFO, **extra_info), "s1", [])
                self.assertEqual(calls[0][2:], ["--target-ecos", ecos] if ecos else [])

    def test_compare_builds_the_other_sites_with_target_ecos_from_build_json(self):
        import translate
        for extra_info, ecos in self.ECOS_CASES:
            seen = []

            def build(cap, ledger, target_ecos=None):
                seen.append(target_ecos)
                raise translate.NothingToConvert("stop")
            with self.subTest(ecos=ecos, info=extra_info), tempfile.TemporaryDirectory() as tmp, mock.patch("capture.capture", return_value="cap2"), \
                    mock.patch.object(translate, "build", side_effect=build), mock.patch.object(translate, "Capture", side_effect=lambda folder: folder), \
                    contextlib.redirect_stdout(io.StringIO()):
                (Path(tmp) / "build.json").write_text(json.dumps(dict(self.INFO, **extra_info)))
                with self.assertRaises(cli.Fail):
                    cli.cmd_compare([tmp, "site2", "--yes"])
                self.assertEqual(seen, [ecos])

    def test_target_ecos_reaches_translate_and_build_json(self):
        calls = []

        def translate_into(out, capture, site, release, extra):
            calls.append(extra)
            out.mkdir(parents=True)
            return "verdict: Complete for preconfig."
        with tempfile.TemporaryDirectory() as tmp, self.fake_capture(tmp), mock.patch("common.VALIDATORS", {"lab"}), \
                mock.patch.object(cli, "translate_into", side_effect=translate_into), mock.patch.object(cli, "checked", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            out = Path(tmp) / "out"
            cli.cmd_build(["--orchestrator", "lab", "--appliance", "Br 1", "--out", str(out), "--target-ecos", "9.6.2.1"])
            self.assertEqual(json.loads((out / "Br_1" / "build.json").read_text())["target_ecos"], "9.6.2.1")
            cli.cmd_build(["--orchestrator", "lab", "--appliance", "Br 1", "--out", str(out)])
        self.assertEqual(calls, [["--target-ecos", "9.6.2.1"], []])

    def test_build_lists_both_csv_files_and_what_each_is_for(self):
        def translate_into(out, capture, site, release, extra):
            out.mkdir(parents=True)
            return "verdict: Complete for preconfig."
        with tempfile.TemporaryDirectory() as tmp, self.fake_capture(tmp), mock.patch("common.VALIDATORS", {"lab"}), \
                mock.patch.object(cli, "translate_into", side_effect=translate_into), mock.patch.object(cli, "checked", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            cli.cmd_build(["--orchestrator", "lab", "--appliance", "Br 1", "--out", str(Path(tmp) / "out")])
        lines = printed.getvalue().splitlines()
        self.assertIn("  br_1.csv            empty sheet (headers only) for new sites", lines)
        self.assertIn("  example.csv         this appliance's own values, secrets blank - copy it to start your sites.csv", lines)


if __name__ == "__main__":
    unittest.main()

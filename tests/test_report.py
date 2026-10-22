import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "building-edgeconnect-preconfig-templates" / "scripts"))
import translate  # noqa: E402
from common import SKILL_DIR, toolkit_version  # noqa: E402

DEP = {"sysConfig": {"ifLabels": {"lan": [], "wan": []}, "vrfs": [{"id": "0", "name": "Default"}], "vrfZonesMap": {}, "roles": []}, "modeIfs": [], "mgmtIfData": {}}
APP = {"hostName": "br-1", "nePk": "9.NE", "networkRole": "0", "site": "s", "groupId": "1.Network"}


class FakeCap:
    folder = "fake"
    run = {"appliance": {"nePk": "9.NE", "hostName": "br-1"}, "orchestrator": "lab", "release": "9.7.1.42046"}
    index = []

    def data(self, ident, prefer="live"):
        return {"appliance": APP, "deployment": DEP}.get(ident)


def entry(disposition, target="", item="x", note="n"):
    return {"item": item, "disposition": disposition, "target": target, "note": note}


class Report(unittest.TestCase):
    def test_unresolved_without_a_preconfig_section_is_outside_preconfig(self):
        ledger = [entry("unresolved", ""), entry("unresolved", "poE,usblte"), entry("unresolved", "ecLicensing.bandwidthLevel"), entry("unresolved", "securityPolicies"),
                  entry("unresolved", "deploymentInfo.deploymentInterfaces[0].raInfo")]
        translate.scope_by_template(ledger, {"poE", "usblte", "ecLicensing", "deploymentInfo"})
        self.assertEqual([e["disposition"] for e in ledger], ["outside-preconfig", "unresolved", "unresolved", "outside-preconfig", "unresolved"])
        self.assertIn("has no securityPolicies section", ledger[3]["note"])

    def test_default_only_nat_data_is_not_content(self):
        catch_all = {"data": {"map1": {"prio": {"65535": {"match": {"0": {"acl": ""}}, "set": {"0": {"nat_type": "no-nat"}}}}}}}
        self.assertTrue(translate.DEFAULT_ONLY["nat-maps"](catch_all))
        self.assertFalse(translate.DEFAULT_ONLY["nat-maps"]({"data": {"map1": {"prio": {"100": {"set": {"0": {"nat_type": "source-nat"}}}}}}}))
        self.assertFalse(translate.DEFAULT_ONLY["nat-maps"]({"data": {"map1": {"prio": {"65535": {"set": {"0": {"nat_type": "source-nat"}}}}}}}))
        self.assertTrue(translate.DEFAULT_ONLY["nat-all"]({"inbound": {"enable": False}, "outbound": {"enable": False}}))
        self.assertFalse(translate.DEFAULT_ONLY["nat-all"]({"inbound": {"enable": False}, "outbound": {"enable": True}}))

    def test_a_mapper_crash_is_a_toolkit_error_not_a_gap(self):
        def boom(ctx):
            raise KeyError("prio")
        ledger = []
        with mock.patch.dict(translate.MAPPERS, {"vxlan": boom}):
            out = translate.build(FakeCap(), ledger)
        self.assertNotIn("vxlan", out)
        self.assertEqual([e["target"] for e in ledger if e["disposition"] == "toolkit-error"], ["vxlan"])

    def test_summary_states_the_version_and_groups_every_entry(self):
        ledger = [entry("toolkit-error", "vxlan"), entry("unresolved", "multicast"), entry("outside-preconfig"), entry("template-group", "nac"), entry("brand-new", "z"),
                  entry("mapped", "ecLicensing", note="useDefaultAccount true is an assumption"), entry("runtime")]
        text = translate.summary_md(ledger, {"applianceInfo": {}}, {"orchestrator": "lab", "release": "9.7.1.42046", "appliance": {"nePk": "9.NE", "hostName": "br-1"}}, "9.7.1.42046")
        self.assertIn("Toolkit: " + toolkit_version(), text)
        self.assertIn("refresh", text)
        for heading in ("Toolkit errors", "Gaps", "Outside preconfig", "Owned elsewhere", "Unknown disposition", "Mapped on an assumption"):
            self.assertIn("## " + heading, text)
        self.assertIn("`x` -> `z`", text)  # an unknown disposition is shown, never hidden
        self.assertIn("1 entries that are mapped, default, runtime state or context", text)

    def test_running_config_is_a_gap_only_for_sections_it_holds(self):
        class Cap(FakeCap):
            def __init__(self, text):
                self.index = [{"id": "running-config", "label": "backup", "status": "ok", "file": "x"}]
                self.text = text

            def data(self, ident, prefer="live"):
                return {"running-config": {"runningConfig": self.text}}.get(ident)
        for text, expected in (("hostname br-1\nno interface blan0 poe\n", []), ("interface lan0 poe detection 802.af_Legacy\n", ["poE"])):
            ledger = []
            translate.tag_unmapped(Cap(text), ledger)
            self.assertEqual([e["target"] for e in ledger if e["item"] == "running-config"], [",".join(expected)] if expected else [])

    def test_a_refused_read_is_reported_to_check_by_hand(self):
        class Cap(FakeCap):
            index = [{"id": "static-routes-configured", "path": "/appliance/rest", "query": {"nePk": "9.NE", "url": "subnets3/configured"}, "label": "live",
                      "status": "denied", "http": 403, "sections": ["localRoutes", "segmentLocalRoutes"]}]

            def data(self, ident, prefer="live"):
                return None
        ledger = []
        translate.tag_unmapped(Cap(), ledger)
        refused = [e for e in ledger if e["item"] == "static-routes-configured"]
        self.assertEqual([(e["disposition"], e["target"]) for e in refused], [("not-readable", "localRoutes,segmentLocalRoutes")])
        self.assertIn("url=subnets3/configured (HTTP 403)", refused[0]["note"])
        self.assertEqual(translate.verdict(refused), "Complete for preconfig; 1 item(s) to check by hand.")

    def test_an_appliance_with_nothing_to_convert_stops_with_one_line(self):
        import contextlib
        import io
        err = io.StringIO()
        with mock.patch.object(translate, "Capture", return_value=FakeCap()), \
                mock.patch.object(translate, "build", side_effect=ValueError("capture 'x' lacks appliance or deployment.sysConfig data")), \
                contextlib.redirect_stderr(err):
            self.assertEqual(translate.main(["--capture", "x", "--site-type", "t", "--out", "/nonexistent-out"]), 2)
        self.assertIn("lacks appliance", err.getvalue())

    def test_business_hours_are_checked_by_hand_not_a_gap(self):
        text = translate.summary_md([entry("not-readable", "availabilityTimeSettings")], {}, {"appliance": {}}, "9.7.1.42046")
        self.assertIn("## Not readable", text)
        self.assertIn("## Gaps: the preconfig format has a place for it, but the value was not carried (0)", text)

    def test_cached_default_templates_are_not_local_edits(self):
        import common
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp)
            (skill / "references" / "default-templates").mkdir(parents=True)
            (skill / "scripts").mkdir()
            (skill / "VERSION").write_text("9.9.9\n")
            (skill / "references" / "default-templates" / "orchestrator-9.7.1.yml").write_text("applianceInfo: {}\n")  # bundled
            git = ["git", "-C", tmp, "-c", "user.name=t", "-c", "user.email=t@example.com"]
            subprocess.run(git + ["init", "-q"], check=True)
            subprocess.run(git + ["add", "-A"], check=True)
            subprocess.run(git + ["commit", "-q", "-m", "base"], check=True)
            with mock.patch.object(common, "SKILL_DIR", skill):
                (skill / "references" / "default-templates" / "orchestrator-10.0.0.1.yml").write_text("applianceInfo: {}\n")  # what capture.py caches
                self.assertNotIn("LOCAL EDITS", common.toolkit_version())
                (skill / "references" / "default-templates" / "orchestrator-9.7.1.yml").write_text("applianceInfo: {edited: true}\n")
                self.assertIn("LOCAL EDITS", common.toolkit_version())  # an edited bundled template is an edit
                subprocess.run(git + ["checkout", "-q", "--", "."], check=True)
                (skill / "scripts" / "mine.py").write_text("x = 1\n")
                self.assertIn("LOCAL EDITS", common.toolkit_version())

    def test_summary_ends_with_the_verdict(self):
        src = {"appliance": {}}
        self.assertIn("Verdict: Complete for preconfig.", translate.summary_md([entry("mapped")], {}, src, "r"))
        self.assertIn("Verdict: Complete for preconfig; 1 item(s) to check by hand.", translate.summary_md([entry("not-readable")], {}, src, "r"))
        text = translate.summary_md([entry("unresolved"), entry("unmapped-source-key"), entry("brand-new"), entry("toolkit-error")], {}, src, "r")
        self.assertIn("Verdict: Incomplete for preconfig: 3 gap(s), 1 toolkit error(s).", text)
        self.assertTrue(text.rstrip().endswith("Verdict: Incomplete for preconfig: 3 gap(s), 1 toolkit error(s)."))  # SKILL.md: summary.md ends with it

    def test_unmapped_keys_of_a_section_the_release_lacks_are_outside_preconfig(self):
        ledger = [entry("unmapped-source-key", "multicast"), entry("unmapped-source-key", "vrrp")]
        translate.scope_by_template(ledger, {"vrrp"})
        self.assertEqual([e["disposition"] for e in ledger], ["outside-preconfig", "unmapped-source-key"])

    def test_names_only_nat_policy_map_does_not_crash(self):
        self.assertTrue(translate.DEFAULT_ONLY["nat-maps"]({"data": {"map1": "map1"}}))

    def test_a_tunnel_the_mapper_cannot_read_is_a_toolkit_error(self):
        from mappers import Ctx, m_tunnelConfiguration
        notes = []
        cap = FakeCap()
        cap.data = lambda ident, prefer="live": {"passthrough-tunnels": {"t1": {"alias": "odd"}}, "deployment": DEP}.get(ident)
        self.assertIsNone(m_tunnelConfiguration(Ctx(cap, notes)))
        self.assertEqual([n["disposition"] for n in notes if n["item"].startswith("passthrough-tunnels")], ["toolkit-error"])

    def test_toolkit_version_starts_with_the_version_file(self):
        self.assertTrue(toolkit_version().startswith((SKILL_DIR / "VERSION").read_text().strip()))


if __name__ == "__main__":
    unittest.main()

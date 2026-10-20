#!/usr/bin/env python3
"""Measure what /validate checks: send deliberately constructed YAML to a validator Orchestrator and record
accept/reject with the error text. Synthetic values and test-pcfg- names only. Non-saving.

Writes <work>/test-runs/<UTC>-probe/probes.json and prints a table. Run at build time and after a validator upgrade.
"""
import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "building-edgeconnect-preconfig-templates" / "scripts"))
from common import DEFAULT_TEMPLATES_DIR, WORK_DIR, client_for, utc_stamp, write_json  # noqa: E402
from validate import PATH, Stop, validate_text, validator  # noqa: E402

DEFAULTS = DEFAULT_TEMPLATES_DIR
MINIMAL = "applianceInfo:\n  hostname: test-pcfg-probe\n"


def deployment(lan_label: str, wan_label: str, segment: str = "Default", zone: str = "") -> str:
    return MINIMAL + """deploymentInfo:
  deploymentMode: inline-router
  totalOutboundBandwidth: 200000
  totalInboundBandwidth: 200000
  deploymentInterfaces:
    - interfaceName: lan0
      interfaceLabel: {lan}
      interfaceType: lan
      ipAddressMask: 192.0.2.1/24
      segment: {segment}
      zone: {zone}
    - interfaceName: wan0
      interfaceLabel: {wan}
      interfaceType: wan
      ipAddressMask: 198.51.100.2/24
      nextHop: 198.51.100.1
      outboundMaxBandwidth: 5000
      inboundMaxBandwidth: 5000
      firewallMode: statefulSNAT
""".format(lan=lan_label, wan=wan_label, segment=segment, zone=zone)


def probes(client: Any) -> List[Dict[str, Any]]:
    labels = client.get("/gms/interfaceLabels")
    lan = next(v["name"] for v in labels.get("lan", {}).values() if v.get("active"))
    wan = next(v["name"] for v in labels.get("wan", {}).values() if v.get("active"))
    group = client.get("/template/templateGroups")
    existing_group = group[0]["name"] if isinstance(group, list) and group else "Default Template Group"
    missing = "test-pcfg-does-not-exist"
    rows = [
        ("P00", "9.7.1 default template as-is", (DEFAULTS / "orchestrator-9.7.1.42046.yml").read_text(), None, "accept"),
        ("P01", "minimal: applianceInfo.hostname only", MINIMAL, None, "accept"),
        ("P02", "empty document", "", None, "measure"),
        ("P03", "broken YAML syntax", "applianceInfo:\n  hostname: [unclosed\n", None, "reject"),
        ("P04", "unknown top-level section", MINIMAL + "notASection:\n  a: 1\n", None, "reject"),
        ("P05", "unknown nested field", MINIMAL + "  notAField: 1\n", None, "reject"),
        ("P06", "wrong type: totalOutboundBandwidth as text", MINIMAL + "deploymentInfo:\n  deploymentMode: inline-router\n  totalOutboundBandwidth: abc\n", None, "reject"),
        ("P07", "malformed serial number", MINIMAL, "XYZ", "reject"),
        ("P08", "well-formed serial number", MINIMAL, "000011112222", "accept"),
        ("P09", "missing required field: applianceInfo without hostname", "applianceInfo:\n  site: test-pcfg-site\n", None, "measure"),
        ("P10", "templateGroups: group that does not exist", MINIMAL + "templateGroups:\n  groups:\n    - {}\n".format(missing), None, "measure"),
        ("P11", "templateGroups: existing group ({})".format(existing_group), MINIMAL + "templateGroups:\n  groups:\n    - {}\n".format(existing_group), None, "accept"),
        ("P12", "applianceInfo.group that does not exist", MINIMAL + "  group: {}\n".format(missing), None, "measure"),
        ("P13", "overlay that does not exist", MINIMAL + "businessIntentOverlays:\n  overlays:\n    - {}\n".format(missing), None, "measure"),
        ("P14", "deployment with existing labels ({} / {})".format(lan, wan), deployment(lan, wan), None, "accept"),
        ("P15", "deployment with LAN label that does not exist", deployment(missing, wan), None, "measure"),
        ("P16", "deployment with segment that does not exist", deployment(lan, wan, segment=missing), None, "measure"),
        ("P17", "deployment with zone that does not exist", deployment(lan, wan, zone=missing), None, "measure"),
        ("P18", "9.6.4 default template as-is (nac.server[].address is host from 9.7)", (DEFAULTS / "orchestrator-9.6.4.40046.yml").read_text(), None, "reject"),
        ("P19", "10.0 default template as-is (has ikePqcgroup, ipsecPqcgroup, flowProcess)", (DEFAULTS / "orchestrator-10.0.0.40516.yml").read_text(), None, "measure"),
    ]
    results = []
    for ident, what, text, serial, expect in rows:
        result = validate_text(client, text, serial)
        outcome = "accept" if result["accepted"] else "reject"
        results.append(dict(result, id=ident, probe=what, serial=serial, expect=expect, outcome=outcome,
                            matches=None if expect == "measure" else expect == outcome, yaml=text if len(text) < 2000 else "<default template, {} lines>".format(text.count("\n"))))
    return results


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orchestrator", required=True)
    args = parser.parse_args(argv)
    try:
        client = validator(args.orchestrator)
        results = probes(client)
    except Stop as stop:
        print("STOPPED: {}".format(stop), file=sys.stderr)
        return 2
    release = client.get("/gms/versions").get("current")
    out = WORK_DIR / "test-runs" / "{}-probe".format(utc_stamp()) / "probes.json"
    write_json(out, {"orchestrator": args.orchestrator, "release": release, "write_requests": client.write_requests, "results": results})
    for r in results:
        flag = "" if r["matches"] is None else ("  ok" if r["matches"] else "  UNEXPECTED")
        print("{} {:7} {}{}{}".format(r["id"], r["outcome"].upper(), r["probe"], flag, "" if r["accepted"] else "\n      " + (r["error"] or "")[:220]))
    print("release {} write_requests {}\n{}".format(release, client.write_requests, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

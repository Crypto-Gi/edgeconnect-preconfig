#!/usr/bin/env python3
"""Feature scan: which appliances of an Orchestrator use which preconfig-related features (GET only, cached reads).

Writes <work>/samples/<orchestrator>/scan.json: per reachable appliance, per catalogue endpoint, whether the
cached read returned data. Used to choose a varied corpus. Counts only; no configuration values are stored.
"""
import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "toolkit" / "scripts"))
from common import CATALOGUE, WORK_DIR, client_for, load_yaml, utc_stamp, write_json  # noqa: E402

SCAN_IDS = ["deployment", "bgp-system", "bgp-neighbors", "ospf-system", "ospf-interfaces", "vrrp", "loopbacks", "vtis", "pppoe", "port-forwarding",
            "ipsla-managers", "nat-pools", "nat-maps", "bridge-groups", "link-aggregation", "passthrough-tunnels", "multicast-config", "secure-web-services",
            "bfd-vrfs", "peer-priority", "custom-tags", "vxlan", "dhcp-remote-subnets", "sse-interfaces", "nac"]


def size(value: Any) -> int:
    if isinstance(value, (dict, list)):
        return len(value)
    return 0 if value in (None, "", False) else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orchestrator", required=True)
    parser.add_argument("--pause", type=float, default=0.1)
    args = parser.parse_args(argv)
    client = client_for(args.orchestrator)
    catalogue = {e["id"]: e for e in load_yaml(CATALOGUE)["endpoints"]}
    apps = [a for a in client.get("/appliance") if int(a.get("state", 2)) == 1]
    result: Dict[str, Any] = {}
    for a in apps:
        nepk = str(a.get("nePk") or a.get("id"))
        row: Dict[str, Any] = {"hostName": a.get("hostName"), "model": a.get("model"), "software": a.get("softwareVersion"), "networkRole": a.get("networkRole")}
        for ident in SCAN_IDS:
            e = catalogue[ident]
            query = {"nePk": nepk}
            if e["source"] in ("cached", "fromGms"):
                query[e["source"]] = "true"
            try:
                body = client.request("GET", e["path"], query=query, expect_json=False)
                row[ident] = size(json.loads(body.decode())) if body else 0
            except Exception as error:
                name = type(error).__name__
                if name in ("AuthenticationError", "PermissionDeniedError", "TransportError"):
                    print("STOPPED", name); return 2
                row[ident] = -1
            time.sleep(args.pause)
        result[nepk] = row
        print(nepk, row["hostName"], {k: v for k, v in row.items() if isinstance(v, int) and v > 0})
    assert client.write_requests == 0
    write_json(WORK_DIR / "samples" / args.orchestrator / "scan.json", {"time": utc_stamp(), "appliances": result})
    return 0


if __name__ == "__main__":
    sys.exit(main())

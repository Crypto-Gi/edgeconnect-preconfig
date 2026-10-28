#!/usr/bin/env python3
"""Stage 9: send rendered preconfig YAML to POST /gms/appliance/preconfiguration/validate on a validator Orchestrator.

Non-saving. Refuses any Orchestrator not in VALIDATORS before connecting. Never calls create, update, delete,
apply or findMatch. Writes validation.json next to the files (or to --out).
"""
import argparse
import base64
import inspect
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import VALIDATORS, client_for, normalize_serial, parse_yaml, utc_stamp, write_json  # noqa: E402

PATH = "/gms/appliance/preconfiguration/validate"


class Stop(Exception):
    pass


def validator(nickname: str) -> Any:
    if nickname not in VALIDATORS:
        raise Stop("{} is not a validator Orchestrator (allowed: {}). To allow it, add it to EC_VALIDATORS in .env "
                   "(comma-separated nicknames).".format(nickname, ", ".join(sorted(VALIDATORS)) or "none"))
    client = client_for(nickname)
    if "read_only" not in inspect.signature(client.request).parameters:
        raise Stop("edgeconnect_automation ApiClient.request has no read_only flag; use a checkout that includes it (branch fix/safety-sweep)")
    return client


def apply_time_error(text: str) -> Optional[str]:
    """Rules /validate accepts but /apply fails (references/known-constraints.yaml, enforced_by: apply), and rules /validate answers only with HTTP 500."""
    if "<SECRET:" in text:  # a redaction placeholder copied from reference.yaml would be applied as the literal secret
        return "contains a <SECRET:...> redaction placeholder: put the real value in the CSV secret column"
    try:
        doc = parse_yaml(text)
    except Exception:
        return None  # malformed YAML: let the Orchestrator report it
    if isinstance(doc, dict) and doc.get("branchNatMapsConfig") and not doc.get("deploymentInfo"):
        return "branchNatMapsConfig needs deploymentInfo in the same preconfig (apply fails: DeploymentInfo cannot be null when branchNatMapsConfig is configured)"
    nat_rules = ((doc.get("branchNatMapsConfig") or {}).get("branchNatRules") or []) if isinstance(doc, dict) else []
    wan_ifs = {i.get("interfaceName") for i in ((doc.get("deploymentInfo") or {}).get("deploymentInterfaces") or []) if isinstance(i, dict) and i.get("interfaceType") == "wan"} if isinstance(doc, dict) else set()
    for r in nat_rules if isinstance(nat_rules, list) else []:
        name = str((r or {}).get("interfaceName") or "")
        if name in wan_ifs or name.startswith("wan"):
            return "branch NAT rule {}: interfaceName {} is a WAN interface (/validate accepts it, apply fails: LAN interface not found in deployment configuration); use a LAN interface or any".format(r.get("priorityLevel"), name)
        if "." in name:
            return "branch NAT rule {}: interfaceName {} is a VLAN subinterface (/validate accepts it, apply fails the whole branchNatMapsConfig: LAN interface not found in deployment configuration)".format(r.get("priorityLevel"), r.get("interfaceName"))
    tunnels = ((doc.get("tunnelConfiguration") or {}).get("tunnels") or []) if isinstance(doc, dict) else []
    for t in tunnels if isinstance(tunnels, list) else []:
        ike = (t or {}).get("ikeConfig") or {}
        if t.get("tunnelEncap") == "no_encap" and not t.get("remoteIp"):
            return "tunnel {}: a no_encap tunnel needs remoteIp (/validate accepts it, apply on ECOS 9.6.2.1 fails the whole tunnel batch with HTTP 500)".format(t.get("tunnelName"))
        if "ipsec" in str(t.get("tunnelEncap")) and len(str(ike.get("presharedKey") or "")) < 8:
            return "tunnel {}: presharedKey needs at least 8 characters (apply fails: Preshared Key must contain at least 8 characters)".format(t.get("tunnelName"))
        if str(ike.get("ikeVersion")) == "2" and not (ike.get("localIkeIdentifier") and ike.get("remoteIkeIdentifier")):
            return "tunnel {}: IKEv2 needs localIkeIdentifier and remoteIkeIdentifier (/validate answers HTTP 500 without them)".format(t.get("tunnelName"))
    return None


def validate_text(client: Any, text: str, serial: Optional[str] = None) -> Dict[str, Any]:
    apply_error = apply_time_error(text)
    if apply_error:
        return {"accepted": False, "http": None, "error": apply_error}
    body: Dict[str, Any] = {"configData": base64.b64encode(text.encode("utf-8")).decode("ascii")}
    if serial:
        try:
            body["serialNum"] = normalize_serial(serial)
        except ValueError as err:
            return {"accepted": False, "http": 400, "error": str(err)}
    try:
        client.request("POST", PATH, json_body=body, expected_status=(200, 204), expect_json=False, read_only=True)
        return {"accepted": True, "http": 200, "error": None}
    except Exception as error:
        name, status = type(error).__name__, getattr(error, "status", None)
        # HTTP 400 rejects the YAML; HTTP 5xx is also a per-document result (e.g. nac with an empty sub-list). Neither is a failure.
        if name in ("ValidationError", "ServerError"):
            return {"accepted": False, "http": status or 400, "error": str(error)[:1000]}
        raise Stop("{} (HTTP {}) from {}: {}".format(name, status, PATH, str(error)[:300])) from None
    finally:
        if client.write_requests:
            raise Stop("write counter is {}; validation must not count as a write".format(client.write_requests))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orchestrator", required=True)
    parser.add_argument("--out", type=Path, help="validation.json path (default: next to the first file's folder)")
    parser.add_argument("files", nargs="*", type=Path, help="rendered YAML files: /validate only")
    parser.add_argument("--yaml", nargs="+", type=Path, metavar="FILE|DIR", help="existing preconfig YAML files or folders: the checks upload --yaml makes, without uploading")
    parser.add_argument("--name", help="with --yaml and one file: the preconfig name checked for conflicts")
    parser.add_argument("--serial", help="with --yaml and one file: the serial number validated and checked for conflicts")
    args = parser.parse_args(argv)
    if args.yaml:
        from upload import yaml_main
        return yaml_main(parser, args.yaml + args.files, args.orchestrator, args.name, args.serial, False, validator)  # never writes
    if not args.files:
        parser.error("give rendered files, or --yaml FILE|DIR")
    try:
        client = validator(args.orchestrator)
        results = []
        for path in args.files:
            result = validate_text(client, path.read_text(encoding="utf-8"))
            results.append(dict(result, file=str(path)))
            print("{} {}{}".format("ACCEPTED" if result["accepted"] else "REJECTED", path.name, "" if result["accepted"] else ": " + result["error"]))
    except Stop as stop:
        print("STOPPED: {}".format(stop), file=sys.stderr)
        return 2
    release = client.get("/gms/versions").get("current")
    out = args.out or args.files[0].parent.parent / "validation.json"
    write_json(out, {"orchestrator": args.orchestrator, "release": release, "time": utc_stamp(), "endpoint": PATH,
                     "checks": "YAML syntax, field names, value types, serial format (Swagger); see references/validate-behaviour.md",
                     "results": results, "write_requests": client.write_requests})
    return 0 if all(r["accepted"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

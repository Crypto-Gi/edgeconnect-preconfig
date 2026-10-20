#!/usr/bin/env python3
"""Closed-loop preconfig verifier on an isolated virtual lab Orchestrator (*vlab* profile ONLY):
  1. Guardrail check: target profile must include 'vlab'; wan0 must stay dhcpv4 at 100000/100000 Kbps; mgmt0 untouched.
  2. Validate YAML via POST /gms/appliance/preconfiguration/validate.
  3. Create stored preconfig via POST /gms/appliance/preconfiguration (autoApply=False).
  4. Apply to managed appliance (--nepk) or discovered appliance (--discovered-id) and poll until completion.
  5. Re-capture the appliance (capture.py) and translate it back to YAML (translate.py).
  6. Diff every section/path of the intended YAML against the post-apply translated YAML.
  7. Delete the temporary stored preconfig entry from Orchestrator (unless --keep-preconfig).
"""
import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "building-edgeconnect-preconfig-templates" / "scripts"))
from common import SKILL_DIR, WORK_DIR, client_for, normalize_serial, parse_yaml, paths_of, redact_secrets, utc_stamp, write_json  # noqa: E402
from validate import validate_text  # noqa: E402


class GuardrailError(Exception):
    pass


def check_guardrails(orchestrator: str, doc: Dict[str, Any]) -> None:
    if "vlab" not in orchestrator.strip().lower():
        raise GuardrailError("apply_and_diff requires a *vlab* Orchestrator profile; refused {!r}".format(orchestrator))
    if "managementIp" in doc:
        raise GuardrailError("refusing preconfig with managementIp section: mgmt0 is DHCP-assigned and must stay untouched")
    dep = doc.get("deploymentInfo")
    if isinstance(dep, dict):
        ifs = dep.get("deploymentInterfaces") or []
        wan0 = [i for i in ifs if str(i.get("interfaceName", "")).startswith("wan0")]
        if not wan0:
            raise GuardrailError("deploymentInfo.deploymentInterfaces must keep wan0 (wan0.4000 DHCP)")
        for w in wan0:
            if w.get("addressingMode") != "dhcpv4":
                raise GuardrailError("wan0 ({}) must keep addressingMode: dhcpv4, got {!r}".format(w.get("interfaceName"), w.get("addressingMode")))
            if w.get("inboundMaxBandwidth") != 100000 or w.get("outboundMaxBandwidth") != 100000:
                raise GuardrailError("wan0 ({}) must keep inbound/outboundMaxBandwidth = 100000, got {}/{}".format(
                    w.get("interfaceName"), w.get("inboundMaxBandwidth"), w.get("outboundMaxBandwidth")))


ALIGN_KEYS = ("interfaceName", "name", "prefix", "ifName")


def align(intended: Any, actual: Any) -> Any:
    """Reorder lists of dicts in `actual` to the order of `intended` by a shared identity key, so a reordered list is not a diff."""
    if isinstance(intended, dict) and isinstance(actual, dict):
        return {k: (align(intended[k], v) if k in intended else v) for k, v in actual.items()}
    if not (isinstance(intended, list) and isinstance(actual, list)):
        return actual
    dicts = intended and actual and all(isinstance(x, dict) for x in intended + actual)
    key = next((k for k in ALIGN_KEYS if dicts and all(k in x for x in intended + actual)), None)
    if key is None:
        return [align(i, a) for i, a in zip(intended, actual)] + actual[len(intended):]
    rest = list(actual)
    out = []
    for item in intended:
        match = next((a for a in rest if a[key] == item[key]), None)
        if match is not None:
            rest.remove(match)
        out.append(None if match is None else align(item, match))  # None keeps later indexes paired; its leaves count as missing
    return out + rest


SEGMENT_TWINS = {"bgpSystemConfig": "segmentBgpSystems", "localRoutes": "segmentLocalRoutes", "ospfSystemConfig": "segmentOspfConfig"}


def compare_intended_vs_actual(intended: Dict[str, Any], actual: Dict[str, Any]) -> Dict[str, Any]:
    """Compare every leaf in `intended` against `actual`, and also note extra sections in `actual`."""
    actual = dict(actual)
    for single, seg in SEGMENT_TWINS.items():
        # translate writes Default inside segment* when another segment is active (merge sections keep it from earlier applies)
        default = next((e for e in actual.get(seg) or [] if isinstance(e, dict) and e.get("segment") == "Default"), None)
        if single in intended and single not in actual and default is not None:
            actual[single] = {k: v for k, v in default.items() if k != "segment"}
    actual = align(intended, actual)
    pi = paths_of(intended)
    pa = paths_of(actual)
    matches, mismatches, missing_in_actual = [], [], []
    for p, val in sorted(pi.items()):
        if isinstance(val, str) and val.startswith("<SECRET"):
            continue
        if p not in pa:
            if val in (None, "", [], {}):
                matches.append({"path": p, "intended": val, "actual": None, "note": "null/empty omitted in actual"})
            else:
                missing_in_actual.append({"path": p, "intended": val})
        elif pa[p] == val and type(pa[p]) is type(val):
            matches.append({"path": p, "value": val})
        else:
            # Handle secret placeholders in actual
            if isinstance(pa[p], str) and pa[p].startswith("<SECRET"):
                matches.append({"path": p, "intended": "<redacted>", "actual": pa[p]})
            else:
                mismatches.append({"path": p, "intended": val, "actual": pa[p]})
    extra_in_actual = [{"path": p, "actual": pa[p]} for p in sorted(set(pa) - set(pi))
                       if p.split(".")[0].split("[")[0] in intended and pa[p] is not None]
    return {
        "intended_sections": list(intended.keys()),
        "actual_sections": list(actual.keys()),
        "matched_count": len(matches),
        "mismatch_count": len(mismatches),
        "missing_count": len(missing_in_actual),
        "extra_in_same_sections_count": len(extra_in_actual),
        "mismatches": mismatches,
        "missing_in_actual": missing_in_actual,
        "extra_in_same_sections": extra_in_actual,
    }


def run_apply_and_diff(
    yaml_path: Path,
    nepk: Optional[str] = None,
    discovered_id: Optional[int] = None,
    serial: Optional[str] = None,
    name: Optional[str] = None,
    orchestrator: str = "vlab",
    keep_preconfig: bool = False,
    ignore_template_ownership: bool = False,
    out_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    if bool(nepk) == bool(discovered_id is not None):
        raise ValueError("specify exactly one of --nepk or --discovered-id")
    yaml_text = yaml_path.read_text(encoding="utf-8")
    intended = parse_yaml(yaml_text)
    check_guardrails(orchestrator, intended)

    norm_serial = normalize_serial(serial)
    client = client_for(orchestrator)
    verdict = validate_text(client, yaml_text, serial=norm_serial)
    if not verdict["accepted"]:
        raise GuardrailError("preconfig failed /validate before upload: {}".format(verdict["error"]))

    stamp = utc_stamp()
    pcfg_name = name or "test-pcfg-{}".format(stamp)
    out = out_dir or (WORK_DIR / "test-runs" / "{}-apply-{}".format(stamp, nepk or f"disc{discovered_id}"))
    out.mkdir(parents=True, exist_ok=True)
    (out / "intended.yml").write_text(yaml_text, encoding="utf-8")

    payload: Dict[str, Any] = {
        "name": pcfg_name,
        "configData": base64.b64encode(yaml_text.encode("utf-8")).decode("ascii"),
        "autoApply": False,
        "comment": "automated preconfig verification {}".format(stamp),
    }
    if norm_serial:
        payload["serialNum"] = norm_serial

    created = client.request("POST", "/gms/appliance/preconfiguration", json_body=payload, expected_status=(200, 201), expect_json=True)
    preconfig_id = created["id"] if isinstance(created, dict) else int(created)
    print("Created preconfig id={} name={}".format(preconfig_id, pcfg_name), flush=True)

    apply_status: Dict[str, Any] = {}
    try:
        if nepk:
            apply_resp = client.request(
                "POST", "/gms/appliance/preconfiguration/apply",
                query={"preconfigId": str(preconfig_id), "nePk": nepk},
                expected_status=(200, 204), expect_json=False
            )
        else:
            apply_resp = client.request(
                "POST", "/gms/appliance/preconfiguration/apply/discovered",
                query={"preconfigId": str(preconfig_id), "discoveredId": str(discovered_id)},
                expected_status=(200, 204), expect_json=False
            )
        print("Triggered apply ({} bytes), polling status...".format(len(apply_resp or b"")), flush=True)

        deadline = time.time() + 240
        while time.time() < deadline:
            time.sleep(3)
            apply_status = client.get("/gms/appliance/preconfiguration/apply", {"preconfigId": str(preconfig_id)})
            task_status = apply_status.get("taskStatus")
            steps = apply_status.get("result") or []
            print("  taskStatus={} completionStatus={} steps={}".format(
                task_status, apply_status.get("completionStatus"),
                [(s.get("name"), s.get("taskStatus"), s.get("result")) for s in steps]
            ), flush=True)
            if task_status not in (0, 1):
                break
    finally:
        if not keep_preconfig:
            try:
                client.request("DELETE", "/gms/appliance/preconfiguration", query={"preconfigId": str(preconfig_id)}, expected_status=(200, 204), expect_json=False)
                print("Deleted temporary preconfig id={}".format(preconfig_id), flush=True)
            except Exception as del_err:
                print("Warning: failed to delete preconfig id={}: {}".format(preconfig_id, del_err), file=sys.stderr)

    # Resolve nePk if we onboarded a discovered appliance
    target_nepk = nepk
    if not target_nepk:
        for step in (apply_status.get("result") or []):
            if step.get("nePk"):
                target_nepk = step["nePk"]
                break
        if not target_nepk:
            want_host = (intended.get("applianceInfo") or {}).get("hostname")
            for a in client.get("/appliance"):
                if a.get("hostName") == want_host:
                    target_nepk = a.get("nePk") or a.get("id")
                    break

    # Wait for the appliance to answer a live /deployment read after apply/reboot before capturing
    reach_deadline = time.time() + 150
    while time.time() < reach_deadline:
        time.sleep(5)
        try:
            r = client.get("/reachability/gms", {"nePk": str(target_nepk)})
            dep_live = client.get("/deployment", {"nePk": str(target_nepk), "cached": "false"})
            if isinstance(r, dict) and int(r.get("state", 2)) in (1, 4) and isinstance(dep_live, dict) and "sysConfig" in dep_live:
                break
        except Exception:
            pass
    time.sleep(3)
    if any(k in intended for k in ("vxlan", "flowRedirection", "ecLicensing", "managementRoutes")):
        try:
            client.request("POST", "/appliance/backup", json_body={"nePks": [str(target_nepk)], "comment": "apply-diff"}, expected_status=(200, 204), expect_json=False)
            time.sleep(6)
        except Exception:
            pass
    here = SKILL_DIR / "scripts"
    cap_cmd = [sys.executable, str(here / "capture.py"), "--orchestrator", orchestrator, "--appliance", str(target_nepk), "--role", "sample", "--pause", "0.05"]
    cap_folder = Path()
    for attempt in range(4):  # the appliance can drop off briefly after a deployment change
        cap_proc = subprocess.run(cap_cmd, capture_output=True, text=True)
        if cap_proc.returncode:
            print("re-capture attempt {} failed: {}".format(attempt + 1, cap_proc.stderr.strip()[-300:]), flush=True)
            if attempt == 3:
                raise RuntimeError("post-apply re-capture failed 4 times")
            time.sleep(8)
            continue
        cap_folder = Path(cap_proc.stdout.strip().splitlines()[-1])
        if json.loads((cap_folder / "run.json").read_text(encoding="utf-8")).get("reachable"):
            break
        time.sleep(8)
    print("Re-captured post-apply state at:", cap_folder, flush=True)

    trans_dir = out / "post-apply-translated"
    trans_cmd = [sys.executable, str(here / "translate.py"), "--capture", str(cap_folder), "--site-type", "post-apply", "--out", str(trans_dir)]
    if ignore_template_ownership:
        trans_cmd.append("--ignore-template-ownership")
    subprocess.run(trans_cmd, capture_output=True, text=True, check=True)

    actual = parse_yaml((trans_dir / "reference.yaml").read_text(encoding="utf-8"))
    diff = compare_intended_vs_actual(intended, actual)
    redacted_status, _ = redact_secrets(apply_status)
    report = {
        "orchestrator": orchestrator,
        "target_nepk": target_nepk,
        "discovered_id": discovered_id,
        "preconfig_id": preconfig_id,
        "apply_status": redacted_status,
        "post_apply_capture": str(cap_folder),
        "diff": diff,
    }
    write_json(out / "apply-diff.json", report)

    md = [
        "# Preconfig Apply & Diff Report: `{}` on `{}`".format(target_nepk, orchestrator),
        "",
        "- **Timestamp:** `{}`".format(stamp),
        "- **Apply `taskStatus`:** `{}` (`completionStatus={}`)".format(apply_status.get("taskStatus"), apply_status.get("completionStatus")),
        "- **Post-apply capture:** `{}`".format(cap_folder),
        "- **Matched leaves:** `{}` | **Mismatches:** `{}` | **Missing in actual:** `{}` | **Extra in same sections:** `{}`".format(
            diff["matched_count"], diff["mismatch_count"], diff["missing_count"], diff["extra_in_same_sections_count"]
        ),
        "",
        "## Apply Steps (from Orchestrator)",
        "",
        "| Step | Task Status | Completion | Result |",
        "|---|---|---|---|",
    ]
    for s in (redacted_status.get("result") or []):
        md.append("| {} | {} | {} | {} |".format(s.get("name"), s.get("taskStatus"), s.get("completionStatus"), str(s.get("result")).replace("\n", " ")[:160]))
    if diff["mismatches"]:
        md += ["", "## Mismatches (Intended vs Actual)", "", "| Path | Intended | Actual |", "|---|---|---|"]
        for m in diff["mismatches"]:
            md.append("| `{}` | `{}` | `{}` |".format(m["path"], json.dumps(m["intended"]), json.dumps(m["actual"])))
    if diff["missing_in_actual"]:
        md += ["", "## Missing in Actual Translated Output", "", "| Path | Intended |", "|---|---|"]
        for m in diff["missing_in_actual"]:
            md.append("| `{}` | `{}` |".format(m["path"], json.dumps(m["intended"])))
    if diff["extra_in_same_sections"]:
        md += ["", "## Extra Leaves in Actual (Same Sections)", "", "| Path | Actual |", "|---|---|"]
        for m in diff["extra_in_same_sections"]:
            md.append("| `{}` | `{}` |".format(m["path"], json.dumps(m["actual"])))
    (out / "apply-diff-report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md), flush=True)
    print("Report dir:", out, flush=True)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yaml", required=True, type=Path, help="Preconfig YAML file to validate, upload, apply, and diff")
    parser.add_argument("--nepk", help="Managed appliance nePk (e.g., 1.NE, 2.NE)")
    parser.add_argument("--discovered-id", type=int, help="Discovered appliance ID (e.g., 28, 29)")
    parser.add_argument("--serial", help="Optional 12-hex-digit serial number")
    parser.add_argument("--name", help="Preconfig name (default: test-pcfg-<UTC>)")
    parser.add_argument("--orchestrator", default="vlab")
    parser.add_argument("--keep-preconfig", action="store_true")
    parser.add_argument("--ignore-template-ownership", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        report = run_apply_and_diff(
            args.yaml, nepk=args.nepk, discovered_id=args.discovered_id, serial=args.serial,
            name=args.name, orchestrator=args.orchestrator, keep_preconfig=args.keep_preconfig,
            ignore_template_ownership=args.ignore_template_ownership, out_dir=args.out,
        )
    except GuardrailError as err:
        print("GUARDRAIL BLOCKED: {}".format(err), file=sys.stderr)
        return 2
    return 0 if report["apply_status"].get("completionStatus") and not report["diff"]["mismatches"] and not report["diff"]["missing_in_actual"] else 1


if __name__ == "__main__":
    sys.exit(main())

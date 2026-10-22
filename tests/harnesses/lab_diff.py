#!/usr/bin/env python3
"""Lab guardrails and the leaf-by-leaf diff of an intended preconfig against what an appliance holds after an apply.

No network calls. The apply itself is done by a lab tool kept outside this repository, behind two human gates
(docs/decisions/0008): this toolkit cannot apply a preconfig.
"""
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "building-edgeconnect-preconfig-templates" / "scripts"))
from common import paths_of  # noqa: E402


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



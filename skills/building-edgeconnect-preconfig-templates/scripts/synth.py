"""Name substitution for validating a configuration taken from another Orchestrator on the validator.

/validate checks that named objects (template groups, appliance group, overlays, interface labels per side, segments, zones)
exist on the validating Orchestrator (references/validate-behaviour.md). A configuration translated from another Orchestrator
names objects the validator lacks, so before it is sent, each distinct source name is replaced one-to-one by an existing
validator name of the same kind. Only names change; structure, types and every other value are untouched. Never used for
the real site type: it exists to test the translation against the validator.
"""
import re
from typing import Any, Dict, List, Optional


def inventory(client: Any) -> Dict[str, List[str]]:
    labels = client.get("/gms/interfaceLabels")
    groups = client.get("/gms/group")
    return {
        "groups": [g["name"] for g in (groups if isinstance(groups, list) else [groups]) if g.get("name") and g.get("subType") != 0],
        "overlays": [o["name"] for o in client.get("/gms/overlays/config")],
        "templateGroups": [g["name"] for g in client.get("/template/templateGroups")],
        "lan": [v["name"] for v in labels.get("lan", {}).values() if v.get("active")],
        "wan": [v["name"] for v in labels.get("wan", {}).values() if v.get("active")],
        "segments": [v["name"] for v in client.get("/vrf/config/segments").values()],
        "zones": sorted({v["name"] for v in client.get("/zones").values()}),
        "clusterProfiles": [p["name"] for p in (client.get("/cluster/profiles") or []) if isinstance(p, dict) and p.get("name")],
        "idsProfiles": [p["name"] for p in _optional(client, "/ids/signatures/profiles") if isinstance(p, dict) and p.get("name")],
    }


def _optional(client: Any, path: str) -> List[Any]:
    """An endpoint some releases lack: an empty list instead of stopping the whole check."""
    try:
        return client.get(path) or []
    except Exception:
        return []


class Mapper:
    def __init__(self, pool: List[str], keep: Optional[List[str]] = None) -> None:
        self.keep, self.seen = set(keep or []), {}
        self.pool = [x for x in pool if x not in self.keep] or pool  # a replaced name must not collide with a kept one

    def __call__(self, name: Any) -> Any:
        if name in (None, "") or name in self.keep:
            return name
        if name not in self.seen:
            self.seen[name] = self.pool[len(self.seen) % len(self.pool)]
        return self.seen[name]


def substitute(doc: Dict[str, Any], inv: Dict[str, List[str]], notes: Optional[List[str]] = None) -> Dict[str, Any]:
    """Return a copy of the preconfig document with object names replaced by names that exist on the validator."""
    import copy
    doc = copy.deepcopy(doc)
    segment, zone = Mapper(inv["segments"], ["Default"]), Mapper(inv["zones"], ["Default"])
    lan, wan = Mapper(inv["lan"]), Mapper(inv["wan"])
    def anyl(name: Any) -> Any:  # a label used without a side: reuse the mapping already made for it on either side, else map as a LAN label
        if name in (None, ""):
            return name
        return wan.seen[name] if name in wan.seen and name not in lan.seen else lan(name)
    if "applianceInfo" in doc and doc["applianceInfo"].get("group"):
        doc["applianceInfo"]["group"] = inv["groups"][0]
    if "applianceInfo" in doc and doc["applianceInfo"].get("region"):
        doc["applianceInfo"]["region"] = None  # the validator may define no regions at all
    if "applianceInfo" in doc and doc["applianceInfo"].get("clusterProfile"):
        profs = inv.get("clusterProfiles") or []
        doc["applianceInfo"]["clusterProfile"] = profs[0] if profs else None
    if "templateGroups" in doc:
        doc["templateGroups"]["groups"] = inv["templateGroups"][:1]
    if "businessIntentOverlays" in doc:
        doc["businessIntentOverlays"]["overlays"] = inv["overlays"][:2]

    def walk(node: Any, side: Optional[str] = None) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, side)
        elif isinstance(node, dict):
            kind = node.get("interfaceType")
            for key, value in list(node.items()):
                if key == "segment" and isinstance(value, str):
                    node[key] = segment(value)
                elif key == "zone" and isinstance(value, str):
                    node[key] = zone(value)
                elif key in ("source_ip_interface", "auth_interface") and isinstance(value, str) and value != "any":
                    node[key] = lan(value)
                elif key in ("dot1x_default_role", "mac_default_role") and isinstance(value, str):
                    node[key] = "default"
                elif key == "role" and isinstance(value, str):
                    node[key] = "default"
                elif key == "sourceInterface" and isinstance(value, str) and value != "any" and not re.match(r"^(lan|wan|tlan|twan|twlan|blan|bwan|slan|mgmt|lo|vti|ppp|cell)\d*(\.\d+)?$", value):
                    node[key] = wan(value)
                elif key in ("interfaceName", "sourceInterfaceName") and isinstance(value, str) and value != "any" and not re.match(r"^(lan|wan|tlan|twan|twlan|blan|bwan|slan|mgmt|lo|vti|ppp|cell)\d*(\.\d+)?$", value):
                    node[key] = anyl(value)  # an interface *label* used where a name or label is allowed (IP SLA monitors)
                elif key == "haInterfaceOrder" and isinstance(value, str):
                    node[key] = ",".join(wan(part.strip()) for part in value.split(",") if part.strip())
                elif key == "interfaceLabel" and isinstance(value, str):
                    is_wan = kind == "wan" or ("inbound" in node and "outbound" in node)
                    node[key] = (lan if kind == "lan" else wan if is_wan else anyl)(value)
                else:
                    walk(value, side)

    for section, value in doc.items():
        if section not in ("applianceInfo", "templateGroups", "businessIntentOverlays"):
            walk(value)
    if doc.get("nac"):
        auth_ifaces = {p.get("auth_interface") for p in (doc["nac"].get("applyPolicies") or []) if isinstance(p, dict)}
        for srv in doc["nac"].get("server") or []:
            if isinstance(srv, dict) and srv.get("source_ip_interface") in auth_ifaces:
                srv["source_ip_interface"] = "any"
                if notes is not None:
                    notes.append("nac.server.source_ip_interface set to 'any': validator has fewer LAN labels than the source, causing a wrap-around collision with auth_interface")
    ids = doc.get("idsIpsConfig")
    profiles = inv.get("idsProfiles") or []
    if isinstance(ids, dict) and profiles and ids.get("profile") not in profiles:
        suffix = "_" + ids["profile"].rsplit("_", 1)[1] if "_" in str(ids.get("profile")) else ""  # _S5/_S7: signature family
        ids["profile"] = "Default" + suffix if "Default" + suffix in profiles else profiles[0]
        if notes is not None:
            notes.append("idsIpsConfig.profile replaced by {}: the source profile does not exist on the validator".format(ids["profile"]))
    if doc.get("appExpressGroups"):
        doc.pop("appExpressGroups")
        if notes is not None:
            notes.append("appExpressGroups removed before validation: the validator has no AppExpress groups (a test group would need to be created)")
    maps = (doc.get("vxlan") or {}).get("vniVrfMaps")
    if maps:
        seen_vni, seen_seg, unique = set(), set(), []
        for m in maps:
            if m.get("vni") not in seen_vni and m.get("segment") not in seen_seg:
                seen_vni.add(m.get("vni"))
                seen_seg.add(m.get("segment"))
                unique.append(m)
        if len(unique) < len(maps) and notes is not None:
            notes.append("vxlan.vniVrfMaps reduced from {} to {} entries: substituting segments made VNIs or segments collide (each must be unique) (the source has more segments than the validator)".format(len(maps), len(unique)))
        doc["vxlan"]["vniVrfMaps"] = unique
    return doc


def dump(doc: Dict[str, Any]) -> str:
    import yaml
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False, width=1000)

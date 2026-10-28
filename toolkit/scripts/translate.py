#!/usr/bin/env python3
"""Stages 4-7: capture folder -> reference preconfig + ledger -> site type.

Offline. Reads a capture folder written by capture.py. Writes to --out:
  reference.yaml   the source appliance's configuration as preconfig YAML (real values; private)
  ledger.json      one disposition per captured endpoint and per decision (mapped, template-group, runtime, ...)
  variables.csv    one row per field: variable or fixed, column name, reason (variables.py); edit it and re-run with --variables
  proposal.md, proposal.json, decisions.yaml   only with --decisions that sets section modes (the older per-section choice, decisions.py)
  <site>.jinja2, <site>.csv, example.csv, columns.json, columns.md   (generate.py)
Every section in mappers.MAPPERS is translated; anything else in the capture is listed in the ledger, never dropped silently.
Mapping assumptions are labelled `assumption` in the ledger and need the owner's confirmation.
"""
import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CATALOGUE, DEFAULT_TEMPLATES_DIR, WORK_DIR, find_default_template, load_yaml, toolkit_version, write_json  # noqa: E402  guard-ok: imported after the sys.path insert
from generate import generate  # noqa: E402
from decisions import list_mode, make_decide, propose  # noqa: E402
from variables import classify, read_sheet, sheet_items, write_sheet  # noqa: E402
from mappers import ITEM_ERRORS, MAPPERS, Ctx, cli_lines, failed_read, guarded, id_names, item_failed, own_sharing, passthrough_max_bandwidth, Unreadable, unreadable_read  # noqa: E402  guard-ok: imported after the sys.path insert

HARDEN = {0: "All", 1: "harden", 2: "stateful", 3: "statefulSNAT"}  # verified via Swagger + live apply-and-diff
NETWORK_ROLE = {0: "non-hub", 1: "hub"}  # verified across multiple Orchestrators: code 1 = overlay hub; code 0 = non-hub; nrhub: no sample, unresolved

DEFAULT_TEMPLATE = DEFAULT_TEMPLATES_DIR / "orchestrator-9.7.1.42046.yml"


class NothingToConvert(ValueError):
    """The capture has no appliance or deployment data, e.g. an offline appliance with no Orchestrator cache."""


class Capture:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.index = json.loads((folder / "capture-index.json").read_text())
        self.run = json.loads((folder / "run.json").read_text())

    def data(self, ident: str, prefer: str = "live") -> Any:
        order = [prefer] + [x for x in ("live", "cached", "orchestrator", "backup") if x != prefer]
        for label in order:
            item = next((i for i in self.index if i["id"] == ident and i["label"] == label and i["status"] == "ok"), None)
            if item:
                return json.loads((self.folder / item["file"]).read_text())["data"]
        return None


IGNORE_OWNERSHIP = False  # set by --ignore-template-ownership (mapper testing on appliances whose groups provide the section)


TEMPLATE_SECTION = {"peerPriorityList": "peerPriority"}  # template names that differ from the preconfig section they provide


def associated_groups(cap: "Capture") -> List[Dict[str, Any]]:
    """The template groups associated with the appliance; a read of another shape counts as none (build() ledgers it)."""
    assoc = cap.data("template-association") or {}
    ids = assoc.get("templateIds", []) if isinstance(assoc, dict) else None
    groups = cap.data("template-groups") or []
    if not isinstance(ids, list) or not isinstance(groups, list):
        return []
    return [g for g in groups if isinstance(g, dict) and g.get("name") in ids]


def selected_names(group: Dict[str, Any]) -> List[str]:
    names = group.get("selectedTemplateNames") or []
    return [t for t in names if isinstance(t, str)] if isinstance(names, list) else []


def selected_templates(cap: "Capture") -> set:
    return {TEMPLATE_SECTION.get(t, t) for g in associated_groups(cap) for t in selected_names(g)}


def local_firewall_rules(cap: "Capture") -> Optional[str]:
    """Ledger text for firewall rules Orchestrator did not push (gms_marked false); preconfig has no firewall section."""
    maps = cap.data("security-maps")
    if not isinstance(maps, dict):
        return None
    rules = [(prio, rule) for pairs in maps.values() if isinstance(pairs, dict) for pair in pairs.values() if isinstance(pair, dict)
             for prio, rule in (pair.get("prio") or {}).items() if isinstance(rule, dict) and not rule.get("gms_marked")]
    if not rules:
        return None
    local = sum(1 for prio, _ in rules if str(prio) != "65535")
    defaults = len(rules) - local  # 65535 is the per-zone-pair catch-all rule
    return ("{} firewall rule(s) and {} zone-pair default rule(s) not pushed by Orchestrator are not carried by preconfig (no firewall section); "
            "recreate them with a template or a separate push. A securityMaps template's rules may also show gms_marked false, so check the template groups first").format(local, defaults)


# Templates that set only some fields of a preconfig section: template key -> preconfig field, per segment id unless flat.
FIELD_TEMPLATES = {
    "routes": {"sections": ("localRoutes", "segmentLocalRoutes"), "segments": ("auto_subnet", "vrf"),
               "fields": {"add_local_lan": "advertiseLocalLanSubnets", "add_local_wan": "advertiseLocalWanSubnets", "add_local_metric": "localMetric",
                          "add_local_communities": "localCommunities", "local_communities": "localCommunities", "local_asn_filter": "filterRoutesWithLocalASN",
                          "rtmap_to_subshare": "redistToSDwanFabricRouteMap"}},
    "bgp": {"sections": ("bgpSystemConfig", "segmentBgpSystems"), "segments": ("vrf",),
            "fields": {"graceful_restart_en": "enableGracefulRestart", "max_restart_time": "maxRestartTime", "stale_path_time": "maxStalePathTime",
                       "remote_as_path_advertise": "propagateAsPath", "route_target_template.import": "importRouteTarget", "route_target_template.export": "exportRouteTarget"},
            "unowned": {"ka", "hold", "next_hop_self", "password", "store_received_routes", "evpn"}},
    "vrrp": {"sections": ("vrrp",), "segments": None,
             "fields": {"admin": "admin", "advTimer": "advertisementTimer", "priority": "priority", "preempt": "preemption", "auth": "authenticationString"}},
    # VNI maps are owned per segment from the template's vrfId; a template that sets the VTEP owns the whole section (vtepSrcInterface is required)
    "vxlan": {"sections": ("vxlan",), "segments": None, "fields": {"vtepIf": "vtepSrcInterface", "port": "destinationUdpPort"}},
}


def template_fields(cap: "Capture") -> Dict[str, Dict[Optional[str], set]]:
    """Preconfig fields each selected FIELD_TEMPLATES template sets, per segment id (None for flat templates).

    Fields from every associated group are combined: a field any group sets is template-owned whichever group wins.
    A blank or "none" template value is taken to mean the template leaves the field alone (inferred from reference Orchestrator templates),
    and so is a route_target_template whose applyStatus is "none"; one without applyStatus still owns its route targets."""
    owned: Dict[str, Dict[Optional[str], set]] = {}
    for group in associated_groups(cap):
        for template in group.get("templates") or []:
            name, value = template.get("name"), template.get("value")
            spec = FIELD_TEMPLATES.get(name)
            if not spec or name not in selected_names(group) or not isinstance(value, dict):
                continue
            per_segment: Dict[Optional[str], Any] = {None: value}
            if spec["segments"]:
                for key in spec["segments"]:
                    value = (value or {}).get(key) or {}
                per_segment = {str(k): v for k, v in value.items() if isinstance(v, dict)}
            fields = owned.setdefault(name, {})
            for segment, src in per_segment.items():
                found = fields.setdefault(segment, set())
                for key, field in spec["fields"].items():
                    head, _, tail = key.partition(".")
                    item = (src.get(head) or {}).get(tail) if tail else src.get(head)
                    if tail and str((src.get(head) or {}).get("applyStatus", "")).lower() == "none":
                        continue  # e.g. route_target_template {"applyStatus": "none", "import": "0:0"} applies no route targets
                    if item not in (None, "", "none"):
                        found.add(field)
            if name == "vxlan":
                for seg, vni_map in (value.get("vrfId") or {}).items():
                    if isinstance(vni_map, dict) and str(vni_map.get("vni")) not in ("None", "", "0", "-1", "4294967295"):
                        fields.setdefault(str(seg), set()).add("vniVrfMaps")
    if "vtepSrcInterface" in owned.get("vxlan", {}).get(None, set()):
        del owned["vxlan"]  # left in template_owned: the section is not written, as for any template that provides a whole section
    return owned


NAC_LISTS = {"dot1x": "name", "macAuthProfile": "name", "server": "name", "serverGroup": "name", "aaaProfile": "name", "applyPolicies": "auth_interface"}


def nac_merge_owned(cap: "Capture") -> Optional[Dict[str, Any]]:
    """What the selected nac template(s) define when every one merges (merge true) with the appliance's own NAC; None otherwise.

    Objects are identified by name; the template's applyPolicies are keyed by LAN label (the preconfig auth_interface)."""
    values = [t.get("value") for g in associated_groups(cap) if "nac" in selected_names(g) for t in g.get("templates") or [] if t.get("name") == "nac"]
    if not values or not all(isinstance(v, dict) and v.get("merge") is True for v in values):
        return None
    owned: Dict[str, Any] = {key: set() for key in NAC_LISTS}
    for v in values:
        for key in NAC_LISTS:
            block = v.get(key) or {}
            if key == "applyPolicies":
                owned[key] |= set(block.get("if") or {})
            elif isinstance(block, dict):
                owned[key] |= {o.get("name") for o in block.values() if isinstance(o, dict)}
    return owned


def ledger_local_nac(value: Optional[Dict[str, Any]], owned: Dict[str, Any], note: Any, read: bool = True) -> None:
    """Under a merge nac template the section is not written; the appliance's own NAC objects are ledgered, never the template's.

    /validate answers HTTP 500 unless all six NAC lists are non-empty (semir_vlab, 2026-10-28), and the lists the template fills hold
    only its objects: a section /validate accepts would write the template's own objects, which a template group's settings never are."""
    note("template nac", "template-group", "nac", "the 'nac' template merges (merge: true) with the appliance's NAC: the section is not written")
    if not read:
        note("nac (local objects under a merge template)", "not-readable", "nac", "the NAC read failed, so the appliance's own NAC objects under the merge template are unknown; check them by hand")
        return
    mine = ["{} {}".format(key, e.get(ident)) for key, ident in NAC_LISTS.items() for e in (value or {}).get(key) or []
            if e.get(ident) not in owned[key] and e.get("auth_type") != "trusted"]  # trusted is the appliance default, nothing to redo
    if mine:
        note("nac (local objects under a merge template)", "outside-preconfig", "nac", "local NAC objects under a merge template: {}; not carried: "
             "/validate requires all six NAC lists non-empty (HTTP 500 otherwise) and the other lists hold only the template's objects; set them on the target by hand".format(", ".join(mine)))


def drop_template_fields(out: Dict[str, Any], owned: Dict[str, Dict[Optional[str], set]], vrfs: Dict[str, str], note: Any) -> None:
    """Leave out of the written sections the fields a template sets; drop a route section left with nothing of its own."""
    def strip(entry: Dict[str, Any], fields: set) -> List[str]:
        gone = sorted(f for f in fields if f in entry)
        for f in gone:
            del entry[f]
        return gone

    for name, by_segment in owned.items():
        spec = FIELD_TEMPLATES[name]
        for section in spec["sections"]:
            value = out.get(section)
            if value is None:
                continue
            gone = set()
            if name == "vrrp":
                targets = [(None, e) for e in value.get("vrrpEntries", [])]
            elif name == "vxlan":
                targets = [(None, value)]
                ids = {n: i for i, n in vrfs.items()}
                theirs = [m for m in value.get("vniVrfMaps") or [] if "vniVrfMaps" in by_segment.get(ids.get(m.get("segment")), set())]
                if theirs:
                    value["vniVrfMaps"] = [m for m in value["vniVrfMaps"] if m not in theirs]
                    gone.add("vniVrfMaps ({})".format(", ".join(str(m.get("segment")) for m in theirs)))
                if "destinationUdpPort" in by_segment.get(None, set()) and "destinationUdpPort" in value:
                    by_segment = {**by_segment, None: by_segment[None] - {"destinationUdpPort"}}  # /validate: "VXLAN - Destination UDP port is required"
                    note("vxlan destinationUdpPort", "mapped", "vxlan.destinationUdpPort", "the appliance's port {} is written although the 'vxlan' template sets the port: "
                         "the format requires destinationUdpPort".format(value["destinationUdpPort"]))
            elif isinstance(value, list):
                ids = {n: i for i, n in vrfs.items()}
                targets = [(ids.get(e.get("segment")), e) for e in value]
            else:
                targets = [("0", value)]
            for segment, entry in targets:
                gone.update(strip(entry, by_segment.get(segment, set())))
            if section == "localRoutes" and not value.get("routes") and set(value) <= {"routes"}:
                del out[section]
            elif section == "segmentLocalRoutes":
                out[section] = [e for e in value if e.get("routes") or (set(e) - {"segment", "routes"} and (ids.get(e.get("segment")) == "0" or own_sharing(e)))] or None
                if out[section] is None:
                    del out[section]
            if gone:
                note("template " + name, "template-group", section, "fields set by the '{}' template are left out: {}".format(name, ", ".join(sorted(gone))))
        unowned = spec.get("unowned")
        if unowned and any(out.get(section) for section in spec["sections"]):
            note("template " + name, "mapped", ", ".join(spec["sections"]),
                 "neighbour settings in the '{}' template ({}) are still written per neighbour: not verified whether the template pushes them to every neighbour (assumption: confirm they belong per neighbour)".format(name, ", ".join(sorted(unowned))))


def build(cap: Capture, ledger: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Every independent block reads its source in a guard: a read of an unexpected shape is ledgered (not-readable for a whole read,
    toolkit-error for one item) and skipped, never the whole translation (owner rule, docs/decisions/0015).
    A replace-on-apply list is never written short without a ledger line."""
    def note(item: str, disposition: str, target: str = "", why: str = "") -> None:
        ledger.append({"item": item, "disposition": disposition, "target": target, "note": why})

    di_notes: List[Dict[str, Any]] = []  # deploymentInfo lines written before it is known whether deploymentInfo is written

    def di_note(*args: str) -> None:
        note(*args)
        di_notes.append(ledger[-1])

    app = cap.data("appliance")
    app = app if isinstance(app, dict) else {}
    dep = cap.data("deployment")
    dep = dep if isinstance(dep, dict) else {}
    if not app.get("hostName") or not dep.get("sysConfig") or not isinstance(dep["sysConfig"], dict):
        raise NothingToConvert("capture {!r} lacks appliance or deployment.sysConfig data (appliance may be offline with no Orchestrator cache)".format(str(cap.folder)))
    ctx = Ctx(cap, ledger)
    # every read below: a read that failed (failed_read) or has another shape is ledgered not-readable and nothing is written from it
    extra_what = "location, contact and the IPsec UDP port are not carried"
    extra = None if failed_read(ctx, "extra-info", extra_what, "applianceInfo") else cap.data("extra-info") or {}
    if extra is not None and not isinstance(extra, dict):
        unreadable_read(ctx, "extra-info", extra_what, "applianceInfo")
        extra = None
    nepk = cap.run["appliance"]["nePk"]
    groups = None if failed_read(ctx, "groups", "the appliance's group is not written", "applianceInfo.group") else \
        id_names(ctx, "groups", cap.data("groups"), key=lambda ident: ident, section="applianceInfo.group")
    out: Dict[str, Any] = {}

    info: Dict[str, Any] = {"hostname": app["hostName"]}
    if groups is not None:  # an id written as the group name would name a group that does not exist
        with guarded(ctx, "appliance.groupId", "applianceInfo.group"):
            info["group"] = groups.get(app["groupId"], app["groupId"])
    with guarded(ctx, "appliance.site", "applianceInfo.site"):
        info["site"] = app["site"]
    prof_what = "the cluster profile is not written"
    prof_read = [failed_read(ctx, ident, prof_what, "applianceInfo.clusterProfile") for ident in ("cluster-profiles", "cluster-profile-mapping")]
    for ident, shape in (("cluster-profiles", list), ("cluster-profile-mapping", dict)):
        if not isinstance(cap.data(ident) or shape(), shape):
            unreadable_read(ctx, ident, prof_what, "applianceInfo.clusterProfile")
            prof_read.append(True)
    if not any(prof_read):
        with guarded(ctx, "cluster-profile-mapping, cluster-profiles", "applianceInfo.clusterProfile"):
            prof_map = cap.data("cluster-profile-mapping") or {}
            profs = {str(p["id"]): p["name"] for p in (cap.data("cluster-profiles") or []) if isinstance(p, dict) and p.get("id") is not None}
            matched_prof = next((profs[str(pid)] for pid, sites in prof_map.items() if isinstance(sites, list) and app.get("site") in sites and str(pid) in profs), None)
            if matched_prof:
                info["clusterProfile"] = matched_prof
                note("cluster-profile-mapping, cluster-profiles", "mapped", "applianceInfo.clusterProfile", "site '{}' mapped to cluster profile '{}'".format(app.get("site"), matched_prof))
    with guarded(ctx, "appliance.networkRole", "applianceInfo.networkRole"):
        role = NETWORK_ROLE.get(int(app["networkRole"]))
        if role:
            info["networkRole"] = role
            note("appliance.networkRole", "mapped", "applianceInfo.networkRole", "verified across reference Orchestrators: code 1 = hub (all overlay hubs), code 0 = non-hub; nrhub has no sample")
        else:
            note("appliance.networkRole", "unresolved", "applianceInfo.networkRole", "code {} has no known meaning".format(app["networkRole"]))
    if not failed_read(ctx, "region-associations", "the region is not written", "applianceInfo.region"):
        with guarded(ctx, "region-associations", "applianceInfo.region"):
            region = (cap.data("region-associations") or {}).get(nepk, {})
            if region.get("regionId") == 0:
                note("region-associations", "default-omitted", "applianceInfo.region", "regionId 0 'Default' = no region; owner to confirm")
            elif region:
                info["region"] = region["regionName"]
    for key in ("location", "contact") if extra is not None else ():
        with guarded(ctx, "extra-info." + key, "applianceInfo." + key):
            info[key] = dict(extra.get(key) or {})
    cli_contact = next((m.group(1).strip('"') for l in cli_lines(ctx) for m in [re.match(r"^system contact (.+)$", l)] if m), None)
    if cli_contact and "contact" in info and not any(info["contact"].values()):
        # not carried: the CLI holds one free-text string, the format splits name/email/phoneNumber, and a split would be a guess
        note("running-config", "unresolved", "applianceInfo.contact", "the appliance has a system contact the Orchestrator record lacks: {!r} (stored backup); "
             "not carried, because applianceInfo.contact splits name, email and phoneNumber and the one free-text string cannot be split without guessing; set it per site".format(cli_contact))
    out["applianceInfo"] = info
    note("appliance, extra-info, groups", "mapped", "applianceInfo", "hostname, group, site, location, contact; softwareVersion and clusterProfile have no source value; latitude/longitude not exposed")

    assoc_what = "templateGroups is not written and a section any template group selects is not written from the appliance"
    if failed_read(ctx, "template-association", assoc_what, "templateGroups.groups"):
        template_groups = None
    else:
        association = cap.data("template-association") or {}
        template_groups = association.get("templateIds", []) if isinstance(association, dict) else None
        if isinstance(template_groups, list):
            out["templateGroups"] = {"groups": template_groups}
            note("template-association", "mapped", "templateGroups.groups", "association only; the groups' own settings are not copied")
        else:  # the list replaces the target's groups on apply: an unread association is not "no groups"
            unreadable_read(ctx, "template-association", assoc_what, "templateGroups.groups")
    groups_what = "which sections the associated template groups provide is unknown, so none is taken as provided by a template"
    group_reads = [] if failed_read(ctx, "template-groups", groups_what) else cap.data("template-groups") or []
    if not isinstance(group_reads, list):
        unreadable_read(ctx, "template-groups", groups_what)
    for i, group in enumerate(group_reads if isinstance(group_reads, list) else []):
        if not isinstance(group, dict) or not isinstance(group.get("name"), str) or not isinstance(group.get("selectedTemplateNames") or [], list):
            item_failed(ctx, "template-groups[{}]".format(i), "", TypeError("a template group without a name or a list of selected templates"))

    overlay_what = "businessIntentOverlays is not written"
    overlays_failed = [failed_read(ctx, ident, overlay_what, "businessIntentOverlays.overlays") for ident in ("overlay-config", "overlay-association")]
    overlays_cfg = cap.data("overlay-config") or []
    assoc = cap.data("overlay-association") or {}
    if any(overlays_failed):
        pass  # the list replaces the target's overlays on apply: a failed read is not "no overlays"
    elif isinstance(overlays_cfg, list) and isinstance(assoc, dict):
        names: List[Any] = []
        for i, o in enumerate(overlays_cfg):
            with guarded(ctx, "overlay-config[{}]".format(i), "businessIntentOverlays.overlays"):
                if nepk in assoc.get(str(o["id"]), []):
                    names.append(o["name"])
        out["businessIntentOverlays"] = {"overlays": names}
        note("overlay-association, overlay-config", "mapped", "businessIntentOverlays.overlays", "overlay ids resolved to names")
        hub_of: List[Any] = []
        for i, o in enumerate(overlays_cfg):
            with guarded(ctx, "overlay-config[{}].topology".format(i), "businessIntentOverlays"):
                if nepk in ((o.get("topology") or {}).get("hubs") or []):
                    hub_of.append(o["name"])
        if hub_of:
            note("overlay-config.topology.hubs", "outside-preconfig", "businessIntentOverlays", "hub in overlays {}: hub lists are Orchestrator overlay settings, so a new site from this template joins them as a spoke until its nePk is added to topology.hubs (PUT /gms/overlays/config/regions keeps regional settings)".format(", ".join(hub_of)))
    else:  # the list replaces the target's overlays on apply
        unreadable_read(ctx, "overlay-association, overlay-config", "businessIntentOverlays is not written", "businessIntentOverlays.overlays")

    sys_cfg = dep["sysConfig"]
    labels, vrfs, zones, roles = ctx.labels, ctx.vrfs, ctx.zones, ctx.roles
    di: Dict[str, Any] = {"deploymentMode": "inline-router" if sys_cfg.get("inline") and sys_cfg.get("mode") == "router" else sys_cfg.get("mode"),
                          "serverPerSegment": bool(sys_cfg.get("serverPerSegment", False))}
    with guarded(ctx, "deployment.sysConfig.maxBW", "deploymentInfo.totalOutboundBandwidth"):
        di["totalOutboundBandwidth"] = int(sys_cfg.get("maxBW") or 100000)
    with guarded(ctx, "deployment.sysConfig.maxInBW", "deploymentInfo.totalInboundBandwidth"):
        di["totalInboundBandwidth"] = int(sys_cfg.get("maxInBW") or sys_cfg.get("maxBW") or 100000)
    if extra is not None:
        with guarded(ctx, "extra-info.overlaySettings", "deploymentInfo.ipsecUdpPort"):
            extra_ipsec = (extra.get("overlaySettings") or {})
            if extra_ipsec.get("isUserDefinedIPSecUDPPort"):
                di["ipsecUdpPort"] = int(extra_ipsec["ipsecUdpPort"])
            else:
                di_note("extra-info.overlaySettings.ipsecUdpPort", "default-omitted", "deploymentInfo.ipsecUdpPort", "not user defined ({} assigned by Orchestrator per appliance)".format(extra_ipsec.get("ipsecUdpPort", "port")))
    di_note("deployment.sysConfig", "mapped", "deploymentInfo", "mode, serverPerSegment, system bandwidths (maxBW, maxInBW)")
    if not failed_read(ctx, "security-maps", "firewall rules Orchestrator did not push are unknown (preconfig has no firewall section): check the appliance's local rules"):
        with guarded(ctx, "security-maps", ""):
            firewall = local_firewall_rules(cap)
            if firewall:
                note("security-maps", "unresolved", "", firewall)
    note("availabilityTimeSettings", "not-readable", "availabilityTimeSettings", "Orchestrator returns business hours only through POST /stats/config/applianceBusinessHours/get and capture is GET-only, so custom hours and timezone are not copied; set them per site")
    distance = cap.data("admin-distance")
    if distance and "adminDistance" in selected_templates(cap):
        note("admin-distance", "template-group", "", "route admin distances come from the 'adminDistance' template of an associated template group")
    elif isinstance(distance, dict) and distance:
        note("admin-distance", "unresolved", "", "route admin distances ({}) have no preconfig field and are not copied; use an adminDistance template if they differ from the target's".format(
            ", ".join("{} {}".format(k, v) for k, v in sorted(distance.items()))))
    elif distance is None and "adminDistance" not in selected_templates(cap):
        failed_read(ctx, "admin-distance", "route admin distances are unknown (no preconfig field): compare them with the target's")
    sse = cap.data("sse-interfaces")
    sse = sse if isinstance(sse, list) else [sse] if isinstance(sse, dict) and sse else []
    if sse:
        note("sse-interfaces", "orchestrator-managed", "", "SSE service interface(s) {} are made by Orchestrator's SSE integration, not by preconfig (inferred: veth names)".format(", ".join(str(i.get("interface")) for i in sse if isinstance(i, dict))))
    cli = cli_lines(ctx)
    pt_bw = passthrough_max_bandwidth(cli)
    if pt_bw is not None and pt_bw != 10000000:
        di["passThroughShapedTraffic"] = {"outboundMaxBandwidth": pt_bw}
        di_note("running-config", "mapped", "deploymentInfo.passThroughShapedTraffic", "outboundMaxBandwidth from 'interface pass-through max-bandwidth' in the stored backup")
    elif cli:
        di_note("running-config", "default-omitted", "deploymentInfo.passThroughShapedTraffic", "no pass-through cap in the stored backup, or the default 10000000")
    elif not any(e["item"] == "running-config" and e["disposition"] == "not-readable" for e in ledger):
        di_note("running-config", "not-readable", "deploymentInfo.passThroughShapedTraffic", "no stored backup (running-config) was read, so a pass-through cap "
                "('interface pass-through max-bandwidth', set only in the CLI) is unknown; check it by hand")

    ha_if = sys_cfg.get("haIf") or ""
    ha_mode_entries: List[Dict[str, Any]] = []
    interfaces: List[Dict[str, Any]] = []
    dhcp: List[Dict[str, Any]] = []
    relay_leases: List[str] = []
    ras: List[Dict[str, Any]] = []
    ra_skipped: List[str] = []
    failover = dep.get("dhcpFailover") or {}
    failover_read = isinstance(failover, dict)
    if not failover_read:
        unreadable_read(ctx, "deployment.dhcpFailover", "DHCP failover (dhcpHA) is not carried", "deploymentInfo.dhcpInfo[].dhcpHA")
        failover = {}
    fo_done: set = set()
    fo_tried: set = set()  # one failover block per physical interface
    fo_wanted: set = set()  # so a server that failed to read is not reported as "no server has failover"
    if_failed, attempted, label_gaps = False, 0, []  # haConfig orders and pairs every WAN link: built only when all were read
    mode_ifs = dep.get("modeIfs")
    if not isinstance(mode_ifs, list):  # interfaces replace the target's on apply: never invented or written empty
        unreadable_read(ctx, "deployment.modeIfs", "deploymentInfo (interfaces, DHCP, router advertisements, HA) is not written", "deploymentInfo")
    for i, mode_if in enumerate(mode_ifs if isinstance(mode_ifs, list) else []):
        try:
            if_name, ips = mode_if["ifName"], mode_if["applianceIPs"]
            if not isinstance(ips, list):
                raise TypeError("applianceIPs is {}, not a list".format(type(ips).__name__))
        except ITEM_ERRORS as err:
            item_failed(ctx, "deployment.modeIfs[{}]".format(mode_if.get("ifName", i) if isinstance(mode_if, dict) else i), "deploymentInfo.deploymentInterfaces", err)
            if_failed = True
            if isinstance(mode_if, dict) and isinstance(mode_if.get("ifName"), str):
                fo_wanted.add(mode_if["ifName"])  # unknown entries may have wanted failover: no "no server has failover" claim
            continue
        if ha_if and if_name == ha_if:
            ha_mode_entries.extend(ips)
            continue
        for j, a in enumerate(ips):
            attempted += 1
            try:
                vlan = int(a.get("vlan") or 0)
                name = "{}.{}".format(if_name, vlan) if vlan > 0 else if_name
                wan = bool(a["wanSide"])
                item: Dict[str, Any] = {"interfaceName": name, "interfaceLabel": labels.get(str(a["label"])) if a["label"] != "" else None, "interfaceType": "wan" if wan else "lan"}
                if a.get("comment"):
                    item["interfaceComment"] = a["comment"]
                dynamic = bool(a.get("intf_mode") and a["intf_mode"] != "static") or (wan and a.get("dhcp"))
                if wan:
                    mode = a.get("intf_mode") or ("dhcpv4" if a.get("dhcp") else "static")
                    parts = mode.split("_")  # dual stack 'dhcpv4_slaac' is two entries, one per IP version
                    item["addressingMode"] = parts[0] if len(parts) == 1 or int(a.get("version") or 4) == 4 else parts[1]
                    dynamic = item["addressingMode"] != "static"  # 'static_slaac': the IPv4 entry keeps its address
                item["ipAddressMask"] = None if dynamic else "{}/{}".format(a["ip"], a["mask"])
                if wan:
                    bw = a.get("maxBW") or {}
                    out_bw = int(bw.get("outbound") or sys_cfg.get("maxBW") or 100000)
                    in_bw = int(bw.get("inbound") or out_bw)
                    item["nextHop"] = None if dynamic else a["wanNexthop"]
                    item["inboundMaxBandwidth"] = in_bw
                    item["outboundMaxBandwidth"] = out_bw
                    item["firewallMode"] = HARDEN[a["harden"]]
                    item["behindNat"] = a["behindNAT"]
                item["segment"] = vrfs[str(a["vrf"])]
                zone = zones.get(str(a["vrf"]), {}).get(str(a["zone"]))
                item["zone"] = zone
                if roles.get(str(a.get("role", 0)), "default") != "default":
                    item["role"] = roles[str(a.get("role", 0))]
            except ITEM_ERRORS as err:
                item_failed(ctx, "deployment.modeIfs[{}] applianceIPs[{}]".format(if_name, j), "deploymentInfo.deploymentInterfaces", err)
                if_failed = True
                if isinstance(if_name, str):
                    fo_wanted.add(if_name)
                continue
            interfaces.append(item)
            if wan and a["label"] != "" and item["interfaceLabel"] is None:
                label_gaps.append(name)
            try:
                dhcp_cfg = a.get("dhcpd") or {}
                server = dhcp_cfg.get("server") if dhcp_cfg.get("type") == "server" else None
                relay = dhcp_cfg.get("relay") if dhcp_cfg.get("type") == "relay" else None
                if server:
                    if server.get("failover") and failover.get(if_name):
                        fo_wanted.add(if_name)
                    elif server.get("failover") and failover_read:
                        ctx.note("deployment.dhcpFailover", "unresolved", "deploymentInfo.dhcpInfo[].dhcpHA", "the DHCP server on {} has failover on, but the appliance "
                                 "holds no failover settings for {}, so dhcpHA is not written; check it by hand".format(if_name, if_name))
                    entry: Dict[str, Any] = {"dhcpInterfaceName": name, "dhcpType": "server", "dhcpAddressMask": server["prefix"], "startIpAddress": server["ipStart"],
                                             "endIpAddress": server["ipEnd"]}
                    if server.get("ip_range"):
                        entry["iPRanges"] = [{"startIp": r["start"], "endIp": r["end"]} for _, r in sorted(server["ip_range"].items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else str(kv[0]))]
                    gw = server.get("gw")
                    entry.update(gatewayIpAddress=(gw[0] if gw else None) if isinstance(gw, list) else (gw or None), dnsServers=server.get("dns") or [])
                    if server.get("ntpd"):
                        entry["ntpServers"] = server["ntpd"]
                    if server.get("netbios"):
                        entry["netbiosNameServers"] = server["netbios"]
                    entry.update(netbiosNodeType=server["netbiosNodeType"], maximumLease=server["maxLease"], defaultLease=server["defaultLease"])
                    if server.get("options"):
                        entry["options"] = [{"option": int(k), "value": v["value"] if isinstance(v, dict) else v} for k, v in sorted(server["options"].items(), key=lambda kv: int(kv[0]))]
                    if server.get("host"):
                        entry["staticIpAssignments"] = [{"hostname": h, "macAddress": v["mac"], "ipAddress": v["ip"]} for h, v in sorted(server["host"].items())]
                    fo = failover.get(if_name) if server.get("failover") else None
                    if fo and if_name not in fo_tried:  # one failover block per physical interface, shared by its VLANs
                        fo_tried.add(if_name)
                        entry["dhcpHA"] = {"dhcpFailoverRole": fo["role"], "ipAddress": fo["my_ip"], "ipAddressPort": fo["my_port"], "peerIpAddress": fo["peer_ip"],
                                           "peerIpAddressPort": fo["peer_port"], "mclt": fo.get("mclt", 3600), "split": fo.get("split", 128),
                                           "maxResponseDelay": fo.get("max_resp_delay", 10), "maxUnackedUpdates": fo.get("max_unack_updates", 10),
                                           "loadBalanceMaxSeconds": fo.get("load_bal_max", 5)}
                        if fo.get("dhcp_fo_rel_name"):
                            entry["dhcpHA"]["foRelationshipName"] = fo["dhcp_fo_rel_name"]
                        fo_done.add(if_name)
                    dhcp.append(entry)
                elif relay:
                    entry = {
                        "dhcpInterfaceName": name, "dhcpType": "relay", "dhcpProxyServers": relay["dhcpserver"],
                        "enableOptions82": relay.get("option82", False), "options82Policy": relay.get("option82_policy"),
                        # sub-options are not configurable before ECOS 9.5.1, so the keys can be absent
                        **{"option82SubOptions{}".format(n): relay.get("opt82SubOpt{}".format(n), False) for n in (1, 5, 10, 11, 150, 151, 152)},
                    }
                    idle = dhcp_cfg.get("server") or {}  # a relay interface keeps an inactive server block; the format's relay entry has no lease fields
                    if (idle.get("defaultLease"), idle.get("maxLease")) not in ((None, None), (600, 7200)):
                        relay_leases.append("{} (default {}, max {})".format(name, idle.get("defaultLease"), idle.get("maxLease")))
                    dhcp.append(entry)
            except ITEM_ERRORS as err:
                item_failed(ctx, "deployment.modeIfs[{}].dhcpd".format(name), "deploymentInfo.dhcpInfo", err)
                if isinstance(if_name, str):
                    fo_wanted.add(if_name)
            try:
                ra = a.get("ra")
                if ra and ":" not in str(a.get("ip")):
                    ra_skipped.append(name)
                    ra = None
                if ra:
                    adv = {"raInterfaceName": name, "hoplimit": str(ra["hoplimit"]), "linkmtu": str(ra["linkmtu"]), "managedflag": ra["managedflag"],
                           "maxinterval": str(ra["maxinterval"]), "mininterval": str(ra["mininterval"]), "otherconfigflag": ra["otherconfigflag"],
                           "reachabletime": str(ra["reachabletime"]), "retranstimer": str(ra["retranstimer"]), "routerdeflifetime": str(ra["routerdeflifetime"]),
                           "routerpreference": ra["routerpreference"], "sendadv": ra["sendadv"]}
                    prefixes = ra.get("prefix_id") or {}
                    if prefixes:  # keyed by prefix id; the template lists them with integer lifetimes
                        adv["prefix_id"] = [{"id": int(p.get("self", k)), "autonomous": p["autonomous"], "onlink": p["onlink"], "prefix": p["prefix"],
                                             "prefixvalidlifetime": int(p["prefixvalidlifetime"]), "prfrdlifetime": int(p["prfrdlifetime"])}
                                            for k, p in sorted(prefixes.items(), key=lambda kv: int(kv[0]))]
                    ras.append(adv)
            except ITEM_ERRORS as err:
                item_failed(ctx, "deployment.modeIfs[{}].ra".format(name), "deploymentInfo.raInfo", err)
    v4_names = {i["interfaceName"] for i in interfaces if ":" not in str((i.get("ipAddressMask") or "")) and i.get("addressingMode") != "slaac" and i.get("addressingMode") != "dhcpv6"}
    dual = [r["raInterfaceName"] for r in ras if r["raInterfaceName"] in v4_names]
    if dual:
        ras = [r for r in ras if r["raInterfaceName"] not in v4_names]
        ra_skipped += dual
    def _if_sort_key(item: Dict[str, Any]):
        parts = str(item.get("interfaceName", "")).split(".", 1)
        return (0 if item.get("interfaceType") == "lan" else 1, parts[0], int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0)

    interfaces.sort(key=_if_sort_key)
    di["deploymentInterfaces"] = interfaces
    write_di = isinstance(mode_ifs, list)  # interfaces replace the target's on apply: never written with names missing, or empty for a failure
    if write_di and ctx.unread_maps:
        unreadable_read(ctx, "deployment.sysConfig", "deploymentInfo is not written: its interfaces need the {} names".format(", ".join(sorted(ctx.unread_maps))), "deploymentInfo")
        write_di = False
    elif write_di and (attempted or if_failed) and not interfaces:
        ctx.note("deployment.modeIfs", "not-readable", "deploymentInfo", "no interface entry could be read, so deploymentInfo is not written; check the interfaces by hand")
        write_di = False
    if write_di:
        note("deployment.modeIfs", "mapped", "deploymentInfo.deploymentInterfaces", "{} interfaces; interface = ifName.vlan; label/zone/segment ids resolved to names".format(len(interfaces)))
        note("deployment.modeIfs[].harden", "mapped", "deploymentInterfaces[].firewallMode", "verified: 0 All, 1 harden, 2 stateful, 3 statefulSNAT")
        note("deployment.modeIfs[] wan dhcp lease", "runtime", "", "leased address, mask and next hop of DHCP WAN interfaces are runtime state; left empty")
        note("deployment.modeIfs[].zone", "mapped", "deploymentInterfaces[].zone", "id resolved through vrfZonesMap; the zone list holds repeated names across segments")
    ha_groups = cap.data("ha-groups") or {}
    if ha_if and ha_mode_entries and failed_read(ctx, "ha-groups", "haConfig is not written", "deploymentInfo.haConfig"):
        ha_groups = {}
    elif not isinstance(ha_groups, dict):
        if ha_if and ha_mode_entries:
            unreadable_read(ctx, "ha-groups", "haConfig is not written", "deploymentInfo.haConfig")
        ha_groups = {}
    my_ha = None
    for key, group in ha_groups.items():
        with guarded(ctx, "ha-groups[{}]".format(key), "deploymentInfo.haConfig"):
            if any(m.get("nePk") == nepk for m in group.get("appliances", [])):
                my_ha = group
                break
    try:
        if write_di and ha_if and my_ha and ha_mode_entries:
            if if_failed or label_gaps:
                raise ValueError("haConfig orders and pairs every WAN link, and an interface or its label could not be read{}".format(
                    " ({})".format(", ".join(label_gaps)) if label_gaps else ""))
            peer_nepk = next((m.get("nePk") for m in my_ha.get("appliances", []) if m.get("nePk") != nepk), None)
            all_apps = cap.data("appliances-all") or []
            peer_what = "haPeerSerial and haPeerHostname are not written"
            if not failed_read(ctx, "appliances-all", peer_what, "deploymentInfo.haConfig") and not isinstance(all_apps, list):
                unreadable_read(ctx, "appliances-all", peer_what, "deploymentInfo.haConfig")
            peer_app = next((x for x in (all_apps if isinstance(all_apps, list) else []) if isinstance(x, dict) and (x.get("nePk") or x.get("id")) == peer_nepk), {})
            own_wans = [i["interfaceLabel"] for i in interfaces if i.get("interfaceType") == "wan" and i.get("interfaceLabel")]
            own_idx = 0
            by_vlan: Dict[int, Dict[str, Any]] = {}
            for a in ha_mode_entries:
                v = int(a.get("vlan") or 0)
                if v not in by_vlan or a.get("wanSide"):
                    by_vlan[v] = a
            order_labels: List[str] = []
            peer_infos: List[Dict[str, Any]] = []
            for v in sorted(by_vlan):
                a = by_vlan[v]
                if a.get("wanSide"):
                    lbl = labels.get(str(a["label"]))
                    if lbl is None and str(a["label"]) != "":
                        raise ValueError("label {} of peer link VLAN {} does not resolve".format(a["label"], v))
                    lbl = lbl or ""
                    order_labels.append(lbl)
                    peer_infos.append({
                        "interfaceLabel": lbl,
                        "outbound": a["maxBW"]["outbound"],
                        "inbound": a["maxBW"]["inbound"],
                        "segment": vrfs[str(a["vrf"])],
                        "zone": zones.get(str(a["vrf"]), {}).get(str(a["zone"])),
                        "ipVersion": int(a.get("version") or (6 if ":" in str(a.get("ip", "")) else 4)),
                    })
                else:
                    if own_idx < len(own_wans):
                        order_labels.append(own_wans[own_idx])
                        own_idx += 1
            ha_cfg: Dict[str, Any] = {}
            if peer_app.get("serial"):
                ha_cfg["haPeerSerial"] = str(peer_app["serial"]).replace("-", "")
            if peer_app.get("hostName"):
                ha_cfg["haPeerHostname"] = peer_app["hostName"]
            ha_cfg.update({
                "haIpPool": my_ha["subnet"],
                "haSubnetMask": int(my_ha["mask"]),
                # ECOS 9.5.x HA groups carry v6HaSubnet null and v6HaMask 0 (no IPv6 HA); /validate needs a mask of 1-127, so use Orchestrator's default pool
                "hav6IpPool": my_ha.get("v6HaSubnet") or "fd00::a9fe:0000/112",
                "hav6SubnetMask": int(my_ha.get("v6HaMask") or 126),
                "haVlanStart": int(my_ha["vlanStart"]),
                "haInterface": ha_if,
                "haInterfaceOrder": ",".join(order_labels),
                "haPeerInterfaceInfo": peer_infos,
            })
            di["haConfig"] = ha_cfg
            note("ha-groups, deployment.modeIfs[haIf]", "mapped", "deploymentInfo.haConfig", "EdgeHA peer {}, interface {}, {} peer WAN links; {} auto-generated HA subinterfaces excluded from deploymentInterfaces".format(peer_nepk, ha_if, len(peer_infos), len(ha_mode_entries)))
    except ITEM_ERRORS as err:  # all or nothing: a peer list with a link missing would replace the target's on apply
        item_failed(ctx, "ha-groups, deployment.modeIfs[haIf]", "deploymentInfo.haConfig", err)
    if not write_di:
        dhcp, relay_leases, ra_skipped, ras, fo_done, fo_wanted = [], [], [], [], set(), set()
        ledger[:] = [e for e in ledger if not any(e is d for d in di_notes)]
    if dhcp:
        di["dhcpInfo"] = dhcp
        note("deployment.modeIfs[].dhcpd", "mapped", "deploymentInfo.dhcpInfo", "{} DHCP servers; empty ntp/netbios lists omitted".format(len(dhcp)))
    if relay_leases:
        note("deployment.modeIfs[].dhcpd.server (relay)", "outside-preconfig", "deploymentInfo.dhcpInfo",
             "relay interfaces keep lease times in an inactive DHCP server block, and the preconfig relay entry has no lease fields: {}".format(", ".join(relay_leases)))
    if ra_skipped:
        note("deployment.modeIfs[].ra", "runtime", "deploymentInfo.raInfo", "{} IPv4 interfaces carry a default router-advertisement block; the validator rejects raInfo on IPv4 interfaces".format(len(ra_skipped)))
    if ras:
        di["raInfo"] = ras
        note("deployment.modeIfs[].ra", "mapped", "deploymentInfo.raInfo", "{} router advertisements; numbers written as strings like the default template".format(len(ras)))
    if write_di:
        out["deploymentInfo"] = di
    if fo_done:
        note("deployment.dhcpFailover", "mapped", "deploymentInfo.dhcpInfo[].dhcpHA", "failover on {}".format(", ".join(sorted(fo_done))))
    if set(failover) - fo_wanted and write_di:
        note("deployment.dhcpFailover", "runtime", "deploymentInfo.dhcpInfo[].dhcpHA",
             "{}: failover settings kept by the appliance but no DHCP server there has failover enabled".format(", ".join(sorted(set(failover) - fo_wanted))))
    try:
        owned_fields = {} if IGNORE_OWNERSHIP else template_fields(cap)
    except ITEM_ERRORS as err:  # every field template then counts as providing its whole section, which is not written
        item_failed(ctx, "template-groups templates", ", ".join(s for name in sorted(selected_templates(cap) & set(FIELD_TEMPLATES)) for s in FIELD_TEMPLATES[name]["sections"]), err)
        owned_fields = {}
        template_owned = {s for name in selected_templates(cap) & set(FIELD_TEMPLATES) for s in FIELD_TEMPLATES[name]["sections"]}
    else:
        template_owned = set()
    try:
        nac_owned = None if IGNORE_OWNERSHIP else nac_merge_owned(cap)
    except ITEM_ERRORS as err:
        item_failed(ctx, "template-groups nac", "nac", err)
        nac_owned = None
    template_owned |= selected_templates(cap) - set(owned_fields) - ({"nac"} if nac_owned is not None else set())
    # association unread: a section some template group selects may come from a template, so it is not written from the appliance
    maybe_owned = set() if isinstance(template_groups, list) or IGNORE_OWNERSHIP else {
        TEMPLATE_SECTION.get(t, t) for g in (group_reads if isinstance(group_reads, list) else []) if isinstance(g, dict) for t in selected_names(g)}
    maybe_owned |= {s for name in maybe_owned & set(FIELD_TEMPLATES) for s in FIELD_TEMPLATES[name]["sections"]}
    for section, mapper in MAPPERS.items():
        if section in template_owned and not IGNORE_OWNERSHIP:
            note("template " + section, "template-group", section, "template '{}' is selected in an associated template group: the section is not written".format(section))
            continue
        if section in maybe_owned:
            note("template " + section, "not-readable", section, "the template association could not be read and a template group selects '{}', so a template may provide the section: not written; check it by hand".format(section))
            continue
        before = len(ledger)
        try:
            value = mapper(ctx)
            if section == "nac" and nac_owned is not None:
                del ledger[before:]  # the mapper's notes describe a section that is not written
                ledger_local_nac(value, nac_owned, note, cap.data("nac") is not None)
                continue
        except Unreadable as err:
            note("mapper " + section, "not-readable", section, "the {} section is not written: {}; check it by hand".format(section, err))
            continue
        except Exception as err:
            note("mapper " + section, "toolkit-error", section, "the {} mapper failed ({}: {}), so the section was not written: a toolkit bug to report, not a limit of preconfig".format(section, type(err).__name__, err))
            continue
        if value is not None:
            out[section] = value
    for name, by_segment in owned_fields.items():
        try:
            drop_template_fields(out, {name: by_segment}, vrfs, note)
        except ITEM_ERRORS as err:  # the sections could still hold fields the template sets: not written
            for section in FIELD_TEMPLATES[name]["sections"]:
                out.pop(section, None)
            item_failed(ctx, "template " + name, ", ".join(FIELD_TEMPLATES[name]["sections"]), err)
    order = list(load_yaml(DEFAULT_TEMPLATE))
    return {k: out[k] for k in sorted(out, key=lambda s: order.index(s) if s in order else 999)}


def flat_paths(value: Any, prefix: str = "") -> set:
    out = set()
    if isinstance(value, dict):
        for k, v in value.items():
            p = "{}.{}".format(prefix, k) if prefix else str(k)
            out.add(p)
            out |= flat_paths(v, p)
    elif isinstance(value, list):
        for v in value:
            out |= flat_paths(v, prefix)
    return out


# Key paths /validate accepts that no default template shows (corpus run of 2026-10-27; docs/decisions/0013).
TEMPLATE_OMISSIONS = {
    "bgpSystemConfig.neighbors.bfdEnabled", "bgpSystemConfig.neighbors.peerAdjacency",
    "segmentBgpSystems.neighbors.bfdEnabled", "segmentBgpSystems.neighbors.peerAdjacency",
    "ospfSystemConfig.interfaces.bfdEnabled",
    "deploymentInfo.deploymentInterfaces.addressingMode",
    "ipSlaRule.ruleEntries.ipslaMonitor.tunnelName", "ipSlaRule.ruleEntries.ipslaMonitor.sourceInterfaceName",
    "ipSlaRule.ruleEntries.ipslaMonitor.forceDnsThroughProbePath", "ipSlaRule.ruleEntries.downAction.subnetMetricDelta",
}


def allowed_paths(target: str) -> set:
    """Key paths a preconfig for the target release may hold: those in its default template (find_default_template), plus TEMPLATE_OMISSIONS."""
    return flat_paths(load_yaml(find_default_template(target)[0])) | TEMPLATE_OMISSIONS


def prune(value: Any, allowed: set, ledger: List[Dict[str, Any]], prefix: str = "") -> Any:
    """Drop every key path not in `allowed`, one ledger line per path: an appliance can report keys the target's format rejects (ECOS 9.5.x NAC)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            p = "{}.{}".format(prefix, k) if prefix else str(k)
            if p not in allowed:
                if not any(e["target"] == p for e in ledger):
                    ledger.append({"item": "source key", "disposition": "outside-preconfig", "target": p,
                                   "note": "not in the target release's preconfig format (its default template), so not written; the target keeps its own setting"})
                continue
            out[k] = prune(v, allowed, ledger, p)
        return out
    if isinstance(value, list):
        return [prune(v, allowed, ledger, prefix) for v in value]
    return value


def _default_nat_maps(data: Any) -> bool:
    maps = (data.get("data") or {}) if isinstance(data, dict) else {}
    maps = {k: m for k, m in maps.items() if isinstance(m, dict)}  # a map listed by name only holds no rules
    return all(set((m or {}).get("prio") or {}) <= {"65535"} and all(set_.get("nat_type") == "no-nat" for e in ((m or {}).get("prio") or {}).values() for set_ in (e.get("set") or {}).values())
               for m in maps.values())


DEFAULT_ONLY = {  # endpoints without a preconfig section whose usual content is only the ECOS default
    "nat-all": lambda d: isinstance(d, dict) and not any((d.get(k) or {}).get("enable") for k in ("inbound", "outbound")),
    "nat-maps": _default_nat_maps,  # the 65535 no-nat catch-all every appliance has
}

# Sections a source feeds only when it holds matching lines; None: another ledger entry covers the section
# shortcut: the usblte pattern is unverified (no USB-LTE appliance captured yet); confirm it on the first one
NO_GAP_WITHOUT_EVIDENCE = {"running-config": {"poE": r"(?m)^interface \S+ poe ", "usblte": r"(?m)^(interface cell|lte )", "availabilityTimeSettings": None}}

# Ledger dispositions grouped the way a report reads them (SKILL.md "How to judge and report the result").
REPORT_GROUPS = [
    ("Toolkit errors: a toolkit bug, report it", {"toolkit-error"}),
    ("Gaps: the preconfig format has a place for it, but the value was not carried", {"unresolved", "unmapped-source-key"}),
    ("Not readable: the format has a place for it, but a GET cannot read it; check it by hand", {"not-readable"}),
    ("Outside preconfig: the format has no place for it; set it another way", {"outside-preconfig", "release-gated"}),  # release-gated: ledgers written before 1.3.0
    ("Owned elsewhere: comes from template groups or Orchestrator, as intended", {"template-group", "orchestrator-managed"}),
]
QUIET = {"mapped", "default-omitted", "runtime", "empty-or-absent", "context", "from-default-template"}


def scope_by_template(ledger: List[Dict[str, Any]], template_sections: set) -> None:
    """An unresolved item whose target names no section of the target release's preconfig format is outside preconfig, not a gap."""
    for e in ledger:
        if e["disposition"] in ("unresolved", "unmapped-source-key"):
            sections = {re.split(r"[.\[]", t.strip())[0] for t in e["target"].split(",") if t.strip()}
            if not sections & template_sections:
                e["disposition"] = "outside-preconfig"
                if sections:  # the section exists in other releases' formats, not in the target's
                    e["note"] += " (the target release's preconfig format has no {} section)".format(", ".join(sorted(sections)))


def verdict(ledger: List[Dict[str, Any]]) -> str:
    """The one-line verdict of SKILL.md "How to judge and report the result"; an unknown disposition counts as a gap."""
    known = set().union(*(d for _, d in REPORT_GROUPS)) | QUIET
    errors = sum(1 for e in ledger if e["disposition"] == "toolkit-error")
    gaps = sum(1 for e in ledger if e["disposition"] in ("unresolved", "unmapped-source-key") or e["disposition"] not in known)
    by_hand = sum(1 for e in ledger if e["disposition"] == "not-readable")
    if errors or gaps:
        return "Incomplete for preconfig: {} gap(s), {} toolkit error(s).".format(gaps, errors)
    return "Complete for preconfig" + ("; {} item(s) to check by hand.".format(by_hand) if by_hand else ".")


def route_label_names(value: Any) -> set:
    """Every routeLabel name written anywhere in the reference."""
    if isinstance(value, dict):
        return set().union(*(route_label_names(v) for v in value.values()), {value["routeLabel"]} if isinstance(value.get("routeLabel"), str) else set())
    if isinstance(value, list):
        return set().union(*(route_label_names(v) for v in value)) if value else set()
    return set()


def summary_md(ledger: List[Dict[str, Any]], reference: Dict[str, Any], source: Dict[str, Any], release: str) -> str:
    lines = ["# Translation summary", "",
             "- Toolkit: {}. If a newer version exists, refresh it (re-run setup.sh in the install folder; it pulls main and reinstalls the skill) and translate again before reporting gaps.".format(toolkit_version()),
             "- Source: {} {} ({}), Orchestrator release {}; written for Orchestrator release {}.".format(
                 source.get("orchestrator"), (source.get("appliance") or {}).get("nePk"), (source.get("appliance") or {}).get("hostName"), source.get("release"), release),
             "- Sections written ({}): {}.".format(len(reference), ", ".join(reference) or "none")]
    route_labels = sorted(route_label_names(reference))
    if route_labels:  # global Orchestrator objects, like segments and interface labels: /validate rejects a name the target lacks
        lines.append("- Objects the YAML names: route labels the target Orchestrator must already have: {}.".format(", ".join(route_labels)))
    lines += ["- Default template: {} ({}).".format(e["target"], e["note"]) for e in ledger if e["item"] == "default template"] + [""]
    known = set().union(*(d for _, d in REPORT_GROUPS)) | QUIET
    groups = REPORT_GROUPS + [("Unknown disposition: treat as a gap", {e["disposition"] for e in ledger} - known)]
    for title, dispositions in groups:
        rows = [e for e in ledger if e["disposition"] in dispositions]
        lines.append("## {} ({})".format(title, len(rows)))
        lines += ["- `{}` -> `{}`: {}".format(e["item"], e["target"] or "-", e["note"]) for e in rows] or ["- none"]
        lines.append("")
    confirm = [e for e in ledger if e["disposition"] == "mapped" and "assumption" in e["note"]]
    lines.append("## Mapped on an assumption: confirm with the owner ({})".format(len(confirm)))
    lines += ["- `{}` -> `{}`: {}".format(e["item"], e["target"], e["note"]) for e in confirm] or ["- none"]
    lines += ["", "Not listed: {} entries that are mapped, default, runtime state or context (see ledger.json).".format(sum(1 for e in ledger if e["disposition"] in QUIET) - len(confirm)),
              "", "Verdict: " + verdict(ledger)]
    return "\n".join(lines) + "\n"


def default_only(ident: str, data: Any) -> bool:
    try:
        return DEFAULT_ONLY[ident](data)
    except ITEM_ERRORS:  # a read of another shape is not the ECOS default: it is listed like any other content
        return False


def tag_unmapped(cap: Capture, ledger: List[Dict[str, Any]]) -> None:
    # build() writes these sections itself; /dhcpConfig/* are other views of the DHCP data deploymentInfo takes from /deployment
    covered = set(MAPPERS) | {"applianceInfo", "templateGroups", "businessIntentOverlays", "deploymentInfo"}
    mapped = {"appliance", "appliances-all", "network-role-and-site", "extra-info", "groups", "region-associations", "template-association", "security-maps", "admin-distance", "sse-interfaces", "overlay-config", "overlay-association", "deployment", "ha-groups", "cluster-profiles", "cluster-profile-mapping"}
    context = {"regions", "interface-labels", "segments", "zones", "template-groups", "system-info"}
    selected = sorted({t for g in associated_groups(cap) for t in selected_names(g)})
    catalogue = {e["id"]: e for e in load_yaml(CATALOGUE)["endpoints"]}
    seen = set()
    readable = {i["id"] for i in cap.index if i["status"] == "ok"}
    for item in cap.index:  # a read the API key's roles refuse is reported, whatever it feeds (docs/decisions/0009)
        if item["status"] == "denied" and item["id"] not in readable and item["id"] not in seen:
            seen.add(item["id"])
            url = (item.get("query") or {}).get("url")
            ledger.append({"item": item["id"], "disposition": "not-readable", "target": ",".join(item.get("sections") or []),
                           "note": "Orchestrator refused GET {}{} (HTTP 403): the API key's RBAC roles do not allow it, so these sections may lack values; "
                                   "check them by hand or widen the key's read roles and capture again".format(item["path"], " url=" + url if url else "")})
    for item in cap.index:
        ident = item["id"]
        if ident in seen or ident in mapped or ident in context:
            continue
        seen.add(ident)
        sections = catalogue.get(ident, {}).get("sections", [])
        if sections and all(s in covered for s in sections):
            continue
        if sections and any(s in covered for s in sections):
            running = cap.data(ident) if isinstance(cap.data(ident), dict) else {}
            uncovered = [s for s in sections if s not in covered and s not in NO_GAP_WITHOUT_EVIDENCE.get(ident, {})]
            uncovered += [s for s, line in NO_GAP_WITHOUT_EVIDENCE.get(ident, {}).items() if line and re.search(line, str(running.get("runningConfig", "")))]
            if not uncovered:
                continue
            ledger.append({"item": ident, "disposition": "unresolved", "target": ",".join(uncovered), "note": "mapped for {}; no mapper for {}".format(", ".join(s for s in sections if s in covered), ", ".join(uncovered))})
            continue
        owner = [s for s in sections if s in selected]
        if owner:
            ledger.append({"item": ident, "disposition": "template-group", "target": ",".join(owner), "note": "template '{}' is selected in an associated template group".format(owner[0])})
        elif item["status"] != "ok" or item.get("empty"):
            ledger.append({"item": ident, "disposition": "empty-or-absent", "target": ",".join(sections), "note": "captured empty, absent on this appliance, or error {}".format(item.get("http", ""))})
        elif ident in DEFAULT_ONLY and default_only(ident, cap.data(ident)):
            ledger.append({"item": ident, "disposition": "default-omitted", "target": ",".join(sections), "note": "holds only the ECOS default ({}), so there is nothing to carry".format(catalogue[ident]["path"])})
        elif not sections:
            ledger.append({"item": ident, "disposition": "outside-preconfig", "target": "", "note": "the preconfig format has no section for {} ({}); set it with a template group, in Orchestrator or with another workflow".format(
                catalogue.get(ident, {}).get("path", ident), catalogue.get(ident, {}).get("notes", "no catalogue note"))})
        else:
            ledger.append({"item": ident, "disposition": "unresolved", "target": ",".join(sections), "note": "the preconfig format has {} but the toolkit has no mapper for it yet".format(", ".join(sections))})
    ledger.append({"item": "template ownership", "disposition": "context", "target": "", "note": "selected templates of associated groups: " + ", ".join(selected)})


def main(argv: Optional[List[str]] = None) -> int:
    import yaml
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--site-type", required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--ignore-template-ownership", action="store_true", help="map sections even when an associated template group provides them (mapper testing only)")
    parser.add_argument("--target-release", default="9.7.1.42046", help="Orchestrator release whose preconfig format is written (default template must exist)")
    parser.add_argument("--decisions", type=Path, help="decisions.yaml: include_from_default, or the older per-section modes (default_mode, sections, extra_variable, extra_fixed)")
    parser.add_argument("--variables", type=Path, help="variables.csv as edited by the owner (decision / column per field); without it a fresh sheet is written and used")
    parser.add_argument("--compare", nargs="+", type=Path, metavar="CAPTURE", help="capture folders of other appliances of this site type: a field that differs becomes a variable, one equal everywhere stays fixed")
    args = parser.parse_args(argv)
    if args.compare and args.variables:
        parser.error("--compare writes a fresh variables.csv: do not combine it with --variables")
    template, template_why = find_default_template(args.target_release)
    if template is None:
        print(template_why, file=sys.stderr)
        return 2
    default_doc = load_yaml(template)
    out = args.out or (Path.cwd() / "preconfig-output" / args.site_type)
    global IGNORE_OWNERSHIP
    IGNORE_OWNERSHIP = args.ignore_template_ownership
    cap = Capture(args.capture)
    ledger: List[Dict[str, Any]] = [{"item": "default template", "disposition": "context", "target": template.name, "note": template_why}]
    try:
        reference = prune(build(cap, ledger), allowed_paths(args.target_release), ledger)
    except NothingToConvert as error:
        print(error, file=sys.stderr)
        return 2
    unseen = sorted(flat_paths(reference) - flat_paths(default_doc))
    if unseen:
        ledger.append({"item": "release fit", "disposition": "context", "target": "", "note": "{} key paths are not shown in the default template {} (valid optional keys or unverified for that release): {}".format(len(unseen), template.name, ", ".join(unseen[:12]) + (" ..." if len(unseen) > 12 else ""))})
    tag_unmapped(cap, ledger)
    scope_by_template(ledger, set(default_doc))
    out.mkdir(parents=True, exist_ok=True)
    decisions_in = load_yaml(args.decisions) if args.decisions else {}
    section_modes = bool(set(decisions_in) - {"include_from_default", "approved_by"}) and not args.variables  # the older per-section choice
    included: List[str] = []
    import copy
    for section in decisions_in.get("include_from_default", []):
        if section in reference:
            continue
        reference[section] = copy.deepcopy(default_doc[section])
        included.append(section)
        ledger.append({"item": "template example", "disposition": "from-default-template", "target": section, "note": "no source value: the section is the default template's example values, every leaf a column (owner choice)"})
        decisions_in.setdefault("sections", {}).setdefault(section, "all")
    order = list(default_doc)
    reference = {k: reference[k] for k in sorted(reference, key=lambda x: order.index(x) if x in order else 999)}
    try:
        others = [] if section_modes else [prune(build(Capture(folder), []), allowed_paths(args.target_release), []) for folder in args.compare or []]
    except NothingToConvert as error:
        print(error, file=sys.stderr)
        return 2
    if others:
        mine = {p for p, _ in sheet_items(reference)}
        extra = sorted({p for o in others for p, _ in sheet_items(o)} - mine)
        ledger.append({"item": "site comparison", "disposition": "context", "target": "", "note": "compared with {} other site(s); {} field(s) exist only at the others (different site type, not in the sheet){}".format(
            len(others), len(extra), ": " + ", ".join(extra[:12]) + (" ..." if len(extra) > 12 else "") if extra else "")})
    (out / "reference.yaml").write_text(yaml.safe_dump(reference, sort_keys=False, allow_unicode=True), encoding="utf-8")
    from generate import Builder
    probe = Builder()
    probe.find_secrets(reference)
    source = {"orchestrator": cap.run["orchestrator"], "release": cap.run["release"], "appliance": cap.run["appliance"], "capture": str(args.capture)}
    write_json(out / "ledger.json", {"toolkit": toolkit_version(), "target_release": args.target_release, "default_template": {"file": template.name, "why": template_why}, "source": source, "entries": ledger})
    (out / "summary.md").write_text(summary_md(ledger, reference, source, args.target_release), encoding="utf-8")
    if section_modes:
        proposal = propose(reference, probe.secret_paths)
        write_json(out / "proposal.json", proposal)
        (out / "proposal.md").write_text("| Section | Leaves | Site-specific | Secrets | Proposed | Why |\n|---|---|---|---|---|---|\n" + "\n".join(
            "| {} | {} | {} | {} | {} | {} |".format(s, p["leaves"], p["site_specific_leaves"], p["secrets"], p["proposed"], p["why"]) for s, p in proposal.items()) + "\n\nSections the source did not provide (add any to `include_from_default` in decisions.yaml to start from the default template's example values): " + ", ".join(s for s in default_doc if s not in reference) + "\n", encoding="utf-8")
        decisions = decisions_in
        (out / "decisions.yaml").write_text(yaml.safe_dump(dict(decisions, approved_by=decisions.get("approved_by", "owner")), sort_keys=False), encoding="utf-8")
        list_fixed = {p for p in ("templateGroups.groups", "businessIntentOverlays.overlays") if list_mode(decisions, p.split(".")[0]) == "fixed"}
        gen = dict(decide=make_decide(decisions), list_fixed=list_fixed)
    else:
        sheet_path = args.variables or out / "variables.csv"
        if not args.variables:
            if sheet_path.exists():
                sheet_path.replace(out / "variables.previous.csv")  # keep the owner's earlier edits
            write_sheet(classify(reference, probe.secret_paths, set(included), others), sheet_path)
        try:
            chosen = read_sheet(sheet_path, reference, probe.secret_paths)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 2
        gen = dict(names=chosen["names"], decide=lambda path, value, secret: path in chosen["names"], list_fixed=chosen["list_fixed"], host_paths=chosen["host_paths"])
    meta = generate(reference, out, args.site_type, release=args.target_release, **gen)
    from collections import Counter
    print("reference sections:", ", ".join(reference))
    print("columns:", len(meta["columns"]), "| ledger:", dict(Counter(e["disposition"] for e in ledger)))
    print("toolkit:", toolkit_version())
    known = set().union(*(d for _, d in REPORT_GROUPS)) | QUIET
    for title, dispositions in REPORT_GROUPS + [("Unknown disposition (a gap)", {e["disposition"] for e in ledger} - known)]:
        n = sum(1 for e in ledger if e["disposition"] in dispositions)
        if n:
            print("{}: {}".format(title.split(":")[0], n))
    print("verdict:", verdict(ledger))
    print("report from", out / "summary.md")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Stage 6 (field level): which leaves of the translated reference become CSV columns, one reviewable row per leaf.

classify() proposes a decision and a reason for every scalar leaf; write_sheet() saves them as variables.csv; the owner edits
`decision` (variable or fixed) or `column` in any editor; read_sheet() turns the edited sheet into what generate.py needs.
Replaces the per-section modes in decisions.py, which still work with `translate.py --decisions`.

Rules, first match wins: secret, hostname, the site's own interface address, an address equal to or inside the site's own
interface addresses (linked to that interface; outside applianceInfo and deploymentInfo, one inside its subnets that is equal at every compared site stays fixed); then in applianceInfo and deploymentInfo (the Deployment page and the
appliance's initial settings) interface names, labels, types, zones, segments, firewall and deployment mode are fixed and every
other value is a variable (owner, docs/decisions/0006), except that a label, zone, segment or firewall mode that differs at the
other sites given is a variable the user may untick (owner, docs/decisions/0018); then, when other sites of the same type are given (`others`), what they
show: a field that differs is a variable, one that is equal everywhere is fixed, one the others lack is fixed and flagged (a
different site type). The template-group and overlay lists are fixed unless the sites differ. Without `others`:
bandwidth/ASN/router ID/peer, blank, any other address (fixed, flagged), everything else (fixed).
"""
import csv
import ipaddress
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from decisions import leaves

SHEET_COLUMNS = ["decision", "column", "linked_to", "value", "path", "reason"]
COMMA_LISTS = {"templateGroups.groups": "template_groups", "businessIntentOverlays.overlays": "overlays"}
RESERVED = {"preconfig_name", "serial_number"}
COLUMN_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ALWAYS_SECTIONS = ("applianceInfo", "deploymentInfo")  # the Deployment page and the appliance's initial settings
ALWAYS = "deployment page or appliance info: always per site"
LAYOUT = "interface names, labels, zones, segments, firewall and deployment mode stay fixed unless you flip them"
LAYOUT_COMPARED = {"interfaceLabel", "zone", "segment", "firewallMode"}  # differing at the compared sites: a ticked choice (docs/decisions/0018)
LAYOUT_DIFFERS = "zone, label, segment or firewall mode that differs between the sites you gave: per site unless you untick it"
LAYOUT_KEYS = {"interfaceName", "dhcpInterfaceName", "raInterfaceName", "haInterface", "interfaceLabel", "interfaceType", "zone", "segment", "firewallMode", "deploymentMode"}
BANDWIDTH_KEY = re.compile(r"(?i)bandwidth$")
SITE_NUMBER_KEY = re.compile(r"^(asn|peerAsn|routerId|rtr_id|vxlanId|vni|ipsecUdpPort|peerIpAddress|peerIp|remoteIp|haPeer\w+|macAddress)$")
LABEL_KEYS = ("interfaceName", "dhcpInterfaceName", "raInterfaceName", "interfaceId", "tunnelName", "name")
ALIASES = {"ipAddressMask": "ip", "nextHop": "next_hop", "outboundMaxBandwidth": "bw_out", "inboundMaxBandwidth": "bw_in",
           "startIpAddress": "start", "endIpAddress": "end", "gatewayIpAddress": "gateway"}
ANY = {"any", "0.0.0.0", "0.0.0.0/0", "::", "::/0"}


def _addr(value: Any) -> Optional[Any]:
    try:
        return ipaddress.ip_interface(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _snake(key: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()


def _walk(doc: Any, path: str) -> Any:
    node = doc
    for part in re.findall(r"[^.\[\]]+", path):
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def column_name(reference: Dict[str, Any], path: str) -> str:
    """Readable name from the path: list items are named by their interfaceName/name, noise sections are dropped."""
    parts = re.findall(r"([^.\[\]]+)|\[(\d+)\]", path)
    tokens: List[str] = []
    walked = ""
    for key, index in parts:
        if key:
            walked = "{}.{}".format(walked, key) if walked else key
            tokens.append(_snake(key))
            continue
        walked += "[{}]".format(index)
        item = _walk(reference, walked)
        label = next((str(item[k]) for k in LABEL_KEYS if isinstance(item, dict) and isinstance(item.get(k), str) and item[k]), index)
        if label != index:
            tokens.pop()  # the list name adds nothing once the item is named
        tokens.append(re.sub(r"\W+", "_", label.lower()).strip("_"))
    if not path.endswith("]") and path.split(".")[-1] in ALIASES:
        tokens[-1] = ALIASES[path.split(".")[-1]]
    name = "_".join(t for t in tokens if t not in ("deployment_info", "appliance_info", "location"))
    return name if not name[:1].isdigit() else "c_" + name


IF_KIND = re.compile(r"^(lan|wan|mgmt|blan|slan|bvi|lo|vti|tun)\d*(\.\d+)?$")
RANKING = Path(__file__).resolve().parents[1] / "references" / "variable-ranking.json"


def setting_key(reference: Dict[str, Any], path: str) -> str:
    """The kind of setting a path is, the same at every site: list items by interface kind (lan, wan, lan.vlan), else [*] or [#].

    deploymentInfo.deploymentInterfaces[2].ipAddressMask on lan0.71 -> deploymentInfo.deploymentInterfaces[lan.vlan].ipAddressMask
    """
    parts, walked = [], ""
    for key, index in re.findall(r"([^.\[\]]+)|\[(\d+)\]", path):
        if key:
            walked = "{}.{}".format(walked, key) if walked else key
            parts.append("." + key if parts else key)
            continue
        walked += "[{}]".format(index)
        item = _walk(reference, walked)
        label = next((str(item[k]) for k in LABEL_KEYS if isinstance(item, dict) and isinstance(item.get(k), str) and item[k]), None)
        kind = IF_KIND.match(label) if label else None
        parts.append("[{}]".format((kind.group(1) + (".vlan" if kind.group(2) else "")) if kind else "*" if label else "#"))
    return "".join(parts)


def labelled(reference: Dict[str, Any], path: str) -> str:
    """The path with each list index replaced by the item's own name (lan0.71, a tunnel name), so sites are compared item by item, not by position."""
    def name(match):
        item = _walk(reference, path[:match.end()])
        label = next((str(item[k]) for k in LABEL_KEYS if isinstance(item, dict) and isinstance(item.get(k), str) and item[k]), None)
        return "[{}]".format(label or match.group(1))
    return re.sub(r"\[(\d+)\]", name, path)


def load_ranking() -> Dict[str, Dict[str, Any]]:
    """setting_key -> {differs_pct, appliances}, from references/variable-ranking.json; empty if the file is missing."""
    try:
        import json
        return {r["setting"]: r for r in json.loads(RANKING.read_text(encoding="utf-8"))["settings"]}
    except (OSError, ValueError, KeyError):
        return {}


def sheet_items(reference: Dict[str, Any]) -> List[Tuple[str, Any]]:
    """The leaves that get a sheet row: scalars, plus each template-group/overlay list as one comma-joined value."""
    items = [(p, v) for p, v in leaves(reference) if v not in ([], {}) and not any(p.startswith(c + "[") for c in COMMA_LISTS)]
    for path in COMMA_LISTS:
        try:
            listed = _walk(reference, path)
        except (KeyError, TypeError, IndexError):
            continue
        if isinstance(listed, list) and listed:
            items.append((path, ",".join(map(str, listed))))
    return items


RELEASE_DEFAULTS = re.compile(r"(^|\.)redistToSDwanFabricRouteMap$|^idsIpsConfig\.(mode|profile)$")  # owner 2026-10-30: per site, never a reason for no row (docs/decisions/0019)


def only_at(mine: Dict[str, Any], theirs: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Non-blank sheet paths (sheet_items of each site) only the other site has, and those only this site has."""
    return sorted(p for p in set(theirs) - set(mine) if theirs[p] not in (None, "")), sorted(p for p in set(mine) - set(theirs) if mine[p] not in (None, ""))


def classify(reference: Dict[str, Any], secret_paths: Set[str], all_sections: Optional[Set[str]] = None,
             others: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """One row per scalar leaf: decision, column, linked_to, value, path, reason (`flag` marks rows needing a human look).

    `others` are the references of other sites of the same type; the comparison replaces the guesses about bandwidth, peers and addresses.
    """
    items = sheet_items(reference)
    seen_at = [dict(sheet_items(o)) for o in others or []]
    own: Dict[str, Tuple[str, Any]] = {}  # interface address -> (path, ip_interface); the site's own addresses
    for path, value in items:
        if path.split(".")[-1].split("[")[0] == "ipAddressMask" and isinstance(value, str) and "/" in value and _addr(value):
            own[path] = (value, _addr(value))
    nets = [a.network for _, a in own.values() if a.network.prefixlen > 0]
    used: Set[str] = set(COMMA_LISTS.values()) | {"hostname"}
    columns: Dict[str, str] = {}  # own interface address value -> column name
    local_cols: Dict[str, str] = {}  # other in-subnet address value -> column name, so the same address is typed once
    primary_of: Dict[str, str] = {}  # address value -> path of the row whose column it shares
    own_name: Dict[str, str] = {}
    rows: List[Dict[str, Any]] = []

    def unique(base: str) -> str:
        name, n = base or "value", 2
        while name in used:
            name, n = "{}_{}".format(base, n), n + 1
        used.add(name)
        return name

    def holds(path: str, primary: str, host: bool = False) -> bool:
        """A link (the same address, or the host part of it) that holds at every other site given, so one column serves all rows."""
        def want(o: Dict[str, Any]) -> Any:
            a = _addr(o.get(primary))
            return str(a.ip) if host and a is not None else o.get(primary)
        return all(path in o and o[path] == want(o) for o in seen_at)

    def row(path: str, value: Any, decision: str, reason: str, column: str = "", linked: str = "", flag: bool = False) -> None:
        rows.append({"decision": decision, "column": column, "linked_to": linked, "value": "" if value is None or path in secret_paths else value,
                     "path": path, "reason": reason, "flag": flag})

    for path, value in items:  # pass 1: interface addresses first, so later rows can link to them
        if path in own:
            own_name[path] = unique(column_name(reference, path))
            columns.setdefault(value, own_name[path])
            primary_of.setdefault(value, path)
    for path, value in items:
        key = re.sub(r"\[\d+\]", "", path).split(".")[-1]
        section = path.split(".")[0].split("[")[0]
        addr = _addr(value)
        if path in secret_paths:
            row(path, value, "variable", "secret: always a column, never stored in the template", unique(column_name(reference, path)))
        elif path == "applianceInfo.hostname":
            row(path, value, "variable", "names the appliance and the output file", "hostname")
        elif (path in COMMA_LISTS or path == "applianceInfo.group") and any(o.get(path, value) != value for o in seen_at):
            row(path, value, "variable", "differs between the sites you gave (group and template groups follow the region or role)",
                COMMA_LISTS.get(path) or unique(column_name(reference, path)))
        elif path in COMMA_LISTS:
            row(path, value, "fixed", "usually the same for every site of this type; make it variable if sites differ", COMMA_LISTS[path])
        elif all_sections and section in all_sections:
            row(path, value, "variable", "section copied from the default template (include_from_default): every field is a column", unique(column_name(reference, path)))
        elif path in own:
            row(path, value, "variable", "this site's own interface address", own_name[path])
        elif addr is not None and str(value).lower() not in ANY and value in columns and holds(path, primary_of[value]):
            row(path, value, "variable", "same address as an interface of this site", columns[value], columns[value])
        elif addr is not None and "/" not in value and any(str(a.ip) == value and holds(path, primary_of[v], True) for v, a in own.values()):
            target = next(columns[v] for v, a in own.values() if str(a.ip) == value and holds(path, primary_of[v], True))
            row(path, value, "variable", "the host part of this site's interface address", target, target)
        elif addr is not None and str(value).lower() not in ANY and any(addr.ip in n for n in nets if n.version == addr.version):
            if seen_at and section not in ALWAYS_SECTIONS and all(path in o and o[path] == value for o in seen_at):  # compare: same at every site (0006 still wins)
                row(path, value, "fixed", "same at every site you gave")
            elif value in local_cols and holds(path, primary_of[value]):
                row(path, value, "variable", "same address as {}".format(local_cols[value]), local_cols[value], local_cols[value])
            else:  # a link that breaks at another site gets its own column
                column = unique(column_name(reference, path))
                local_cols.setdefault(value, column)
                primary_of.setdefault(value, path)
                row(path, value, "variable", "inside one of this site's own subnets", column)
        elif section in ALWAYS_SECTIONS and key in LAYOUT_COMPARED and any(o.get(path, value) != value for o in seen_at):  # owner, docs/decisions/0018
            row(path, value, "variable", LAYOUT_DIFFERS, unique(column_name(reference, path)))
        elif section in ALWAYS_SECTIONS and key in LAYOUT_KEYS:  # owner's rule, docs/decisions/0006
            row(path, value, "fixed", LAYOUT)
        elif section in ALWAYS_SECTIONS and value not in (None, ""):  # blank in the source: nothing to vary (decision 0006)
            row(path, value, "variable", ALWAYS, unique(column_name(reference, path)))
        elif seen_at:
            seen = [o[path] for o in seen_at if path in o]
            if any(v != value for v in seen):  # before the check below: a site that has it must get its own value
                row(path, value, "variable", "differs between the sites you gave", unique(column_name(reference, path)))
            elif len(seen) < len(seen_at) and RELEASE_DEFAULTS.search(path):  # owner, docs/decisions/0019
                row(path, value, "variable", "set at only some of the sites you gave: per site, a blank cell where a site has none", unique(column_name(reference, path)))
            elif len(seen) < len(seen_at):
                row(path, value, "fixed", "only {} of the {} other sites have this field: different site type? (check)".format(len(seen), len(seen_at)), flag=True)
            else:
                row(path, value, "fixed", "same at every site you gave")
        elif BANDWIDTH_KEY.search(key) and not isinstance(value, bool):
            row(path, value, "variable", "bandwidth differs per link", unique(column_name(reference, path)))
        elif SITE_NUMBER_KEY.match(key):
            row(path, value, "variable", "identifier or peer of this site ({})".format(key), unique(column_name(reference, path)))
        elif value is None:
            row(path, value, "fixed", "blank in the source")
        elif addr is not None and str(value).lower() not in ANY:
            row(path, value, "fixed", "address outside this site's own subnets: shared by the site type, or per site? (check)", flag=True)
        elif isinstance(value, str) and value.lower() in ANY:
            row(path, value, "fixed", "any / default route")
        elif key in ("interfaceName", "sourceIpInterface") or key.endswith("InterfaceName"):
            row(path, value, "fixed", "interface names are the site type's hardware layout")
        else:
            row(path, value, "fixed", "setting shared by the site type")
    rows.sort(key=lambda r: (r["decision"] != "variable", not r["flag"]))  # variables, then rows needing a look, then the rest
    return rows


def write_sheet(rows: List[Dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SHEET_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_sheet(path: Path, reference: Dict[str, Any], secret_paths: Set[str]) -> Dict[str, Any]:
    """The edited sheet as {names: path->column, host_paths, list_fixed}; ValueError names every row that cannot be used."""
    with path.open(newline="", encoding="utf-8") as handle:
        sheet = {r["path"]: r for r in csv.DictReader(handle) if r.get("path")}
    errors: List[str] = []
    expected = {p for p, _ in sheet_items(reference)}
    stale = sorted(p for p in sheet if p not in expected)
    if stale:
        errors.append("{} sheet path(s) not in this reference, e.g. {} (sheet from another site, or a section added with include_from_default: pass the same --decisions)".format(len(stale), stale[0]))
    if stale:
        raise ValueError("variables sheet {}:\n  ".format(path) + errors[0])
    names: Dict[str, str] = {}
    by_column: Dict[str, List[str]] = {}
    list_fixed: Set[str] = set()
    for p, r in sheet.items():
        decision = (r.get("decision") or "").strip().lower()
        if decision not in ("variable", "fixed"):
            errors.append("{}: decision {!r} must be variable or fixed".format(p, r.get("decision")))
        elif p in COMMA_LISTS:
            if decision == "fixed":
                list_fixed.add(p)
            else:
                names[p] = (r.get("column") or "").strip() or COMMA_LISTS[p]
        elif decision == "variable" or p in secret_paths or p == "applianceInfo.hostname":
            col = "hostname" if p == "applianceInfo.hostname" else (r.get("column") or "").strip()
            if not COLUMN_OK.match(col) or col in RESERVED:
                errors.append("{}: column {!r} must be letters, digits and _ only, not start with a digit, and not be one of {}".format(p, col, ", ".join(sorted(RESERVED))))
            else:
                names[p] = col
                by_column.setdefault(col, []).append(p)
    host_paths: Set[str] = set()
    for col, paths in by_column.items():
        if len(paths) < 2:
            continue
        values = {p: _walk(reference, p) for p in paths}
        primary = next((p for p in paths if "/" in str(values[p])), paths[0])
        for p in paths:
            if values[p] == values[primary]:
                continue
            a, b = _addr(values[primary]), _addr(values[p])
            if a is not None and b is not None and "/" not in values[p] and a.ip == b.ip:
                host_paths.add(p)
            else:
                errors.append("column {!r} links {} and {}, whose source values differ ({!r} vs {!r})".format(col, primary, p, values[primary], values[p]))
    missing = sorted(expected - set(sheet))
    if missing:
        errors.append("{} reference path(s) missing from the sheet, e.g. {} (re-run translate.py without --variables for a fresh sheet)".format(len(missing), missing[0]))
    if errors:
        raise ValueError("variables sheet {}:\n  ".format(path) + "\n  ".join(errors[:10]))
    return {"names": names, "host_paths": host_paths, "list_fixed": list_fixed}

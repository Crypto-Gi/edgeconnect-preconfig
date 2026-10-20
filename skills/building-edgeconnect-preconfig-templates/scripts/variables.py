"""Stage 6 (field level): which leaves of the translated reference become CSV columns, one reviewable row per leaf.

classify() proposes a decision and a reason for every scalar leaf; write_sheet() saves them as variables.csv; the owner edits
`decision` (variable or fixed) or `column` in any editor; read_sheet() turns the edited sheet into what generate.py needs.
Replaces the per-section modes in decisions.py, which still work with `translate.py --decisions`.

Rules, first match wins: secret, hostname, identity (site, region, location, contact), the site's own interface address,
an address equal to or inside the site's own interface addresses (linked to that interface), then, when other sites of the
same type are given (`others`), what they show: a field that differs is a variable, one that is equal everywhere is fixed,
one the others lack is fixed and flagged (a different site type). The group and the template-group and overlay lists are fixed unless the sites differ. Without `others`: bandwidth/ASN/router ID/peer,
blank, any other address (fixed, flagged), everything else (fixed).
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
IDENTITY_FIXED = {"networkRole", "softwareVersion", "clusterProfile", "group"}
BANDWIDTH_KEY = re.compile(r"(?i)bandwidth$")
SITE_NUMBER_KEY = re.compile(r"^(asn|peerAsn|routerId|rtr_id|vxlanId|vni|ipsecUdpPort|peerIpAddress|peerIp|remoteIp|haPeer\w+|macAddress)$")
LABEL_KEYS = ("interfaceName", "interfaceId", "tunnelName", "name")
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
    own_name: Dict[str, str] = {}
    rows: List[Dict[str, Any]] = []

    def unique(base: str) -> str:
        name, n = base or "value", 2
        while name in used:
            name, n = "{}_{}".format(base, n), n + 1
        used.add(name)
        return name

    def row(path: str, value: Any, decision: str, reason: str, column: str = "", linked: str = "", flag: bool = False) -> None:
        rows.append({"decision": decision, "column": column, "linked_to": linked, "value": "" if value is None or path in secret_paths else value,
                     "path": path, "reason": reason, "flag": flag})

    for path, value in items:  # pass 1: interface addresses first, so later rows can link to them
        if path in own:
            own_name[path] = unique(column_name(reference, path))
            columns.setdefault(value, own_name[path])
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
        elif section == "applianceInfo" and key not in IDENTITY_FIXED:
            row(path, value, "variable", "identifies this site", unique(column_name(reference, path)))
        elif section == "applianceInfo":
            row(path, value, "fixed", "same for every site of this type")
        elif path in own:
            row(path, value, "variable", "this site's own interface address", own_name[path])
        elif addr is not None and str(value).lower() not in ANY and value in columns:
            row(path, value, "variable", "same address as an interface of this site", columns[value], columns[value])
        elif addr is not None and "/" not in value and any(str(a.ip) == value for _, a in own.values()):
            target = next(columns[v] for v, a in own.values() if str(a.ip) == value)
            row(path, value, "variable", "the host part of this site's interface address", target, target)
        elif addr is not None and str(value).lower() not in ANY and any(addr.ip in n for n in nets if n.version == addr.version):
            if value in local_cols:
                row(path, value, "variable", "same address as {}".format(local_cols[value]), local_cols[value], local_cols[value])
            else:
                local_cols[value] = unique(column_name(reference, path))
                row(path, value, "variable", "inside one of this site's own subnets", local_cols[value])
        elif seen_at:
            seen = [o[path] for o in seen_at if path in o]
            if len(seen) < len(seen_at):
                row(path, value, "fixed", "only {} of the {} other sites have this field: different site type? (check)".format(len(seen), len(seen_at)), flag=True)
            elif any(v != value for v in seen):
                row(path, value, "variable", "differs between the sites you gave", unique(column_name(reference, path)))
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

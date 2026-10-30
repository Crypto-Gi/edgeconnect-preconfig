"""Offline checks of one CSV row against columns.json, so a typo is caught by row and column before any rendering or /validate.

cell_problems: whitespace, blank required addresses, address format, integer ranges and enums the default template documents.
subnet_problems: an address that sat inside one of the reference's interface subnets must sit inside the same interface's subnet in the row.
describe: the one-line format hint used in columns.md.
"""
import ipaddress
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Problem, find_default_template  # noqa: E402  guard-ok: imported after the sys.path insert
from options import extract  # noqa: E402


INTERFACE_ADDRESS = re.compile(r"^(deploymentInfo\.deploymentInterfaces\[\d+\])\.(ipAddressMask|nextHop)$")  # blank renders null (DHCP leaves them empty)
# Address columns whose default-template comment also allows words ("behindNat: auto | none | <address>"), so a site that is not behind a fixed address is valid.
ADDRESS_OR_WORD = {"behindNat": ("auto", "none")}


def _words(meta: Dict[str, Any]) -> Tuple[str, ...]:
    return ADDRESS_OR_WORD.get(re.sub(r"\[\d+\]", "", meta["path"]).split(".")[-1], ())


def address_format(value: Any) -> Optional[str]:
    """'ip/prefix' or 'ip' when the reference value is an address, else None."""
    if not isinstance(value, str):
        return None
    try:
        ipaddress.ip_interface(value)
    except ValueError:
        return None
    return "ip/prefix" if "/" in value else "ip"


def option_limits(release: Optional[str]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """(section, key) -> enum/range documented once in the release's default template; keys defined twice are skipped."""
    path = find_default_template(release)[0] if release else None
    if path is None:
        return {}
    items = extract(path.read_text(encoding="utf-8"))
    count: Dict[Tuple[str, str], int] = {}
    for item in items:
        count[(item["section"], item["key"])] = count.get((item["section"], item["key"]), 0) + 1
    # "[0, 4294967295]" is a range written as a list, so a two-integer enum is not trusted
    return {(i["section"], i["key"]): i for i in items if count[(i["section"], i["key"])] == 1 and not (i["enum"] and len(i["enum"]) == 2 and all(type(v) is int for v in i["enum"]))}


def _limit(meta: Dict[str, Any], limits: Dict[Tuple[str, str], Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return limits.get((meta["path"].split(".")[0].split("[")[0], re.sub(r"\[\d+\]", "", meta["path"]).split(".")[-1]))


def describe(meta: Dict[str, Any], limits: Dict[Tuple[str, str], Dict[str, Any]]) -> str:
    if meta.get("secret"):
        return "secret: type it in each row"
    blank = ", blank when {} is not static".format(meta["mode_column"]) if meta.get("mode_column") else ""
    if meta.get("format") == "ip/prefix":
        return "address with prefix length, e.g. 192.0.2.1/24" + blank
    if meta.get("format") == "ip":
        return "address without prefix length" + "".join(", or " + w for w in _words(meta)) + blank
    limit = _limit(meta, limits)
    if limit and limit.get("enum"):
        return "one of " + ", ".join(str(v).lower() if isinstance(v, bool) else str(v) for v in limit["enum"])
    if limit and limit.get("range") and meta["type"] == "int":
        return "whole number {} to {}".format(*limit["range"])
    return {"int": "whole number", "float": "number", "bool": "true or false", "comma-list": "names separated by commas"}.get(meta["type"], "text")


# Secrets /validate rejects when empty ("NAC - server key can not be empty"); other secrets (OSPF, BGP keys) may be blank
REQUIRED_SECRET = re.compile(r"^nac\.server\[\d+\]\.key$")


def cell_problems(columns: Dict[str, Dict[str, Any]], row: Dict[str, str], limits: Dict[Tuple[str, str], Dict[str, Any]]) -> List[str]:
    problems = []
    for name, meta in columns.items():
        cell = row.get(name)
        if cell is None:
            continue
        if meta.get("secret"):
            if cell == "" and REQUIRED_SECRET.match(meta.get("path", "")):
                problems.append(Problem("secret column {} is empty: fill it in the CSV (the toolkit never stores secrets)".format(name), name, "<SECRET>"))
            continue
        fmt = meta.get("format")
        mode = (row.get(meta["mode_column"]) or "") if meta.get("mode_column") else meta.get("mode")
        if mode is not None and mode.strip().lower() not in ("", "static"):  # dhcpv4, dhcpv6, slaac: the default template says leave it empty
            if cell.strip():
                problems.append(Problem("{}: {!r} must be blank when the interface's addressingMode is {}".format(name, cell, mode.strip()), name, cell))
            continue
        if cell != cell.strip():
            problems.append(Problem("{}: {!r} has leading or trailing spaces".format(name, cell), name, cell))
        elif cell == "":
            if (fmt or meta["type"] in ("int", "float", "bool")) and (not meta["optional"] or (mode or "").strip().lower() == "static"):
                problems.append(Problem("{}: blank, expected {}".format(name, describe(meta, limits)), name, cell))
        elif fmt and cell.lower() in _words(meta):
            continue
        elif fmt:
            try:
                ipaddress.ip_interface(cell) if fmt == "ip/prefix" else ipaddress.ip_address(cell)
                if (fmt == "ip/prefix") != ("/" in cell):
                    raise ValueError
            except ValueError:
                problems.append(Problem("{}: {!r} is not {} ({})".format(name, cell, "an address with prefix length" if fmt == "ip/prefix" else "an address without prefix length", describe(meta, limits)), name, cell))
        elif meta["type"] == "bool":
            if cell.lower() not in ("true", "false"):
                problems.append(Problem("{}: {!r} must be true or false".format(name, cell), name, cell))
        elif meta["type"] in ("int", "float") and not re.fullmatch(r"-?\d+" if meta["type"] == "int" else r"-?\d+(\.\d+)?", cell):
            problems.append(Problem("{}: {!r} must be a {}".format(name, cell, "whole number" if meta["type"] == "int" else "number"), name, cell))
        elif cell == str(meta["example"]):
            continue  # the reference appliance runs with this value (e.g. maxBandwidth 0 with auto bandwidth), whatever the template comment says
        else:
            limit = _limit(meta, limits)
            if limit and limit.get("range") and meta["type"] == "int" and re.fullmatch(r"-?\d+", cell) and not limit["range"][0] <= int(cell) <= limit["range"][1]:
                problems.append(Problem("{}: {} is outside {} to {}".format(name, cell, *limit["range"]), name, cell))
            elif limit and limit.get("enum") and cell.lower() not in [str(v).lower() for v in limit["enum"]]:
                problems.append(Problem("{}: {!r} must be {}".format(name, cell, describe(meta, limits)), name, cell))
    return problems


def _is_interface(meta: Dict[str, Any]) -> bool:
    return meta.get("format") == "ip/prefix" and re.sub(r"\[\d+\]", "", meta["path"]).split(".")[-1] == "ipAddressMask"


def _net(text: str) -> Optional[Any]:
    try:
        net = ipaddress.ip_interface(text).network
    except ValueError:
        return None
    return net if net.prefixlen < net.max_prefixlen else None  # a host route says nothing about a subnet


def subnet_problems(columns: Dict[str, Dict[str, Any]], row: Dict[str, str]) -> List[str]:
    """Each address column that sat inside an interface subnet in the reference must sit inside that interface's subnet in the row."""
    reference_nets = {n: _net(str(m["example"])) for n, m in columns.items() if _is_interface(m)}
    problems = []
    for name, meta in columns.items():
        if name in reference_nets or not meta.get("format") or not row.get(name) or not address_format(meta["example"]):
            continue
        ours = ipaddress.ip_interface(str(meta["example"])).ip
        homes = [n for n, net in reference_nets.items() if net is not None and ours.version == net.version and ours in net]
        nets = [(n, _net(row.get(n, ""))) for n in homes]
        nets = [(n, net) for n, net in nets if net is not None]
        try:
            cell = ipaddress.ip_interface(row[name]).ip
        except ValueError:
            continue  # cell_problems reports it
        if nets and not any(cell.version == net.version and cell in net for _, net in nets):
            problems.append("{}: {} is not inside the subnet of {} ({})".format(name, row[name], ", ".join(n for n, _ in nets), ", ".join(row[n] for n, _ in nets)))
    return problems


def interface_addresses(columns: Dict[str, Dict[str, Any]], row: Dict[str, str]) -> List[str]:
    out = []
    for name, meta in columns.items():
        if _is_interface(meta) and row.get(name):
            try:
                out.append(str(ipaddress.ip_interface(row[name]).ip))
            except ValueError:
                pass
    return out

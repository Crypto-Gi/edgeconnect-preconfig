"""Stage 6: which leaves of the translated reference become CSV columns.

The skill reads the source configuration, proposes a mode per section, shows the owner a table, and the owner decides.
Modes per section:
  fixed          every leaf stays a literal copied from the source (secrets are still columns, never literals)
  site-specific  only leaves that identify or address one site become columns (IPs, names, bandwidths, location, ...)
  all            every leaf becomes a column
`templateGroups` and `businessIntentOverlays` use `variable` (comma-separated column) or `fixed` (literal list).
decisions.yaml:
  default_mode: site-specific
  sections: {nac: fixed, bgpSystemConfig: all, templateGroups: variable}
  extra_variable: ['regex on path']      # force columns
  extra_fixed: ['regex on path']         # force literals
  include_from_default: [section, ...]   # sections the source lacks, started from the default template (every leaf a column)
  approved_by: owner                     # written by translate.py as a record; no script checks it
"""
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

IP_VALUE = re.compile(r"^(?:\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?|[0-9A-Fa-f:]*:[0-9A-Fa-f:]*(?:/\d{1,3})?|(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2})$")
SITE_KEY = re.compile(r"(?i)^(hostname|site|asn|peerAsn|routerId|rtr_id|serial\w*|latitude|longitude|city|state|zipCode|country|address2?|email|phoneNumber|name|"
                      r"\w*(ip|ipAddress|ipAddressMask|ipAddressAliasMask|nextHop|gateway\w*|subnet|prefix|mask|peerIpAddress|remoteIp|localIp|sourceIp|startIp|endIp|"
                      r"startIpAddress|endIpAddress|ipaddr|dnsServers|ntpServers|mac\w*|bandwidth|vlan|interfaceName|\w*InterfaceName|sourceIpInterface)|"
                      r"(total|in|out)\w*bandwidth|(in|out)boundMaxBandwidth|outboundMaxBandwidth|maxBandwidth|ipsecUdpPort|haPeer\w+|vxlanId|vni|id)$")
SECTION_MODES = ("fixed", "site-specific", "all")
LIST_MODES = ("variable", "fixed")
SPECIAL = {"templateGroups": "variable", "businessIntentOverlays": "variable"}


def is_site_specific(path: str, value: Any) -> Tuple[bool, str]:
    key = re.sub(r"\[\d+\]", "", path).split(".")[-1]
    if isinstance(value, str) and IP_VALUE.match(value):
        return True, "address value"
    if SITE_KEY.match(key):
        return True, "site-identifying key '{}'".format(key)
    return False, ""


def make_decide(decisions: Dict[str, Any]) -> Callable[[str, Any, bool], bool]:
    """Returns decide(path, value, secret) -> becomes a column?"""
    default = decisions.get("default_mode", "site-specific")
    sections = decisions.get("sections", {})
    force_var = [re.compile(r) for r in decisions.get("extra_variable", [])]
    force_fix = [re.compile(r) for r in decisions.get("extra_fixed", [])]

    def decide(path: str, value: Any, secret: bool) -> bool:
        if secret:
            return True
        if any(r.search(path) for r in force_var):
            return True
        if any(r.search(path) for r in force_fix):
            return False
        mode = sections.get(path.split(".")[0].split("[")[0], default)
        if mode == "all":
            return True
        if mode == "fixed":
            return False
        return is_site_specific(path, value)[0]

    return decide


def list_mode(decisions: Dict[str, Any], path: str) -> str:
    return decisions.get("sections", {}).get(path, SPECIAL.get(path, "variable"))


def leaves(value: Any, path: str = "") -> List[Tuple[str, Any]]:
    if isinstance(value, dict):
        out: List[Tuple[str, Any]] = []
        for k, v in value.items():
            out += leaves(v, "{}.{}".format(path, k) if path else str(k))
        return out
    if isinstance(value, list):
        out = []
        for i, v in enumerate(value):
            out += leaves(v, "{}[{}]".format(path, i))
        return out
    return [(path, value)]


def propose(reference: Dict[str, Any], secret_paths: Optional[set] = None) -> Dict[str, Any]:
    """Per-section counts and the proposed mode, for the owner to review."""
    secret_paths = secret_paths or set()
    table: Dict[str, Any] = {}
    for section, value in reference.items():
        items = [(p, v) for p, v in leaves(value, section)]
        site = [p for p, v in items if is_site_specific(p, v)[0]]
        sec = [p for p, _ in items if p in secret_paths]
        if section in SPECIAL:
            proposed, why = "variable", "which groups/overlays a site gets differs per site type"
        elif not site and not sec:
            proposed, why = "fixed", "no site-identifying leaves found"
        else:
            proposed, why = "site-specific", "{} of {} leaves identify or address a site".format(len(site), len(items))
        table[section] = {"leaves": len(items), "site_specific_leaves": len(site), "secrets": len(sec), "proposed": proposed, "why": why,
                          "examples": site[:4]}
    return table

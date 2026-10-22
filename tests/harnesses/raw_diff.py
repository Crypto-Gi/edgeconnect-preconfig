#!/usr/bin/env python3
"""Real-config round-trip diff: compare two capture.py runs (an original appliance and its clone) on raw appliance data.

The lab apply-and-diff tool checks only the leaves the preconfig asked for, after translating them back to YAML. This compares what the
appliances actually hold: every appliance-scope endpoint in the capture, the CLI running-config of the newest backup, and where
each appliance appears in Orchestrator-wide objects (overlays, template groups, IDS state). The original's values are rewritten
through --map (its IPs, names and nePk to the clone's) first, so whatever still differs is a real round-trip gap.

Trigger a fresh backup of both appliances (POST /appliance/backup) before capturing, or the running-config is stale.
No network: reads the two capture folders only.
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "building-edgeconnect-preconfig-templates" / "scripts"))
from common import load_yaml, paths_of, write_json  # noqa: E402

ID_KEYS = ("ifName", "interfaceName", "name", "self", "prefix", "subnet", "id")
# Values that differ between any two appliances or any two reads; each needs a reason. Grows with live evidence.
DEFAULT_IGNORE = [
    r"^(appliance|network-role-and-site)\.orchestrator:(applianceId|dynamicUuid|portalObjectId)$",  # per-appliance identity
    r"^(appliance|network-role-and-site)\.orchestrator:hasUnsavedChanges$",  # flips with every save
    r"^interface-state\.",  # link state and counters, not configuration
    r"^subnets(-all-vrfs)?\.",  # route table incl. learned routes; configured routes are compared in static-routes-configured
    r"^passthrough-tunnels\.orchestrator:passThrough_\d+",  # breakout tunnels Orchestrator builds per site
    r"^passthrough-tunnels\.orchestrator:ptTunnelUi_\d+\.(remoteIdState|operStatus)$",  # tunnel state
    r"^vrrp\.(live|cached):\[\d+\]\.uptime$",
    r"^system-info\.(live|cached):site_id$", r"^running-config:\s*system site-id \d+$",  # Orchestrator-assigned site id
    r"^running-config:\s*interface tunnel (passThrough_|tunnel_|bondedTunnel_)\d+ ",  # Orchestrator-built tunnel ids
    r"^running-config:\s*overlay \S+ internet-traffic policy local-breakout (primary|backup) passThrough_\d+$",
    r"^running-config:\s*internal set modify - /auth/passwd/user/\S+/passwd_lastchange_date",
    r"^running-config:\s*internal set modify - /cn/tunneld/cluster/config/nonce",
    r"^ipsla-managers\.(live|cached):MGR_ip-monitor_om_passThrough_\d+",  # breakout IP SLA Orchestrator builds per overlay tunnel
    r"^running-config:\s*ip-tracking (action|manager|operation) \S*_om_passThrough_\d+",
]  # evidence: 2026-10-17 lab round trips (spoke and hub clones)


class Mapper:
    """Rewrites the original's identifiers to the clone's: whole tokens only, so 10.0.0.1 does not touch 10.0.0.15."""

    def __init__(self, pairs: Iterable[Tuple[str, str]]) -> None:
        pairs = sorted(((str(a), str(b)) for a, b in pairs if str(a)), key=lambda p: -len(p[0]))
        self.table = dict(pairs)
        self.pattern = None
        if pairs:
            alts = "|".join(re.escape(a) + (r"(?![\w])" if re.match(r".*\w$", a) else "") for a, _ in pairs)
            self.pattern = re.compile(r"(?<![\w.])(?:{})".format(alts))

    def text(self, value: str) -> str:
        return self.pattern.sub(lambda m: self.table[m.group(0)], value) if self.pattern else value

    def data(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {self.text(str(k)): self.data(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.data(v) for v in value]
        return self.text(value) if isinstance(value, str) else value


def align(ref: Any, other: Any) -> Any:
    """Reorder lists of dicts in `other` to the order of `ref` by a shared identity key, so a reordered list is not a diff."""
    if isinstance(ref, dict) and isinstance(other, dict):
        return {k: (align(ref[k], v) if k in ref else v) for k, v in other.items()}
    if not (isinstance(ref, list) and isinstance(other, list)):
        return other
    items = ref + other
    key = next((k for k in ID_KEYS if items and all(isinstance(x, dict) and k in x for x in items)), None)
    if key is None:
        return [align(r, o) for r, o in zip(ref, other)] + other[len(ref):]
    rest, out = list(other), []
    for item in ref:
        match = next((o for o in rest if o[key] == item[key]), None)
        if match is not None:
            rest.remove(match)
            out.append(align(item, match))
    return out + rest


def membership(data: Any, nepk: str, where: str = "") -> List[str]:
    """Paths (list indexes dropped) at which `nepk` appears in an Orchestrator-wide object, as a key or a value."""
    found: List[str] = []
    if isinstance(data, dict):
        for k, v in data.items():
            here = "{}.{}".format(where, "{nePk}" if str(k) == nepk else k).lstrip(".")
            if str(k) == nepk:
                found.append(here)
            found += membership(v, nepk, here)
    elif isinstance(data, list):
        for v in data:
            found += membership(v, nepk, where + "[]")
    elif str(data) == nepk:
        found.append(where)
    return found


def _load(folder: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    run = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    files = {}
    for path in sorted((folder / "capture").glob("*.json")):
        files[path.name[:-5]] = json.loads(path.read_text(encoding="utf-8"))  # "<id>.<label>"
    return run, files


def _config_lines(text: Optional[str]) -> Counter:
    return Counter(line.rstrip() for line in (text or "").splitlines() if line.strip() and not line.lstrip().startswith("#"))


def compare(source: Path, clone: Path, pairs: List[Tuple[str, str]], ignore: List[str]) -> Dict[str, Any]:
    run_a, files_a = _load(source)
    run_b, files_b = _load(clone)
    nepk_a, nepk_b = run_a["appliance"]["nePk"], run_b["appliance"]["nePk"]
    auto = [(nepk_a, nepk_b), (run_a["appliance"].get("hostName") or "", run_b["appliance"].get("hostName") or "")]
    mapper = Mapper([p for p in auto + pairs if p[0] != p[1]])
    skip = [re.compile(r) for r in DEFAULT_IGNORE + ignore]
    report: Dict[str, Any] = {"source": str(source), "clone": str(clone), "map": mapper.table, "endpoints": {}, "running_config": {}, "membership": {}}

    for name in sorted(set(files_a) | set(files_b)):
        a, b = files_a.get(name), files_b.get(name)
        if name.startswith("running-config."):
            continue
        if a is None or b is None:
            report["endpoints"][name] = {"only_in": "source" if b is None else "clone"}
            continue
        if "nePk" not in (a.get("query") or {}):  # Orchestrator scope: compare only where each appliance appears
            ma, mb = sorted(set(membership(a["data"], nepk_a))), sorted(set(membership(b["data"], nepk_b)))
            if ma or mb:
                report["membership"][name] = {"only_source": sorted(set(ma) - set(mb)), "only_clone": sorted(set(mb) - set(ma)),
                                              "matched": len(set(ma) & set(mb))}
            continue
        data_a = mapper.data(a.get("data"))
        data_b = align(data_a, b.get("data"))
        pa = {"{}:{}".format(name, p): v for p, v in paths_of(data_a).items()}
        pb = {"{}:{}".format(name, p): v for p, v in paths_of(data_b).items()}
        diffs, ignored, matched = [], 0, 0
        for p in sorted(set(pa) | set(pb)):
            if p in pa and p in pb and pa[p] == pb[p] and type(pa[p]) is type(pb[p]):
                matched += 1
            elif (pa.get(p, pb.get(p)) in ([], {}) and any(q.startswith(p) for q in (pb if p in pa else pa))):
                continue  # an empty container on one side; its children on the other side are reported instead
            elif any(r.search(p) for r in skip):
                ignored += 1
            else:
                diffs.append({"path": p, "source": pa.get(p, "<absent>"), "clone": pb.get(p, "<absent>")})
        report["endpoints"][name] = {"matched": matched, "ignored": ignored, "diffs": diffs}

    rc_a, rc_b = files_a.get("running-config.backup"), files_b.get("running-config.backup")
    if rc_a and rc_b:
        la = _config_lines(mapper.text((rc_a.get("data") or {}).get("runningConfig") or ""))
        lb = _config_lines((rc_b.get("data") or {}).get("runningConfig"))
        only_a = [l for l in sorted((la - lb).elements()) if not any(r.search("running-config:" + l) for r in skip)]
        only_b = [l for l in sorted((lb - la).elements()) if not any(r.search("running-config:" + l) for r in skip)]
        report["running_config"] = {"backup_time": [(rc_a.get("data") or {}).get("backupTime"), (rc_b.get("data") or {}).get("backupTime")],
                                    "matched": sum((la & lb).values()), "only_source": only_a, "only_clone": only_b}
    eps = report["endpoints"].values()
    report["totals"] = {
        "matched_leaves": sum(e.get("matched", 0) for e in eps),
        "ignored_leaves": sum(e.get("ignored", 0) for e in eps),
        "diff_leaves": sum(len(e.get("diffs", [])) for e in eps),
        "endpoints_only_one_side": sum(1 for e in eps if "only_in" in e),
        "config_lines_matched": report["running_config"].get("matched", 0),
        "config_lines_differ": len(report["running_config"].get("only_source", [])) + len(report["running_config"].get("only_clone", [])),
        "membership_differ": sum(len(m["only_source"]) + len(m["only_clone"]) for m in report["membership"].values()),
    }
    return report


def markdown(report: Dict[str, Any]) -> str:
    t = report["totals"]
    out = ["# Real-config round-trip diff", "", "- Source: `{}`".format(report["source"]), "- Clone: `{}`".format(report["clone"]),
           "- Leaves: {matched_leaves} matched, {diff_leaves} differ, {ignored_leaves} ignored; running-config lines: {config_lines_matched} "
           "matched, {config_lines_differ} differ; Orchestrator memberships differing: {membership_differ}".format(**t), ""]
    rows = [(n, e) for n, e in report["endpoints"].items() if e.get("diffs") or "only_in" in e]
    if rows:
        out += ["## Endpoint differences", "", "| Path | Source (mapped) | Clone |", "|---|---|---|"]
        for name, e in rows:
            if "only_in" in e:
                out.append("| `{}` | only in {} | |".format(name, e["only_in"]))
            for d in e.get("diffs", []):
                out.append("| `{}` | `{}` | `{}` |".format(d["path"], json.dumps(d["source"])[:120], json.dumps(d["clone"])[:120]))
    rc = report["running_config"]
    if rc.get("only_source") or rc.get("only_clone"):
        out += ["", "## Running-config lines that differ", "", "```diff"]
        out += ["- " + l for l in rc["only_source"]] + ["+ " + l for l in rc["only_clone"]] + ["```"]
    diff_m = {n: m for n, m in report["membership"].items() if m["only_source"] or m["only_clone"]}
    if diff_m:
        out += ["", "## Orchestrator memberships that differ", ""]
        for name, m in diff_m.items():
            out.append("- `{}`: only source {}, only clone {}".format(name, m["only_source"], m["only_clone"]))
    return "\n".join(out) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, type=Path, help="capture run folder of the original appliance")
    parser.add_argument("--clone", required=True, type=Path, help="capture run folder of the clone")
    parser.add_argument("--map", nargs="*", default=[], metavar="OLD=NEW", help="identifier in the original -> its value on the clone")
    parser.add_argument("--map-file", type=Path, help="YAML mapping {original: clone} (IPs, subnets, names, MACs)")
    parser.add_argument("--ignore", nargs="*", default=[], metavar="REGEX", help="path regexes ('<id>.<label>:<path>' or 'running-config:<line>') to leave out")
    parser.add_argument("--out", type=Path, help="folder for raw-diff.json and raw-diff.md")
    args = parser.parse_args(argv)
    pairs = [tuple(m.split("=", 1)) for m in args.map]
    if args.map_file:
        pairs += [(str(k), str(v)) for k, v in (load_yaml(args.map_file) or {}).items()]
    report = compare(args.source, args.clone, pairs, args.ignore)
    text = markdown(report)
    if args.out:
        write_json(args.out / "raw-diff.json", report)
        (args.out / "raw-diff.md").write_text(text, encoding="utf-8")
    print(text)
    t = report["totals"]
    return 1 if t["diff_leaves"] or t["config_lines_differ"] or t["membership_differ"] or t["endpoints_only_one_side"] else 0


if __name__ == "__main__":
    sys.exit(main())

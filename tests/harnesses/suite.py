#!/usr/bin/env python3
"""Option test suite: one site type (Jinja + CSV) per preconfig section, one CSV row per documented option value.

For each section of a release's default template:
  1. generate the site type from the section's example values (generate.py);
  2. base row = example row, with names adapted to the validator (group, overlays) and dummy secrets where the example had one;
  3. one variation row per documented option (enum value, range minimum/maximum, and just-outside-range negatives);
  4. render each row (StrictUndefined), parse it, check the value flowed through, check ONLY that field changed
     (mutation test), then send it to the validator's /validate (non-saving).
Writes <work>/test-runs/<UTC>-suite/ with every site type, CSV, rendered YAML, results.json and report.md.
Synthetic values only. Secrets are the literal 'Test-Only-Secret-1'.
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "building-edgeconnect-preconfig-templates" / "scripts"))
from common import DEFAULT_TEMPLATES_DIR, WORK_DIR, load_yaml, parse_yaml, utc_stamp, write_json  # noqa: E402
from generate import generate  # noqa: E402
from options import extract  # noqa: E402
from render import dig, flow_through, read_rows, render_row  # noqa: E402
from validate import Stop, validate_text, validator  # noqa: E402

DEFAULT = DEFAULT_TEMPLATES_DIR / "orchestrator-9.7.1.42046.yml"
SECRET_VALUE = "Test-Only-Secret-1"


def cell(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def paths_of(doc: Any, prefix: str = "") -> Dict[str, Any]:
    if isinstance(doc, dict):
        out: Dict[str, Any] = {}
        for key, item in doc.items():
            out.update(paths_of(item, "{}.{}".format(prefix, key) if prefix else str(key)))
        return out or {prefix: {}}
    if isinstance(doc, list):
        out = {}
        for index, item in enumerate(doc):
            out.update(paths_of(item, "{}[{}]".format(prefix, index)))
        return out or {prefix: []}
    return {prefix: doc}


def changed_paths(a: Any, b: Any) -> List[str]:
    pa, pb = paths_of(a), paths_of(b)
    return sorted(p for p in set(pa) | set(pb) if p not in pa or p not in pb or pa[p] != pb[p] or type(pa[p]) is not type(pb[p]))


def inventory(client: Any) -> Dict[str, Any]:
    groups = client.get("/gms/group")
    names = [g.get("name") for g in (groups if isinstance(groups, list) else [groups]) if g.get("name")]
    overlays = [o["name"] for o in client.get("/gms/overlays/config")]
    return {"groups": names, "overlays": overlays}


def candidate_column(columns: Dict[str, Dict[str, Any]], section: str, key: str, occurrence: int = 0) -> Optional[str]:
    """Column for the n-th documented definition of `key` in `section`: the n-th distinct index-free path, first list item."""
    seen: Dict[str, str] = {}
    for name, meta in columns.items():
        flat = re.sub(r"\[\d+\]", "", meta["path"])
        if meta["path"].split(".")[0] == section and flat.split(".")[-1] == key and meta["type"] in ("string", "int", "bool", "float"):
            seen.setdefault(flat, name)
    names = list(seen.values())
    return names[min(occurrence, len(names) - 1)] if names else None


def load_constraints() -> List[Dict[str, Any]]:
    # a missing file silently turned every known dependency rule into an "unexpected reject", so it is a hard error
    return load_yaml(DEFAULT_TEMPLATES_DIR.parent / "known-constraints.yaml")["constraints"]


def constraint_for(constraints: List[Dict[str, Any]], path: str, value: str) -> Optional[Dict[str, Any]]:
    return next((c for c in constraints if re.search(c["path"], path) and str(c["value"]).lower() == value.lower()), None)



def variations(meta: Dict[str, Any], option: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """(label, cell value, expectation) for one documented option on one column."""
    out: List[Tuple[str, str, str]] = []
    kind = meta["type"]
    if option["enum"]:
        for value in option["enum"]:
            if kind == "bool" and not isinstance(value, bool):
                continue
            if kind == "int" and not isinstance(value, int):
                continue
            out.append(("enum {}".format(cell(value)), cell(value), "accept"))
    if option["range"] and kind == "int":
        low, high = option["range"]
        if option.get("exclusive"):
            out += [("range min+1 {} (exclusive)".format(low + 1), str(low + 1), "accept"), ("range max-1 {} (exclusive)".format(high - 1), str(high - 1), "accept"),
                    ("range bound {} (exclusive)".format(low), str(low), "reject"), ("range bound {} (exclusive)".format(high), str(high), "reject")]
        else:
            out += [("range min {}".format(low), str(low), "accept"), ("range max {}".format(high), str(high), "accept"),
                    ("below range {}".format(low - 1), str(low - 1), "reject"), ("above range {}".format(high + 1), str(high + 1), "reject")]
    return out


def run(orchestrator: str, only: Optional[List[str]], pause: float) -> int:
    import time
    client = validator(orchestrator)
    inv = inventory(client)
    reference = load_yaml(DEFAULT)
    text = DEFAULT.read_text(encoding="utf-8")
    options = extract(text)
    root = WORK_DIR / "test-runs" / "{}-suite".format(utc_stamp())
    base_fix = {"businessIntentOverlays.overlays": ",".join(inv["overlays"][:2]), "applianceInfo.group": inv["groups"][0],
                "ospfSystemConfig.interfaces[1].areaId": "0.0.0.2"}
    results: List[Dict[str, Any]] = []
    constraints = load_constraints()

    def check(site: str, out: Path, columns: Dict[str, Any], template: Path, base_row: Dict[str, str], base_doc: Any, label: str, row: Dict[str, str], expect: str, changed: Optional[Any], also: Optional[List[str]] = None) -> None:
        cols = [] if changed is None else [changed] if isinstance(changed, str) else list(changed)
        record: Dict[str, Any] = {"site_type": site, "test": label, "column": cols[0] if len(cols) == 1 else cols or None, "expect": expect}
        try:
            rendered = render_row(template, row)
            doc = parse_yaml(rendered)
        except Exception as error:  # a render or parse failure is a skill defect, recorded as such
            record.update(stage="render", outcome="skill-defect", detail=str(error)[:300])
            results.append(record)
            return
        (out / "rendered").mkdir(exist_ok=True)
        (out / "rendered" / "{}_preconfig.yml".format(row["hostname"])).write_text(rendered, encoding="utf-8")
        problems = flow_through(doc, columns, row)
        if problems:
            record.update(stage="flow-through", outcome="skill-defect", detail="; ".join(problems[:3]))
            results.append(record)
            return
        if cols:
            allowed = [columns[c]["path"] for c in cols] + ["applianceInfo.hostname"] + list(also or [])
            extra = [p for p in changed_paths(base_doc, doc) if p not in allowed]
            if extra:
                record.update(stage="mutation", outcome="skill-defect", detail="also changed: " + ", ".join(extra[:3]))
                results.append(record)
                return
        verdict = validate_text(client, rendered, row.get("serial_number") or None)
        accepted = verdict["accepted"]
        record.update(stage="validate", accepted=accepted, detail=None if accepted else verdict["error"][:300],
                      outcome="as-expected" if accepted == (expect == "accept") else "unexpected-" + ("reject" if expect == "accept" else "accept"))
        results.append(record)
        time.sleep(pause)

    sections = [s for s in reference if not only or s in only]
    for section in sections:
        sub = {"applianceInfo": {"hostname": "test-pcfg-base"}, section: reference[section]} if section != "applianceInfo" else {"applianceInfo": reference["applianceInfo"]}
        site = "sect-" + section
        out = root / site
        meta = generate(sub, out, site)
        columns = meta["columns"]
        base_row = read_rows(out / "example.csv")[0]
        base_row["hostname"] = "test-pcfg-base"
        for name, col in columns.items():
            if col["secret"] and col["had_value"]:
                base_row[name] = SECRET_VALUE
            if col["path"] in base_fix and name in base_row:
                base_row[name] = base_fix[col["path"]]
        template = out / (site + ".jinja2")
        base_doc = parse_yaml(render_row(template, base_row))
        check(site, out, columns, template, base_row, base_doc, "base: example values", base_row, "accept", None)
        rows = [base_row]
        n = 0
        occurrences: Dict[str, int] = {}
        for option in [o for o in options if o["section"] == section]:
            occurrence = occurrences.get(option["key"], 0)
            occurrences[option["key"]] = occurrence + 1
            name = candidate_column(columns, section, option["key"], occurrence)
            if not name:
                results.append({"site_type": site, "test": "option {} (documented key not in example YAML)".format(option["key"]), "column": None,
                                "expect": "-", "stage": "mapping", "outcome": "key-not-in-yaml", "detail": option["line"][:200]})
                continue
            for label, value, expect in variations(columns[name], option):
                known = constraint_for(constraints, columns[name]["path"], value)
                companions: Dict[str, str] = {}
                if known and known.get("also"):
                    parent = name.rsplit(".", 1)[0]
                    companions = {"{}.{}".format(parent, k): str(v) for k, v in known["also"].items()}
                    missing = [c for c in companions if c not in columns]
                    if missing:
                        results.append({"site_type": site, "test": "{} = {} (companion columns missing)".format(name, label), "column": name, "expect": "-", "stage": "mapping", "outcome": "companion-missing", "detail": ", ".join(missing)})
                        continue
                    label = "{} + {} [dependency: {}]".format(label, ", ".join("{}={}".format(k.rsplit(".", 1)[1], v) for k, v in companions.items()), known["reason"])
                elif known:
                    expect, label = "reject", "{} [known constraint: {}]".format(label, known["reason"])
                n += 1
                row = dict(base_row, hostname="test-pcfg-{}-{}".format(section[:18], n), **{name: value}, **companions)
                rows.append(row)
                check(site, out, columns, template, base_row, base_doc, "{} = {}".format(name, label), row, expect, name, [columns[c]["path"] for c in companions])
                results[-1]["value"] = value
                results[-1]["plain"] = not known
        with (out / "variations.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(base_row))
            writer.writeheader()
            writer.writerows(rows)
    if not only:
        pool = [(r["column"], r["value"]) for r in results if r.get("plain") and r["outcome"] == "as-expected" and r["expect"] == "accept" and "value" in r and isinstance(r["column"], str)]
        for site, parts in COMPOSITES.items():
            composite(site, parts, pool, reference, base_fix, root, check, results)
    release = client.get("/gms/versions").get("current")
    write_json(root / "results.json", {"orchestrator": orchestrator, "release": release, "inventory_used": inv, "write_requests": client.write_requests, "results": results})
    summarize(root, results, orchestrator, release, client.write_requests)
    return 0


COMPOSITES = {
    "branch-router": ["templateGroups", "businessIntentOverlays", "deploymentInfo", "localRoutes", "interfaceConfig", "customApplianceTags"],
    "hub-routing": ["templateGroups", "businessIntentOverlays", "deploymentInfo", "bgpSystemConfig", "segmentBgpSystems", "ospfSystemConfig", "localRoutes", "segmentLocalRoutes", "vrrp", "ipSlaRule"],
    "security-edge": ["templateGroups", "nac", "poE", "secureWebServicesConfig", "ipAllowList", "idsIpsConfig", "thirdPartyServicesAssociation", "natPools", "branchNatMapsConfig", "multicast", "bridgeGroups"],
    "wan-mix": ["templateGroups", "tunnelConfiguration", "loopbackInterface", "virtualTunnelInterface", "pppoe", "usblte", "linkAggregation", "flowRedirection", "availabilityTimeSettings"],
    "everything": None,
}
ROWS_PER_COMPOSITE = 20


def composite(site: str, parts: Optional[List[str]], pool: List[Tuple[str, str]], reference: Dict[str, Any], base_fix: Dict[str, str], root: Path, check: Any, results: List[Dict[str, Any]]) -> None:
    """A multi-section site type; rows are seeded random combinations of 1-4 documented options plus a distinct hostname and serial."""
    import random
    sub = {s: reference[s] for s in (reference if parts is None else ["applianceInfo"] + parts)}
    sub["applianceInfo"] = dict(reference["applianceInfo"])
    out = root / ("site-" + site)
    columns = generate(sub, out, site)["columns"]
    base_row = read_rows(out / "example.csv")[0]
    for name, col in columns.items():
        if col["secret"] and col["had_value"]:
            base_row[name] = SECRET_VALUE
        if col["path"] in base_fix and name in base_row:
            base_row[name] = base_fix[col["path"]]
    base_row.update(hostname="test-pcfg-{}-0".format(site), serial_number="0011223300{:02X}".format(0))
    template = out / (site + ".jinja2")
    base_doc = parse_yaml(render_row(template, base_row))
    check(site, out, columns, template, base_row, base_doc, "base: example values", dict(base_row), "accept", None)
    rng = random.Random(site)
    usable = [(c, v) for c, v in pool if c in columns]
    rows = [base_row]
    for n in range(1, ROWS_PER_COMPOSITE + 1):
        picks: Dict[str, str] = {}
        for column, value in rng.sample(usable, min(rng.randint(1, 4), len(usable))):
            picks[column] = value
        row = dict(base_row, hostname="test-pcfg-{}-{}".format(site, n), serial_number="0011223300{:02X}".format(n), **picks)
        rows.append(row)
        check(site, out, columns, template, base_row, base_doc, "row {}: {}".format(n, "; ".join("{}={}".format(k.split(".", 1)[-1] if "." in k else k, v) for k, v in picks.items())), row, "accept", list(picks))
    with (out / "sites.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(base_row))
        writer.writeheader()
        writer.writerows(rows)


def summarize(root: Path, results: List[Dict[str, Any]], orchestrator: str, release: str, writes: int) -> None:
    from collections import Counter
    total = Counter(r["outcome"] for r in results)
    lines = ["# Option suite on {} {}".format(orchestrator, release), "", "{} tests, write requests {}.".format(len(results), writes), "",
             "| Outcome | Count |", "|---|---|"] + ["| {} | {} |".format(k, v) for k, v in sorted(total.items())]
    by_site: Dict[str, Counter] = {}
    for r in results:
        by_site.setdefault(r["site_type"], Counter())[r["outcome"]] += 1
    lines += ["", "| Site type (one per section) | Tests | As expected | Other |", "|---|---|---|---|"]
    for site, c in by_site.items():
        lines.append("| {} | {} | {} | {} |".format(site, sum(c.values()), c["as-expected"], ", ".join("{} {}".format(v, k) for k, v in c.items() if k != "as-expected")))
    lines += ["", "## Not as expected", "", "| Site type | Test | Expected | Outcome | Detail |", "|---|---|---|---|---|"]
    for r in results:
        if r["outcome"] != "as-expected":
            lines.append("| {} | {} | {} | {} | {} |".format(r["site_type"], r["test"], r["expect"], r["outcome"], (r.get("detail") or "").replace("|", "/")[:200]))
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:12 + len(by_site) + 4]))
    print(root)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orchestrator", default="lab")
    parser.add_argument("--only", nargs="*", help="section names (default: all)")
    parser.add_argument("--pause", type=float, default=0.05)
    args = parser.parse_args(argv)
    try:
        return run(args.orchestrator, args.only, args.pause)
    except Stop as stop:
        print("STOPPED: {}".format(stop), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

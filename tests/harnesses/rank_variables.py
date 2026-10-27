#!/usr/bin/env python3
"""Rank preconfig settings by how often they differ between sites of the same design; writes references/variable-ranking.json.

Offline. For the newest capture of every appliance under <work>/samples (minus --skip Orchestrators, whose appliances
tests have rewritten), translate the capture, then compare every pair of appliances of the same design (same
Orchestrator, same network role) setting by setting. A setting's differs_pct is the share of those pairs where both
have it and the values differ. The file holds setting kinds and percentages only, never values or names.
The CLI's `variables` Top 10 / Top 20 list reads it.
"""
import argparse
import itertools
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "building-edgeconnect-preconfig-templates" / "scripts"))
from common import WORK_DIR  # noqa: E402
from corpus import latest  # noqa: E402
from translate import Capture, allowed_paths, build, prune  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
from variables import RANKING, labelled, setting_key, sheet_items  # noqa: E402

LOCKED = {"applianceInfo.hostname"}  # always a column; ranking it adds nothing


def settings(reference):
    """item-matched path -> (setting kind, value), secrets left out."""
    return {labelled(reference, p): (setting_key(reference, p), v) for p, v in sheet_items(reference)
            if p not in LOCKED and not (isinstance(v, str) and v.startswith("<SECRET"))}


def rank(refs, min_appliances: int = 5, min_pairs: int = 10):
    """refs: [(design, {path: (kind, value)})] -> ranked rows."""
    groups, support, pairs = defaultdict(list), defaultdict(set), defaultdict(lambda: [0, 0])
    for i, (design, items) in enumerate(refs):
        groups[design].append(i)
        for kind, _ in items.values():
            support[kind].add(i)
    for members in groups.values():
        for a, b in itertools.combinations(members, 2):
            first, second = refs[a][1], refs[b][1]
            differs = {}
            for path in first.keys() & second.keys():
                kind = first[path][0]
                differs[kind] = differs.get(kind, False) or first[path][1] != second[path][1]
            for kind, d in differs.items():
                pairs[kind][0] += 1
                pairs[kind][1] += d
    rows = [{"setting": k, "differs_pct": round(100 * d / n), "pairs": n, "appliances": len(support[k])}
            for k, (n, d) in pairs.items() if len(support[k]) >= min_appliances and n >= min_pairs]
    return sorted(rows, key=lambda r: (-r["differs_pct"], -r["appliances"], r["setting"]))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip", nargs="*", default=[], help="Orchestrator folders under samples/ to leave out (lab appliances tests rewrote)")
    parser.add_argument("--target-release", default="9.7.1.42046")
    parser.add_argument("--out", type=Path, default=RANKING)
    args = parser.parse_args(argv)
    refs = []
    for orch in sorted(p for p in (WORK_DIR / "samples").glob("*") if p.is_dir() and p.name not in args.skip):
        for appliance in sorted(p for p in orch.glob("*") if p.is_dir() and not p.name.startswith(".")):
            run = latest(appliance)
            try:
                reference = prune(build(Capture(run), []), allowed_paths(args.target_release), [])
            except Exception as error:  # offline appliance without a cache: nothing to rank
                print("skip {}/{}: {}".format(orch.name, appliance.name, error), file=sys.stderr)
                continue
            design = (orch.name, (reference.get("applianceInfo") or {}).get("networkRole"))
            refs.append((design, settings(reference)))
    rows = rank(refs)
    args.out.write_text(json.dumps({"appliances": len(refs), "designs": len({d for d, _ in refs}), "settings": rows}, indent=1) + "\n", encoding="utf-8")
    print("{} appliances, {} settings ranked -> {}".format(len(refs), len(rows), args.out))
    for r in rows[:20]:
        print("{differs_pct:4}%  {appliances:3}  {setting}".format(**r))
    return 0


if __name__ == "__main__":
    sys.exit(main())

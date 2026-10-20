#!/usr/bin/env python3
"""Stage 2: compare live with cached captures, per endpoint. Offline; reads one capture folder.

Writes live-vs-cached.json and live-vs-cached.md into the capture folder. Differences are reported, never resolved.
"""
import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import write_json  # noqa: E402

# Leaf keys that change on their own between reads; ignored in the comparison (shortcut: name-based, extend with evidence).
VOLATILE = re.compile(r"(?i)(time(stamp)?$|^uptime|lastupdate|^date$|counter|^seq(uence)?$|^stats?$|bytes$|packets$|^lease)")


def flatten(value: Any, path: str = "$") -> Dict[str, Any]:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if not VOLATILE.search(str(key)):
                out.update(flatten(item, "{}.{}".format(path, key)))
        return out or {path: {}}
    if isinstance(value, list):
        out = {}
        for index, item in enumerate(value):
            out.update(flatten(item, "{}[{}]".format(path, index)))
        return out or {path: []}
    return {path: value}


def compare(folder: Path, limit: int = 20) -> List[Dict[str, Any]]:
    index = json.loads((folder / "capture-index.json").read_text())
    by_id: Dict[str, Dict[str, Any]] = {}
    for item in index:
        by_id.setdefault(item["id"], {})[item["label"]] = item
    results = []
    for ident, labels in by_id.items():
        if "live" not in labels and "cached" not in labels:
            continue
        live, cached = labels.get("live", {}), labels.get("cached", {})
        row: Dict[str, Any] = {"id": ident, "path": (live or cached)["path"], "live": live.get("status"), "cached": cached.get("status")}
        if live.get("status") == "ok" and cached.get("status") == "ok":
            a = flatten(json.loads((folder / live["file"]).read_text())["data"])
            b = flatten(json.loads((folder / cached["file"]).read_text())["data"])
            diffs = [{"path": p, "live": a.get(p, "<absent>"), "cached": b.get(p, "<absent>")} for p in sorted(set(a) | set(b)) if a.get(p, "<absent>") != b.get(p, "<absent>")]
            row.update(result="same" if not diffs else "different", differences=len(diffs), sample=diffs[:limit])
        else:
            row.update(result="one-side-only" if "ok" in (live.get("status"), cached.get("status")) else "neither")
        results.append(row)
    return results


def markdown(folder: Path, results: List[Dict[str, Any]]) -> str:
    run = json.loads((folder / "run.json").read_text())
    counts = {k: sum(1 for r in results if r["result"] == k) for k in ("same", "different", "one-side-only", "neither")}
    lines = ["# Live versus cached: {} {} ({})".format(run["orchestrator"], run["appliance"]["nePk"], run["appliance"]["hostName"]), "",
             "Capture {}, Orchestrator release {}. Volatile keys ignored: `{}`.".format(run["started"], run["release"], VOLATILE.pattern), "",
             "Summary: " + ", ".join("{} {}".format(v, k) for k, v in counts.items()), "",
             "| Endpoint | Live | Cached | Result | Differences |", "|---|---|---|---|---|"]
    lines += ["| `{}` | {} | {} | {} | {} |".format(r["path"], r["live"], r["cached"], r["result"], r.get("differences", "")) for r in results]
    for r in results:
        if r["result"] == "different":
            lines += ["", "## `{}` ({} differences, first {})".format(r["path"], r["differences"], len(r["sample"])), "",
                      "| Path | Live | Cached |", "|---|---|---|"]
            lines += ["| `{}` | `{}` | `{}` |".format(d["path"], json.dumps(d["live"]), json.dumps(d["cached"])) for d in r["sample"]]
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="capture folder containing capture-index.json")
    args = parser.parse_args(argv)
    results = compare(args.folder)
    write_json(args.folder / "live-vs-cached.json", results)
    (args.folder / "live-vs-cached.md").write_text(markdown(args.folder, results), encoding="utf-8")
    print(", ".join("{} {}".format(sum(1 for r in results if r["result"] == k), k) for k in ("same", "different", "one-side-only", "neither")))
    return 0


if __name__ == "__main__":
    sys.exit(main())

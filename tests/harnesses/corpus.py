#!/usr/bin/env python3
"""Run the offline pipeline on every captured appliance and validate each result on a validator Orchestrator.

For each capture folder under <work>/reference and <work>/samples: translate.py (target release from the source
Orchestrator: 9.6.x -> 9.6.4.40046, otherwise 9.7.1.42046), then check.py --validate (names substituted unless the source is the validator itself).
Writes <work>/test-runs/corpus/report.md with per-appliance sections, ledger counts and the validation result.
Reads nothing from an Orchestrator itself; only check.py sends non-saving /validate requests to the validator.
"""
import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "toolkit" / "scripts"))
from common import SKILL_DIR, WORK_DIR  # noqa: E402

HERE = SKILL_DIR / "scripts"


def latest(folder: Path) -> Optional[Path]:
    """Newest complete capture, preferring one taken while the appliance was reachable.

    A partial `capture.py --only` run would hide every endpoint it skipped; an unreachable run has no live reads."""
    def meta(p: Path) -> dict:
        run = p / "run.json"
        return json.loads(run.read_text()) if run.exists() else {}
    runs = [(meta(p), p) for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")]
    runs = sorted((m.get("reachable") is not False, p.name, p) for m, p in runs if not m.get("only"))
    return runs[-1][2] if runs else None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validator", default="lab")
    parser.add_argument("--only", nargs="*", help="orchestrator/nePk keys, e.g. prod/0.NE")
    args = parser.parse_args(argv)
    captures = []
    for role in ("reference", "samples"):
        for orch in sorted((WORK_DIR / role).glob("*")):
            for appliance in sorted(orch.glob("*")) if orch.is_dir() and orch.name != "default-templates" else []:
                run = latest(appliance) if appliance.is_dir() else None
                if run and (run / "run.json").exists():
                    captures.append((orch.name, appliance.name, run))
    out_root = WORK_DIR / "test-runs" / "corpus"
    rows = []
    for orch, nepk, run in captures:
        key = "{}/{}".format(orch, nepk)
        if args.only and key not in args.only:
            continue
        info = json.loads((run / "run.json").read_text())
        target = "9.6.4.40046" if str(info["release"]).startswith("9.6") else "9.7.1.42046"
        out = out_root / "{}-{}".format(orch, nepk)
        t = subprocess.run([sys.executable, str(HERE / "translate.py"), "--capture", str(run), "--site-type", "{}-{}".format(orch, nepk), "--out", str(out), "--target-release", target], capture_output=True, text=True)
        row = {"key": key, "host": info["appliance"].get("hostName"), "model": info["appliance"].get("model"), "ecos": info["appliance"].get("softwareVersion"), "release": info["release"], "target": target}
        if t.returncode:
            row.update(status="translate-error", detail=t.stderr.strip().splitlines()[-1][:200])
            rows.append(row)
            continue
        cmd = [sys.executable, str(HERE / "check.py"), str(out), "--validate", args.validator] + ([] if orch == args.validator else ["--substitute"])
        c = subprocess.run(cmd, capture_output=True, text=True)
        checks = json.loads((out / "checks.json").read_text()) if (out / "checks.json").exists() else {}
        ledger = json.loads((out / "ledger.json").read_text())["entries"]
        ref = (out / "reference.yaml").read_text().splitlines()
        sections = [l.split(":")[0] for l in ref if l and not l.startswith((" ", "-"))]
        verdict = (checks.get("checks", {}).get("10 validation recorded", {}) or {}).get("detail") or []
        row.update(status="ok" if checks.get("ok") else "failed", sections=sections, ledger=dict(Counter(e["disposition"] for e in ledger)),
                   failing=[n for n, v in checks.get("checks", {}).items() if not v["ok"]],
                   error=(verdict[0]["error"] if verdict and not verdict[0]["accepted"] else None) or (c.stderr.strip().splitlines()[-1][:200] if c.returncode and not checks else None),
                   notes=(checks.get("validator") or {}).get("substitution_notes", []))
        rows.append(row)
        print("{:<16} {:<8} {} sections".format(key, row["status"], len(sections)), flush=True)
    lines = ["# Corpus run", "", "{} appliances; validator `{}`.".format(len(rows), args.validator), "",
             "| Appliance | Host | Model | ECOS | Source release | Target | Result | Sections written | Unresolved | Orchestrator-managed skipped |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| {key} | {host} | {model} | {ecos} | {release} | {target} | {status} | {n} | {u} | {m} |".format(n=len(r.get("sections", [])), u=r.get("ledger", {}).get("unresolved", ""), m=r.get("ledger", {}).get("orchestrator-managed", ""), **r))
    bad = [r for r in rows if r["status"] != "ok"]
    if bad:
        lines += ["", "## Not passing", ""] + ["- {}: {} {}".format(r["key"], r.get("failing") or r.get("detail"), r.get("error") or "") for r in bad]
    shown = len(lines)  # the console shows the table and every failure; notes and coverage stay in report.md
    notes = sorted({n for r in rows for n in r.get("notes", [])})
    if notes:
        lines += ["", "## Substitution notes (what validation could not cover)", ""] + ["- " + n for n in notes]
    covered = Counter(s for r in rows for s in r.get("sections", []))
    lines += ["", "## Sections written across the corpus", "", ", ".join("{} ({})".format(k, v) for k, v in sorted(covered.items()))]
    (out_root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:shown]))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())

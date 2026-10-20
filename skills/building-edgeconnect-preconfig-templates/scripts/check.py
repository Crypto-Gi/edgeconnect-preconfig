#!/usr/bin/env python3
"""Stage 8 checks for one site type folder (output of translate.py or generate.py), plus optional /validate.

1 headers match   2 renders and parses   4 variables flow through   5 mutation (one column at a time)
8 example row reproduces reference.yaml   10 validation recorded (with --validate)   7 no secrets in the example row
Reads <site>.jinja2, <site>.csv, example.csv, columns.json, reference.yaml. Writes checks.json and rendered/.
Extra rows (--rows CSV) are rendered and checked the same way.
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import changed_paths, load_yaml, normalize_serial, parse_yaml, resolve_output_filename, write_json  # noqa: E402
from render import flow_through, headers, read_rows, render_row  # noqa: E402


def mutate(value: str, meta: Dict[str, Any]) -> Optional[str]:
    kind = meta["type"]
    if kind == "bool":
        return "false" if value.lower() == "true" else "true"
    if kind == "int":
        return str(int(value) + 1)
    if kind == "float":
        return str(float(value) + 1)
    if kind == "comma-list":
        head, _, last = value.rpartition(",")
        return (head + "," if head else "") + last + "-m"
    return value + "-m"


def run(site_dir: Path, site: str, extra_rows: Optional[Path], validate: Optional[str], substitute: bool = False) -> Dict[str, Any]:
    meta = json.loads((site_dir / "columns.json").read_text())
    columns = meta["columns"]
    site = meta.get("site_type", site)
    template = site_dir / (site + ".jinja2")
    result: Dict[str, Any] = {"site_type": site, "checks": {}}

    def record(name: str, ok: bool, detail: Any = "") -> None:
        result["checks"][name] = {"ok": ok, "detail": detail}

    wanted = list(columns) + [c for c in meta["runner_columns"]]
    csv_header = (site_dir / (site + ".csv")).read_text().splitlines()[0].split(",")
    found = headers(template.read_text())
    record("1 headers match", set(found) == set(columns) and set(csv_header) == set(wanted) | ({"hostname"} - set(wanted)), {"template": len(found), "csv": len(csv_header)})

    rows = read_rows(site_dir / "example.csv") + (read_rows(extra_rows) if extra_rows else [])
    rendered_dir = site_dir / "rendered"
    rendered_dir.mkdir(exist_ok=True)
    val_client, inv, subst_notes = None, None, []
    if validate:
        from validate import validate_text, validator
        val_client = validator(validate)
        if substitute:
            from synth import dump, inventory, substitute as swap
            inv = inventory(val_client)

    docs, files, problems, verdicts = [], [], [], []
    seen_files: Dict[str, int] = {}
    for idx, row in enumerate(rows, 1):
        row_problems: List[str] = []
        pcfg_name, fname = ("row_{}".format(idx), "")
        try:
            pcfg_name, fname = resolve_output_filename(row, idx)
            if fname in seen_files:
                row_problems.append("duplicate output filename {!r} in rows {} and {}".format(fname, seen_files[fname], idx))
            else:
                seen_files[fname] = idx
            norm_serial = normalize_serial(row.get("serial_number")) if row.get("serial_number") else None
            text = render_row(template, row)
            doc = parse_yaml(text)
            row_problems.extend(flow_through(doc, columns, row))
        except Exception as error:
            row_problems.append(str(error)[:150])
            text, doc, norm_serial = "", None, None

        if not row_problems and val_client is not None:
            row_for_validation = dict(row)
            for n, c in columns.items():
                if c["secret"] and c["had_value"] and not row_for_validation[n]:
                    row_for_validation[n] = "Test-Only-Secret-1"
            val_text = render_row(template, row_for_validation)
            if inv is not None:
                val_text = dump(swap(parse_yaml(val_text), inv, subst_notes))
            verdict = validate_text(val_client, val_text, norm_serial)
            verdicts.append({"file": fname, "accepted": verdict["accepted"], "error": verdict["error"]})
            if not verdict["accepted"]:
                row_problems.append("Orchestrator /validate rejected row: {}".format(verdict["error"]))

        if row_problems:
            problems.extend("{}: {}".format(pcfg_name, p) for p in row_problems)
            if fname and (rendered_dir / fname).exists() and seen_files.get(fname) == idx:
                (rendered_dir / fname).unlink()
            continue

        path = rendered_dir / fname
        path.write_text(text, encoding="utf-8")
        docs.append((row, doc))
        files.append(path)

    record("2 renders and parses", len(docs) == len(rows), "{} valid of {} rows".format(len(docs), len(rows)))
    record("4 variables flow through", not problems, problems[:5])

    if docs:
        row0, doc0 = docs[0]
        bad = []
        for name, col in columns.items():
            changed = mutate(row0[name], col) if row0[name] != "" or col["type"] in ("string", "comma-list") else None
            if changed is None:
                continue
            try:
                doc = parse_yaml(render_row(template, dict(row0, **{name: changed})))
            except Exception as error:
                bad.append("{}: {}".format(name, str(error)[:80]))
                continue
            extra = [p for p in changed_paths(doc0, doc) if p != col["path"] and not p.startswith(col["path"] + "[")]
            if extra:
                bad.append("{} also changed {}".format(name, extra[:2]))
        record("5 mutation: one column changes only its field", not bad, bad[:5])

        reference = load_yaml(site_dir / "reference.yaml") if (site_dir / "reference.yaml").exists() else None
        if reference is not None:
            diffs = [p for p in changed_paths(reference, doc0)]
            secret_paths = {c["path"] for c in columns.values() if c["secret"]}
            record("8 example row reproduces reference", set(diffs) <= secret_paths, [d for d in diffs if d not in secret_paths][:8])
    leaks = [n for n, c in columns.items() if c["secret"] and rows[0].get(n)]
    record("7 example row holds no secret values", not leaks, leaks)

    if validate:
        record("10 validation recorded", bool(verdicts) and all(v["accepted"] for v in verdicts), verdicts)
        result["validator"] = {"orchestrator": validate, "write_requests": val_client.write_requests, "substitution_notes": sorted(set(subst_notes))}
    result["ok"] = all(c["ok"] for c in result["checks"].values())
    write_json(site_dir / "checks.json", result)
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site_dir", type=Path)
    parser.add_argument("--rows", type=Path, help="extra CSV rows to render and check")
    parser.add_argument("--validate", metavar="ORCHESTRATOR", help="validator nickname (e.g. lab)")
    parser.add_argument("--substitute", action="store_true", help="replace object names by names that exist on the validator (for configurations taken from another Orchestrator)")
    args = parser.parse_args(argv)
    from validate import Stop
    try:
        result = run(args.site_dir, args.site_dir.name, args.rows, args.validate, args.substitute)
    except Stop as stop:
        print("STOPPED: {}".format(stop), file=sys.stderr)
        return 2
    for name, c in result["checks"].items():
        print("{} {}{}".format("PASS" if c["ok"] else "FAIL", name, "" if c["ok"] else "  " + json.dumps(c["detail"])[:400]))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

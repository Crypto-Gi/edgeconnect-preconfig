#!/usr/bin/env python3
"""Stage 8: render one preconfig YAML per CSV row, with pyedgeconnect's Jinja settings plus StrictUndefined.

Also holds the header extractor (pyedgeconnect's regex, unchanged in behaviour) and the type-aware check
that every column's row value comes through the YAML unchanged.
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cells import INTERFACE_ADDRESS, cell_problems, interface_addresses, option_limits, subnet_problems  # noqa: E402
from common import Problem  # noqa: E402  guard-ok: imported after the sys.path insert

# From pyedgeconnect examples/generate_preconfig/templates/generate_csv_from_jinja2.py (MIT): data['x'] or data["x"].
HEADER_RE = re.compile(r"data\[['\"](.*?)['\"]\]")


def headers(template_text: str) -> List[str]:
    seen: List[str] = []
    for name in HEADER_RE.findall(template_text):
        if name not in seen:
            seen.append(name)
    return seen


def environment(template_dir: Path):
    import jinja2
    return jinja2.Environment(loader=jinja2.FileSystemLoader(str(template_dir)), trim_blocks=True, lstrip_blocks=True, undefined=jinja2.StrictUndefined)


def render_row(template_path: Path, row: Dict[str, str]) -> str:
    missing = [h for h in headers(template_path.read_text(encoding="utf-8")) if h not in row]
    if missing:
        raise ValueError("CSV row lacks columns the template uses: " + ", ".join(missing[:8]))
    return environment(template_path.parent).get_template(template_path.name).render(data=row)


def read_rows(csv_path: Path) -> List[Dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def dig(doc: Any, path: str) -> Tuple[bool, Any]:
    node = doc
    for part in re.findall(r"[^.\[\]]+", path):
        if isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return False, None
    return True, node


def expected(meta: Dict[str, Any], cell: str) -> Tuple[bool, Any]:
    """(present, python value) the YAML should hold for a CSV cell."""
    kind = meta["type"]
    if cell == "" and (meta["optional"] or INTERFACE_ADDRESS.match(meta["path"])):
        return True, None  # blank optional cell renders as "key:" (null), as in the default template
    if kind == "int":
        return True, int(cell)
    if kind == "float":
        return True, float(cell)
    if kind == "bool":
        return True, cell.strip().lower() == "true"
    if kind == "comma-list":
        return True, [part.strip() for part in cell.split(",")]
    return True, cell


def flow_through(doc: Any, columns: Dict[str, Dict[str, Any]], row: Dict[str, str]) -> List[str]:
    problems = []
    for name, meta in columns.items():
        try:
            want_present, want = expected(meta, row[name])
        except ValueError as error:
            problems.append(Problem("{}: cell {!r} is not a valid {} ({})".format(name, row[name], meta["type"], error), name, row[name]))
            continue
        present, got = dig(doc, meta["path"])
        if want_present != present or (present and got != want):
            problems.append(Problem("{}: expected {!r} at {}, found {!r}".format(name, want if want_present else "<omitted>", meta["path"], got if present else "<omitted>"), name, row[name]))
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    from validate import Stop
    try:
        return _main(argv)
    except Stop as stop:  # unknown validator, or 401/403/network on /validate: every row would fail the same way
        print("STOPPED: {}".format(stop), file=sys.stderr)
        return 2


def _main(argv: Optional[List[str]] = None) -> int:
    from common import normalize_serial, output_root, parse_yaml, problem_data, record, resolve_output_filename
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-dir", type=Path, help="site folder from translate.py: finds the template and columns.json, writes rendered/ there")
    parser.add_argument("--template", type=Path, help="template path (not needed with --site-dir)")
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--out", type=Path, help="folder for rendered <preconfig_name>.yml files (default: <site-dir>/rendered, else ./preconfig-output/<site>/rendered)")
    parser.add_argument("--columns", type=Path, help="columns.json from generate.py, enables the cell and flow-through checks (automatic with --site-dir)")
    parser.add_argument("--validate", metavar="ORCHESTRATOR", help="validate each row via Orchestrator /validate before writing its .yml file")
    parser.add_argument("--substitute", action="store_true", help="with --validate: replace object names by names that exist on the validator (configuration from another Orchestrator); the written file keeps the real names")
    args = parser.parse_args(argv)
    if args.site_dir:
        meta = json.loads((args.site_dir / "columns.json").read_text())
        args.template = args.template or args.site_dir / "{}.jinja2".format(meta["site_type"])
        args.columns = args.columns or args.site_dir / "columns.json"
        args.out = args.out or args.site_dir / "rendered"
    elif not args.template:
        parser.error("provide --site-dir or --template")
    out_dir = args.out or (output_root() / args.template.stem / "rendered")
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = json.loads(args.columns.read_text()) if args.columns else {}
    columns = meta.get("columns")
    limits = option_limits(meta.get("release"))
    seen_addresses: Dict[str, int] = {}
    secrets = {n for n, m in (columns or {}).items() if m.get("secret")}
    val_client, inv = None, None
    if args.validate:
        from validate import validate_text, validator
        val_client = validator(args.validate)
        if args.substitute:
            from synth import dump, inventory, substitute
            inv = inventory(val_client)
    failed = 0
    seen_files: Dict[str, int] = {}
    seen_serials: Dict[str, int] = {}
    for idx, row in enumerate(read_rows(args.csv), 1):
        problems: List[str] = []
        pcfg_name, fname = ("row_{}".format(idx), "")
        try:
            pcfg_name, fname = resolve_output_filename(row, idx)
            if fname in seen_files:
                problems.append(Problem("duplicate output filename {!r} (already used by row {})".format(fname, seen_files[fname]), "preconfig_name", pcfg_name))
            else:
                seen_files[fname] = idx
        except ValueError as err:
            problems.append(str(err))
        norm_serial = None
        if row.get("serial_number"):
            try:
                norm_serial = normalize_serial(row.get("serial_number"))
                if norm_serial in seen_serials:
                    problems.append(Problem("duplicate serial_number {!r} (already used by row {})".format(norm_serial, seen_serials[norm_serial]), "serial_number", norm_serial))
                elif norm_serial:
                    seen_serials[norm_serial] = idx
            except ValueError as err:
                problems.append(str(err))
        text = ""
        if columns and not problems:
            problems.extend(cell_problems(columns, row, limits) + subnet_problems(columns, row))
            for address in interface_addresses(columns, row):
                if address in seen_addresses and seen_addresses[address] != idx:
                    print("WARN {}: interface address {} is also used by row {} (fine for private LANs, a mistake for WAN addresses)".format(pcfg_name, address, seen_addresses[address]), flush=True)
                seen_addresses.setdefault(address, idx)
        if not problems:
            try:
                text = render_row(args.template, row)
                doc = parse_yaml(text)
                if columns:
                    problems.extend(flow_through(doc, columns, row))
            except Exception as err:
                problems.append(str(err))
        if not problems and val_client is not None:
            verdict = validate_text(val_client, dump(substitute(parse_yaml(text), inv)) if inv is not None else text, norm_serial)
            if not verdict["accepted"]:
                problems.append("Orchestrator /validate rejected row: {}".format(verdict["error"]))
        if problems:
            failed += 1
            if fname and (out_dir / fname).exists() and seen_files.get(fname) == idx:
                (out_dir / fname).unlink()
            print("FAIL {}{}".format(pcfg_name, "".join("\n     " + p for p in problems[:5])), flush=True)
            record("row", index=idx, preconfig_name=pcfg_name, status="failed", file=None, problems=[problem_data(idx, p, secrets) for p in problems])
            continue
        (out_dir / fname).write_text(text, encoding="utf-8")
        print("ok   {} -> {}".format(pcfg_name, out_dir / fname), flush=True)
        record("row", index=idx, preconfig_name=pcfg_name, status="ok", file=str(out_dir / fname), problems=[])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

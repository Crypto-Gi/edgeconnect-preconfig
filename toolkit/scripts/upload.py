#!/usr/bin/env python3
"""Row-by-row preconfig validation, immediate local YAML generation for valid rows, conflict check, and approval-gated upload.

Behavior:
  1. Processes CSV rows one by one: checks the cells (cells.py) -> renders in memory -> runs local type/serial checks -> calls Orchestrator /validate.
  2. Immediately writes <out>/rendered/<preconfig_name>.yml to disk as soon as a row passes validation (never waits for the
     rest of the CSV, and never writes a .yml file for an invalid row). Local .yml files are kept permanently.
  3. Checks existing Orchestrator preconfigs (GET /gms/appliance/preconfiguration): if a row's preconfig_name or
     serial_number already exists on Orchestrator, only that conflicting row is skipped from upload; other valid rows proceed.
  4. Never uploads automatically: without --approve, prints the validation/conflict table and stops with 0 writes.
     With --approve, uploads only the valid, non-conflicting rows and verifies them via readback.
  5. --yaml FILE|DIR ...: checks ready preconfig files the same way and uploads every file that passes without --approve
     (docs/decisions/0017: uploading is not applying). autoApply is always off; nothing here ever applies a preconfig.
"""
import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cells import cell_problems, interface_addresses, option_limits, subnet_problems  # noqa: E402
from common import EdgeConnectError, client_for, normalize_serial, output_root, parse_yaml, resolve_output_filename, utc_stamp, write_json  # noqa: E402  guard-ok: imported after the sys.path insert
from render import flow_through, read_rows, render_row  # noqa: E402
from validate import Stop, validate_text  # noqa: E402


def fetch_existing_preconfigs(client: Any) -> List[Dict[str, Any]]:
    data = client.get("/gms/appliance/preconfiguration")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        return [x for x in data.values() if isinstance(x, dict)]
    return []


def existing_names_serials(client: Any) -> Tuple[set, set]:
    """Names and normalised serial numbers of the preconfigs already on the Orchestrator."""
    existing = fetch_existing_preconfigs(client)
    names = {str(p.get("name") or "").strip() for p in existing if p.get("name")}
    serials = set()
    for p in existing:
        if p.get("serialNum"):
            try:
                norm = normalize_serial(str(p.get("serialNum")))
                if norm:
                    serials.add(norm)
            except ValueError:
                serials.add(str(p.get("serialNum")).strip().upper())
    return names, serials


def upload_ready(client: Any, ready_to_upload: List[Dict[str, Any]]) -> int:
    """Upload each {"entry", "text"} (never applied: autoApply is always false), then read the list back; returns the number uploaded."""
    uploaded_count = 0
    for item in ready_to_upload:
        entry = item["entry"]
        payload: Dict[str, Any] = {
            "name": entry["preconfig_name"],
            "configData": base64.b64encode(item["text"].encode("utf-8")).decode("ascii"),
            "autoApply": False,  # this toolkit never applies, not even later through Orchestrator (docs/decisions/0008)
            "comment": "uploaded via building-edgeconnect-preconfig-templates",
        }
        if entry["serial_number"]:
            payload["serialNum"] = entry["serial_number"]
        try:
            resp = client.request("POST", "/gms/appliance/preconfiguration", json_body=payload, expected_status=(200, 201, 204), expect_json=True)
        except EdgeConnectError as err:  # one rejected upload must not hide the rows already uploaded or stop the rest
            entry["status"] = "upload-failed"
            entry["detail"] = "upload failed: {}".format(str(err)[:300])
            print("UPLOAD FAILED | {:<24} | {}".format(entry["preconfig_name"], entry["detail"]), flush=True)
            continue
        preconfig_id = resp.get("id") if isinstance(resp, dict) else None
        entry["status"] = "uploaded"
        entry["preconfig_id"] = preconfig_id
        entry["detail"] = "uploaded to Orchestrator (id={})".format(preconfig_id)
        uploaded_count += 1
        print("UPLOADED | {:<24} | serial={} | id={}".format(entry["preconfig_name"], entry["serial_number"] or "-", preconfig_id), flush=True)

    # Readback verification
    try:
        after_names = {str(p.get("name") or "").strip() for p in fetch_existing_preconfigs(client)}
    except EdgeConnectError as err:  # keep the report of what was uploaded even if the readback fails
        print("READBACK FAILED: {}".format(err), flush=True)
        after_names = None
    for item in ready_to_upload:
        entry = item["entry"]
        if entry["status"] == "uploaded":
            entry["verified_on_orchestrator"] = None if after_names is None else entry["preconfig_name"] in after_names
    return uploaded_count


def run_batch(
    template_path: Path,
    csv_path: Path,
    out_dir: Path,
    client: Any,
    columns: Optional[Dict[str, Dict[str, Any]]] = None,
    approve: bool = False,
    release: Optional[str] = None,
) -> Dict[str, Any]:
    limits = option_limits(release)
    seen_addresses: Dict[str, int] = {}
    rendered_dir = out_dir / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)

    existing_names, existing_serials = existing_names_serials(client)

    seen_files: Dict[str, int] = {}
    seen_serials: Dict[str, int] = {}
    row_results: List[Dict[str, Any]] = []
    ready_to_upload: List[Dict[str, Any]] = []

    for idx, row in enumerate(read_rows(csv_path), 1):
        problems: List[str] = []
        pcfg_name, fname = ("row_{}".format(idx), "")
        try:
            pcfg_name, fname = resolve_output_filename(row, idx)
            if fname in seen_files:
                problems.append("duplicate preconfig_name/filename {!r} (already used by row {})".format(fname, seen_files[fname]))
            else:
                seen_files[fname] = idx
        except ValueError as err:
            problems.append(str(err))

        norm_serial = None
        if row.get("serial_number"):
            try:
                norm_serial = normalize_serial(row.get("serial_number"))
                if norm_serial in seen_serials:
                    problems.append("duplicate serial_number {!r} (already used by row {})".format(norm_serial, seen_serials[norm_serial]))
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
                text = render_row(template_path, row)
                doc = parse_yaml(text)
                if columns:
                    problems.extend(flow_through(doc, columns, row))
            except Exception as err:
                problems.append(str(err))

        if not problems:
            verdict = validate_text(client, text, norm_serial)
            if not verdict["accepted"]:
                problems.append("Orchestrator /validate rejected: {}".format(verdict["error"]))

        if problems:
            if fname and (rendered_dir / fname).exists() and seen_files.get(fname) == idx:
                (rendered_dir / fname).unlink()
            entry = {
                "row": idx,
                "preconfig_name": pcfg_name,
                "hostname": row.get("hostname") or "",
                "serial_number": norm_serial or row.get("serial_number") or "",
                "validated": False,
                "yaml_file": None,
                "status": "invalid",
                "detail": "; ".join(problems),
            }
            row_results.append(entry)
            print("ROW {:2d} | INVALID  | {:<24} | skipped (no .yml written): {}".format(idx, pcfg_name, entry["detail"]), flush=True)
            continue

        # Immediately emit the validated YAML file to disk right away!
        yml_path = rendered_dir / fname
        yml_path.write_text(text, encoding="utf-8")

        conflicts = []
        if pcfg_name in existing_names:
            conflicts.append("preconfig_name {!r} already exists on Orchestrator".format(pcfg_name))
        if norm_serial and norm_serial in existing_serials:
            conflicts.append("serial_number {!r} already exists on Orchestrator".format(norm_serial))

        if conflicts:
            entry = {
                "row": idx,
                "preconfig_name": pcfg_name,
                "hostname": row.get("hostname") or "",
                "serial_number": norm_serial or "",
                "validated": True,
                "yaml_file": str(yml_path),
                "status": "conflict",
                "detail": "; ".join(conflicts),
            }
            row_results.append(entry)
            print("ROW {:2d} | CONFLICT | {:<24} | .yml written ({}), upload skipped: {}".format(idx, pcfg_name, yml_path.name, entry["detail"]), flush=True)
            continue

        entry = {
            "row": idx,
            "preconfig_name": pcfg_name,
            "hostname": row.get("hostname") or "",
            "serial_number": norm_serial or "",
            "validated": True,
            "yaml_file": str(yml_path),
            "status": "ready",
            "detail": "validated and written to {}".format(yml_path.name),
        }
        row_results.append(entry)
        ready_to_upload.append({"entry": entry, "text": text})
        print("ROW {:2d} | VALID    | {:<24} | .yml written immediately -> {}".format(idx, pcfg_name, yml_path), flush=True)

    uploaded_count = upload_ready(client, ready_to_upload) if approve and ready_to_upload else 0

    report = {
        "timestamp": utc_stamp(),
        "approved": bool(approve),
        "output_dir": str(out_dir),
        "rendered_dir": str(rendered_dir),
        "summary": {
            "total_rows": len(row_results),
            "valid_yaml_written": sum(1 for r in row_results if r["validated"]),
            "invalid_skipped": sum(1 for r in row_results if r["status"] == "invalid"),
            "conflict_skipped": sum(1 for r in row_results if r["status"] == "conflict"),
            "ready_to_upload": sum(1 for r in row_results if r["status"] == "ready"),
            "uploaded": uploaded_count,
            "upload_failed": sum(1 for r in row_results if r["status"] == "upload-failed"),
        },
        "rows": row_results,
        "write_requests": getattr(client, "write_requests", None),
    }
    write_json(out_dir / "upload-report.json", report)
    return report


# ---------------------------------------------------------------- existing preconfig YAML files (upload --yaml, validate --yaml)

def yaml_files(paths: List[Path]) -> List[Path]:
    """The files named, and every *.yml / *.yaml directly in a folder named (sorted, not recursive)."""
    out: List[Path] = []
    for path in paths:
        out += sorted(f for f in path.iterdir() if f.is_file() and f.suffix.lower() in (".yml", ".yaml")) if path.is_dir() else [path]
    return out


def secret_paths(value: Any, path: str = "") -> List[str]:
    if isinstance(value, dict):
        return [x for k, v in value.items() for x in secret_paths(v, "{}.{}".format(path, k) if path else str(k))]
    if isinstance(value, list):
        return [x for i, v in enumerate(value) for x in secret_paths(v, "{}[{}]".format(path, i))]
    return [path] if isinstance(value, str) and "<SECRET:" in value else []


def interface_route_warnings(doc: Dict[str, Any]) -> List[str]:
    """Routes with next hop 0.0.0.0 or :: and an interface: /validate accepts them, ECOS 9.7+ rejects the routes step (docs/decisions/0016)."""
    local = doc.get("localRoutes") if isinstance(doc.get("localRoutes"), dict) else {}
    segments = doc.get("segmentLocalRoutes") if isinstance(doc.get("segmentLocalRoutes"), list) else []
    routes = [("Default", r) for r in local.get("routes") or []] + [(s.get("segment"), r) for s in segments if isinstance(s, dict) for r in s.get("routes") or []]
    return ["route {} (segment {}) has next hop {} and interface {}: passes /validate but ECOS 9.7+ rejects the whole routes step at apply; remove the interface - "
            "see docs/decisions/0016".format(r.get("routeIpSubnet"), seg, r.get("nextHop"), r.get("interfaceName"))
            for seg, r in routes if isinstance(r, dict) and r.get("nextHop") in ("0.0.0.0", "::") and r.get("interfaceName") not in (None, "", "default")]


def yaml_refusal(path: Path) -> Tuple[Optional[str], str, str]:
    """(refusal, detail, text) for one file; refusal None when the file is a preconfig YAML to send to /validate."""
    if not path.exists():  # e.g. a shell glob that matched nothing arrives as a literal path
        return "refused: no such file", "check the path or pattern", ""
    try:
        text = path.read_text(encoding="utf-8")
        doc = None if "{{" in text else parse_yaml(text)
    except Exception as err:  # unreadable or not YAML
        return "refused: not a preconfig YAML", "{}: {}".format(type(err).__name__, str(err).splitlines()[0][:200] if str(err) else ""), ""
    if "{{" in text:
        return "refused: not a preconfig YAML", "it holds Jinja {{ }}: a template, not a rendered preconfig", text
    if not isinstance(doc, dict) or not doc:
        return "refused: not a preconfig YAML", "empty file" if doc is None else "the top level is not a mapping of sections", text
    secrets = secret_paths(doc)
    if secrets:
        return "refused: secret placeholder", "fill these in: {}".format(", ".join(secrets)), text
    for warning in interface_route_warnings(doc):
        print("WARN {}: {}".format(path.name, warning), flush=True)
    return None, "", text


def run_yaml(files: List[Path], client: Any, send: bool, name: Optional[str] = None, serial: Optional[str] = None) -> List[Dict[str, Any]]:
    """Check each file like a CSV row (shape, placeholders, /validate, name and serial conflicts); with send, upload the ones that pass.
    Without send nothing is written. A refused, invalid or conflicting file is skipped and the others continue."""
    names, serials = existing_names_serials(client)
    results: List[Dict[str, Any]] = []
    ready: List[Dict[str, Any]] = []
    for path in files:
        entry = {"file": str(path), "preconfig_name": name or path.stem, "serial_number": serial or "", "status": "", "detail": ""}
        results.append(entry)
        refusal, detail, text = yaml_refusal(path)
        if refusal:
            entry.update(status=refusal, detail=detail)
            continue
        verdict = validate_text(client, text, serial)
        if not verdict["accepted"]:
            entry.update(status="validate failed", detail=verdict["error"])
        elif entry["preconfig_name"] in names:
            entry.update(status="skipped: name conflict", detail="{!r} already exists on Orchestrator or earlier in this batch".format(entry["preconfig_name"]))
        elif serial and serial in serials:
            entry.update(status="skipped: serial conflict", detail="{} already exists on Orchestrator".format(serial))
        else:
            names.add(entry["preconfig_name"])
            entry.update(status="passed", detail="not uploaded (validate only)" if not send else "")
            ready.append({"entry": entry, "text": text})
    if send and ready:  # upload --yaml uploads what passed with no further confirmation (docs/decisions/0017)
        upload_ready(client, ready)
        for item in ready:
            e = item["entry"]
            if e["status"] == "uploaded":
                e["detail"] = "{}, {}".format(e["detail"], {True: "verified on Orchestrator", False: "NOT found on readback", None: "readback failed"}[e.get("verified_on_orchestrator")])
    return results


def yaml_main(parser: argparse.ArgumentParser, paths: List[Path], nick: str, name: Optional[str], serial: Optional[str], send: bool, connect: Callable[[str], Any]) -> int:
    files = yaml_files(paths)
    if not files:
        parser.error("no .yml or .yaml file in {}".format(", ".join(map(str, paths))))
    if (name or serial) and len(files) != 1:
        parser.error("--name and --serial need exactly one file ({} given)".format(len(files)))
    try:
        name = resolve_output_filename({"preconfig_name": name})[0] if name else None
        serial = normalize_serial(serial) if serial else None
    except ValueError as err:
        parser.error(str(err))
    try:
        results = run_yaml(files, connect(nick), send, name, serial)
    except Stop as stop:  # 401/403/network on /validate: every file would fail the same way
        print("STOPPED: {}".format(stop), file=sys.stderr)
        return 2
    print("", flush=True)
    for r in results:
        print("{:<30} {:<24} {}{}".format(r["status"], Path(r["file"]).name, r["preconfig_name"], ": " + r["detail"] if r["detail"] else ""), flush=True)
    print("\n{} file(s): {} {}".format(len(results), sum(1 for r in results if r["status"] in ("uploaded", "passed")), "uploaded" if send else "passed"), flush=True)
    return 0 if all(r["status"] in ("uploaded", "passed") or r["status"].startswith("skipped") for r in results) else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-dir", type=Path, help="site folder (default: ./preconfig-output/<site>)")
    parser.add_argument("--template", type=Path, help="Jinja2 template path (overrides --site-dir)")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--csv", type=Path, help="CSV with one row per site to upload (example.csv holds the source appliance's own values)")
    source.add_argument("--yaml", nargs="+", type=Path, metavar="FILE|DIR", help="existing preconfig YAML files, or folders of them: each is checked and, if it passes, uploaded (no --approve needed)")
    parser.add_argument("--name", help="with --yaml and one file: the preconfig name (default: the file name without .yml)")
    parser.add_argument("--serial", help="with --yaml and one file: the appliance serial number")
    parser.add_argument("--columns", type=Path, help="columns.json path (default: <site-dir>/columns.json)")
    parser.add_argument("--out", type=Path, help="output directory (default: ./preconfig-output/<site>)")
    parser.add_argument("--orchestrator", required=True, help="Orchestrator profile nickname")
    parser.add_argument("--approve", action="store_true", help="with --csv: upload valid non-conflicting rows after user approval (accepted and ignored with --yaml)")
    args = parser.parse_args(argv)
    if args.yaml:
        return yaml_main(parser, args.yaml, args.orchestrator, args.name, args.serial, True, client_for)
    if args.name or args.serial:
        parser.error("--name and --serial go with --yaml")

    if args.site_dir:
        site_dir = args.site_dir
        meta = json.loads((site_dir / "columns.json").read_text(encoding="utf-8")) if (site_dir / "columns.json").exists() else {}
        site = meta.get("site_type") or site_dir.name
        template_path = args.template or (site_dir / "{}.jinja2".format(site))
        csv_path = args.csv
        columns = meta.get("columns")
        release = meta.get("release")
        out_dir = args.out or site_dir
    else:
        if not args.template:
            parser.error("provide --site-dir or --template")
        template_path = args.template
        csv_path = args.csv
        meta = json.loads(args.columns.read_text(encoding="utf-8")) if args.columns and args.columns.exists() else {}
        columns, release = meta.get("columns"), meta.get("release")
        out_dir = args.out or (output_root() / template_path.stem)

    if not csv_path:
        parser.error("--csv is required: the CSV of sites to upload (pass example.csv explicitly only to upload the source appliance's own values)")
    client = client_for(args.orchestrator)
    try:
        report = run_batch(template_path, csv_path, out_dir, client, columns, approve=args.approve, release=release)
    except Stop as stop:  # 401/403/network on /validate: every row would fail the same way
        print("STOPPED: {}".format(stop), file=sys.stderr)
        return 2
    s = report["summary"]
    print(
        "\nSummary: total={total_rows} | valid_yaml_written={valid_yaml_written} | invalid_skipped={invalid_skipped} | conflict_skipped={conflict_skipped} | uploaded={uploaded} | upload_failed={upload_failed}".format(**s),
        flush=True,
    )
    if not args.approve and s["ready_to_upload"] > 0:
        print("Approval required: review the rows above and re-run with --approve to upload the {} ready row(s).".format(s["ready_to_upload"]), flush=True)
    return 0 if s["invalid_skipped"] == 0 and s["upload_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

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
"""
import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cells import cell_problems, interface_addresses, option_limits, subnet_problems  # noqa: E402
from common import EdgeConnectError, client_for, normalize_serial, parse_yaml, resolve_output_filename, utc_stamp, write_json  # noqa: E402
from render import flow_through, read_rows, render_row  # noqa: E402
from validate import Stop, validate_text  # noqa: E402


def fetch_existing_preconfigs(client: Any) -> List[Dict[str, Any]]:
    data = client.get("/gms/appliance/preconfiguration")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        return [x for x in data.values() if isinstance(x, dict)]
    return []


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

    existing = fetch_existing_preconfigs(client)
    existing_names = {str(p.get("name") or "").strip() for p in existing if p.get("name")}
    existing_serials = set()
    for p in existing:
        if p.get("serialNum"):
            try:
                norm = normalize_serial(str(p.get("serialNum")))
                if norm:
                    existing_serials.add(norm)
            except ValueError:
                existing_serials.add(str(p.get("serialNum")).strip().upper())

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

    uploaded_count = 0
    if approve and ready_to_upload:
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-dir", type=Path, help="site folder (default: ./preconfig-output/<site>)")
    parser.add_argument("--template", type=Path, help="Jinja2 template path (overrides --site-dir)")
    parser.add_argument("--csv", type=Path, help="CSV with one row per site to upload (required; example.csv holds the source appliance's own values)")
    parser.add_argument("--columns", type=Path, help="columns.json path (default: <site-dir>/columns.json)")
    parser.add_argument("--out", type=Path, help="output directory (default: ./preconfig-output/<site>)")
    parser.add_argument("--orchestrator", required=True, help="Orchestrator profile nickname")
    parser.add_argument("--approve", action="store_true", help="upload valid non-conflicting rows after user approval")
    args = parser.parse_args(argv)

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
        out_dir = args.out or (Path.cwd() / "preconfig-output" / template_path.stem)

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

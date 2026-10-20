#!/usr/bin/env python3
"""Stage 7: turn a reference preconfig configuration (YAML mapping) into a site type's Jinja2 template + CSV template.

Deterministic. Variables are written only as data['<column>'] so pyedgeconnect's header regex finds every one.
Structure (sections, list lengths) is copied from the reference; rows vary values, not structure.

Outputs in --out: <site>.jinja2  <site>.csv (header only)  example.csv (reference values)  columns.json
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cells import INTERFACE_ADDRESS, address_format, describe, option_limits  # noqa: E402
from common import load_yaml, write_json  # noqa: E402

SECRET = re.compile(r"(?i)(passw|passphrase|secret|psk|pre_?shared|private_?key|auth(?:entication)?_?key|md5|ike_?key|token)")
SERVER_SIBLINGS = ("host", "address", "authport", "acctport", "server", "serverIp")
FIXED_NAMES = {"applianceInfo.hostname": "hostname", "templateGroups.groups": "template_groups", "businessIntentOverlays.overlays": "overlays"}
COMMA_LISTS = {"templateGroups.groups", "businessIntentOverlays.overlays"}
KEY_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")
FIELD_FORMAT = {"ipAddressMask": "ip/prefix", "nextHop": "ip"}  # address fields, whatever the reference value (null on a DHCP interface)


class Builder:
    def __init__(self, fixed: Optional[List[str]] = None, names: Optional[Dict[str, str]] = None, variables: Optional[List[str]] = None, decide: Any = None, list_fixed: Optional[set] = None,
                 host_paths: Optional[set] = None) -> None:
        self.host_paths = host_paths or set()  # leaves rendered as the host part of an address column that holds ip/prefix
        self.decide = decide  # decide(path, value, secret) -> column?; overrides `variables`
        self.list_fixed = list_fixed or set()  # comma-list paths kept as literal lists
        self.fixed = set(fixed or [])  # path prefixes whose leaves stay literal
        self.variables = [re.compile(v) for v in variables] if variables else None  # when given, only matching paths become columns
        self.names = dict(FIXED_NAMES, **(names or {}))
        self.columns: Dict[str, Dict[str, Any]] = {}
        self.lines: List[str] = []
        self.secret_paths: set = set()

    def find_secrets(self, value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            server = any(s in value for s in SERVER_SIBLINGS)
            for key, item in value.items():
                sub = "{}.{}".format(path, key) if path else str(key)
                if SECRET.search(str(key)) or (key == "key" and server):
                    self.secret_paths.add(sub)
                self.find_secrets(item, sub)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                self.find_secrets(item, "{}[{}]".format(path, index))

    def column(self, path: str, value: Any) -> str:
        name = self.names.get(path) or re.sub(r"\[(\d+)\]", r".\1", path)
        if name in self.columns:  # several leaves share one column (same value, typed once)
            known = self.columns[name]
            if known["path"] in self.host_paths and path not in self.host_paths:  # the full ip/prefix leaf owns the column, not a host-only one
                also = known.get("also_at", []) + [known["path"]]
                del self.columns[name]
                self.column(path, value)
                self.columns[name]["also_at"] = also
            else:
                known.setdefault("also_at", []).append(path)
            return name
        kind = "bool" if isinstance(value, bool) else "int" if isinstance(value, int) else "float" if isinstance(value, float) else "string"
        secret = path in self.secret_paths and kind == "string"
        self.columns[name] = {"path": path, "type": kind, "optional": value is None, "secret": secret, "had_value": value is not None, "example": "" if (value is None or secret) else value}
        fmt = (address_format(value) or FIELD_FORMAT.get(path.split(".")[-1])) if not secret else None
        if fmt:
            self.columns[name]["format"] = fmt
        return name

    def scalar(self, path: str, key: str, value: Any, indent: str, dash: str = "") -> None:
        lead = indent + dash
        label = "{}: ".format(key) if key is not None else ""
        literal = any(path == f or path.startswith(f + ".") or path.startswith(f + "[") for f in self.fixed)
        if self.decide is not None and not literal:
            literal = not (self.decide(path, value, path in self.secret_paths and isinstance(value, str)) or path in self.names)
        elif self.variables is not None and not literal:
            literal = not any(v.search(path) for v in self.variables) and path not in self.names and not path in self.secret_paths
        if literal:
            self.lines.append("{}{}{}".format(lead, label, json.dumps(value) if value is not None else "").rstrip())
            return
        name = self.column(path, value)
        ref = "data['{}']".format(name)
        if path in self.host_paths:
            self.lines.append("{}{}{{{{ {}.split('/')[0] | tojson }}}}".format(lead, label, ref))
            return
        kind = self.columns[name]["type"]
        if value is None or INTERFACE_ADDRESS.match(path):  # a blank cell renders null: a DHCP interface leaves these empty
            self.lines.append("{}{}{{{{ ({} | tojson) if {} != '' else '' }}}}".format(lead, label, ref, ref).rstrip())
            return
        body = {"bool": "{{ %s | lower }}", "int": "{{ %s }}", "float": "{{ %s }}", "string": "{{ %s | tojson }}"}[kind] % ref
        self.lines.append("{}{}{}".format(lead, label, body))

    def walk(self, value: Any, path: str, indent: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if not KEY_OK.match(str(key)):
                    raise ValueError("unsupported key {!r} at {}".format(key, path))
                self.node(item, "{}.{}".format(path, key) if path else str(key), str(key), indent)
        else:
            raise ValueError("root must be a mapping")

    def node(self, value: Any, path: str, key: str, indent: str) -> None:
        if isinstance(value, dict) and value:
            self.lines.append("{}{}:".format(indent, key))
            self.walk(value, path, indent + "  ")
        elif isinstance(value, dict) or (isinstance(value, list) and not value):
            self.lines.append("{}{}: {}".format(indent, key, "{}" if isinstance(value, dict) else "[]"))
        elif isinstance(value, list) and path in COMMA_LISTS and path not in self.list_fixed:
            name = self.names[path]
            self.columns[name] = {"path": path, "type": "comma-list", "optional": False, "secret": False, "example": ",".join(map(str, value))}
            self.lines += ["{}{}:".format(indent, key), "{{% for item in data['{}'].split(',') %}}".format(name),
                           "{}  - {{{{ item | trim | tojson }}}}".format(indent), "{% endfor %}"]
        elif isinstance(value, list):
            self.lines.append("{}{}:".format(indent, key))
            for index, item in enumerate(value):
                self.item(item, "{}[{}]".format(path, index), indent + "  ")
        else:
            self.scalar(path, key, value, indent)

    def item(self, value: Any, path: str, indent: str) -> None:
        if isinstance(value, dict) and value:
            first = True
            for key, sub in value.items():
                sub_path = "{}.{}".format(path, key)
                if first:
                    self.dict_first(sub, sub_path, str(key), indent)
                    first = False
                else:
                    self.node(sub, sub_path, str(key), indent + "  ")
        elif isinstance(value, list) or isinstance(value, dict):
            self.lines.append("{}- {}".format(indent, "[]" if isinstance(value, list) else "{}"))
        else:
            self.scalar(path, None, value, indent, dash="- ")

    def dict_first(self, value: Any, path: str, key: str, indent: str) -> None:
        """First key of a list item shares the line with the dash."""
        if isinstance(value, (dict, list)) and value and not (isinstance(value, list) and path in COMMA_LISTS):
            self.lines.append("{}- {}:".format(indent, key))
            if isinstance(value, dict):
                self.walk(value, path, indent + "    ")
            else:
                for index, sub in enumerate(value):
                    self.item(sub, "{}[{}]".format(path, index), indent + "    ")
        elif isinstance(value, (dict, list)):
            self.lines.append("{}- {}: {}".format(indent, key, "{}" if isinstance(value, dict) else "[]"))
        else:
            self.scalar(path, key, value, indent, dash="- ")


def generate(reference: Dict[str, Any], out: Path, site: str, fixed: Optional[List[str]] = None, names: Optional[Dict[str, str]] = None, variables: Optional[List[str]] = None,
             decide: Any = None, list_fixed: Optional[set] = None, host_paths: Optional[set] = None, release: Optional[str] = None) -> Dict[str, Any]:
    builder = Builder(fixed, names, variables, decide, list_fixed, host_paths)
    builder.find_secrets(reference)
    builder.walk(reference, "", "")
    for col in builder.columns.values():  # an interface address is required or blank by that interface's addressingMode
        match = INTERFACE_ADDRESS.match(col["path"])
        if not match:
            continue
        mode_path = match.group(1) + ".addressingMode"
        owner = next((n for n, c in builder.columns.items() if mode_path in [c["path"]] + c.get("also_at", [])), None)
        index = int(re.search(r"\[(\d+)\]", match.group(1)).group(1))
        fixed_mode = reference["deploymentInfo"]["deploymentInterfaces"][index].get("addressingMode")
        if owner:
            col["mode_column"] = owner
        elif fixed_mode:
            col["mode"] = fixed_mode
    out.mkdir(parents=True, exist_ok=True)
    (out / (site + ".jinja2")).write_text("\n".join(builder.lines) + "\n", encoding="utf-8")
    header = list(builder.columns)
    runner = ["preconfig_name", "serial_number"]
    if "hostname" not in header:
        header.insert(0, "hostname")
    header += [r for r in runner if r not in header]
    ref_host = (reference.get("applianceInfo") or {}).get("hostname") if isinstance(reference.get("applianceInfo"), dict) else ""
    example = {}
    for name in header:
        meta = builder.columns.get(name)
        if name == "preconfig_name":
            host_val = builder.columns.get("hostname", {}).get("example") or ref_host or site
            example[name] = "{}_preconfig".format(host_val)
        elif meta is None:
            example[name] = ""
        else:
            value = meta["example"]
            example[name] = str(value).lower() if isinstance(value, bool) else "" if value is None else str(value)
    for fname, rows in (("{}.csv".format(site), []), ("example.csv", [example])):
        with (out / fname).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=header)
            writer.writeheader()
            writer.writerows(rows)
    meta = {"site_type": site, "columns": builder.columns, "runner_columns": runner, "fixed": sorted(builder.fixed), "release": release}
    write_json(out / "columns.json", meta)
    limits = option_limits(release)
    rows = ["| Column | What to enter | Reference value | Fills |", "|---|---|---|---|",
            "| `preconfig_name` | name of the preconfig (also the file name) | | |", "| `serial_number` | 12 hex characters, optional | | |"]
    for name, col in builder.columns.items():
        shown = "" if col["secret"] or col["example"] in ("", None) else "`{}`".format(col["example"])
        rows.append("| `{}` | {} | {} | {} |".format(name, describe(col, limits), shown, ", ".join("`{}`".format(p) for p in [col["path"]] + col.get("also_at", []))))
    (out / "columns.md").write_text("# Columns of {}\n\nFill one row per site in a copy of `{}.csv`. The reference value is the source appliance's.\n\n".format(site, site) + "\n".join(rows) + "\n", encoding="utf-8")
    return meta


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, type=Path, help="YAML mapping (the site type's reference configuration)")
    parser.add_argument("--site-type", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--fixed", nargs="*", default=[], help="path prefixes kept as literals, e.g. deploymentInfo.deploymentMode")
    args = parser.parse_args(argv)
    meta = generate(load_yaml(args.reference), args.out, args.site_type, args.fixed)
    print("{}: {} columns -> {}".format(args.site_type, len(meta["columns"]), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

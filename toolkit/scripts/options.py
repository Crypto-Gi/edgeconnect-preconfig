#!/usr/bin/env python3
"""Extract documented options from the default preconfig template's comments.

Finds, per top-level section and key, (a) enumerations such as `Value: [All,stateful,harden]` or
`Value: [true, false]` and (b) integer ranges such as `Value: integer[1-100,000,000]`.
Heuristic, deliberately conservative: a key is reported only when its definition line (`# key - text`) is followed
by a Value line the patterns understand. Output is data for tests and for the mapping record; the YAML example
lines remain the authority for key names (the 9.7.1 comments misname some keys).
"""
import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFINITION = re.compile(r"^\s*#\s{1,6}([A-Za-z][A-Za-z0-9_]*)\s*(?:-|:)\s")
TOP_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):")
VALUE = re.compile(r"(?i)\bvalues?\s*:\s*(.*)$")
BRACKET = re.compile(r"\[([^\]]+)\]")
RANGE = re.compile(r"(?i)\b(?:integer|int|number)?\s*\[\s*(-?[\d,]+)\s*-\s*(-?[\d,]+)\s*(?:,[^\]]*)?\]")


def scalar(text: str) -> Any:
    text = text.strip().strip("\"'")
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    return int(text) if re.fullmatch(r"-?\d+", text) else text


def extract(template_text: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for line in template_text.splitlines():
        top = TOP_KEY.match(line)
        if top:
            for item in pending:
                item["section"] = top.group(1)
            out += pending
            pending, current = [], None
            continue
        definition = DEFINITION.match(line)
        if definition and definition.group(1).lower() in ("value", "values", "note", "example", "syntax", "default"):
            definition = None
        if definition:
            current = {"key": definition.group(1), "enum": None, "range": None, "line": line.strip()}
            pending.append(current)
        value = VALUE.search(line)
        if current is not None and value:
            text = value.group(1)
            rng = RANGE.search(text)
            if rng:
                low, high = (int(g.replace(",", "")) for g in rng.groups())
                current["range"] = [low, high]
                current["exclusive"] = "exclusive" in text.lower()
                continue
            bracket = BRACKET.search(text)
            if bracket and not re.search(r"\d\s*-\s*\d", bracket.group(1)):
                items = [scalar(part) for part in bracket.group(1).split(",") if part.strip()]
                if 1 < len(items) <= 40 and all(not isinstance(i, str) or len(i) < 40 for i in items) and "..." not in items:
                    current["enum"] = items
    return [item for item in out if item.get("section") and (item["enum"] or item["range"])]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", type=Path)
    args = parser.parse_args(argv)
    found = extract(args.template.read_text(encoding="utf-8"))
    print(json.dumps(found, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

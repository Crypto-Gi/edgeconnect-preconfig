#!/usr/bin/env python3
"""Source coverage (fuzz oracle 2): every leaf of every read the converter takes is written, ledgered or on the ignore list.

check 11 proves the other half (a ledger "mapped" line has its YAML value). This harness runs translate.py on each capture with every
read wrapped so that each key the converter takes is recorded, then lists the leaves it never took, less:
  * keys a ledger line names as not mapped (unmapped-source-key) for that capture;
  * reads whose sections the ledger hands to a template group, or calls outside the preconfig format, not readable or empty;
  * entries of tests/harnesses/source_ignore.yaml (read id, path with * wildcards, reason).
A leaf is taken when its key is read with [] or get(), or its value is handed out by items()/values()/iteration of a list.
dict(x) and {**x} copies count as takes; json.dumps of a mapping is not seen: such leaves show up here and go on the ignore list with that reason.
Writes key paths only, never values. Usage: source_coverage.py --out report.md [--target 9.7.1.42046 ...] CAPTURE_FOLDER...
"""
import argparse
import json
import re
import sys
import tempfile
from collections import defaultdict
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "toolkit" / "scripts"))
import translate  # noqa: E402  guard-ok: imported after the sys.path insert
from common import CATALOGUE, load_yaml, toolkit_version  # noqa: E402  guard-ok: imported after the sys.path insert

IGNORE = Path(__file__).with_name("source_ignore.yaml")
NAME = re.compile(r"^(?![a-z]+[0-9]+$)[a-z][a-zA-Z0-9_-]*$")  # a field name (as translate.SOURCE_KEY, hyphens kept); ids, prefixes and interface names such as lan0 are collection keys, "*"
EXPLAINED = ("template-group", "outside-preconfig", "not-readable", "empty-or-absent", "orchestrator-managed")


def glob(pattern: str, path: str) -> bool:
    """"*" matches any run of characters; everything else, "[]" included, is literal (fnmatch would read [] as a class)."""
    return re.fullmatch(".*".join(re.escape(x) for x in pattern.split("*")), path) is not None


def part(key: Any, collection: bool) -> str:
    return "*" if collection or not NAME.match(str(key)) else str(key)


class Seen:
    """The normalised paths of one read that the converter took, and every leaf path the read holds."""

    def __init__(self) -> None:
        self.taken: Set[str] = set()
        self.leaves: Set[str] = set()


def wrap(value: Any, path: str, seen: Seen) -> Any:
    if isinstance(value, dict):
        return TDict(value, path, seen)
    if isinstance(value, list):
        return TList(value, path, seen)
    if path:  # a read that is absent (None) or a bare value has no fields to account for
        seen.leaves.add(path)
    return value


def join(path: str, key: str) -> str:
    return "{}.{}".format(path, key) if path else key


class TDict(dict):
    def __init__(self, src: Dict[str, Any], path: str, seen: Seen) -> None:
        self._path, self._seen = path, seen
        collection = len(src) > 0 and all(not NAME.match(str(k)) for k in src)
        self._parts = {k: part(k, collection) for k in src}
        super().__init__({k: wrap(v, join(path, self._parts[k]), seen) for k, v in src.items()})
        if not src and path:
            seen.leaves.add(path)  # an empty mapping is one leaf: taken when it is read

    def _take(self, key: Any) -> None:
        if key in self._parts:
            self._seen.taken.add(join(self._path, self._parts[key]))

    def __getitem__(self, key: Any) -> Any:
        self._take(key)
        return super().__getitem__(key)

    def get(self, key: Any, default: Any = None) -> Any:
        self._take(key)
        return super().get(key, default)

    def pop(self, key: Any, *default: Any) -> Any:
        self._take(key)
        return super().pop(key, *default)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        self._take(key)
        return super().setdefault(key, default)

    def __iter__(self):  # not a take (ctx.unmapped lists keys); defined so that dict(x) and {**x} copy through keys() and [], which are takes
        return iter(list(dict.keys(self)))

    def items(self):  # type: ignore[override]
        for k in list(dict.keys(self)):
            self._take(k)
        return super().items()

    def values(self):  # type: ignore[override]
        for k in list(dict.keys(self)):
            self._take(k)
        return super().values()


class TList(list):
    def __init__(self, src: List[Any], path: str, seen: Seen) -> None:
        super().__init__(wrap(v, path + "[]", seen) for v in src)
        self._path, self._seen = path, seen
        if not src and path:
            seen.leaves.add(path)

    def _take(self) -> None:
        self._seen.taken.add(self._path + "[]")
        self._seen.taken.add(self._path)

    def __iter__(self):
        self._take()
        return super().__iter__()

    def __getitem__(self, i: Any) -> Any:
        self._take()
        return super().__getitem__(i)


def ignore_rules() -> List[Dict[str, str]]:
    if not IGNORE.is_file():
        return []
    return list((load_yaml(IGNORE) or {}).get("ignore") or [])


def run_one(capture: Path, target: str) -> Tuple[Dict[str, Set[str]], List[Dict[str, Any]], Optional[str]]:
    """(read id -> leaves never taken, ledger, error) for one capture and target."""
    seen: Dict[str, Seen] = defaultdict(Seen)
    real = translate.Capture.data

    def data(self: Any, ident: str, prefer: str = "live") -> Any:
        return wrap(real(self, ident, prefer), "", seen[ident])

    translate.Capture.data = data  # type: ignore[method-assign]
    try:
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(StringIO()), redirect_stderr(StringIO()) as err:
            code = translate.main(["--capture", str(capture), "--site-type", "coverage", "--out", tmp, "--target-release", target])
            ledger_file = Path(tmp) / "ledger.json"
            ledger = json.loads(ledger_file.read_text()).get("entries", []) if ledger_file.is_file() else []
            if code not in (0, 1) and not ledger:
                return {}, [], "translate exit {}: {}".format(code, err.getvalue().strip().splitlines()[-1:] or "")
    except Exception as error:  # a crash is a finding of its own
        return {}, [], "{}: {}".format(type(error).__name__, error)
    finally:
        translate.Capture.data = real  # type: ignore[method-assign]
    return {ident: s.leaves - s.taken for ident, s in seen.items()}, ledger, None


def unmapped_keys(ledger: List[Dict[str, Any]]) -> Set[str]:
    """Key names a ledger line names: unmapped-source-key ("source keys not mapped: a, b") and new-source-field ("...: x.a, y[].b; check by hand")."""
    keys: Set[str] = set()
    for e in ledger:
        note = str(e.get("note"))
        if e.get("disposition") == "unmapped-source-key" and "source keys not mapped:" in note:
            keys |= {k.strip() for k in note.split("source keys not mapped:", 1)[1].split(",")}
        elif e.get("disposition") == "new-source-field" and "in view: " in note:
            listed = note.split("in view: ", 1)[1].split("; check by hand", 1)[0]
            keys |= {p.strip().rsplit(".", 1)[-1].replace("[]", "") for p in listed.split(",")}
    return keys


def explained_reads(ledger: List[Dict[str, Any]], catalogue: Dict[str, Dict[str, Any]]) -> Set[str]:
    """Reads the ledger accounts for as a whole: the read's own line, or every one of its sections handed elsewhere."""
    whole = {e["item"] for e in ledger if e.get("disposition") in EXPLAINED}
    owned = {s for e in ledger if e.get("disposition") == "template-group" for s in str(e.get("target", "")).split(",")}
    return whole | {i for i, c in catalogue.items() if c.get("sections") and all(s in owned for s in c["sections"])}


def untraced(capture: Path, target: str, rules: List[Dict[str, str]], catalogue: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, List[str]], Optional[str]]:
    left, ledger, error = run_one(capture, target)
    if error:
        return {}, error
    named, whole = unmapped_keys(ledger), explained_reads(ledger, catalogue)
    out: Dict[str, List[str]] = {}
    for ident, leaves in left.items():
        if ident in whole:
            continue
        keep = sorted(p for p in leaves if p.rsplit(".", 1)[-1].rstrip("[]") not in named
                      and not any(r.get("read") in (ident, "*") and glob(r.get("path", ""), p) for r in rules))
        if keep:
            out[ident] = keep
    return out, None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("captures", nargs="+", type=Path)
    parser.add_argument("--target", action="append", help="target release (repeatable; default 9.7.1.42046)")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    targets = args.target or ["9.7.1.42046"]
    rules = ignore_rules()
    catalogue = {e["id"]: e for e in load_yaml(CATALOGUE)["endpoints"]}
    found: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    errors: List[str] = []
    for i, capture in enumerate(args.captures):
        for target in targets:
            result, error = untraced(capture, target, rules, catalogue)
            if error:
                errors.append("capture #{} target {}: {}".format(i + 1, target, error))
            for ident, paths in result.items():
                for p in paths:
                    found[(ident, p)].add("#{}@{}".format(i + 1, target))
    lines = ["# Source coverage (fuzz oracle 2)", "",
             "Toolkit {}; {} captures x {} targets; ignore rules {}.".format(toolkit_version(), len(args.captures), len(targets), len(rules)), "",
             "Untraced leaves: {} (read id, key path, captures that hold it). Each must be mapped, ledgered, or added to source_ignore.yaml with a reason.".format(len(found)), ""]
    lines += ["| read | key path | captures |", "|---|---|---|"]
    for (ident, p), where in sorted(found.items()):
        lines.append("| {} | `{}` | {} |".format(ident, p, len({w.split("@")[0] for w in where})))
    if errors:
        lines += ["", "## Errors", ""] + ["- " + e for e in errors]
    lines += ["", "Captures: " + ", ".join("#{} {}".format(i + 1, c.name) for i, c in enumerate(args.captures))]
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("{} untraced leaves, {} errors -> {}".format(len(found), len(errors), args.out))
    return 1 if found or errors else 0


if __name__ == "__main__":
    sys.exit(main())

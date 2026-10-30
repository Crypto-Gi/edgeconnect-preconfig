#!/usr/bin/env python3
"""edgeconnect-preconfig: build preconfig templates from a working appliance, without an AI.

  list       --orchestrator NICK [--search TEXT]       test the Orchestrator profile, list its appliances
  preconfigs --orchestrator NICK [--show NAME|ID]      list the preconfigs on the Orchestrator, or print one as YAML
                                                       with its secrets redacted
  build      --orchestrator NICK --appliance NAME       read one appliance, write a validated YAML, template and CSV
             [--out DIR] [--validate-on NICK]
  variables  FOLDER [--pick top10|top20] [--set C,..]  choose the per-site settings: Top 10, Top 20 or all, by section
             [--name NAME] [--yes]
  compare    FOLDER SITE [SITE ...] [--name NAME]       other sites of the same design pick the per-site settings;
             [--yes]                                    the CSV gets a row for each site
  validate   FOLDER --csv FILE [--orchestrator NICK]    render and /validate every CSV row
  upload     FOLDER --csv FILE [--orchestrator NICK]    show what would be uploaded; uploads only with --approve
             [--approve]
  validate   --yaml FILE|DIR ... --orchestrator NICK    check existing preconfig YAML files; never writes
  upload     --yaml FILE|DIR ... --orchestrator NICK    same checks, then upload every file that passes (no --approve)
             [--name NAME] [--serial SERIAL]

FOLDER is what build wrote (or a site-<name> folder inside it). Validation runs on the Orchestrator the appliance was read
from, unless --validate-on names another; either way it must be listed in EC_VALIDATORS. Every Orchestrator read is a GET.
The pipeline stages still run by name: capture, cache-check, translate, generate, render, check, options,
validate and upload with their own options (edgeconnect-preconfig STAGE --help).
"""
import argparse
import base64
import binascii
import csv
import io
import json
import importlib
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from variables import ALWAYS_SECTIONS, COMMA_LISTS, LAYOUT, LAYOUT_COMPARED, LAYOUT_DIFFERS, LAYOUT_KEYS, RELEASE_DEFAULTS  # noqa: E402  guard-ok: imported after the sys.path insert
from common import DEFAULT_TEMPLATES_DIR, EdgeConnectError, PermissionDeniedError, captured_output, client_for, load_yaml, output_root, parse_yaml, record, redact_secrets, toolkit_version, utc_stamp, validators, work_dir, write_json  # noqa: E402  guard-ok: imported after the sys.path insert

PROG = "edgeconnect-preconfig"
STATES = {1: "yes", 2: "NO", 3: "unsupported version", 4: "yes (out of sync)", 5: "yes (syncing)"}  # /appliance `state`
NOT_STAGES = {"cells", "cli", "common", "decisions", "mappers", "runlog", "synth", "variables"}  # libraries, not commands
INTERFACE_NAME_KEYS = ("interfaceName", "dhcpInterfaceName", "raInterfaceName")
COMMENT = re.compile(r"(?i)comment$")  # free text: one only a partner has is left out of its row, never a reason for no row
DESIGN_KEYS = LAYOUT_KEYS - LAYOUT_COMPARED - set(INTERFACE_NAME_KEYS)  # differing at a compared site: another design, no row (docs/decisions/0018)
ADDRESS_REASONS = ("this site's own interface address", "inside one of this site's own subnets")


class Fail(Exception):
    """A user-facing error: printed as one line, exit code 2."""


def table(header: List[str], rows: List[List[Any]]) -> str:
    """A bordered table, the setup.sh style."""
    widths = [max(len(str(c)) for c in col) for col in zip(header, *rows)]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def fmt(cells: List[Any]) -> str:
        return "| " + " | ".join(str(c).ljust(w) for c, w in zip(cells, widths)) + " |"
    return "\n".join([line, fmt(header), line] + [fmt(r) for r in rows] + [line])


def cell(value: Any) -> str:
    """A value as a CSV cell: true/false, blank for none."""
    return str(value).lower() if isinstance(value, bool) else "" if value is None else str(value)


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(text)).strip("_") or "appliance"


def check_validator(nick: str) -> None:
    if nick.lower() not in validators():
        raise Fail("{} is not a validator Orchestrator: /validate runs only on the ones listed in EC_VALIDATORS in .env "
                   "(now: {}). Add it there (comma-separated nicknames) if /validate may run on it, or pass --validate-on <lab profile>."
                   .format(nick, ", ".join(sorted(validators())) or "none"))


def load_build(folder: Path) -> Dict[str, Any]:
    for candidate in (folder / "build.json", folder.parent / "build.json"):
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise Fail("{} is not a build folder (no build.json): run `{} build` first".format(folder, PROG))


def keep_previous(folder: Path) -> None:
    """An earlier result is renamed, never overwritten."""
    if folder.exists():
        old = folder.with_name("{}.previous-{}".format(folder.name, utc_stamp()))
        folder.rename(old)
        print("earlier {} kept as {}".format(folder.name, old.name))


def quiet(fn: Callable[..., Any], *args: Any) -> Any:
    """Run a stage function with this run's stdout and stderr captured (not a parallel run's); returns (result, text)."""
    buffer = io.StringIO()
    with captured_output(buffer):
        result = fn(*args)
    return result, buffer.getvalue()


# ---------------------------------------------------------------- list

def cmd_list(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog=PROG + " list", description="Test an Orchestrator profile from .env and list its appliances (GET only).")
    p.add_argument("--orchestrator", required=True, help=".env profile nickname")
    p.add_argument("--search", help="show only appliances whose hostname, nePk, model or site contains this text")
    a = p.parse_args(argv)
    client = client_for(a.orchestrator)
    try:
        versions = client.get("/gms/versions")
        release = versions.get("current") if isinstance(versions, dict) else "?"
    except PermissionDeniedError:  # a key without that right still lists appliances
        release = "unknown (GET /gms/versions refused)"
    listing = client.get("/appliance")
    validates = "yes" if a.orchestrator.lower() in validators() else "no (not in EC_VALIDATORS)"
    print("Orchestrator {}: {} | release {} | login OK | /validate allowed: {}".format(a.orchestrator, client.config.base_url, release, validates))
    rows = []
    for item in listing if isinstance(listing, list) else []:
        row = [item.get("hostName") or "", item.get("nePk") or item.get("id") or "", item.get("model") or "", item.get("softwareVersion") or "",
               item.get("site") or "", STATES.get(item.get("state"), "unknown")]
        if not a.search or any(a.search.lower() in str(c).lower() for c in row[:5]):
            rows.append(row)
    rows.sort(key=lambda r: str(r[0]).lower())
    for r in rows:  # the GUI reads these, not the table, which a hostname with spaces or wide characters makes ambiguous (cli-requests R8)
        record("appliance", **dict(zip(("hostname", "nepk", "model", "ecos", "site", "reachable"), r)))
    print(table(["Hostname", "nePk", "Model", "ECOS", "Site", "Reachable"], rows) if rows else "no appliances{}".format(" match" if a.search else ""))
    print("{} appliance(s), {} reachable".format(len(rows), sum(1 for r in rows if r[5].startswith("yes"))))
    if client.write_requests:
        raise Fail("write counter is {}; list is read-only".format(client.write_requests))
    return 0


# ---------------------------------------------------------------- preconfigs

LAST_CHANGE = "modifiedTime"  # shortcut: a field name the repo has never seen in a live list response; confirm it in the step 10 lab check


def cmd_preconfigs(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog=PROG + " preconfigs", description="List the preconfigs on an Orchestrator, or print one as YAML with its secrets redacted (GET only).")
    p.add_argument("--orchestrator", required=True, help=".env profile nickname")
    p.add_argument("--show", metavar="NAME|ID", help="print this preconfig's YAML, secrets as <SECRET:path>")
    a = p.parse_args(argv)
    from upload import fetch_existing_preconfigs
    client = client_for(a.orchestrator)
    items = fetch_existing_preconfigs(client)  # each item's configData is never printed or recorded as it came
    if client.write_requests:
        raise Fail("write counter is {}; preconfigs is read-only".format(client.write_requests))
    if a.show is None:
        rows = sorted(({"name": i.get("name"), "serial": i.get("serialNum"), "tag": i.get("tag"), "last_change": i.get(LAST_CHANGE), "id": i.get("id")} for i in items),
                      key=lambda r: str(r["name"] or "").lower())
        for r in rows:
            record("preconfig", **r)
        print(table(["Name", "Serial", "Tag", "Last change", "Id"], [[cell(v) for v in r.values()] for r in rows]) if rows else "no preconfigs")
        print("{} preconfig(s) on {}; print one with: {} preconfigs --orchestrator {} --show NAME|ID".format(len(rows), a.orchestrator, PROG, a.orchestrator))
        return 0
    hits = [i for i in items if str(i.get("id")) == a.show] or [i for i in items if i.get("name") == a.show]  # an id is unique, a name may not be
    if len(hits) > 1:
        raise Fail("preconfig {!r}: {} preconfigs have that name on {} (ids {}): give the id".format(a.show, len(hits), a.orchestrator, ", ".join(str(i.get("id")) for i in hits)))
    if not hits:
        raise Fail("preconfig {!r}: 0 matches by name or id on {}".format(a.show, a.orchestrator))
    item = hits[0]
    label = "preconfig {} (id {})".format(item.get("name"), item.get("id"))
    if not item.get("configData"):
        raise Fail("{}: the list from GET /gms/appliance/preconfiguration holds no configData for it".format(label))
    try:
        text = base64.b64decode("".join(str(item["configData"]).split()), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise Fail("{}: its configData is not base64 of UTF-8 text, so it is not shown".format(label))
    try:
        doc = parse_yaml(text)
    except Exception as error:  # the parser's message quotes the line, which may hold a secret: only where it broke is shown
        mark = getattr(error, "problem_mark", None)
        raise Fail("{}: its YAML does not parse{}; its text is not shown, as it may hold a secret".format(label, " (line {}, column {})".format(mark.line + 1, mark.column + 1) if mark else ""))
    import yaml
    if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(text)):  # redaction is by key: an alias (or <<: merge) would print a secret's value under another key
        raise Fail("{}: its YAML uses aliases (*name), which can copy a secret onto a key that is not secret; it is not shown".format(label))
    if not isinstance(doc, dict):
        raise Fail("{}: its YAML is not a mapping of sections, so it is not shown".format(label))
    try:
        shown = yaml.safe_dump(redact_secrets(doc, strict=True)[0], sort_keys=False, allow_unicode=True, default_flow_style=False, width=1000)
    except RecursionError:  # a few hundred levels of nesting parse, but the walk or the dump runs out of stack
        raise Fail("{}: its YAML nests too deep, so it is not shown".format(label))
    record("preconfig-yaml", name=item.get("name"), id=item.get("id"), text=shown)
    print("# {} on {}, secrets redacted as <SECRET:path>\n{}".format(label, a.orchestrator, shown), end="")
    return 0


# ---------------------------------------------------------------- build

def translate_into(out: Path, capture: Path, site: str, release: str, extra: List[str]) -> str:
    import translate
    code, text = quiet(translate.main, ["--capture", str(capture), "--site-type", site, "--out", str(out), "--target-release", release] + extra)
    if code != 0:
        raise Fail("translate stopped (code {}): {}".format(code, text.strip()[-400:]))
    return text


def checked(folder: Path, site: str, validator: str, substitute: bool) -> List[str]:
    """Problems with the template's example row: renders, reproduces the reference, and Orchestrator /validate accepts it."""
    import check
    result, _ = quiet(check.run, folder, site, None, validator, substitute)
    return ["{}: {}".format(name, json.dumps(c["detail"])[:400]) for name, c in result["checks"].items() if not c["ok"]]


def release_of(nick: str) -> Optional[str]:
    """The Orchestrator's release, or None when its key may not read it."""
    try:
        versions = client_for(nick).get("/gms/versions")
    except PermissionDeniedError:
        return None
    return versions.get("current") if isinstance(versions, dict) else None


def cmd_build(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog=PROG + " build", description="Read one appliance (GET only), convert it, and keep the result only if Orchestrator /validate accepts it.")
    p.add_argument("--orchestrator", required=True, help=".env profile nickname to read the appliance from")
    p.add_argument("--appliance", required=True, help="hostname or nePk (e.g. 12.NE)")
    p.add_argument("--out", type=Path, help="parent folder (default ./preconfig-output)")
    p.add_argument("--validate-on", metavar="NICK", help="validate on this Orchestrator instead of the source one")
    p.add_argument("--target-release", help="the validator's Orchestrator release, when its key may not read it")
    from translate import ecos_arg
    p.add_argument("--target-ecos", type=ecos_arg, help="ECOS release of the appliances the preconfig is for, e.g. 9.6.2.1 (unknown by default)")
    a = p.parse_args(argv)
    a.out = a.out or output_root()
    validator = a.validate_on or a.orchestrator
    check_validator(validator)
    from capture import Stop, capture
    print("reading {} from {} (GET only) ...".format(a.appliance, a.orchestrator), flush=True)
    try:
        cap = capture(a.orchestrator, a.appliance, "reference", 0.2, work_dir() / "reference" / a.orchestrator)
    except Stop as stop:
        raise Fail(str(stop))
    run = json.loads((cap / "run.json").read_text(encoding="utf-8"))
    host = run["appliance"].get("hostName") or run["appliance"]["nePk"]
    release = a.target_release or (run["release"] if validator == a.orchestrator else release_of(validator))
    if not release:
        raise Fail("{} did not give its release (GET /gms/versions refused): pass --target-release, e.g. one of {}".format(
            validator, ", ".join(sorted(p.stem[len("orchestrator-"):] for p in DEFAULT_TEMPLATES_DIR.glob("orchestrator-*.yml")))))
    site = safe_name(host).lower()
    folder = a.out / safe_name(host)
    staging = a.out / ".building-{}".format(safe_name(host))
    shutil.rmtree(staging, ignore_errors=True)
    print("converting for Orchestrator release {} ...".format(release), flush=True)
    try:
        text = translate_into(staging, cap, site, release, ["--target-ecos", a.target_ecos] if a.target_ecos else [])
        print("checking and validating on {} ...".format(validator), flush=True)
        problems = checked(staging, site, validator, validator != a.orchestrator)
        if problems:
            raise Fail("not saved, the result did not pass:\n  " + "\n  ".join(problems))
        info = {"orchestrator": a.orchestrator, "validator": validator, "appliance": run["appliance"], "capture": str(cap),
                "target_release": release, "target_ecos": a.target_ecos, "site_type": site, "built": utc_stamp(), "toolkit": toolkit_version()}
        write_json(staging / "build.json", info)
        a.out.mkdir(parents=True, exist_ok=True)
        keep_previous(folder)
        staging.rename(folder)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    verdict = next((l.split(":", 1)[1].strip() for l in text.splitlines() if l.startswith("verdict:")), "")
    record("build", folder=str(folder), verdict=verdict, summary=str(folder / "summary.md"), validator=validator, release=release)
    print("\nSaved {}  (/validate on {}: accepted)".format(folder, validator))
    print("  reference.yaml      the appliance as preconfig YAML")
    print("  {:<19} the template".format(site + ".jinja2"))
    print("  {:<19} empty sheet (headers only) for new sites".format(site + ".csv"))
    print("  example.csv         this appliance's own values, secrets blank - copy it to start your sites.csv")
    print("  summary.md          what was carried and what was not; verdict: {}".format(verdict))
    print("\nNext: choose the per-site settings\n  {} variables {}\nor let other sites of the same design choose them\n  {} compare {} <site> <site>".format(PROG, folder, PROG, folder))
    return 0


# ---------------------------------------------------------------- variables

class Picker:
    """The per-site choice for every sheet row of a build, shown as Top N or section by section.

    Locked rows (secrets, hostname) are always columns; linked rows follow the row they copy (a gateway is the host part
    of its interface address). Every other row has a number and flips between VARIABLE and FIXED.
    """

    def __init__(self, reference: Dict[str, Any], rows: List[Dict[str, str]], ranking: Dict[str, Dict[str, Any]], others: Optional[List[Dict[str, Any]]] = None) -> None:
        from variables import labelled, setting_key
        self.rows = rows
        self.others = others or []
        primary = {r["column"]: r for r in rows if r["decision"] == "variable" and not r.get("linked_to")}
        for r in rows:
            r["label"] = labelled(reference, r["path"]).replace("deploymentInfo.", "", 1)  # the section column says it
            r["locked"] = (r["path"] == "applianceInfo.hostname" or r["reason"].startswith("secret")
                           or (r["decision"] == "variable" and r["path"].split(".")[0] in ALWAYS_SECTIONS and r["reason"] != LAYOUT_DIFFERS))  # docs/decisions/0006, 0018
            r["follows"] = primary.get(r.get("linked_to") or "") if r.get("linked_to") else None
            rank = ranking.get(setting_key(reference, r["path"]))
            r["pct"] = rank["differs_pct"] if rank else None
            r["support"] = rank["appliances"] if rank else 0
            parts = r["path"].split(".")
            r["section"] = ".".join(parts[:2]).split("[")[0] if parts[0] == "deploymentInfo" and len(parts) > 2 else parts[0].split("[")[0]
        self.free = [r for r in rows if not r["locked"] and r["follows"] is None]
        self.free.sort(key=lambda r: (r["section"], r["path"]))
        for n, r in enumerate(self.free, 1):
            r["n"] = n
        self.sections = sorted({r["section"] for r in self.free})
        self.on = {r["path"]: r["decision"] == "variable" for r in self.free}

    def start_all(self) -> None:
        """--pick all: every setting per site."""
        for r in self.free:
            self.on[r["path"]] = True

    def ranked(self, count: int) -> List[Dict[str, str]]:
        hits = sorted((r for r in self.free if r["pct"] is not None and r["reason"] != LAYOUT and r["path"] not in COMMA_LISTS),
                      key=lambda r: (-r["pct"], -r["support"], r["n"]))  # layout, template groups and overlays: only by a flip
        return hits[:count]

    def start_top(self, count: int) -> None:
        """Tick the Top N and this site's own addresses; untick the rest."""
        top = {r["path"] for r in self.ranked(count)}
        for r in self.free:
            self.on[r["path"]] = r["path"] in top or r["reason"] in ADDRESS_REASONS

    def value(self, r: Dict[str, str]) -> str:
        if r["locked"] and r["reason"].startswith("secret"):
            return "(secret)"
        return (r["value"] if len(r["value"]) <= 30 else r["value"][:27] + "...") or '""'

    def line(self, r: Dict[str, str]) -> str:
        num = str(r.get("n", "-"))
        pick = "VARIABLE" if r["locked"] or self.on.get(r["path"]) else "fixed"
        pct = "{}%".format(r["pct"]) if r.get("pct") is not None else ""
        extra = "  ".join(self.value_at(o, r) for o in self.others)
        return " {:>4}  {:<52} {:<30} {:>5}  {}{}".format(num, r["label"][:52], self.value(r), pct, pick, ("   " + extra) if extra else "")

    @staticmethod
    def value_at(other: Dict[str, Any], r: Dict[str, str]) -> str:
        return "| " + (cell(other[r["path"]]) if r["path"] in other else "-")[:20]

    def follower_lines(self, r: Dict[str, str]) -> List[str]:
        return ["        {:<52} follows {}".format(f["label"][:52], "#{}".format(r["n"]) if "n" in r else "the line above") for f in self.rows if f["follows"] is r]

    def show(self, rows: List[Dict[str, str]], title: str) -> List[str]:
        out = ["  -- {} {}".format(title, "-" * max(0, 60 - len(title)))]
        for r in rows:
            out.append(self.line(r))
            out.extend(self.follower_lines(r))
        return out

    def header(self) -> List[str]:
        locked = [r for r in self.rows if r["locked"]]
        secrets = sum(1 for r in locked if r["reason"].startswith("secret"))
        return ["  Always per site, no questions: hostname, {} secret(s) and {} Deployment-page and appliance-info values (type l to list them)".format(
                    secrets, len(locked) - secrets - 1),
                "  {:>4}  {:<52} {:<30} {:>5}  {}".format("#", "Setting", "Value now", "Diff", "Choice")]

    def locked_view(self) -> List[str]:
        return self.show([r for r in self.rows if r["locked"] and r["follows"] is None], "Always per site (locked)")

    def top_view(self, count: int) -> List[str]:
        top = self.ranked(count)
        own = [r for r in self.free if r["reason"] in ADDRESS_REASONS and r not in top]
        out = self.header() + self.show(top, "Top {}: most often different between sites".format(count))
        if own:
            out += self.show(own, "Also this site's own addresses")
        rest = len(self.free) - len(top) - len(own)
        return out + ["  -- {} more settings: type a to see all, by section".format(rest)]

    def section_list(self) -> List[str]:
        out = ["  {:>4}  {:<40} {:>8} {:>8}".format("", "Section", "Settings", "Variable")]
        for i, s in enumerate(self.sections, 1):
            rows = [r for r in self.free if r["section"] == s]
            out.append("  {:>4}  {:<40} {:>8} {:>8}".format("s" + str(i), s, len(rows), sum(self.on[r["path"]] for r in rows)))
        return out

    def section_view(self, index: int) -> List[str]:
        name = self.sections[index - 1]
        return self.show([r for r in self.free if r["section"] == name], name)

    def flip(self, numbers: List[int]) -> List[str]:
        out = []
        for n in numbers:
            r = next((x for x in self.free if x["n"] == n), None)
            if r is None:
                out.append("  no setting #{}".format(n))
                continue
            self.on[r["path"]] = not self.on[r["path"]]
            out.append(self.line(r))
        return out

    def search(self, word: str) -> List[str]:
        hits = [r for r in self.free if word.lower() in r["label"].lower() or word.lower() in r["value"].lower()]
        return [self.line(r) for r in hits[:40]] + (["  ... {} more".format(len(hits) - 40)] if len(hits) > 40 else []) or ["  nothing matches {!r}".format(word)]

    def set_by_name(self, names: List[str]) -> List[str]:
        """--set: column names or paths made VARIABLE; returns the names that match nothing."""
        unknown = []
        for name in names:
            hit = [r for r in self.free if name in (r["path"], r["column"], r["label"])]
            if not hit:
                unknown.append(name)
            for r in hit:
                self.on[r["path"]] = True
        return unknown

    def fix_by_name(self, names: List[str], reference: Dict[str, Any]) -> List[str]:
        """--fix: column names or paths made FIXED (the opposite of --set); returns the reason for each name it refused."""
        columns = {r["path"]: s["column"] for r, s in zip(self.rows, self.sheet(reference))}  # the names a dry run showed
        problems, unknown, fixed = [], [], []
        for name in names:
            hit = [r for r in self.rows if name in (r["path"], r["column"], columns[r["path"]], r["label"])]
            hit = [r for r in hit if r["follows"] is None] or hit  # a column a follower shares names the row it follows
            if not hit:
                unknown.append(name)
            for r in hit:
                if r["locked"]:
                    problems.append("{} cannot be fixed: always per site, {}".format(name, self.locked_why(r)))
                elif r["follows"] is not None:
                    problems.append("{} cannot be fixed on its own: it follows {} ({})".format(name, r["follows"]["column"], r["follows"]["path"]))
                else:
                    fixed.append(r)
        for r in fixed:
            self.on[r["path"]] = False
        return problems + (["no setting named {}".format(", ".join(unknown))] if unknown else [])

    @staticmethod
    def locked_why(r: Dict[str, Any]) -> str:
        return ("hostname" if r["path"] == "applianceInfo.hostname" else "secret" if r["reason"].startswith("secret")
                else "deployment-page value (docs/decisions/0006)")

    def sheet(self, reference: Dict[str, Any]) -> List[Dict[str, str]]:
        """The rows as variables.csv: chosen rows VARIABLE with a column name, followers as their primary."""
        from variables import RESERVED, column_name
        used = {r["column"] for r in self.rows if r["column"]} | RESERVED
        out = []
        for r in self.rows:
            row = {k: r[k] for k in ("decision", "column", "linked_to", "value", "path", "reason")}
            if r["locked"]:
                pass
            elif r["follows"] is not None:
                row["decision"] = "variable" if r["follows"]["locked"] or self.on.get(r["follows"]["path"]) else "fixed"
            else:
                row["decision"] = "variable" if self.on[r["path"]] else "fixed"
                if row["decision"] == "variable" and not row["column"]:
                    base, n = column_name(reference, r["path"]), 2
                    name = base
                    while name in used:
                        name, n = "{}_{}".format(base, n), n + 1
                    used.add(name)
                    row["column"] = name
            out.append(row)
        return out

    def record(self, reference: Dict[str, Any], folder: Optional[Path], lines: Optional[Dict[str, List[int]]] = None) -> None:
        """Every row as the picker held it, the chosen columns and the folder written, for the run's recorder.

        A dry run writes no folder (None) and gives lines: each ticked row then also has the lines of its value in reference.yaml."""
        rows = []
        for r, sheet_row in zip(self.rows, self.sheet(reference)):
            secret = r["reason"].startswith("secret")
            rows.append({"n": r.get("n"), "path": r["path"], "section": r["section"], "label": r["label"], "value": "<SECRET>" if secret else r["value"],
                         "pct": r["pct"], "support": r["support"], "locked": r["locked"], "locked_why": self.locked_why(r) if r["locked"] else None,
                         "follows": r["follows"]["path"] if r["follows"] is not None else None, "on": sheet_row["decision"] == "variable",
                         "column": sheet_row["column"] if sheet_row["decision"] == "variable" else ""})
            if lines is not None:
                rows[-1]["lines"] = lines.get(r["path"]) if rows[-1]["on"] else None
        record("picker", rows=rows, columns=self.columns(reference), folder=str(folder) if folder else None)

    def columns(self, reference: Dict[str, Any]) -> List[str]:
        names: List[str] = []
        for r in self.sheet(reference):
            if r["decision"] == "variable" and r["column"] not in names:
                names.append(r["column"])
        return names


def value_lines(text: str) -> Dict[str, List[int]]:
    """Every path of a YAML text (as variables.csv writes it: a.b[0].c) -> [first, last] line, from 1, of its value."""
    import yaml
    found: Dict[str, List[int]] = {}

    def walk(node: Any, path: str) -> None:
        end = node.end_mark.index
        while end > node.start_mark.index and text[end - 1].isspace():  # a block ends where the next key starts
            end -= 1
        found[path] = [node.start_mark.line + 1, node.end_mark.line + 1 - text.count("\n", end, node.end_mark.index)]
        if isinstance(node, yaml.MappingNode):
            for key, item in node.value:
                walk(item, "{}.{}".format(path, key.value) if path else str(key.value))
        elif isinstance(node, yaml.SequenceNode):
            for index, item in enumerate(node.value):
                walk(item, "{}[{}]".format(path, index))
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    if root is not None:
        walk(root, "")
    return found


def interact(picker: Picker, mode: str, read: Callable[[str], str]) -> bool:
    """The tick list. Returns False when the user quits."""
    view = picker.top_view(int(mode[3:])) if mode.startswith("top") else picker.section_list()
    print("\n".join(view))
    help_line = "Type numbers to flip (e.g. 4 12), s1.. to open a section, a for all sections, t for the Top list, l for the locked ones, a word to search, Enter when done, q to quit."
    print(help_line)
    while True:
        answer = read("> ").strip()
        if answer == "":
            return True
        if answer.lower() == "q":
            return False
        if answer.lower() == "l":
            print("\n".join(picker.locked_view()))
        elif answer.lower() == "a":
            print("\n".join(picker.section_list()))
        elif answer.lower() == "t":
            print("\n".join(picker.top_view(int(mode[3:]) if mode.startswith("top") else 20)))
        elif re.fullmatch(r"(?i)s\d+", answer) and 0 < int(answer[1:]) <= len(picker.sections):
            print("\n".join(picker.section_view(int(answer[1:]))))
        elif re.fullmatch(r"[\d ,]+", answer):
            print("\n".join(picker.flip([int(x) for x in re.findall(r"\d+", answer)])))
        elif answer in ("?", "h", "help"):
            print(help_line)
        else:
            print("\n".join(picker.search(answer)))


def build_site(folder: Path, info: Dict[str, Any], name: str, sheet: List[Dict[str, str]], rows_for: Optional[List[Dict[str, Any]]] = None,
               compare: Optional[List[str]] = None) -> Path:
    """Write site-<name>/ from an edited sheet: template, CSV and reference, kept only if the example row validates."""
    from variables import write_sheet
    base = folder if (folder / "build.json").is_file() else folder.parent
    target = base / "site-{}".format(name)
    staging = base / ".building-site-{}".format(name)
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        write_sheet(sheet, staging / "variables.csv")
        translate_into(staging, Path(info["capture"]), name, info["target_release"], ["--variables", str(staging / "variables.csv")] + (["--target-ecos", info["target_ecos"]] if info.get("target_ecos") else []))
        substitute = info["validator"] != info["orchestrator"]
        problems = checked(staging, name, info["validator"], substitute)
        if problems:
            raise Fail("site-{} not saved, the example row did not pass:\n  ".format(name) + "\n  ".join(problems))
        if rows_for:
            write_rows(staging, name, rows_for)
        keep_previous(target)
        staging.rename(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    print("\nSaved {}  (example row validated on {})".format(target, info["validator"]))
    print("  {:<20} the template\n  {:<20} fill one row per site (the first row is {}'s own values)".format(name + ".jinja2", "sites.csv", info["appliance"].get("hostName")))
    if rows_for:
        print("  sites.csv also has a row for {}".format(", ".join(str(r["hostname"]) for r in rows_for)))
    print("\nNext:\n  {} validate {} --csv {}".format(PROG, target, target / "sites.csv"))
    return target


def layout_differences(reference: Dict[str, Any], other: Dict[str, Any]) -> List[str]:
    """List items another site holds at the same position under another interface name (wan1 where this site has wan0), and
    a deployment mode, interface type or HA interface that differs: another design, so such a site gets no row.

    Rows are matched by position, so such a site's values would land on the wrong interface. Names are compared directly;
    a tunnel or rule name may differ per site and does not count."""
    out: List[str] = []

    def walk(mine: Any, yours: Any, path: str) -> None:
        if isinstance(mine, dict) and isinstance(yours, dict):
            for key in mine:
                if key in yours:
                    walk(mine[key], yours[key], "{}.{}".format(path, key) if path else key)
        elif isinstance(mine, list) and isinstance(yours, list):
            for i, (x, y) in enumerate(zip(mine, yours)):
                name = next((k for k in INTERFACE_NAME_KEYS if isinstance(x, dict) and k in x), None)
                if name and isinstance(y, dict) and x.get(name) != y.get(name):
                    out.append("{}[{}]: {} vs {}".format(path, i, x.get(name), y.get(name)).replace("deploymentInfo.", "", 1))
                walk(x, y, "{}[{}]".format(path, i))

    walk(reference, other, "")
    from variables import sheet_items
    theirs = dict(sheet_items(other))
    for path, value in sheet_items(reference):
        key = re.sub(r"\[\d+\]", "", path).split(".")[-1]
        if path.split(".")[0] in ALWAYS_SECTIONS and key in DESIGN_KEYS and theirs.get(path, value) != value:
            out.append("{}: {} vs {}".format(path.replace("deploymentInfo.", "", 1), value, theirs[path]))
    return out


def shape_note(mine: Dict[str, Any], theirs: Dict[str, Any], site: str, base: str) -> str:
    """Why `site` cannot be a row of the base's template (sheet items of each), or '' when it fits.

    The template holds the base's settings only: a row would drop the site's own extra settings and give it the base's."""
    from variables import only_at
    extra, missing = only_at(mine, theirs)
    extra = [p for p in extra if not COMMENT.search(p)]
    missing = [p for p in missing if not RELEASE_DEFAULTS.search(p)]  # a column with a blank cell for that site (docs/decisions/0019)
    parts = ["{} uses DHCP {} on {}, {} uses {}".format(site, theirs[p], mine.get(p[:-len("dhcpType")] + "dhcpInterfaceName"), base, v)
             for p, v in mine.items() if p.endswith(".dhcpType") and theirs.get(p, v) != v]
    for paths, what in ((extra, "{} has {} setting(s) {} lacks"), (missing, "{} lacks {} setting(s) {} has")):
        if paths:
            parts.append(what.format(site, len(paths), base) + ": " + ", ".join(paths[:3]) + (" ..." if len(paths) > 3 else ""))
    return "; ".join(parts)


def write_rows(site_dir: Path, name: str, others: List[Dict[str, Any]]) -> None:
    """sites.csv: the example row, then one row per compared site with its own values (secrets and serial left blank)."""
    meta = json.loads((site_dir / "columns.json").read_text(encoding="utf-8"))
    with (site_dir / "example.csv").open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        header, rows = reader.fieldnames or [], list(reader)
    for other in others:
        row = {}
        for column in header:
            col = meta["columns"].get(column)
            if column == "preconfig_name":
                row[column] = "{}_preconfig".format(other["hostname"])
            elif column == "serial_number" or col is None or col["secret"]:
                row[column] = ""
            else:
                row[column] = cell(other["items"].get(col["path"]))
        rows.append(row)
    with (site_dir / "sites.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)


def ensure_sites_csv(site_dir: Path) -> None:
    if not (site_dir / "sites.csv").exists():
        shutil.copyfile(site_dir / "example.csv", site_dir / "sites.csv")


def read_sheet_rows(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(r, linked_to=r.get("linked_to") or "") for r in csv.DictReader(handle) if r.get("path")]


def show_columns(picker: Picker, reference: Dict[str, Any]) -> None:
    cols = picker.columns(reference)
    print("\n{} columns: {}".format(len(cols), ", ".join(cols)))


def finish(picker: Picker, reference: Dict[str, Any], default_name: str, name: Optional[str], yes: bool, read: Callable[[str], str]) -> Optional[str]:
    show_columns(picker, reference)
    if not name:
        name = (read("Site type name [{}]: ".format(default_name)).strip() if not yes else "") or default_name
    name = safe_name(name).lower()
    if not yes and read("Build site-{}? [Y/n] ".format(name)).strip().lower() not in ("", "y", "yes"):
        print("nothing written")
        return None
    return name


def cmd_variables(argv: List[str], read: Callable[[str], str] = input) -> int:
    p = argparse.ArgumentParser(prog=PROG + " variables", description="Choose which settings are per site (CSV columns) and build site-<name>/ from a build folder.")
    p.add_argument("folder", type=Path)
    p.add_argument("--pick", choices=("top10", "top20", "all"), help="start from the Top 10 or Top 20, or tick every setting (all); with --yes no questions are asked")
    p.add_argument("--set", help="comma-separated column names or paths to make per site as well")
    p.add_argument("--fix", help="comma-separated column names or paths to keep fixed (the opposite of --set; applied after it)")
    p.add_argument("--name", help="site type name (default: the appliance's)")
    p.add_argument("--yes", action="store_true", help="accept the choice without the tick list or questions")
    p.add_argument("--dry-run", action="store_true", help="print the columns this choice gives and write nothing: no questions, no Orchestrator")
    a = p.parse_args(argv)
    if a.dry_run:
        record("dry-run")  # the run log skips this run: a preview writes nothing (cli-requests R5)
    info = load_build(a.folder)
    base = a.folder if (a.folder / "build.json").is_file() else a.folder.parent
    from variables import load_ranking
    reference = load_yaml(base / "reference.yaml")
    picker = Picker(reference, read_sheet_rows(base / "variables.csv"), load_ranking())
    mode = a.pick
    if not mode:
        if a.yes or a.dry_run:
            raise Fail("{} needs --pick top10, top20 or all".format("--yes" if a.yes else "--dry-run"))
        print("Hostname, serial number and secrets are always per site.\nHow do you want to pick the rest?\n"
              "  1) Top 10 - the settings most often different between sites (start here)\n  2) Top 20\n  3) All settings, by section")
        mode = {"1": "top10", "2": "top20", "3": "all", "": "top10"}.get(read("> ").strip())
        if not mode:
            raise Fail("answer 1, 2 or 3")
    if mode == "all" and a.pick:
        picker.start_all()
    else:
        picker.start_top(20 if mode == "top20" else 10)
    if a.set:
        unknown = picker.set_by_name([s.strip() for s in a.set.split(",") if s.strip()])
        if unknown:
            raise Fail("--set: no setting named {}".format(", ".join(unknown)))
    if a.fix:
        refused = picker.fix_by_name([s.strip() for s in a.fix.split(",") if s.strip()], reference)
        if refused:
            raise Fail("--fix: " + "; ".join(refused))
    if a.dry_run:  # the GUI's preview after each tick (cli-requests R5)
        show_columns(picker, reference)
        picker.record(reference, None, value_lines((base / "reference.yaml").read_text(encoding="utf-8")))
        return 0
    if not a.yes and not interact(picker, mode, read):
        print("nothing written")
        return 1
    name = finish(picker, reference, info["site_type"], a.name, a.yes, read)
    if not name:
        return 1
    target = build_site(a.folder, info, name, picker.sheet(reference))
    ensure_sites_csv(target)
    picker.record(reference, target)
    return 0


# ---------------------------------------------------------------- compare

def cmd_compare(argv: List[str], read: Callable[[str], str] = input) -> int:
    p = argparse.ArgumentParser(prog=PROG + " compare", description="Read other sites of the same design (GET only); settings that differ become CSV columns, and the CSV gets a row per site.")
    p.add_argument("folder", type=Path)
    p.add_argument("sites", nargs="+", help="hostnames or nePks on the same Orchestrator (1 to 4; 2 recommended)")
    p.add_argument("--name", help="site type name (default: the appliance's)")
    p.add_argument("--yes", action="store_true", help="build without asking")
    a = p.parse_args(argv)
    if len(a.sites) > 4:
        raise Fail("compare takes 1 to 4 other sites")
    info = load_build(a.folder)
    me = info.get("appliance") or {}
    base_ne = me.get("nePk")

    def refuse_base(site: str) -> None:
        raise Fail("{} is the appliance this build was read from: give other sites of the same design".format(site))
    for i, site in enumerate(a.sites):
        if site.lower() in {str(me.get(k) or "").lower() for k in ("hostName", "nePk")}:
            refuse_base(site)
        if site.lower() in (s.lower() for s in a.sites[:i]):  # two rows with one preconfig_name otherwise
            raise Fail("{} is given twice: give each other site once".format(site))
    base = a.folder if (a.folder / "build.json").is_file() else a.folder.parent
    from capture import Stop, capture
    from translate import Capture, NothingToConvert, allowed_paths, build, prune
    from variables import classify, only_at, sheet_items, write_sheet
    caps = []
    for site in a.sites:
        print("reading {} from {} (GET only) ...".format(site, info["orchestrator"]), flush=True)
        try:
            caps.append(capture(info["orchestrator"], site, "sample", 0.2, work_dir() / "samples" / info["orchestrator"]))
        except Stop as stop:
            raise Fail(str(stop))
    allowed = allowed_paths(info["target_release"])
    others = []
    read_ne = set()
    for site, c in zip(a.sites, caps):
        try:
            cap = Capture(c)
            other = prune(build(cap, [], info.get("target_ecos")), allowed, [])
        except NothingToConvert:
            raise Fail("{}: no appliance or deployment data could be read (offline with no Orchestrator cache?), so it cannot be compared".format(site))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:  # a damaged capture: one line naming the site as typed, not a traceback
            detail = Path(error.filename).name if isinstance(error, OSError) and error.filename else str(error)[:200]
            raise Fail("{}: its capture could not be read ({}: {}); run compare again to read it afresh".format(site, type(error).__name__, detail))
        except Exception as error:  # anything else is a bug in the toolkit, not in the capture
            raise Fail("{}: toolkit error {}: {} (please report it)".format(site, type(error).__name__, str(error)[:200]))
        ne = (cap.run.get("appliance") or {}).get("nePk")
        if base_ne and ne == base_ne:
            refuse_base(site)
        if ne and ne in read_ne:  # one box typed by hostname and by nePk
            raise Fail("{} is given twice: give each other site once".format(site))
        read_ne.add(ne)
        others.append(other)
    reference = load_yaml(base / "reference.yaml")
    names = [o["applianceInfo"]["hostname"] for o in others]
    base_host = me.get("hostName")
    seen = [dict(sheet_items(o)) for o in others]
    mine = dict(sheet_items(reference))
    notes: Dict[str, str] = {}  # site -> why it gets no row; decided first, so such a site decides no column either
    for n, o, s in zip(names, others, seen):
        diffs = layout_differences(reference, o)
        why = "" if diffs else shape_note(mine, s, n, base_host)
        if diffs:
            notes[n] = ("Note: {} has another design ({}{}): its values would not fit this template, so sites.csv gets no row for it; "
                        "build a separate template for that design.".format(n, ", ".join(diffs[:4]), " ..." if len(diffs) > 4 else ""))
        elif why:
            notes[n] = ("Note: {}. sites.csv gets no row for {}: the template holds {}'s settings only. Run compare with {} as the base "
                        "(build it first), or build a separate template for that design.".format(why, n, base_host, n))
    kept = [(n, o, s) for n, o, s in zip(names, others, seen) if n not in notes]
    from generate import Builder
    probe = Builder()
    probe.find_secrets(reference)
    rows = [{k: cell(v) for k, v in r.items()} for r in classify(reference, probe.secret_paths, None, [o for _, o, _ in kept])]
    picker = Picker(reference, rows, {}, [s for _, _, s in kept])
    differ = [r for r in picker.free if picker.on[r["path"]]]
    same = sum(1 for r in rows if r["reason"] == "same at every site you gave")
    only = sum(1 for r in rows if r["flag"] == "true" and r["reason"].startswith("only "))
    locked = sum(1 for r in picker.rows if r["locked"])
    shown = [n for n, _, _ in kept]
    print("\n{} settings are always per site (hostname, secrets, Deployment page, appliance info), and {} more are per site at {} and {}:".format(
        locked, len(differ), base_host, ", ".join(shown) or "no other site"))
    print("  {:>4}  {:<52} {:<30} {:>5}  {}   {}".format("#", "Setting", base_host, "", "Choice", "   ".join("| " + n for n in shown)))
    print("\n".join(picker.show(differ, "Different between your sites (pre-ticked)")))
    print("{} settings are the same at every site and stay fixed.".format(same))
    if only:
        print("{} settings exist only at some of the sites and are left fixed.".format(only))
    for n, _, s in zip(names, others, seen):
        comments = [p for p in only_at(mine, s)[0] if COMMENT.search(p)]
        unset = [p for p in only_at(mine, s)[1] if RELEASE_DEFAULTS.search(p)]
        if n in notes:
            print(notes[n])
            continue
        if unset:
            print("Note: {} has no {} in its capture: those cells of its row are blank; fill them in (or untick to give it {}'s value).".format(n, ", ".join(unset), base_host))
        if comments:
            print("Note: {}'s comment{} {} {} left out of its row: {} has none there.".format(
                n, "s" if len(comments) > 1 else "", ", ".join(comments[:3]) + (" ..." if len(comments) > 3 else ""), "are" if len(comments) > 1 else "is", base_host))
    if not a.yes:
        answer = read("Build the template with these settings per site? [Y/n/edit] ").strip().lower()
        if answer in ("n", "no"):
            print("nothing written")
            return 1
        if answer in ("e", "edit") and not interact(picker, "all", read):
            print("nothing written")
            return 1
    label = {r["path"]: r["label"] for r in picker.rows}
    for r in picker.sheet(reference):  # a fixed setting gives every row the base's value: say so where a site has another
        differs = [n for n, _, s in kept if r["decision"] == "fixed" and r["path"] in mine and cell(s.get(r["path"])) != cell(mine[r["path"]])]
        if differs:
            print("Warning: {} stays fixed: {} {} {}'s value {}".format(label[r["path"]], ", ".join(differs), "gets" if len(differs) == 1 else "get",
                                                                         base_host, cell(mine[r["path"]]) or '""'))
    name = finish(picker, reference, info["site_type"], a.name, a.yes, read)
    if not name:
        return 1
    rows_for = [{"hostname": n, "items": s} for n, _, s in kept]
    target = build_site(a.folder, info, name, picker.sheet(reference), rows_for)
    ensure_sites_csv(target)  # every other site may have been left out: sites.csv still holds this site's row
    picker.record(reference, target)
    return validate_rows(target, target / "sites.csv", info, None)


# ---------------------------------------------------------------- validate, upload

def validate_rows(folder: Path, csv_path: Path, info: Dict[str, Any], orchestrator: Optional[str]) -> int:
    import render
    nick = orchestrator or info["validator"]
    check_validator(nick)
    print("\nvalidating every row of {} on {} ...".format(csv_path.name, nick), flush=True)
    args = ["--site-dir", str(folder), "--csv", str(csv_path), "--validate", nick] + (["--substitute"] if nick != info["orchestrator"] else [])
    code = render.main(args)
    print("rows that passed are in {}".format(folder / "rendered") if code == 0 else "fix the FAIL rows in {} and run validate again; the rows that passed are in {}".format(csv_path, folder / "rendered"))
    return code


def site_folder(folder: Path) -> Path:
    if not (folder / "columns.json").is_file():
        raise Fail("{} has no template (columns.json): give a build folder or a site-<name> folder".format(folder))
    return folder


def cmd_validate(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog=PROG + " validate", description="Render every CSV row and validate it on the Orchestrator; passing rows are written to rendered/.")
    p.add_argument("folder", type=Path)
    p.add_argument("--csv", required=True, type=Path)
    p.add_argument("--orchestrator", help="validate on this Orchestrator instead of the one from build")
    a = p.parse_args(argv)
    return validate_rows(site_folder(a.folder), a.csv, load_build(a.folder), a.orchestrator)


def cmd_upload(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog=PROG + " upload", description="Validate every CSV row, skip names or serials already on the Orchestrator, and upload only with --approve.")
    p.add_argument("folder", type=Path)
    p.add_argument("--csv", required=True, type=Path)
    p.add_argument("--orchestrator", help="upload to this Orchestrator instead of the one the build was validated on")
    p.add_argument("--approve", action="store_true", help="upload the valid rows that do not conflict")
    a = p.parse_args(argv)
    import upload
    info = load_build(a.folder)
    nick = a.orchestrator or info["validator"]
    print("{} {} on {} ...".format("uploading" if a.approve else "checking (no upload without --approve)", a.csv.name, nick), flush=True)
    return upload.main(["--site-dir", str(site_folder(a.folder)), "--csv", str(a.csv), "--orchestrator", nick] + (["--approve"] if a.approve else []))


# ---------------------------------------------------------------- dispatch

VERBS: Dict[str, Callable[[List[str]], int]] = {"list": cmd_list, "preconfigs": cmd_preconfigs, "build": cmd_build, "variables": cmd_variables,
                                                "compare": cmd_compare, "validate": cmd_validate, "upload": cmd_upload}


def stages() -> List[str]:
    return sorted(p.stem.replace("_", "-") for p in HERE.glob("*.py") if p.stem not in NOT_STAGES)


def run_stage(name: str, argv: List[str]) -> int:
    script = HERE / "{}.py".format(name.replace("-", "_"))
    if not re.fullmatch(r"[a-z][a-z_-]*", name) or name.replace("-", "_") in NOT_STAGES or not script.is_file():
        print("unknown command {!r}; run: {} --help".format(name, PROG), file=sys.stderr)
        return 2
    stage = importlib.import_module(name.replace("-", "_"))  # imported, not run as __main__, so its Stop is the class main() catches
    if not callable(getattr(stage, "main", None)):
        print("unknown command {!r}; run: {} --help".format(name, PROG), file=sys.stderr)
        return 2
    sys.argv = ["{} {}".format(PROG, name)] + argv
    try:
        return stage.main(argv)
    except SystemExit as done:
        return done.code if isinstance(done.code, int) else (0 if done.code is None else 1)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip() + "\n\nstages: " + ", ".join(stages()))
        return 0 if argv else 2
    if argv[0] in ("-V", "--version", "version"):
        print(toolkit_version())
        return 0
    import runlog
    with runlog.logged(argv) as log:  # one line in reports/preconfig.jsonl per command (cli-requests R4)
        log["code"] = run_verb(argv[0], argv[1:])
    return log["code"]


def run_verb(verb: str, rest: List[str]) -> int:
    from validate import Stop as ValidatorStop  # 401/403/network on /validate: every row would fail the same way
    try:
        if verb in ("validate", "upload") and rest and rest[0].startswith("--") and rest[0] not in ("--help", "-h"):
            return run_stage(verb, rest)  # the stage form: validate --orchestrator X files..., upload --site-dir ...
        if verb not in VERBS:
            return run_stage(verb, rest)
        return VERBS[verb](rest)
    except (Fail, ValidatorStop) as fail:
        print("error: {}".format(fail), file=sys.stderr)
        return 2
    except EdgeConnectError as error:
        print("error: {} ({})".format(error, type(error).__name__), file=sys.stderr)
        return 2
    except EOFError:  # a prompt read the end of stdin (a GUI run has an empty one): ends as a quit at the prompt does
        print("\nnothing written")
        return 1
    except KeyboardInterrupt:
        print("\nstopped; nothing more written", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

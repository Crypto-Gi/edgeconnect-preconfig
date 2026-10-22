#!/usr/bin/env python3
"""edgeconnect-preconfig: build preconfig templates from a working appliance, without an AI.

  appliances --orchestrator NICK [--search TEXT]       test the Orchestrator profile, list its appliances
  build      --orchestrator NICK --appliance NAME       read one appliance, write a validated YAML, template and CSV
             [--out DIR] [--validate-on NICK]
  variables  FOLDER [--pick top10|top20] [--set C,..]  choose the per-site settings: Top 10, Top 20 or all, by section
             [--name NAME] [--yes]
  compare    FOLDER SITE [SITE ...] [--name NAME]       other sites of the same design pick the per-site settings;
             [--yes]                                    the CSV gets a row for each site
  validate   FOLDER --csv FILE [--orchestrator NICK]    render and /validate every CSV row
  upload     FOLDER --csv FILE [--orchestrator NICK]    show what would be uploaded; uploads only with --approve
             [--approve]

FOLDER is what build wrote (or a site-<name> folder inside it). Validation runs on the Orchestrator the appliance was read
from, unless --validate-on names another; either way it must be listed in EC_VALIDATORS. Every Orchestrator read is a GET.
The pipeline stages still run by name: capture, cache-check, translate, generate, render, check, cells, synth, options,
validate and upload with their own options (edgeconnect-preconfig STAGE --help).
"""
import argparse
import contextlib
import csv
import io
import json
import re
import runpy
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import VALIDATORS, WORK_DIR, EdgeConnectError, client_for, load_yaml, toolkit_version, utc_stamp, write_json  # noqa: E402

PROG = "edgeconnect-preconfig"
STATES = {1: "yes", 2: "NO", 3: "unsupported version", 4: "yes (out of sync)", 5: "yes (syncing)"}  # /appliance `state`
NOT_STAGES = {"cli", "common", "decisions", "mappers", "variables"}  # libraries, not commands
ADDRESS_REASONS = ("this site's own interface address", "inside one of this site's own subnets")


class Fail(Exception):
    """A user-facing error: printed as one line, exit code 2."""


def table(header: List[str], rows: List[List[Any]]) -> str:
    """A bordered table, the setup.sh style."""
    widths = [max(len(str(c)) for c in col) for col in zip(header, *rows)]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    fmt = lambda cells: "| " + " | ".join(str(c).ljust(w) for c, w in zip(cells, widths)) + " |"  # noqa: E731
    return "\n".join([line, fmt(header), line] + [fmt(r) for r in rows] + [line])


def cell(value: Any) -> str:
    """A value as a CSV cell: true/false, blank for none."""
    return str(value).lower() if isinstance(value, bool) else "" if value is None else str(value)


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(text)).strip("_") or "appliance"


def check_validator(nick: str) -> None:
    if nick.lower() not in VALIDATORS:
        raise Fail("{} is not a validator Orchestrator: /validate runs only on the ones listed in EC_VALIDATORS in .env "
                   "(now: {}). Add it there if /validate may run on it, or pass --validate-on <lab profile>.".format(nick, ", ".join(sorted(VALIDATORS))))


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
    """Run a stage function with its stdout captured; returns (result, text)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        result = fn(*args)
    return result, buffer.getvalue()


# ---------------------------------------------------------------- appliances

def cmd_appliances(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog=PROG + " appliances", description="Test an Orchestrator profile from .env and list its appliances (GET only).")
    p.add_argument("--orchestrator", required=True, help=".env profile nickname")
    p.add_argument("--search", help="show only appliances whose hostname, nePk, model or site contains this text")
    a = p.parse_args(argv)
    client = client_for(a.orchestrator)
    versions = client.get("/gms/versions")
    release = versions.get("current") if isinstance(versions, dict) else "?"
    validates = "yes" if a.orchestrator.lower() in VALIDATORS else "no (not in EC_VALIDATORS)"
    print("Orchestrator {}: {} | release {} | login OK | /validate allowed: {}".format(a.orchestrator, client.config.base_url, release, validates))
    listing = client.get("/appliance")
    rows = []
    for item in listing if isinstance(listing, list) else []:
        row = [item.get("hostName") or "", item.get("nePk") or item.get("id") or "", item.get("model") or "", item.get("softwareVersion") or "",
               item.get("site") or "", STATES.get(item.get("state"), "unknown")]
        if not a.search or any(a.search.lower() in str(c).lower() for c in row[:5]):
            rows.append(row)
    rows.sort(key=lambda r: str(r[0]).lower())
    print(table(["Hostname", "nePk", "Model", "ECOS", "Site", "Reachable"], rows) if rows else "no appliances{}".format(" match" if a.search else ""))
    print("{} appliance(s), {} reachable".format(len(rows), sum(1 for r in rows if r[5].startswith("yes"))))
    if client.write_requests:
        raise Fail("write counter is {}; appliances is read-only".format(client.write_requests))
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


def cmd_build(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog=PROG + " build", description="Read one appliance (GET only), convert it, and keep the result only if Orchestrator /validate accepts it.")
    p.add_argument("--orchestrator", required=True, help=".env profile nickname to read the appliance from")
    p.add_argument("--appliance", required=True, help="hostname or nePk (e.g. 12.NE)")
    p.add_argument("--out", type=Path, default=Path.cwd() / "preconfig-output", help="parent folder (default ./preconfig-output)")
    p.add_argument("--validate-on", metavar="NICK", help="validate on this Orchestrator instead of the source one")
    a = p.parse_args(argv)
    validator = a.validate_on or a.orchestrator
    check_validator(validator)
    from capture import Stop, capture
    print("reading {} from {} (GET only) ...".format(a.appliance, a.orchestrator), flush=True)
    try:
        cap = capture(a.orchestrator, a.appliance, "reference", 0.2, WORK_DIR / "reference" / a.orchestrator)
    except Stop as stop:
        raise Fail(str(stop))
    run = json.loads((cap / "run.json").read_text(encoding="utf-8"))
    host = run["appliance"].get("hostName") or run["appliance"]["nePk"]
    release = run["release"] if validator == a.orchestrator else client_for(validator).get("/gms/versions").get("current")
    site = safe_name(host).lower()
    folder = a.out / safe_name(host)
    staging = a.out / ".building-{}".format(safe_name(host))
    shutil.rmtree(staging, ignore_errors=True)
    print("converting for Orchestrator release {} ...".format(release), flush=True)
    try:
        text = translate_into(staging, cap, site, release, [])
        print("checking and validating on {} ...".format(validator), flush=True)
        problems = checked(staging, site, validator, validator != a.orchestrator)
        if problems:
            raise Fail("not saved, the result did not pass:\n  " + "\n  ".join(problems))
        info = {"orchestrator": a.orchestrator, "validator": validator, "appliance": run["appliance"], "capture": str(cap),
                "target_release": release, "site_type": site, "built": utc_stamp(), "toolkit": toolkit_version()}
        write_json(staging / "build.json", info)
        a.out.mkdir(parents=True, exist_ok=True)
        keep_previous(folder)
        staging.rename(folder)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    verdict = next((l.split(":", 1)[1].strip() for l in text.splitlines() if l.startswith("verdict:")), "")
    print("\nSaved {}  (/validate on {}: accepted)".format(folder, validator))
    print("  reference.yaml      the appliance as preconfig YAML")
    print("  {:<20}the template".format(site + ".jinja2"))
    print("  example.csv         one row: this appliance's own values")
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
            r["locked"] = r["path"] == "applianceInfo.hostname" or r["reason"].startswith("secret")
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

    def ranked(self, count: int) -> List[Dict[str, str]]:
        hits = sorted((r for r in self.free if r["pct"] is not None), key=lambda r: (-r["pct"], -r["support"], r["n"]))
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
        return ["        {:<52} follows #{}".format(f["label"][:52], r["n"]) for f in self.rows if f["follows"] is r]

    def show(self, rows: List[Dict[str, str]], title: str) -> List[str]:
        out = ["  -- {} {}".format(title, "-" * max(0, 60 - len(title)))]
        for r in rows:
            out.append(self.line(r))
            out.extend(self.follower_lines(r))
        return out

    def header(self) -> List[str]:
        locked = [r for r in self.rows if r["locked"]]
        return ["  {:>4}  {:<52} {:<30} {:>5}  {}".format("#", "Setting", "Value now", "Diff", "Choice")] + self.show(locked, "Always per site (locked)")

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

    def sheet(self, reference: Dict[str, Any]) -> List[Dict[str, str]]:
        """The rows as variables.csv: chosen rows VARIABLE with a column name, followers as their primary."""
        from variables import RESERVED, column_name
        used = {r["column"] for r in self.rows if r["column"]} | RESERVED
        out = []
        for r in self.rows:
            row = {k: r[k] for k in ("decision", "column", "linked_to", "value", "path", "reason")}
            if r["follows"] is not None:
                row["decision"] = "variable" if self.on.get(r["follows"]["path"]) else "fixed"
            elif not r["locked"]:
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

    def columns(self) -> List[str]:
        names = []
        for r in self.rows:
            if (r["locked"] or (r["follows"] is None and self.on.get(r["path"]))) and r["column"] not in names:
                names.append(r["column"] or r["label"])
        return names


def interact(picker: Picker, mode: str, read: Callable[[str], str]) -> bool:
    """The tick list. Returns False when the user quits."""
    view = picker.top_view(int(mode[3:])) if mode.startswith("top") else picker.section_list()
    print("\n".join(view))
    help_line = "Type numbers to flip (e.g. 4 12), s1.. to open a section, a for all sections, t for the Top list, a word to search, Enter when done, q to quit."
    print(help_line)
    while True:
        answer = read("> ").strip()
        if answer == "":
            return True
        if answer.lower() == "q":
            return False
        if answer.lower() == "a":
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
        translate_into(staging, Path(info["capture"]), name, info["target_release"], ["--variables", str(staging / "variables.csv")])
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


def finish(picker: Picker, reference: Dict[str, Any], default_name: str, name: Optional[str], yes: bool, read: Callable[[str], str]) -> Optional[str]:
    cols = picker.columns()
    print("\n{} columns: {}".format(len(cols), ", ".join(cols)))
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
    p.add_argument("--pick", choices=("top10", "top20", "all"), help="start from the Top 10 or Top 20 (all: browse every section); with --yes no questions are asked")
    p.add_argument("--set", help="comma-separated column names or paths to make per site as well")
    p.add_argument("--name", help="site type name (default: the appliance's)")
    p.add_argument("--yes", action="store_true", help="accept the choice without the tick list or questions")
    a = p.parse_args(argv)
    info = load_build(a.folder)
    base = a.folder if (a.folder / "build.json").is_file() else a.folder.parent
    from variables import load_ranking
    reference = load_yaml(base / "reference.yaml")
    picker = Picker(reference, read_sheet_rows(base / "variables.csv"), load_ranking())
    mode = a.pick
    if not mode:
        if a.yes:
            raise Fail("--yes needs --pick top10 or top20")
        print("Hostname, serial number and secrets are always per site.\nHow do you want to pick the rest?\n"
              "  1) Top 10 - the settings most often different between sites (start here)\n  2) Top 20\n  3) All settings, by section")
        mode = {"1": "top10", "2": "top20", "3": "all", "": "top10"}.get(read("> ").strip())
        if not mode:
            raise Fail("answer 1, 2 or 3")
    picker.start_top(20 if mode == "top20" else 10)
    if a.set:
        unknown = picker.set_by_name([s.strip() for s in a.set.split(",") if s.strip()])
        if unknown:
            raise Fail("--set: no setting named {}".format(", ".join(unknown)))
    if not a.yes and not interact(picker, mode, read):
        print("nothing written")
        return 1
    name = finish(picker, reference, info["site_type"], a.name, a.yes, read)
    if not name:
        return 1
    target = build_site(a.folder, info, name, picker.sheet(reference))
    ensure_sites_csv(target)
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
    base = a.folder if (a.folder / "build.json").is_file() else a.folder.parent
    from capture import Stop, capture
    from translate import Capture, build, gated_paths, prune
    from variables import classify, sheet_items, write_sheet
    caps = []
    for site in a.sites:
        print("reading {} from {} (GET only) ...".format(site, info["orchestrator"]), flush=True)
        try:
            caps.append(capture(info["orchestrator"], site, "sample", 0.2, WORK_DIR / "samples" / info["orchestrator"]))
        except Stop as stop:
            raise Fail(str(stop))
    gated = gated_paths(info["target_release"])
    others = [prune(build(Capture(c), []), gated, []) for c in caps]
    reference = load_yaml(base / "reference.yaml")
    from generate import Builder
    probe = Builder()
    probe.find_secrets(reference)
    rows = [{k: cell(v) for k, v in r.items()} for r in classify(reference, probe.secret_paths, None, others)]
    names = [o["applianceInfo"]["hostname"] for o in others]
    seen = [dict(sheet_items(o)) for o in others]
    picker = Picker(reference, rows, {}, seen)
    differ = [r for r in picker.free if picker.on[r["path"]]]
    same = sum(1 for r in rows if r["reason"] == "same at every site you gave")
    only = sum(1 for r in rows if r["flag"] == "true" and r["reason"].startswith("only "))
    print("\n{} settings are per site at {} and {}:".format(len(differ), info["appliance"].get("hostName"), ", ".join(names)))
    print("  {:>4}  {:<52} {:<30} {:>5}  {}   {}".format("#", "Setting", info["appliance"].get("hostName"), "", "Choice", "   ".join("| " + n for n in names)))
    print("\n".join(picker.show(differ, "Different between your sites (pre-ticked)")))
    print("{} settings are the same at every site and stay fixed.".format(same))
    if only:
        print("{} settings exist only at some of the sites and are left fixed.".format(only))
    layout = [r for r in rows if re.search(r"(?i)interfacename$", r["path"]) and r["decision"] == "variable"]
    if layout or only:
        print("Note: the sites' interface layouts differ ({}). Settings are matched by position, so compare is reliable only for sites "
              "of the same design; for different designs build one template per design.".format(
                  ", ".join(sorted({r["value"] for r in layout})[:4]) or "{} settings missing at some sites".format(only)))
    if not a.yes:
        answer = read("Build the template with these settings per site? [Y/n/edit] ").strip().lower()
        if answer in ("n", "no"):
            print("nothing written")
            return 1
        if answer in ("e", "edit") and not interact(picker, "all", read):
            print("nothing written")
            return 1
    name = finish(picker, reference, info["site_type"], a.name, a.yes, read)
    if not name:
        return 1
    rows_for = [{"hostname": n, "items": s} for n, s in zip(names, seen)]
    target = build_site(a.folder, info, name, picker.sheet(reference), rows_for)
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

VERBS: Dict[str, Callable[[List[str]], int]] = {"appliances": cmd_appliances, "build": cmd_build, "variables": cmd_variables,
                                                "compare": cmd_compare, "validate": cmd_validate, "upload": cmd_upload}


def stages() -> List[str]:
    return sorted(p.stem.replace("_", "-") for p in HERE.glob("*.py") if p.stem not in NOT_STAGES)


def run_stage(name: str, argv: List[str]) -> int:
    script = HERE / "{}.py".format(name.replace("-", "_"))
    if not re.fullmatch(r"[a-z][a-z_-]*", name) or name.replace("-", "_") in NOT_STAGES or not script.is_file():
        print("unknown command {!r}; run: {} --help".format(name, PROG), file=sys.stderr)
        return 2
    sys.argv = ["{} {}".format(PROG, name)] + argv
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as done:
        return done.code if isinstance(done.code, int) else (0 if done.code is None else 1)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip() + "\n\nstages: " + ", ".join(stages()))
        return 0 if argv else 2
    if argv[0] in ("-V", "--version", "version"):
        print(toolkit_version())
        return 0
    verb, rest = argv[0], argv[1:]
    if verb in ("validate", "upload") and rest and rest[0].startswith("--") and rest[0] not in ("--help", "-h"):
        return run_stage(verb, rest)  # the stage form: validate --orchestrator X files..., upload --site-dir ...
    if verb not in VERBS:
        return run_stage(verb, rest)
    try:
        return VERBS[verb](rest)
    except Fail as fail:
        print("error: {}".format(fail), file=sys.stderr)
        return 2
    except EdgeConnectError as error:
        print("error: {} ({})".format(error, type(error).__name__), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nstopped; nothing more written", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

"""Python API over the CLI commands, for the GUI (docs/gui/BUILD_PLAN.md step 3), as the automation GUI's engine.

Each call builds a normal command line and runs it through cli.main in this process, in the calling thread, with its own
RunContext (source "gui"), its own printed text and an empty stdin, and returns a Result: issues, per-row outcomes in the
automation words, counts, the files written, the text with secret values replaced, and what approving the upload takes.
Uploads are gated here before the CLI runs: <nickname>_gui_preconfig_uploads=on in the run's .env (D6, D20), and an
approver from approve() for --approve, without which run_batch would upload unapproved.
Import it after inserting toolkit/scripts and toolkit/gui on sys.path, as the tests do.
"""
import argparse
import contextlib
import contextvars
import csv
import io
import json
import os
import re
import sys
import threading
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

import cli
import common
import upload as upload_stage
from approval import ApprovalError, Gate, GuiApprovals, upload_gates

# The recorder's row and file statuses (render.py, upload.py) in the run log's words (BUILD_PLAN "Engineering rules").
OUTCOMES = {"ok": "VALID", "failed": "INVALID",  # render rows
            "invalid": "INVALID", "conflict": "SKIPPED", "ready": "PLANNED", "uploaded": "CREATED", "upload-failed": "FAILED",  # upload rows
            "passed": "VALID", "validate failed": "INVALID", "refused": "INVALID", "skipped": "SKIPPED"}  # --yaml files: "refused: why", "skipped: why"
NOT_APPROVED = "NOT APPROVED, nothing uploaded: "  # upload.main's line when the run's approver refused the batch


@dataclass
class Result:
    exit_code: int
    status: str                      # done, failed, or rejected (the upload was refused: by the engine's gates or its approver)
    issues: List[Dict[str, Any]]     # row, column, value, message, fix
    # fix: the CLI gives no separate fix text (it is in the message), so it is empty; kept to match automation's issues
    rows: List[Dict[str, Any]]       # per row or --yaml file: row, item, status (the CLI's), outcome (VALID...), reason, file
    counts: Dict[str, int]           # rows per outcome
    files: List[str]                 # the YAML files this run wrote
    text: str                        # what the command printed, stdout and stderr in order, secret values replaced
    approval: Optional[Dict[str, Any]]  # {gates, fingerprint, orchestrator} for a CSV upload check that has rows to upload
    outputs: List[Dict[str, Any]] = field(default_factory=list)  # the recorder's results, secret values replaced


def approve(approvals: GuiApprovals, request: Mapping[str, Any], answers: Sequence[str], typed_orchestrator: str = "") -> Callable[[str], Dict[str, Any]]:
    """Check the typed answers against a Result.approval request and return the approver of the upload run for that exact batch."""
    gates = [Gate(**gate) for gate in request["gates"]]
    token = approvals.issue(gates, answers, request["fingerprint"], request["orchestrator"])
    redeem = approvals.approver(token, request["orchestrator"], tuple(answers))
    if not typed_orchestrator:
        return redeem
    return lambda fingerprint: dict(redeem(fingerprint), typed_orchestrator=typed_orchestrator)


# ---------------------------------------------------------------- one run

_IN_RUN: "contextvars.ContextVar[bool]" = contextvars.ContextVar("edgeconnect_preconfig_engine_run", default=False)
_STDIN = threading.Lock()


class _Stdin:
    """Stands in for sys.stdin: inside an engine run every read is at end of file, so a prompt ends the command at once instead
    of waiting; other threads read the stream it replaced (one swap of sys.stdin per run would leak into parallel runs)."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream

    def __getattr__(self, name: str) -> Any:
        return getattr(io.StringIO() if _IN_RUN.get() else self.stream, name)


@contextlib.contextmanager
def _no_stdin() -> Iterator[None]:
    with _STDIN:
        if not isinstance(sys.stdin, _Stdin):
            sys.stdin = _Stdin(sys.stdin)
    token = _IN_RUN.set(True)
    try:
        yield
    finally:
        _IN_RUN.reset(token)


class _LineError(Exception):
    pass


class _Line(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # raise instead of printing usage and exiting
        raise _LineError(message)


def _line(argv: Sequence[str]) -> argparse.Namespace:
    """The options the gates and the secret scrub read, parsed as argparse reads them (abbreviations included, so --appr counts)."""
    parser = _Line(add_help=False)
    for option in ("--orchestrator", "--csv", "--columns", "--site-dir", "--template", "--out"):
        parser.add_argument(option)
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--yaml", nargs="*")
    line, line.rest = parser.parse_known_args(list(argv[1:]))
    return line


def _refusal(argv: Sequence[str], approver: Optional[Callable[..., Any]]) -> Optional[str]:
    """Why the engine does not run this command (nothing is sent), or None. Only `upload` reaches upload.main and its POSTs."""
    try:
        line = _line(argv) if argv else None
    except _LineError as error:
        return "the GUI could not read this command line ({})".format(error)
    if line is None or not _built_here(argv, line):  # every other verb or stage reads inputs the secret scrub does not know
        return ("the GUI runs only the command lines it builds (list, preconfigs [--show], build, variables, compare, validate FOLDER --csv, upload FOLDER --csv, "
                "upload --template --csv, render --template --csv, upload or render --site-dir --csv --out, validate --yaml, upload --yaml); for existing YAML files use "
                "validate --yaml or upload --yaml, which parse each file first")
    if argv[0] != "upload":
        return None
    if line.approve and approver is None:
        return "an upload with --approve needs the GUI's approval (the nickname typed in the approve dialog)"
    if not line.orchestrator:
        return "a GUI upload names its Orchestrator with --orchestrator"
    return uploads_off(line.orchestrator, common.env_map())  # also gates the upload's own /validate there (D20)


def uploads_off(orchestrator: str, env: Mapping[str, str]) -> Optional[str]:
    """Why GUI uploads to this Orchestrator are refused (its <nickname>_gui_preconfig_uploads is not on, D6), or None."""
    key = "{}_gui_preconfig_uploads".format(orchestrator.strip().lower().replace("-", "_"))
    if env.get(key, "").strip().lower() != "on":
        return "GUI uploads are off for {0}: set {1}=on in .env to allow them".format(orchestrator, key)
    return None


def _built_here(argv: Sequence[str], line: argparse.Namespace) -> bool:
    """An allowlist: the shapes of the helpers below."""
    verb, stage = argv[0], len(argv) > 1 and argv[1].startswith("-")  # cli.main: validate/upload with an option first run the stage
    if verb in ("list", "build", "variables"):
        return True
    if verb == "compare":
        return _compare_shape(argv[1:])
    if verb == "preconfigs":  # exactly what preconfigs() builds: GET only, and the CLI redacts what it prints
        return argv[1:2] == ["--orchestrator"] and (len(argv) == 3 or (len(argv) == 5 and argv[3] == "--show"))
    # a saved site folder with --out: what it writes goes to the run's own folder, never into the build folder
    site_out = line.site_dir is not None and line.out is not None
    if verb == "render":
        return (line.template is not None or site_out) and line.csv is not None
    if verb in ("validate", "upload") and not stage:  # FOLDER --csv
        return line.csv is not None
    if verb in ("validate", "upload"):  # the stage only with --yaml (validate's own file arguments go out unparsed) or upload --template/--site-dir --csv
        return line.yaml is not None or (verb == "upload" and (line.template is not None or site_out) and line.csv is not None)
    return False


def _compare_shape(rest: Sequence[str]) -> bool:
    """Exactly what compare() builds: FOLDER SITE{1,4} [--name N] [--set S] [--fix F] [--captures DIR per site] and --dry-run or
    --yes, so it never asks (a site never starts with -; argparse would also take --cap or --set=... as these flags)."""
    first = next((i for i, item in enumerate(rest) if item.startswith("-")), len(rest))
    sites, tail = len(rest[:first]) - 1, list(rest[first:])
    if not 1 <= sites <= 4:
        return False
    for flag in ("--name", "--set", "--fix"):
        if tail[:1] == [flag]:
            if len(tail) < 2 or tail[1].startswith("-"):
                return False
            tail = tail[2:]
    if tail[:1] == ["--captures"]:
        if len(tail) < 1 + sites or any(item.startswith("-") for item in tail[1:1 + sites]):
            return False
        tail = tail[1 + sites:]
    return tail in (["--dry-run"], ["--yes"])


def _refuse(fingerprint: str) -> None:
    raise ApprovalError("no approval was given for this upload")


def _main(argv: List[str]) -> int:
    try:
        return cli.main(argv)
    except SystemExit as done:  # a verb's argparse error: its usage is in the text
        return done.code if isinstance(done.code, int) else (0 if done.code is None else 1)
    except Exception as error:  # what the CLI would end on with a traceback: the GUI shows it as the run's last line
        print("error: {} ({})".format(error, type(error).__name__), file=sys.stderr)
        return 1


def run(argv: Sequence[str], approver: Optional[Callable[[str], Any]] = None, on_line: Optional[Callable[[str], None]] = None,
        **context: Any) -> Result:
    """Run one CLI command line in this thread and return its Result. context: RunContext fields (dotenv, env, work_dir, out_dir...).
    on_line gets each printed line as it comes, secret values replaced as in Result.text."""
    argv = [str(a) for a in argv]
    # as automation's _refuse: without a GUI approval run_batch is refused, never left to upload unapproved, even past the gates
    run_context = common.RunContext(recorder=common.Recorder(), approver=approver or _refuse, source="gui", **context)
    found = _secret_forms(argv)  # read before the run, from the input files the command line names
    held = on_line is not None and _held(argv, found)
    out = _Lines(_scrub(found), on_line) if on_line is not None and not held else io.StringIO()
    with common.running(run_context), common.captured_output(out), _no_stdin():
        refusal = _refusal(argv, approver)
        if refusal:
            print("refused, nothing sent: {}".format(refusal), file=sys.stderr)
        code = 2 if refusal else _main(argv)
    text, outputs = out.getvalue(), run_context.recorder.results
    for r in outputs:  # the YAML parser's message quotes the text it stopped at, which may be a secret no scan can find
        if r["kind"] == "file" and r["status"] == "refused: not a preconfig YAML" and re.match(r"\w+: ", r["detail"]):
            withheld = r["detail"].split(":")[0] + ": the parser's message is not shown, as it may quote a secret"
            text, r["detail"] = text.replace(r["detail"], withheld), withheld
    text, outputs = _each(text, _no_snippet), _each(outputs, _no_snippet)
    scrub = _scrub(found)
    if isinstance(out, _Lines):
        out.end()
    elif held:
        for line in scrub(text).splitlines():
            on_line(line)
    rows = [_row(r) for r in outputs if r["kind"] in ("row", "file")]
    status = "done" if code == 0 else "rejected" if refusal or (code == 2 and any(l.startswith(NOT_APPROVED) for l in text.splitlines())) else "failed"
    return Result(
        exit_code=code,
        status=status,
        issues=scrub([{"row": p["row"], "column": p["column"] or "", "value": "" if p["value"] is None else p["value"], "message": p["message"], "fix": p.get("fix", "")}
                      for r in outputs if r["kind"] == "row" for p in r["problems"]]),
        rows=scrub(rows),
        counts=dict(Counter(r["outcome"] for r in rows)),
        files=[r["file"] for r in outputs if r["kind"] == "row" and r["file"]],
        text=scrub(text),
        approval=None if refusal else _approval(argv, outputs),
        outputs=scrub(outputs),
    )


def _row(result: Mapping[str, Any]) -> Dict[str, Any]:
    status = result["status"]
    outcome = OUTCOMES.get(status) or OUTCOMES.get(status.split(":")[0], status)
    if result["kind"] == "file":
        return {"row": None, "item": result["name"], "status": status, "outcome": outcome, "reason": result["detail"], "file": result["file"]}
    return {"row": result["index"], "item": result["preconfig_name"], "status": status, "outcome": outcome,
            "reason": "; ".join(p["message"] for p in result["problems"]), "file": result["file"]}


def _approval(argv: Sequence[str], outputs: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """What approving takes: only a CSV upload check (no --approve) whose batch has rows to upload records a fingerprint."""
    if argv[0] != "upload":
        return None
    line = _line(argv)
    fingerprints = [r["value"] for r in outputs if r["kind"] == "fingerprint"]
    if line.yaml is not None or line.approve or not fingerprints:
        return None
    return {"gates": [asdict(gate) for gate in upload_gates(line.orchestrator)], "fingerprint": fingerprints[-1], "orchestrator": line.orchestrator}


# ---------------------------------------------------------------- secrets in text (BUILD_PLAN "Engineering rules")

def hide_secrets(reference: Any, text: str) -> str:
    """text (a build's reference.yaml or summary.md) with every secret of the YAML file `reference`, and every <SECRET:...>
    placeholder capture wrote, shown as <SECRET> (D13), for the GUI's read-only viewers.
    shortcut: a secret spanning lines becomes one line and moves the lines after it; capture redacts PEM and hashes first."""
    return re.sub(r"<SECRET:[^>\n]*>", "<SECRET>", _scrub(_secret_forms(["validate", "--yaml", _abs(reference)]))(text))


def _secret_forms(argv: Sequence[str]) -> Dict[str, str]:
    """Every literal secret value of the run's inputs, in each form the CLI can print it in, with its placeholder: a CSV secret
    column's cells (from columns.json, or secret-like headers without one) by <SECRET:column>, a secret value of a YAML file
    the argv names by its <SECRET:path>."""
    try:
        line = _line(argv) if argv else None
    except _LineError:
        line = None
    found: Dict[str, str] = {}
    for value, placeholder in (_csv_secrets(argv, line) + _yaml_secrets(line)) if line else []:
        for form in _forms(value):
            found.setdefault(form, placeholder)
    return found


def _scrub(found: Mapping[str, str]) -> Callable[[Any], Any]:
    """Replace each secret form in strings at any depth, and its start where the CLI cut a message short."""
    if not found:
        return lambda value: value
    # The CLI cuts messages ([:1000] /validate errors, [:300], [:200]); a cut piece ends a line or a "; " list item (PyYAML's
    # snippets, cut from either side, are withheld by _no_snippet before this runs).
    # shortcut: a cut that keeps 3 characters or fewer of a secret shows them; scrub suffixes too if a message is ever cut from its start.
    cut = {form[:k]: placeholder for form, placeholder in found.items() for k in range(4, len(form))}

    def alternatives(forms: Mapping[str, str]) -> str:
        return "|".join(re.escape(form) for form in sorted(forms, key=len, reverse=True))
    pattern = re.compile("(?P<full>{})".format(alternatives(found)) + (r"|(?P<cut>{})(?=; |\r?$)".format(alternatives(cut)) if cut else ""), re.M)

    # one pass: a placeholder is never rescanned
    return lambda value: _each(value, lambda text: pattern.sub(lambda m: found[m["full"]] if m["full"] is not None else cut[m["cut"]], text))


def _each(value: Any, change: Callable[[str], str]) -> Any:
    """value with change applied to every string in it, at any depth."""
    if isinstance(value, str):
        return change(value)
    if isinstance(value, dict):
        return {k: _each(v, change) for k, v in value.items()}
    if isinstance(value, list):
        return [_each(v, change) for v in value]
    return value


# PyYAML's mark and the source snippet under it: the quoted line (cut to about 75 characters, from either side) and its ^
_SNIPPET = re.compile(r'(in "[^"\n]*", line \d+, column \d+):\n[^\n]*(?:\n[ \t]*\^)?')


def _no_snippet(text: str) -> str:
    """Withhold the source a YAML error quotes: a cut piece of a secret in it matches no form of the secret."""
    return _SNIPPET.sub(r"\1 (the source snippet is withheld: it may hold a secret)", text)


def _forms(value: str) -> Set[str]:
    """A value as the CLI path can print it: as is or trimmed; JSON-escaped (ASCII or not) or as Jinja's tojson writes it in a
    template (htmlsafe_json_dumps also escapes < > & '); each of those as repr() quotes it."""
    forms: Set[str] = set()
    for plain in {value, value.strip()}:
        dumped = json.dumps(plain)[1:-1]
        forms |= {plain, dumped, json.dumps(plain, ensure_ascii=False)[1:-1],
                  dumped.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("'", "\\u0027")}
    forms |= {repr(form)[1:-1] for form in forms}
    return {form for form in forms if form.strip()}


def _csv_secrets(argv: Sequence[str], line: argparse.Namespace) -> List[Tuple[str, str]]:
    if not line.csv:
        return []
    folder = argv[1] if argv[0] in ("validate", "upload") and len(argv) > 1 and not argv[1].startswith("-") else None
    columns_path = Path(line.columns) if line.columns else Path(line.site_dir or folder) / "columns.json" if (line.site_dir or folder) else None
    try:
        with open(line.csv, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        meta = json.loads(columns_path.read_text(encoding="utf-8")) if columns_path and columns_path.is_file() else {}
    except (OSError, UnicodeError, ValueError, csv.Error):  # the CLI reports an unreadable input itself
        return []
    columns = meta.get("columns") if isinstance(meta, dict) else None
    headers = list(rows[0]) if rows else []
    secret = {n for n, m in columns.items() if m.get("secret")} if columns else {h for h in headers if h and _secret_key(h)}
    return [(v, "<SECRET:{}>".format(n)) for row in rows for n, v in row.items() if n in secret and isinstance(v, str) and v]


def _secret_key(name: str) -> bool:
    return bool(common.redact_secrets({name: "x"})[1])


_KEY_LINE = re.compile(r"^[ \t]*(?:-[ \t]+)?[\"']?([\w-]+)[\"']?[ \t]*:[ \t]+(.*?)[ \t]*$", re.M)
_NOT_A_VALUE = {"", "|", ">", "|-", ">-", "|+", ">+", "~", "null", "true", "false"}


def _yaml_secrets(line: argparse.Namespace) -> List[Tuple[str, str]]:
    """Secret values of every YAML file the argv names (--yaml and the stage's own file arguments): those of a parsed file,
    numbers included, and of every secret-looking `key: value` line, so a file that does not parse is covered too."""
    found: List[Tuple[str, str]] = []
    for path in upload_stage.yaml_files([Path(p) for p in (line.yaml or []) + line.rest if not p.startswith("-") and os.path.exists(p)]):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        try:  # first, so a parsed secret is named by its path
            doc = _strings(common.parse_yaml(text))
            found += list(_pairs(doc, common.redact_secrets(doc)[0]))
        except Exception:  # not YAML: the CLI refuses the file, and the lines below still find its secrets
            pass
        for key, raw in _KEY_LINE.findall(text):
            value = raw.split(" #")[0].strip()
            if _secret_key(key) and value.lower() not in _NOT_A_VALUE and "{{" not in value and not value.startswith("<SECRET:"):
                found += [(v, "<SECRET:{}>".format(key)) for v in {value, value.strip("\"'"), value.lstrip("*&").strip("\"'")} if v]
    return found


def _strings(value: Any) -> Any:
    """The parsed file with numbers as text, as redact_secrets replaces only strings (authKey: 98765432 is an int)."""
    if isinstance(value, dict):
        return {k: _strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strings(v) for v in value]
    return str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value


def _pairs(value: Any, redacted: Any) -> Iterator[Tuple[str, str]]:
    """(secret, placeholder) for each string redact_secrets replaced as a whole."""
    if isinstance(value, dict):
        for key in value:
            yield from _pairs(value[key], redacted[key])
    elif isinstance(value, list):
        for item, other in zip(value, redacted):
            yield from _pairs(item, other)
    elif isinstance(value, str) and value != redacted and re.fullmatch(r"<SECRET:[^>]*>", redacted):
        yield value, redacted


# ---------------------------------------------------------------- printed lines as they come (BUILD_PLAN step 5)

def _held(argv: Sequence[str], found: Mapping[str, str]) -> bool:
    """Lines that can be made safe only once the run has ended are handed over then: a --yaml run (the parser's message for a file
    that does not parse is withheld by its recorder result, made after its line) and a secret that spans lines."""
    try:
        line = _line(argv) if argv else None
    except _LineError:
        return True
    return line is None or line.yaml is not None or any("\n" in form or "\r" in form for form in found)


_MARK = re.compile(r'(in "[^"\n]*", line \d+, column \d+):$')
_CARET = re.compile(r"[ \t]*\^")


class _Lines(io.StringIO):
    """The run's capture, also handing each finished line to on_line: the PyYAML snippet withheld as _no_snippet does, then scrubbed."""

    def __init__(self, scrub: Callable[[str], str], on_line: Callable[[str], None]) -> None:
        super().__init__()
        self._scrub, self._on_line, self._open, self._after_mark = scrub, on_line, "", 0

    def write(self, text: str) -> int:
        written = super().write(text)
        *lines, self._open = (self._open + text).split("\n")
        for line in lines:
            self._line(line)
        return written

    def end(self) -> None:
        if self._open:
            self._line(self._open)
        self._open = ""

    def _line(self, line: str) -> None:
        if self._after_mark == 1:  # the source line PyYAML quotes
            self._after_mark = 2
            return
        if self._after_mark == 2:  # and its ^ under it
            self._after_mark = 0
            caret = _CARET.match(line)
            if caret:
                line = line[caret.end():]
                if not line:
                    return
        mark = _MARK.search(line)
        if mark:
            line, self._after_mark = line[:mark.start()] + mark.group(1) + " (the source snippet is withheld: it may hold a secret)", 1
        self._on_line(self._scrub(line))


# ---------------------------------------------------------------- one helper per action; paths are made absolute here

def _abs(path: Any) -> str:
    return os.path.abspath(str(path))


def _opt(flag: str, value: Any) -> List[str]:
    return [] if value is None else [flag, str(value)]


def list_appliances(orchestrator: str, search: Optional[str] = None, **context: Any) -> Result:
    """Each appliance list prints is an output {"kind": "appliance", hostname, nepk, model, ecos, site, reachable} (R8)."""
    return run(["list", "--orchestrator", orchestrator] + _opt("--search", search), **context)


def preconfigs(orchestrator: str, show: Optional[str] = None, **context: Any) -> Result:
    """The preconfigs on the Orchestrator (outputs of kind "preconfig", never their configData), or with show (a name or id) one
    as YAML with its secrets redacted by the CLI (kind "preconfig-yaml"). A YAML that does not parse ends failed, the CLI's
    one line naming where it broke, never its text. GET only."""
    return run(["preconfigs", "--orchestrator", orchestrator] + _opt("--show", show), **context)


def build(orchestrator: str, appliance: str, out: Any = None, validate_on: Optional[str] = None, target_release: Optional[str] = None,
          target_ecos: Optional[str] = None, **context: Any) -> Result:
    return run(["build", "--orchestrator", orchestrator, "--appliance", appliance] + _opt("--out", out and _abs(out)) + _opt("--validate-on", validate_on)
               + _opt("--target-release", target_release) + _opt("--target-ecos", target_ecos), **context)


def variables(folder: Any, pick: str, settings: Sequence[str] = (), name: Optional[str] = None, yes: bool = False,
              fixed: Sequence[str] = (), dry_run: bool = False, **context: Any) -> Result:
    """--pick is always given, so no tick list or question is shown (BUILD_PLAN: no hidden prompts); dry_run is the preview."""
    return run(["variables", _abs(folder), "--pick", pick] + _opt("--set", ",".join(settings) or None) + _opt("--fix", ",".join(fixed) or None)
               + _opt("--name", name) + (["--yes"] if yes else []) + (["--dry-run"] if dry_run else []), **context)


def validate(folder: Any, csv_path: Any, orchestrator: Optional[str] = None, **context: Any) -> Result:
    return run(["validate", _abs(folder), "--csv", _abs(csv_path)] + _opt("--orchestrator", orchestrator), **context)


def upload(csv_path: Any, orchestrator: str, folder: Any = None, template: Any = None, out: Any = None,
           approver: Optional[Callable[[str], Any]] = None, site_dir: Any = None, **context: Any) -> Result:
    """A CSV upload: a check without an approver (its Result.approval is what approving takes), uploaded with one from approve().
    folder: `upload FOLDER`, which writes into it; site_dir: the stage `upload --site-dir S --out OUT`, which writes into out."""
    if site_dir is not None:
        source = ["--site-dir", _abs(site_dir), "--out", _abs(out)]
    else:
        source = [_abs(folder)] if folder is not None else ["--template", _abs(template)] + _opt("--out", out and _abs(out))
    return run(["upload"] + source + ["--csv", _abs(csv_path), "--orchestrator", orchestrator] + (["--approve"] if approver else []), approver, **context)


def validate_yaml(paths: Sequence[Any], orchestrator: str, name: Optional[str] = None, serial: Optional[str] = None, **context: Any) -> Result:
    return run(["validate", "--yaml"] + [_abs(p) for p in paths] + ["--orchestrator", orchestrator] + _opt("--name", name) + _opt("--serial", serial), **context)


def upload_yaml(paths: Sequence[Any], orchestrator: str, name: Optional[str] = None, serial: Optional[str] = None, **context: Any) -> Result:
    """Uploads every file that passes: no --approve (decision 0017), but <nickname>_gui_preconfig_uploads=on is still needed."""
    return run(["upload", "--yaml"] + [_abs(p) for p in paths] + ["--orchestrator", orchestrator] + _opt("--name", name) + _opt("--serial", serial), **context)


def render(template: Any, csv_path: Any, out: Any, validate_on: Optional[str] = None, substitute: bool = False, **context: Any) -> Result:
    """The render stage: one YAML per row into out; with validate_on each row through /validate there (a validator only)."""
    return run(["render", "--template", _abs(template), "--csv", _abs(csv_path), "--out", _abs(out)] + _opt("--validate", validate_on)
               + (["--substitute"] if substitute else []), **context)


def render_site(site_dir: Any, csv_path: Any, out: Any, validate_on: Optional[str] = None, substitute: bool = False, **context: Any) -> Result:
    """`validate SITE --csv` (render --site-dir --validate, as cli.validate_rows runs it) with the rows written into out instead of SITE/rendered."""
    return run(["render", "--site-dir", _abs(site_dir), "--csv", _abs(csv_path), "--out", _abs(out)] + _opt("--validate", validate_on)
               + (["--substitute"] if substitute else []), **context)


def compare(folder: Any, sites: Sequence[str], name: Optional[str] = None, settings: Sequence[str] = (), fixed: Sequence[str] = (),
            captures: Sequence[Any] = (), dry_run: bool = False, **context: Any) -> Result:
    """Other sites of the same design (GET only) pick the per-site settings. captures: the sample captures an earlier compare of the
    same sites recorded, in site order, so no site is read again; dry_run is the preview, else it saves with --yes (no question)."""
    return run(["compare", _abs(folder)] + list(sites) + _opt("--name", name) + _opt("--set", ",".join(settings) or None)
               + _opt("--fix", ",".join(fixed) or None) + (["--captures"] + [_abs(c) for c in captures] if captures else [])
               + (["--dry-run"] if dry_run else ["--yes"]), **context)

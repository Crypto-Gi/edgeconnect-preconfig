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
    for option in ("--orchestrator", "--csv", "--columns", "--site-dir"):
        parser.add_argument(option)
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--yaml", nargs="*")
    line, line.rest = parser.parse_known_args(list(argv[1:]))
    return line


def _refusal(argv: Sequence[str], approver: Optional[Callable[..., Any]]) -> Optional[str]:
    """Why the engine does not run this upload (nothing is sent), or None. Only `upload` reaches upload.main and its POSTs."""
    if not argv or argv[0] != "upload":
        return None
    try:
        line = _line(argv)
    except _LineError as error:
        return "the GUI could not read this upload command line ({})".format(error)
    if line.approve and approver is None:
        return "an upload with --approve needs the GUI's approval (the nickname typed in the approve dialog)"
    if not line.orchestrator:
        return "a GUI upload names its Orchestrator with --orchestrator"
    key = "{}_gui_preconfig_uploads".format(line.orchestrator.strip().lower().replace("-", "_"))
    if common.env_map().get(key, "").strip().lower() != "on":  # also gates the upload's own /validate there (D20)
        return "GUI uploads are off for {0}: set {1}=on in .env to allow them".format(line.orchestrator, key)
    return None


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


def run(argv: Sequence[str], approver: Optional[Callable[[str], Any]] = None, **context: Any) -> Result:
    """Run one CLI command line in this thread and return its Result. context: RunContext fields (dotenv, env, work_dir, out_dir...)."""
    argv = [str(a) for a in argv]
    # as automation's _refuse: without a GUI approval run_batch is refused, never left to upload unapproved, even past the gates
    run_context = common.RunContext(recorder=common.Recorder(), approver=approver or _refuse, source="gui", **context)
    out = io.StringIO()
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
    scrub = _scrubber(argv)
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

def _scrubber(argv: Sequence[str]) -> Callable[[Any], Any]:
    """Replace every literal secret value of the run's inputs, in strings at any depth: a CSV secret column's cells (from
    columns.json, or secret-like headers without one) by <SECRET:column>, a secret value of a YAML file the argv names by its
    <SECRET:path>. Each in every form the CLI can print it in, and its start where the CLI cut a message short."""
    try:
        line = _line(argv) if argv else None
    except _LineError:
        line = None
    found: Dict[str, str] = {}
    for value, placeholder in (_csv_secrets(argv, line) + _yaml_secrets(line)) if line else []:
        for form in _forms(value):
            found.setdefault(form, placeholder)
    if not found:
        return lambda value: value
    # The CLI cuts messages ([:1000] /validate errors, [:300], [:200]); a cut piece ends a line, a "; " list item or a PyYAML snippet.
    # shortcut: a cut that keeps 3 characters or fewer of a secret shows them, as does a snippet that starts inside one; scrub
    # suffixes too if a message is ever cut from its start.
    cut = {form[:k]: placeholder for form, placeholder in found.items() for k in range(4, len(form))}
    def alternatives(forms: Mapping[str, str]) -> str:
        return "|".join(re.escape(form) for form in sorted(forms, key=len, reverse=True))
    pattern = re.compile("(?P<full>{})".format(alternatives(found)) + (r"|(?P<cut>{})(?=; | \.\.\.|\r?$)".format(alternatives(cut)) if cut else ""), re.M)

    def scrub(value: Any) -> Any:
        if isinstance(value, str):  # one pass: a placeholder is never rescanned
            return pattern.sub(lambda m: found[m["full"]] if m["full"] is not None else cut[m["cut"]], value)
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value
    return scrub


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


# ---------------------------------------------------------------- one helper per action; paths are made absolute here

def _abs(path: Any) -> str:
    return os.path.abspath(str(path))


def _opt(flag: str, value: Any) -> List[str]:
    return [] if value is None else [flag, str(value)]


def list_appliances(orchestrator: str, search: Optional[str] = None, **context: Any) -> Result:
    return run(["list", "--orchestrator", orchestrator] + _opt("--search", search), **context)


def build(orchestrator: str, appliance: str, out: Any = None, validate_on: Optional[str] = None, target_release: Optional[str] = None,
          target_ecos: Optional[str] = None, **context: Any) -> Result:
    return run(["build", "--orchestrator", orchestrator, "--appliance", appliance] + _opt("--out", out and _abs(out)) + _opt("--validate-on", validate_on)
               + _opt("--target-release", target_release) + _opt("--target-ecos", target_ecos), **context)


def variables(folder: Any, pick: str, settings: Sequence[str] = (), name: Optional[str] = None, yes: bool = False, **context: Any) -> Result:
    """--pick is always given, so no tick list or question is shown (BUILD_PLAN: no hidden prompts)."""
    return run(["variables", _abs(folder), "--pick", pick] + _opt("--set", ",".join(settings) or None) + _opt("--name", name) + (["--yes"] if yes else []), **context)


def validate(folder: Any, csv_path: Any, orchestrator: Optional[str] = None, **context: Any) -> Result:
    return run(["validate", _abs(folder), "--csv", _abs(csv_path)] + _opt("--orchestrator", orchestrator), **context)


def upload(csv_path: Any, orchestrator: str, folder: Any = None, template: Any = None, out: Any = None,
           approver: Optional[Callable[[str], Any]] = None, **context: Any) -> Result:
    """A CSV upload: a check without an approver (its Result.approval is what approving takes), uploaded with one from approve()."""
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


# shortcut: compare and preconfigs get their helpers with their CLI additions (R6: compare --dry-run/--set/--fix/--captures,
# R7: preconfigs --orchestrator N [--show NAME]), which are not merged yet; run(argv) already runs them as they are.

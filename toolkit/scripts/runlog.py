"""The run log (cli-requests R4): every command appends one JSON line to reports/preconfig.jsonl, in the record format of the
automation tools' report log, so their `report summarize` and both GUIs list preconfig runs too.

The folder is EDGECONNECT_REPORT_DIR (default reports/ in the current folder; an empty value turns the log off). The file is
created with mode 0600. Before a line is written, every secret CSV cell the run read is replaced by <SECRET:column> (also
quoted or escaped, as error text shows it), then secret-looking keys and key=value text are redacted as automation does.
"""
import contextlib
import dataclasses
import datetime
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import SECRET_HEADER, Recorder, RunContext, collecting_secrets, run_context, running, toolkit_version  # noqa: E402  guard-ok: imported after the sys.path insert

REPORT_DIR_ENV, DEFAULT_REPORT_DIR, LOG_NAME = "EDGECONNECT_REPORT_DIR", "reports", "preconfig.jsonl"

# the status words of the commands' rows, in automation's words
OUTCOMES = {"ok": "VALID", "written": "VALID", "passed": "VALID", "invalid": "INVALID", "failed": "INVALID", "validate failed": "INVALID",
            "conflict": "SKIPPED", "ready": "PLANNED", "uploaded": "CREATED", "upload-failed": "FAILED"}
EXIT_MEANING = {0: "completed", 1: "some rows failed, or the command stopped", 2: "refused or blocked; nothing was sent", 130: "interrupted by the operator"}
NEXT_STEP = {
    "SUCCESS": "Nothing to do.",
    "VALID": "Upload the CSV, or check it against Orchestrator first.",
    "PLANNED": "Review the rows, then run the same upload with --approve.",
    "INVALID": "Fix every row listed above, then rerun the same command.",
    "PARTIAL": "Review the failed rows above; the rows marked CREATED are on Orchestrator.",
    "BLOCKED": "Read the error above, fix it, then rerun the same command.",
    "REFUSED": "Nothing was uploaded. Check the rows again and approve the batch you see.",
    "INTERRUPTED": "The run was interrupted. If that happened during an upload, check Orchestrator's preconfigurations.",
    "ERROR": "Read the error above and rerun the same command.",
}
_SECRET_TEXT = re.compile(r"(?i)(authorization|x-auth-token|api[-_]?key|password|secret|cookie)\s*[:=]\s*([^\s,;]+)")
GRAM = 8  # a run of this many characters of a secret anywhere in the text is masked, as the whole secret is


FREE_TEXT = {"message", "reason", "detail", "blocking_issues", "text"}  # error text, which may quote a secret cut or shortened
CUT = re.compile(r"\.\.\. ?|\Z")  # where PyYAML's snippet (and a cut text) stops; a secret's start may sit just before it


class Masker:
    """Masks the run's secret cells: everywhere, each cell whole (raw, trimmed, repr- and JSON-escaped, 4+ characters); in free
    text (FREE_TEXT keys) also any run of GRAM or more of its characters, 4 to 7 of its first characters just before a cut, and
    cells shorter than 4. Error text shortens and cuts lines (PyYAML's snippet, /validate text cut at 1000); names and paths
    are left whole, so a site name that shares 8 characters with a key stays readable. Built once per log line: the cost is
    the length of the text, not rows x text."""

    def __init__(self, cells: Mapping[str, str]) -> None:
        self.names = sorted(set(cells.values()))
        forms: Dict[str, int] = {}
        for cell, column in cells.items():
            if cell.strip():
                for form in {cell, cell.strip(), repr(cell)[1:-1], json.dumps(cell)[1:-1]} - {""}:
                    forms.setdefault(form, self.names.index(column))
        self.forms = forms
        self.grams: Dict[str, int] = {}
        self.owners: Dict[str, List[str]] = {}  # gram -> the forms it is part of
        for form, n in forms.items():
            for i in range(len(form) - GRAM + 1):
                self.grams.setdefault(form[i:i + GRAM], n)
                self.owners.setdefault(form[i:i + GRAM], []).append(form)
        self.heads = {form[:k]: n for form, n in forms.items() for k in range(4, min(len(form), GRAM))}
        self.tiny = self.pattern(f for f in forms if len(f) < 4)
        self.short = self.pattern(f for f in forms if 4 <= len(f) < GRAM)

    @staticmethod
    def pattern(forms: Iterator[str]) -> Optional["re.Pattern[str]"]:
        found = sorted(forms, key=len, reverse=True)
        return re.compile("|".join(map(re.escape, found))) if found else None

    def text(self, value: str, free: bool) -> str:
        if not self.forms:
            return value
        marks: List[Tuple[int, int, int]] = []
        runs: List[List[Any]] = []  # [start, end, label, forms seen] of consecutive GRAM hits
        for i in range(len(value) - GRAM + 1):
            n = self.grams.get(value[i:i + GRAM])
            if n is not None:
                if runs and i <= runs[-1][1]:
                    runs[-1][1] = i + GRAM
                    runs[-1][3].update(self.owners[value[i:i + GRAM]])
                else:
                    runs.append([i, i + GRAM, n, set(self.owners[value[i:i + GRAM]])])
        for start, end, n, owners in runs:
            if free:
                marks.append((start, end, n))
            else:  # names and paths: only a whole form inside the run
                piece = value[start:end]
                for form in owners:
                    at = piece.find(form)
                    while at >= 0:
                        marks.append((start + at, start + at + len(form), self.forms[form]))
                        at = piece.find(form, at + 1)
        for regex in (self.short, self.tiny if free else None):
            if regex:
                marks += [(m.start(), m.end(), self.forms[m.group(0)]) for m in regex.finditer(value)]
        if free and self.heads:
            for cut in CUT.finditer(value):
                for k in range(min(cut.start(), GRAM), 3, -1):  # one more for a space before the cut
                    head = value[cut.start() - k:cut.start()].rstrip()
                    if head in self.heads:
                        marks.append((cut.start() - k, cut.start() - k + len(head), self.heads[head]))
                        break
        spans: List[List[int]] = []
        for start, end, n in sorted(marks):
            if spans and start <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], end)
            else:
                spans.append([start, end, n])
        for start, end, n in reversed(spans):  # first a mark the key=value pattern below cannot match
            value = value[:start] + "\0{}\0".format(n) + value[end:]
        return value

    def unmark(self, value: str) -> str:
        if not self.forms:
            return value
        return re.sub("\0(\\d+)\0", lambda match: "<SECRET:{}>".format(self.names[int(match.group(1))]), value)


def report_dir() -> Optional[Path]:
    value = os.environ.get(REPORT_DIR_ENV, DEFAULT_REPORT_DIR)
    return Path(value) if value.strip() else None


def redact(value: Any, masker: Masker, free: bool = False) -> Any:
    """automation's util.redact, after the run's secret cells are masked (<SECRET:column>)."""
    if isinstance(value, Mapping):
        return {str(k): ("<REDACTED>" if v not in (None, "") and not isinstance(v, (bool, int, float)) else v) if SECRET_HEADER.search(str(k))
                else redact(v, masker, free or str(k) in FREE_TEXT) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, masker, free) for item in value]
    if isinstance(value, str):
        return masker.unmark(_SECRET_TEXT.sub(lambda match: match.group(1) + "=<REDACTED>", masker.text(value, free)))
    return value


def option(argv: Sequence[str], name: str) -> Optional[str]:
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def rows_of(results: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for n, r in enumerate((r for r in results if r["kind"] == "file"), 1):
        status = str(r.get("status") or "")
        outcome = OUTCOMES.get(status) or ("SKIPPED" if status.startswith("skipped") else "INVALID")
        rows.append({"row": n, "item": str(r.get("name") or ""), "scope": Path(r["file"]).name if r.get("file") else "", "outcome": outcome, "reason": str(r.get("detail") or "")})
    for r in (r for r in results if r["kind"] == "row"):
        rows.append({"row": r.get("index", ""), "item": str(r.get("preconfig_name") or ""), "scope": Path(r["file"]).name if r.get("file") else "",
                     "outcome": OUTCOMES.get(r.get("status"), "UNKNOWN"), "reason": "; ".join(p["message"] for p in r.get("problems") or [])})
    return rows


def status_of(code: int, outcomes: set, refused: bool) -> str:
    if code == 130:
        return "INTERRUPTED"
    if refused:
        return "REFUSED"
    if code == 2:
        return "BLOCKED"
    if code:  # a FAILED row was sent: as in automation, a failed write is PARTIAL, never INVALID (nothing sent)
        return "PARTIAL" if outcomes & {"CREATED", "FAILED"} else "INVALID" if "INVALID" in outcomes else "ERROR"
    return "PLANNED" if "PLANNED" in outcomes else "VALID" if outcomes and outcomes <= {"VALID"} else "SUCCESS"


def written(status: str, workflow: str, outcomes: set) -> str:
    """automation's _written words: an upload that stopped or failed partway may have left preconfigs on Orchestrator."""
    if status == "PARTIAL":
        return "possibly; see the result details"
    if status in ("INTERRUPTED", "ERROR") and workflow == "preconfig upload":
        return "unknown; check Orchestrator"
    return "yes" if "CREATED" in outcomes else "no"


def entry(argv: Sequence[str], code: int, run_id: str, started: datetime.datetime, seconds: float, results: List[Dict[str, Any]], source: str,
          cells: Mapping[str, str]) -> Dict[str, Any]:
    """The log record: automation's headline fields, its summary (with the rows `report summarize` reads) and the results."""
    rows = rows_of(results)
    outcomes = {r["outcome"] for r in rows}
    status = status_of(code, outcomes, any(r["kind"] == "refused" for r in results))
    seen = [r for r in results if r["kind"] == "orchestrator"]
    builds = [r for r in results if r["kind"] == "build"]
    csv_path = option(argv, "--csv")
    csv_info: Dict[str, Any] = {}
    if csv_path and Path(csv_path).is_file():
        data = Path(csv_path).read_bytes()
        csv_info = {"path": csv_path, "sha256": hashlib.sha256(data).hexdigest(), "data_rows": max(len(data.decode("utf-8-sig", "replace").splitlines()) - 1, 0)}
    totals: Dict[str, int] = {}
    for r in rows:
        totals[r["outcome"]] = totals.get(r["outcome"], 0) + 1
    summary = {
        "run_id": run_id,
        "timestamp": started.isoformat(timespec="seconds"),
        "duration_seconds": round(seconds, 1),
        "tool_version": toolkit_version(),
        "orchestrator_release": next((b["release"] for b in reversed(builds) if b.get("release")), "not recorded"),
        "orchestrator_target": "",
        "orchestrator": option(argv, "--orchestrator") or (seen[-1]["name"] if seen else "not contacted"),
        "orchestrator_url": seen[-1]["url"] if seen else "not contacted",
        "workflow": "preconfig " + (argv[0] if argv else ""),
        "command": "edgeconnect-preconfig " + " ".join(argv),
        "source": source,
        "dry_run": False,
        "csv": csv_info,
        "status": status,
        "changes_written": written(status, "preconfig " + (argv[0] if argv else ""), outcomes),
        "exit_code": code,
        "exit_meaning": EXIT_MEANING.get(code, "unknown"),
        "counts": ["{} {}".format(n, outcome) for outcome, n in sorted(totals.items())],
        "row_outcomes": totals,
        "rows": rows,
        "blocking_issues": [str(r.get("reason") or "") for r in results if r["kind"] == "refused"],
        "warnings": [],
        "targets": [],
        "run_reference": "",
        "next_step": NEXT_STEP[status],
    }
    approvals = [r for r in results if r["kind"] == "approval"]
    if approvals:
        summary["approval"] = approvals[-1]["record"]
    headline = {k: summary[k] for k in ("status", "exit_code", "changes_written", "timestamp", "run_id", "workflow", "orchestrator", "orchestrator_url", "source")}
    files = sorted({str(r["file"]) for r in results if r.get("file")} | {str(b["folder"]) for b in builds if b.get("folder")})
    record = redact(dict(headline, summary=summary, outputs=results, files_written=files), Masker(cells))
    record["report_fingerprint"] = "sha256:" + hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
    return record


def append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(text)


@contextlib.contextmanager
def logged(argv: Sequence[str]) -> Iterator[Dict[str, int]]:
    """Run one command with a recorder (the run's own, or a new one) and log it at the end, also when it raises; set ["code"]."""
    run = run_context()
    recorder = run.recorder if run and run.recorder else Recorder()
    outcome, mine = {"code": 1}, len(recorder.results)  # code: an exception that escapes the command; mine: where this run's results start
    run_id, started, clock = "run-" + uuid.uuid4().hex[:12], datetime.datetime.now().astimezone(), time.monotonic()
    with running(dataclasses.replace(run or RunContext(), recorder=recorder)), collecting_secrets() as cells:
        try:
            yield outcome
        except KeyboardInterrupt:
            outcome["code"] = 130
            raise
        except SystemExit as done:  # argparse: --help, or a usage error; the code the process exits with, as run_stage maps it
            outcome["code"] = done.code if isinstance(done.code, int) else (0 if done.code is None else 1)
            raise
        finally:
            folder = report_dir()
            if folder is not None and not any(r["kind"] == "dry-run" for r in recorder.results[mine:]):  # a dry run writes no line (cli-requests R5)
                record = entry(list(argv), outcome["code"], run_id, started, time.monotonic() - clock, list(recorder.results), (run.source if run else "cli"), cells)
                try:
                    append(folder / LOG_NAME, json.dumps(record, ensure_ascii=False, default=str) + "\n")
                except OSError as err:  # the command's own result stands; only its log line is missing
                    print("warning: run log not written to {}: {}".format(folder / LOG_NAME, err), file=sys.stderr)

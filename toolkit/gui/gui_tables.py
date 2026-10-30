"""The GUI's tables (docs/gui/BUILD_PLAN.md step 7): the run log and each run's per-row outcomes.

Copied from the automation GUI's gui_tables.py (7af5e9d). Python searches, filters, sorts and pages every table, so the browser
only ever holds one page (the run log can have tens of thousands of runs). The row CSV is the one automation's
`report summarize --rows-csv` writes. Not copied: the inventory, change and issue tables of automation's own screens.
Changed: the runs come from preconfig's own log, reports/preconfig.jsonl (runlog.py), whose every line already holds
automation's summary with its rows, so automation's rebuilding of older report shapes (reporting._summary_of) is not needed;
a line that is not such a record is counted as unreadable, as a damaged line is. Every line is redacted again on load with the
CLI's own run-log redactor, so a line edited or written by hand shows no key=value secret either.
"""
import csv
import io
import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import runlog
from runlog import LOG_NAME

PAGE_SIZES = (25, 50, 100, 200)
OK_STATUSES = {"SUCCESS", "NO_OP", "DRY_RUN", "VALID", "PLANNED"}
RUN_FIELDS = ("timestamp", "orchestrator", "workflow", "status", "changes_written", "row_outcomes", "duration_seconds", "source", "run_id")
RUN_FILTERS = ("orchestrator", "workflow", "status", "source")
# from automation's reporting.py
_PROBLEMS = ("BLOCKED", "FAILED", "SKIPPED", "NOT WRITTEN", "INVALID", "UNVERIFIED")
ROW_FIELDS = ("row", "item", "scope", "outcome", "reason")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
RUN_ID = re.compile(r"^[\w-]{1,80}$", re.ASCII)  # what the server accepts in /api/runs/<run_id>; also safe in a file name and a header
_NO_CELLS = runlog.Masker({})  # a log line read back has no CSV cells to mask: only the key and key=value redaction applies
# runlog's key=value pattern takes only the first word after "Authorization:", so the scheme's token is masked first
_SCHEME_TOKEN = re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer|basic)\s+[^\s,;\"\\]+")


def spreadsheet_safe(value: Any) -> Any:
    """Text a spreadsheet would run as a formula (it starts with = + - @, a tab or a carriage return) gets a leading '."""
    return "'" + value if isinstance(value, str) and value.startswith(_FORMULA_START) else value


def csv_text(rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> str:
    """Rows as CSV for people to open; names and reasons come from CSVs and Orchestrator, so no cell can run as a formula."""
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows({key: spreadsheet_safe(value) for key, value in row.items()} for row in rows)
    return handle.getvalue()


def select(rows: Sequence[Dict[str, Any]], params: Mapping[str, str], filters: Sequence[str] = (), default_sort: str = "", descending: bool = False) -> Tuple[List[Dict[str, Any]], str, str]:
    """Search (rows' "_text"), filter (exact match on each named field, plus "problems") and sort; returns (rows, sort, order)."""
    query = params.get("q", "").strip().lower()
    chosen = [row for row in rows if (not query or query in row["_text"]) and (params.get("problems") != "1" or row["_problem"])]
    for name in filters:
        if params.get(name):
            chosen = [row for row in chosen if str(row.get(name, "")) == params[name]]
    columns = {key for row in rows[:1] for key in row if not key.startswith("_")}
    sort = params.get("sort") if params.get("sort") in columns else default_sort
    order = params.get("order") if params.get("order") in ("asc", "desc") else ("desc" if descending else "asc")
    if sort:
        key = "_sort_" + sort if rows and "_sort_" + sort in rows[0] else sort
        filled = [row for row in chosen if row.get(key) not in (None, "")]
        empty = [row for row in chosen if row.get(key) in (None, "")]
        chosen = sorted(filled, key=lambda row: _sort_key(row[key]), reverse=order == "desc") + empty  # blanks last either way
    return chosen, sort, order


def page(rows: Sequence[Dict[str, Any]], params: Mapping[str, str], filters: Sequence[str] = (), default_sort: str = "", descending: bool = False) -> Dict[str, Any]:
    """One page of select(). Facets list each filter's values across all rows, so a filter never hides its own choices."""
    chosen, sort, order = select(rows, params, filters, default_sort, descending)
    size = _integer(params.get("size"), 50)
    size = size if size in PAGE_SIZES else 50
    pages = max((len(chosen) + size - 1) // size, 1)
    number = min(max(_integer(params.get("page"), 1), 1), pages)
    return {
        "total": len(rows),
        "matching": len(chosen),
        "page": number,
        "pages": pages,
        "size": size,
        "sort": sort,
        "order": order,
        "facets": {name: sorted({str(row.get(name, "")) for row in rows}) for name in filters},
        "rows": public(chosen[(number - 1) * size:number * size]),
    }


def public(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [{key: value for key, value in row.items() if not key.startswith("_")} for row in rows]


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _sort_key(value: Any) -> Tuple[int, Any]:
    """Numbers (and numeric text such as row numbers) in number order, before text in case-insensitive order."""
    if isinstance(value, str) and value.isdecimal():  # isdigit() also takes "²", which int() refuses
        return (0, int(value))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return (1, str(value).lower())
    return (0, value)


def _text(values: Iterable[Any]) -> str:
    return " ".join(str(value) for value in values if value not in (None, "")).lower()


def outcome_text(totals: Mapping[str, int]) -> str:
    """Row outcome counts as `report summarize` prints them."""
    return ", ".join("{} {}".format(count, outcome) for outcome, count in sorted(totals.items()))


def run_row(summary: Mapping[str, Any]) -> Dict[str, Any]:
    totals = summary.get("row_outcomes") or {}
    problem = summary.get("status") not in OK_STATUSES or any(outcome in _PROBLEMS for outcome in totals)
    row = {key: summary.get(key, "not recorded") for key in RUN_FIELDS}
    row.update(row_outcomes=outcome_text(totals), rows=len(summary.get("rows") or []), exit_code=summary.get("exit_code"), tone="bad" if problem else "ok")
    row.update(
        _problem=problem,
        _sort_row_outcomes=row["rows"],
        _text=_text([row[key] for key in RUN_FIELDS] + [summary.get("command"), summary.get("orchestrator_url"), (summary.get("csv") or {}).get("path")] + list(summary.get("blocking_issues") or [])),
    )
    return row


def outcome_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    problem = row.get("outcome") in _PROBLEMS
    return dict({key: row.get(key, "") for key in ROW_FIELDS}, tone="bad" if problem else "ok", _problem=problem, _text=_text(row.get(key) for key in ROW_FIELDS))


def to_csv(rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> bytes:
    """The same CSV that automation's reporting.write_rows_csv writes, as bytes for a download."""
    return csv_text(rows, fields).encode("utf-8")


class RunLog:
    """Every run in the report folder's preconfig.jsonl. The log file is read again only when its size or time changes."""

    def __init__(self, directory: Optional[Path]) -> None:
        self.directory = directory
        self._files: Dict[Path, Tuple[Tuple[int, int], List[Dict[str, Any]], int]] = {}
        self._lock = threading.Lock()

    def load(self) -> Tuple[List[Dict[str, Any]], int]:
        """(run summaries newest first, number of log lines that could not be read)."""
        if self.directory is None or not self.directory.is_dir():
            return [], 0
        summaries: List[Dict[str, Any]] = []
        unreadable = 0
        with self._lock:
            paths = sorted(self.directory.glob(LOG_NAME))
            self._files = {path: entry for path, entry in self._files.items() if path in paths}
            for path in paths:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                stamp = (stat.st_mtime_ns, stat.st_size)
                if path not in self._files or self._files[path][0] != stamp:
                    self._files[path] = (stamp,) + _read(path)
                summaries += self._files[path][1]
                unreadable += self._files[path][2]
        summaries.sort(key=lambda summary: str(summary.get("timestamp", "")), reverse=True)
        return summaries, unreadable

    def runs(self) -> Tuple[List[Dict[str, Any]], int]:
        summaries, unreadable = self.load()
        return [summary["_row"] for summary in summaries], unreadable

    def find(self, run_id: str) -> Optional[Dict[str, Any]]:
        return next((summary for summary in self.load()[0] if summary.get("run_id") == run_id), None)


def _read(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    """Each log line as its run summary, with its table rows built once; a half-written or damaged line is counted, not fatal."""
    summaries, unreadable = [], 0
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return [], 1
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            summary = runlog.redact(dict(json.loads(_SCHEME_TOKEN.sub(r"\1<REDACTED>", line))["summary"]), _NO_CELLS)
            run_id = str(summary.get("run_id", ""))
            if not (run_id.startswith("run-") and RUN_ID.match(run_id)):
                summary["run_id"] = "{}-line-{}".format(path.stem, number)  # give a line without a usable run ID one a link can name
            summary.setdefault("source", "not recorded")
            summary["_row"] = run_row(summary)
            summary["_outcomes"] = [outcome_row(row) for row in summary.get("rows") or []]
        except (ValueError, TypeError, KeyError, AttributeError):
            unreadable += 1
            continue
        summaries.append(summary)
    return summaries, unreadable


def run_detail(summary: Mapping[str, Any], params: Mapping[str, str]) -> Dict[str, Any]:
    """One run's summary (as `report summarize` shows it, with its approval record when it has one) and one page of its rows."""
    headline = {key: value for key, value in summary.items() if key not in ("rows",) and not key.startswith("_")}
    headline["row_outcomes_text"] = outcome_text(summary.get("row_outcomes") or {})
    headline["tone"] = summary["_row"]["tone"]
    return {"run": headline, "rows": page(summary["_outcomes"], params, ("outcome",))}

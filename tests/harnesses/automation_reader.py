"""A copy of the automation tools' report-log reader (edgeconnect-orchestrator-automation, branch gui at 7af5e9d,
src/edgeconnect_automation/reporting.py: summaries_from_file, the summary branch of _summary_of, render, rows_table,
_section), so the run-log tests read preconfig's lines the way `edgeconnect-auto report summarize` does. Keep it a copy:
change it only when automation's reader changes."""
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

_PROBLEMS = ("BLOCKED", "FAILED", "SKIPPED", "NOT WRITTEN", "INVALID", "UNVERIFIED")
_MAX_LINES = 25


def summaries_from_file(path: str, last: int = 1) -> List[Dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        values = [json.loads(text)]
    except json.JSONDecodeError:
        values = [json.loads(line) for line in text.splitlines() if line.strip()]
    values = values[-last:] if last > 0 else values
    return [_summary_of(value) for value in values]


def _summary_of(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping) and isinstance(value.get("summary"), Mapping) and "rows" in value["summary"]:
        return dict(value["summary"])
    raise AssertionError("not a log record with a summary and rows: automation would rebuild it from its own outputs")


def rows_table(rows: Sequence[Mapping[str, Any]], limit: Optional[int] = None) -> List[str]:
    ordered = sorted(rows, key=lambda row: (not str(row["outcome"]).startswith(_PROBLEMS), row["row"] if isinstance(row["row"], int) else 10 ** 9))
    shown = list(ordered if limit is None else ordered[:limit])
    widths = [max([len(field)] + [len(str(row[field])) for row in shown]) for field in ("row", "outcome", "item")]
    lines = ["{:<{}}  {:<{}}  {:<{}}  {}".format("ROW", widths[0], "OUTCOME", widths[1], "ITEM", widths[2], "SCOPE / REASON")]
    for row in shown:
        detail = row["scope"] + (" | " + row["reason"] if row["reason"] else "")
        lines.append("{:<{}}  {:<{}}  {:<{}}  {}".format(str(row["row"]), widths[0], row["outcome"], widths[1], row["item"], widths[2], detail))
    if limit is not None and len(ordered) > limit:
        lines.append("... {} more rows in the report log (or: edgeconnect-auto report summarize <file> --rows-csv rows.csv)".format(len(ordered) - limit))
    return lines


def _section(title: str, lines: Sequence[str]) -> List[str]:
    if not lines:
        return []
    shown = list(lines[:_MAX_LINES])
    if len(lines) > _MAX_LINES:
        shown.append("... {} more in the report log".format(len(lines) - _MAX_LINES))
    return ["", title] + ["  - " + line for line in shown]


def render(summary: Mapping[str, Any], title: str = "RUN SUMMARY", row_limit: Optional[int] = _MAX_LINES) -> str:
    bar = "=" * 78
    csv_info = summary.get("csv") or {}
    lines = [
        "", bar, title, bar,
        "Workflow     : {}".format(summary["workflow"]),
        "Run          : {}  at {}{}".format(summary["run_id"], summary["timestamp"], "" if summary.get("duration_seconds") is None else "  ({}s)".format(summary["duration_seconds"])),
        "Versions     : tool {}, Orchestrator {}".format(summary["tool_version"], summary["orchestrator_release"]),
    ]
    if summary.get("orchestrator_target"):
        lines.append("Orchestrator : {}".format(summary["orchestrator_target"]))
    if csv_info:
        lines.append("CSV          : {} ({} data rows, sha256 {}...)".format(csv_info["path"], csv_info["data_rows"], csv_info["sha256"][:12]))
    lines += [
        "RESULT       : {}".format(summary["status"]),
        "Changes made : {}".format(summary["changes_written"]),
        "Exit code    : {} = {}".format(summary["exit_code"], summary["exit_meaning"]),
    ]
    approval = summary.get("approval")
    if approval:
        lines.append("Approved by  : {} at {} in the GUI, typing {}".format(approval.get("by"), approval.get("time"), ", ".join(repr(text) for text in [approval.get("typed_orchestrator")] + list(approval.get("typed") or []) if text)))
    if summary.get("run_reference"):
        lines.append("Run ref      : {} (Orchestrator audit log comments start with it, followed by -<segment map>)".format(summary["run_reference"]))
    lines += _section("What was planned or done", summary.get("counts", []))
    if summary.get("rows"):
        totals = ", ".join("{} {}".format(count, outcome) for outcome, count in sorted(summary.get("row_outcomes", {}).items()))
        lines += ["", "Rows ({}): {}".format(len(summary["rows"]), totals)] + ["  " + line for line in rows_table(summary["rows"], row_limit)]
    lines += _section("Why it stopped or what failed" if summary["status"] not in {"SUCCESS", "NO_OP", "DRY_RUN", "VALID", "PLANNED"} else "Issues", summary.get("blocking_issues", []))
    lines += _section("Warnings (do not block)", summary.get("warnings", []))
    lines += _section("Appliances", summary.get("targets", []))
    for path in summary.get("reports", []):
        lines.append("Report       : {}".format(path))
    lines += ["", "Next step    : {}".format(summary["next_step"]), bar]
    return "\n".join(lines)

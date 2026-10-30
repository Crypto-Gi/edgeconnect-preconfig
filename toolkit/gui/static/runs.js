// "See what changed": the run log (reports/preconfig.jsonl) and one run's rows, as the automation GUI shows them
// (edgeconnect-orchestrator-automation 7af5e9d, static/app.js runsTable / rowsTable / renderRun), without its Undo (D7).
// Load after app.js and tables.js. The server reads, searches, sorts and pages the log; every value is written with textContent.
"use strict";

const runsTable = new PagedTable({
  id: "runs",
  label: "runs",
  container: $("runs-table"),
  path: () => "/api/runs",
  exportPath: () => "/api/runs.csv",
  exportName: () => "runs.csv",
  problems: "Problems only",
  empty: "No runs logged yet.",
  filters: [{ key: "orchestrator", label: "All Orchestrators" }, { key: "workflow", label: "All workflows" }, { key: "status", label: "All results" }, { key: "source", label: "CLI and GUI" }],
  columns: [
    { key: "timestamp", label: "Time", mono: true, cell: (row) => String(row.timestamp).replace("T", " ") },
    { key: "orchestrator", label: "Orchestrator" },
    { key: "workflow", label: "Workflow" },
    { key: "status", label: "Result", cell: (row) => toneCell(row.status, row.tone) },
    { key: "changes_written", label: "Changes made" },
    { key: "row_outcomes", label: "Rows" },
    { key: "duration_seconds", label: "Seconds" },
    { key: "source", label: "From" },
    { key: "run_id", label: "Run", mono: true, cell: (row) => el("a", { href: "#/runs/" + row.run_id, text: row.run_id }) },
  ],
  after: (data) => {
    const parts = [data.folder ? "Every run logged in " + data.folder + ", newest first." : "Run logging is off (EDGECONNECT_REPORT_DIR is empty), so there are no runs to show."];
    if (data.unreadable) parts.push(data.unreadable + " log line" + (data.unreadable > 1 ? "s" : "") + " could not be read and " + (data.unreadable > 1 ? "are" : "is") + " left out.");
    $("runs-text").textContent = parts.join(" ");
  },
});

let runId = "";
const rowsTable = new PagedTable({
  id: "rows",
  label: "rows of this run",
  container: $("run-rows"),
  path: () => runId && "/api/runs/" + runId,
  exportPath: () => "/api/runs/" + runId + "/rows.csv",
  exportName: () => runId + "-rows.csv",
  problems: "Problems only",
  empty: "This run has no per-row table.",
  filters: [{ key: "outcome", label: "All outcomes" }],
  table: (data) => data.rows,
  columns: [
    { key: "row", label: "Row" },
    { key: "item", label: "Item" },
    { key: "scope", label: "Scope" },
    { key: "outcome", label: "Outcome", cell: (row) => toneCell(row.outcome, row.tone) },
    { key: "reason", label: "Reason" },
  ],
  after: (data) => renderRun(data.run),
});

function loadRun(id) {
  if (id !== runId) {
    runId = id;
    rowsTable.reset();
    $("run-title").textContent = "Loading…";
    $("run-approval").hidden = true;
  }
  rowsTable.load();
}

function facts(list, pairs) {
  list.replaceChildren(...pairs.filter(([, value]) => value !== "" && value != null)
    .flatMap(([name, value, mono]) => [el("dt", { text: name }), el("dd", mono ? { class: "mono", text: String(value) } : { text: String(value) })]));
}

function renderRun(run) {
  $("run-id").textContent = run.run_id;
  $("run-title").replaceChildren(run.workflow + " · ", toneCell(run.status, run.tone));
  $("run-next").textContent = run.next_step || "";
  facts($("run-facts"), [
    ["Time", String(run.timestamp).replace("T", " ")],
    ["Orchestrator", [run.orchestrator, run.orchestrator_url].filter(Boolean).join(" · ")],
    ["Release", run.orchestrator_release],
    ["From", run.source],
    ["Changes made", run.changes_written],
    ["Exit code", run.exit_code + " = " + run.exit_meaning],
    ["Seconds", run.duration_seconds],
    ["Rows", run.row_outcomes_text],
    ["CSV", run.csv && run.csv.path ? run.csv.path + " (" + run.csv.data_rows + " data rows)" : ""],
    ["Tool", run.tool_version],
    ["Command", run.command, true],
  ]);
  const notes = [["What was planned or done", run.counts], ["Why it stopped or what failed", run.blocking_issues], ["Warnings (do not block)", run.warnings]]
    .filter(([, items]) => items && items.length)
    .flatMap(([title, items]) => [el("p", { class: "notes-title", text: title }), el("ul", { class: "notes" }, ...items.map((item) => el("li", { text: item })))]);
  $("run-notes").replaceChildren(...(notes.length ? notes : [el("p", { class: "muted", text: "Nothing else was recorded for this run." })]));
  // The approval record an upload run keeps (approval.py): who approved, when, what they typed, and the batch it was bound to.
  const approval = run.approval;
  $("run-approval").hidden = !approval;
  if (approval) facts($("run-approval-facts"), [
    ["Approved by", approval.by],
    ["At", String(approval.time || "").replace("T", " ")],
    ["Orchestrator", approval.orchestrator],
    ["Typed", (approval.typed || []).join(", ")],
    ["Batch fingerprint", approval.plan_fingerprint, true],
  ]);
}

SCREENS.push(
  { hash: /^#\/runs$/, view: "view-runs", render: () => runsTable.load() },
  { hash: /^#\/runs\/([\w-]+)$/, view: "view-run", render: (match) => loadRun(match[1]) },
);

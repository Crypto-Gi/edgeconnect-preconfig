// Inventory (docs/gui/BUILD_PLAN.md step 10): the appliances from `list` and the preconfigs from `preconfigs` on the chosen
// Orchestrator, and one preconfig's YAML exactly as the CLI prints it, its secrets already redacted by Python. Read only: every
// request is a read task, nothing here can be edited, and every value is written with textContent. Uses app.js's helpers.
"use strict";

const INVENTORY_PAGE = 50;  // shortcut: the browser pages one task's rows; PagedTable (tables.js) would need a server endpoint for them
// list's recorded rows (R8) and the column names its table prints
const APPLIANCE_KEYS = ["hostname", "nepk", "model", "ecos", "site", "reachable"];
const APPLIANCE_COLUMNS = ["Hostname", "nePk", "Model", "ECOS", "Site", "Reachable"];
const READ_ENDS = new Set(["done", "failed", "rejected", "expired", "cancelled"]);
const inventory = { orch: null, appliances: null, page: 0, search: "", preconfigs: null, shown: null };

// Start one read task, follow its event stream to the end, then fetch its result.
async function runRead(body) {
  const started = await post("/api/tasks", body);
  const task = await new Promise((resolve, reject) => {
    const stream = new EventSource("/api/tasks/" + started.id + "/events");
    stream.onmessage = (message) => {
      const event = JSON.parse(message.data);
      if (READ_ENDS.has(event.task.state)) { stream.close(); resolve(event.task); }
    };
    stream.onerror = () => { stream.close(); reject(new Error("the task's progress stream stopped")); };
  });
  const result = (await api("/api/tasks/" + started.id + "/result")).result;
  return { task, outputs: (result && result.outputs) || [] };
}

function renderInventory() {
  const orch = current();
  $("inv-orch").textContent = orch ? nameOf(orch) : "No Orchestrator configured";
  if (!state.current) {  // nothing to read: say why instead of waiting for a read that never starts
    const why = { ok: !state.failed && state.loading, columns: [], rows: [],
      message: state.loading ? "Checking the Orchestrators…" : state.failed ? "Could not check the Orchestrators: check again on the home page" : "No Orchestrator is configured in .env" };
    Object.assign(inventory, { orch: null, appliances: why, preconfigs: why, shown: null });
  } else if (inventory.orch !== state.current) {  // a new Orchestrator: nothing of the last one is kept
    Object.assign(inventory, { orch: state.current, search: "", shown: null });
    $("inv-search").value = "";
    loadAppliances();
    loadPreconfigs();
  }
  drawAppliances();
  drawPreconfigs();
  drawShown();
}

function inventoryStatus(id, text, bad) {
  $(id).textContent = text;
  $(id).className = bad ? "tone-bad" : "muted";
}

/* appliances, as `list` shows them */
async function loadAppliances() {
  const pending = { busy: true };
  inventory.appliances = pending;
  drawAppliances();
  let found;
  try {
    const { task, outputs } = await runRead({ action: "list", orchestrator: inventory.orch, search: inventory.search });
    const rows = outputs.filter((output) => output.kind === "appliance").map((box) => APPLIANCE_KEYS.map((key) => (box[key] == null ? "" : String(box[key]))));
    found = { ok: task.state === "done", message: task.message, columns: rows.length ? APPLIANCE_COLUMNS : [], rows };
  } catch (error) {
    found = { ok: false, message: "Could not list the appliances: " + error.message, columns: [], rows: [] };
  }
  if (inventory.appliances !== pending) return;  // a newer search or another Orchestrator
  Object.assign(inventory, { appliances: found, page: 0 });
  drawAppliances();
}

function drawAppliances() {
  const found = inventory.appliances || { busy: true };
  inventoryStatus("inv-apps-text", found.busy ? "Reading the appliances…" : found.message + (inventory.search ? " (search: " + inventory.search + ")" : ""), !found.busy && !found.ok);
  const rows = found.rows || [];
  const pages = Math.max(Math.ceil(rows.length / INVENTORY_PAGE), 1);
  inventory.page = Math.min(inventory.page, pages - 1);
  const first = inventory.page * INVENTORY_PAGE;
  $("inv-apps-head").replaceChildren(...(found.columns || []).map((name) => el("th", { scope: "col", text: name })));
  $("inv-apps").replaceChildren(...rows.slice(first, first + INVENTORY_PAGE).map((row) => el("tr", null, ...row.map((value) => el("td", { text: value })))));
  $("inv-apps-count").textContent = rows.length ? (first + 1) + "–" + Math.min(first + INVENTORY_PAGE, rows.length) + " of " + rows.length : "";
  $("inv-apps-prev").disabled = inventory.page === 0;
  $("inv-apps-next").disabled = inventory.page >= pages - 1;
}

function pageAppliances(step) {
  inventory.page = Math.max(inventory.page + step, 0);
  drawAppliances();
}

/* the preconfigs on the Orchestrator */
async function loadPreconfigs() {
  const pending = { busy: true };
  inventory.preconfigs = pending;
  drawPreconfigs();
  let found;
  try {
    const { task, outputs } = await runRead({ action: "preconfigs", orchestrator: inventory.orch });
    found = { ok: task.state === "done", message: task.message, rows: outputs.filter((output) => output.kind === "preconfig") };
  } catch (error) {
    found = { ok: false, message: "Could not list the preconfigs: " + error.message, rows: [] };
  }
  if (inventory.preconfigs !== pending) return;
  inventory.preconfigs = found;
  drawPreconfigs();
}

// modifiedTime as the Orchestrator sends it; a number is read as milliseconds (seconds if it is too small for that)
function changed(value) {
  if (typeof value !== "number" || !isFinite(value)) return value == null ? "" : String(value);
  const date = new Date(value < 1e11 ? value * 1000 : value);
  return isNaN(date) ? String(value) : date.toISOString().replace(/:\d\d\.\d{3}Z$/, "").replace("T", " ") + " UTC";  // toISOString throws past ±8.64e15 ms
}

function drawPreconfigs() {
  const found = inventory.preconfigs || { busy: true };
  inventoryStatus("inv-pcs-text", found.busy ? "Reading the preconfigs…" : found.message, !found.busy && !found.ok);
  $("inv-pcs").replaceChildren(...(found.rows || []).map((row) => {
    const open = el("button", { class: "btn", type: "button", text: row.name == null ? "(no name)" : String(row.name), "aria-label": "Show the YAML of " + row.name });
    open.addEventListener("click", () => showPreconfig(row));
    return el("tr", null, el("td", null, open), el("td", { class: "mono", text: row.serial == null ? "" : String(row.serial) }),
      el("td", { text: row.tag == null ? "" : String(row.tag) }), el("td", { class: "mono", text: changed(row.last_change) }),
      el("td", { class: "mono", text: row.id == null ? "" : String(row.id) }));
  }));
}

/* one preconfig's YAML: read only, secrets as <SECRET:path>; a YAML that does not parse shows only the CLI's message */
async function showPreconfig(row) {
  const label = "preconfig " + row.name + (row.id == null ? "" : " (id " + row.id + ")");
  const pending = { busy: true, label };
  inventory.shown = pending;
  drawShown();
  $("inv-yaml-title").focus();
  let found;
  try {
    const { task, outputs } = await runRead({ action: "preconfigs", orchestrator: inventory.orch, show: String(row.id == null ? row.name : row.id) });  // an id is unique, a name may not be
    const yaml = outputs.find((output) => output.kind === "preconfig-yaml");
    found = { label, ok: task.state === "done" && !!yaml, text: yaml ? yaml.text : "", message: task.message };
  } catch (error) {
    found = { label, ok: false, text: "", message: "Could not read it: " + error.message };
  }
  if (inventory.shown !== pending) return;
  inventory.shown = found;
  drawShown();
}

function drawShown() {
  const shown = inventory.shown;
  $("inv-yaml-panel").hidden = !shown;
  if (!shown) return;
  $("inv-yaml-title").textContent = shown.label;
  inventoryStatus("inv-yaml-text", shown.busy ? "Reading it…" : shown.ok ? "Read only, as the command line prints it: secrets are shown as <SECRET:path>." : shown.message, !shown.busy && !shown.ok);
  $("inv-yaml").textContent = shown.ok ? shown.text : "";
  $("inv-yaml").hidden = !shown.ok;
}

SCREENS.push({ hash: /^#\/inventory$/, view: "view-inventory", render: renderInventory });
$("inv-search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  inventory.search = $("inv-search").value.trim();
  loadAppliances();
});
$("inv-apps-refresh").addEventListener("click", loadAppliances);
$("inv-pcs-refresh").addEventListener("click", loadPreconfigs);
$("inv-apps-prev").addEventListener("click", () => pageAppliances(-1));
$("inv-apps-next").addEventListener("click", () => pageAppliances(1));

// EdgeConnect Preconfig GUI shell, copied from edgeconnect-orchestrator-automation 7af5e9d (static/app.js) and cut to the home page.
// The browser only shows what the Python server decided; it never validates or decides anything itself. Every value is
// written with textContent, never as HTML.
"use strict";

// cli: what the same task is on the command line, shown while its screen is not built yet ("{orch}" is the chosen nickname).
const TASKS = [
  { id: "build", title: "Build a template from an appliance", text: "Read one appliance and turn its configuration into a preconfig template, checked on a validator.", writes: false, href: "#/build",
    cli: ["build --orchestrator {orch} --appliance <appliance>"] },
  { id: "variables", title: "Choose what changes per site", text: "Tick the settings that differ from site to site; the rest stay as the appliance has them.", writes: false, href: "#/variables",
    cli: ["variables <build folder>"] },
  { id: "compare", title: "Compare with other sites", text: "Read other sites of the same design and start from the settings that differ between them.", writes: false, href: "#/compare",
    cli: ["compare <build folder> <site> [<site> ...]"] },
  { id: "make", title: "Make the preconfig files", text: "From a template and a filled CSV: create the YAML files, validate them, and upload them.", writes: true, href: "#/make",
    cli: ["render --template <template> --csv <csv> --out <folder>", "validate <site folder> --csv <csv>", "upload <site folder> --csv <csv> --orchestrator {orch}"] },
  { id: "yaml", title: "Check or upload existing YAML files", text: "Validate preconfig YAML files you already have, or upload the ones that pass.", writes: true, href: "#/yaml",
    cli: ["validate --yaml <file or folder> --orchestrator {orch}", "upload --yaml <file or folder> --orchestrator {orch}"] },
  { id: "inventory", title: "Inventory", text: "Look through the appliances and the preconfigs this Orchestrator has now.", writes: false, href: "#/inventory",
    cli: ["list --orchestrator {orch}", "preconfigs --orchestrator {orch}"] },
  { id: "changes", title: "See what changed", text: "Look through earlier runs, their outcomes and every row they touched.", writes: false, href: "#/runs",
    cli: [] },
];
const PROG = "edgeconnect-preconfig";
const SHAPE = { green: "●", amber: "▲", red: "■" };
const OPEN_STATES = new Set(["queued", "running", "awaiting_approval"]);  // as automation: an upload that is sending cannot be cancelled

const state = { fleet: [], failed: false, note: "", warnings: [], current: null, tasks: [], loading: false, work: { files: {} } };
const $ = (id) => document.getElementById(id);
// Screens built in their own files (runs.js, ...) add { hash: RegExp, view: section id, render(match) } here before the first render.
const SCREENS = [];

function store(key, value) {
  try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch (_) { return null; }
  return null;
}

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) if (child != null) node.append(child);
  return node;
}

async function api(path, options) {
  const response = await fetch(path, Object.assign({ credentials: "same-origin" }, options));
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || response.statusText);
  return body;
}

function post(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
}

function nameOf(orch) { return orch ? (orch.label || orch.name || orch.url) : ""; }
function current() { return state.fleet.find((orch) => orch.name === state.current) || null; }

/* theme */
function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme; else delete document.documentElement.dataset.theme;
}
function toggleTheme() {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  const next = dark ? "light" : "dark";
  applyTheme(next);
  store("ec-theme", next);
}

/* fleet status and the switcher */
async function loadFleet() {
  if (state.loading) return;  // a check is running; the button stays usable (nothing is greyed out) and waits for it
  state.loading = true;
  const list = $("fleet");
  list.setAttribute("aria-busy", "true");
  try {
    const fleet = await api("/api/orchestrators");
    Object.assign(state, { failed: false, fleet: fleet.orchestrators, note: fleet.note || "", warnings: fleet.warnings || [] });
  } catch (error) {
    Object.assign(state, { failed: true, fleet: [], note: "", warnings: [] });
    list.replaceChildren(el("li", { class: "s-red", text: "Could not check the Orchestrators: " + error.message }));
  }
  state.loading = false;
  list.setAttribute("aria-busy", "false");
  renderNotes();
  if (!state.fleet.length) { renderFleet(); renderSwitcher(); renderHeader(); return render(); }
  const remembered = store("ec-orchestrator");
  if (!state.fleet.some((orch) => orch.name === state.current)) {
    const pick = state.fleet.find((orch) => orch.name === remembered) || state.fleet.find((orch) => orch.status === "green") || state.fleet[0];
    state.current = pick.name;
  }
  renderFleet();
  renderSwitcher();
  renderHeader();
  render();
}

function renderFleet() {
  if (state.failed) return;  // the error loadFleet showed stays until a check succeeds
  const items = state.fleet.map((orch) => {
    const item = el("li", { class: "orch" },
      el("span", { class: "name", text: nameOf(orch) + (orch.name === state.current ? " (selected)" : "") }),
      el("span", { class: "status-word s-" + orch.status, text: SHAPE[orch.status] + " " + orch.status }),
      el("span", { class: "reason", text: orch.reason }),
      el("span", { class: "meta", text: [orch.url, orch.release && "release " + orch.release, "validator " + (orch.validator ? "yes" : "no"), "uploads " + (orch.writes ? "on" : "off")].filter(Boolean).join(" · ") }));
    if (orch.color) item.style.setProperty("--c", orch.color);
    return item;
  });
  $("fleet").replaceChildren(...(items.length ? items
    : [el("li", { class: "muted", text: "No Orchestrator is listed in .env: add orch_1=<nickname> with <nickname>_base_url and <nickname>_api_key, then check again." })]));
}

// Why some .env nicknames are not listed, and the keys the automation tools would refuse: both are the server's own words.
function renderNotes() {
  $("fleet-note").hidden = !state.note;
  $("fleet-note").textContent = state.note ? state.note + "." : "";
  $("fleet-warnings").hidden = !state.warnings.length;
  $("fleet-warnings-list").replaceChildren(...state.warnings.map((item) => el("li", { text: item })));
}

function renderSwitcher() {
  const select = $("orch");
  if (!state.fleet.length) return select.replaceChildren(el("option", { value: "", text: "No Orchestrator" }));
  select.replaceChildren(...state.fleet.map((orch) => {
    const option = el("option", { value: orch.name, text: nameOf(orch) + " — " + orch.status });
    option.selected = orch.name === state.current;
    return option;
  }));
}

function renderHeader() {
  const orch = current();
  const root = document.documentElement.style;
  if (orch && orch.color) root.setProperty("--orch-color", orch.color); else root.removeProperty("--orch-color");
  const status = $("orch-status");
  status.className = "status-word" + (orch ? " s-" + orch.status : "");
  status.textContent = orch ? SHAPE[orch.status] + " " + orch.status : "";
  status.title = orch ? orch.reason : "";
  const label = orch ? nameOf(orch) : "No Orchestrator configured";
  $("home-orch").textContent = label;
  $("home-writes").textContent = !orch ? "Add an Orchestrator to .env, then check again."
    : [orch.writes ? "Uploads are on for " + nameOf(orch) + ". Every upload still shows the rows first and needs your typed approval."
      : "Uploads are off on " + nameOf(orch) + ": you can build, make and check the files, but not upload them. Set " + orch.name + "_gui_preconfig_uploads=on in .env to allow them.",
    orch.validator ? "/validate runs here." : "/validate does not run here: it is not listed in EC_VALIDATORS."].join(" ");
  document.title = (orch ? nameOf(orch) + " · " : "") + "EdgeConnect Preconfig";
}

/* switching Orchestrator resets everything; ask first when there is something to lose */
function openTasks(name) { return state.tasks.filter((task) => task.orchestrator === name && OPEN_STATES.has(task.state)); }

function requestSwitch(name) {
  if (name === state.current) return;
  const files = Object.keys(state.work.files).length;
  const open = openTasks(state.current).length;
  if (!files && !open) return switchTo(name);
  const parts = [];
  if (files) parts.push(files === 1 ? "the loaded file" : files + " loaded files");
  if (open) parts.push(open + " open task" + (open > 1 ? "s, which will be cancelled" : ", which will be cancelled"));
  $("switch-text").textContent = "Switching to " + nameOf(state.fleet.find((orch) => orch.name === name)) + " starts fresh. You would lose " + parts.join(" and ") + ". Checks and approvals are never carried to another Orchestrator; files already written to preconfig-output stay there.";
  $("switch-download").hidden = !files;
  const dialog = $("switch-dialog");
  dialog.returnValue = "";
  dialog.onclose = () => {
    if (dialog.returnValue === "switch") switchTo(name);
    else $("orch").value = state.current;
  };
  dialog.showModal();
}

async function switchTo(name) {
  for (const task of openTasks(state.current)) {
    try { await post("/api/tasks/" + task.id + "/cancel"); } catch (_) { /* already finished */ }
  }
  state.current = name;
  state.work = { files: {} };
  store("ec-orchestrator", name);
  renderSwitcher();
  renderFleet();
  renderHeader();
  location.hash = "#/";
  render();
  $("main").focus();
}

function downloadFiles() {
  for (const [file, text] of Object.entries(state.work.files)) {
    const link = el("a", { href: URL.createObjectURL(new Blob([text], { type: "text/plain" })), download: file });
    link.click();
    URL.revokeObjectURL(link.href);
  }
}

/* views */
function kindText(task, orch) {
  return task.writes ? (orch && orch.writes ? "can upload here" : "uploads off here") : "reads only";
}

function renderIntents() {
  const orch = current();
  $("intents").replaceChildren(...TASKS.map((task) => el("li", null,
    el("a", { class: "intent", href: task.href },
      // Title first, so a screen reader names the link by its task; CSS shows the kind above it.
      el("h2", { text: task.title }),
      el("p", { text: task.text }),
      el("span", { class: "kind" + (task.writes ? " writes" : ""), text: kindText(task, orch) + (SCREENS.some((screen) => screen.hash.test(task.href)) ? "" : " · not yet available") })))));
}

// A screen a later step builds: says so, and gives the command line that does the same today.
function renderLater(task) {
  const orch = current();
  $("later-orch").textContent = orch ? nameOf(orch) : "No Orchestrator configured";
  $("later-title").textContent = task.title;
  $("later-text").textContent = task.text;
  $("later-cli-text").textContent = "This screen is not in this version of the GUI yet. The command line does the same:";
  $("later-cli").replaceChildren(...task.cli.map((line) => el("li", null, el("code", { text: PROG + " " + line.split("{orch}").join(state.current || "<nickname>") }))));
}

function show(view) {
  for (const id of ["view-home", "view-later"].concat(SCREENS.map((screen) => screen.view))) $(id).hidden = id !== view;
}

function render() {
  for (const screen of SCREENS) {
    const match = location.hash.match(screen.hash);
    if (match) { show(screen.view); return screen.render(match); }
  }
  const task = TASKS.find((item) => item.href === location.hash);
  if (task) { show("view-later"); return renderLater(task); }
  show("view-home");
  renderIntents();
}

async function loadTasks() {
  try { state.tasks = (await api("/api/tasks")).tasks; } catch (_) { return; }
  const rows = state.tasks.map((task) => el("tr", null,
    el("td", { text: [task.action, task.kind].filter(Boolean).join(" ") }),
    el("td", { text: task.orchestrator || "(unnamed)" }),
    el("td", { text: task.state.replace("_", " ") }),
    el("td", { text: task.message })));
  $("tasks").replaceChildren(...(rows.length ? rows : [el("tr", null, el("td", { colspan: "4", class: "muted", text: "No tasks yet." }))]));
}

/* earlier builds in preconfig-output: the server lists them and names every download; the browser builds no path */
// A download link only to a path on this server under /api/; anything else (another origin, a scheme, "//host") gets no link.
function apiHref(url) { return typeof url === "string" && /^\/api\//.test(url) ? url : ""; }

async function loadBuilds() {
  let data;
  try {
    data = await api("/api/builds");
  } catch (error) {
    $("builds-text").textContent = "Could not list the earlier builds: " + error.message;
    $("builds-text").className = "tone-bad";
    return;
  }
  $("builds-text").className = "muted";
  $("builds-text").textContent = "Builds in " + data.folder + ", newest first. They stay there after the GUI stops.";
  const rows = data.builds.map((build) => el("tr", null,
    el("td", null, el("a", { href: "#/variables/" + encodeURIComponent(build.appliance), text: build.appliance, title: "Choose what changes per site" })),
    el("td", { text: build.orchestrator }),
    el("td", { class: "mono", text: String(build.built || "").replace("T", " ") }),
    el("td", null, build.site_types.length ? el("ul", { class: "notes" }, ...build.site_types.map((site) => el("li", null,
      el("span", { text: site.name + " " }),
      apiHref(site.template_url) ? el("a", { href: apiHref(site.template_url), download: "", text: "Download template" }) : null,
      apiHref(site.template_url) && apiHref(site.csv_url) ? " · " : null,
      apiHref(site.csv_url) ? el("a", { href: apiHref(site.csv_url), download: "", text: "Download CSV" }) : null))) : el("span", { class: "muted", text: "No site type saved yet" }))));
  $("builds").replaceChildren(...(rows.length ? rows : [el("tr", null, el("td", { colspan: "4", class: "muted", text: "No builds yet. Build a template from an appliance to start." }))]));
}

/* command palette: combobox and listbox, all by keyboard */
let paletteItems = [];
let paletteIndex = 0;

function commands() {
  const list = [{ label: "Go to all tasks", hint: "home", run: () => { location.hash = "#/"; } }];
  for (const task of TASKS) list.push({ label: task.title, hint: kindText(task, current()), run: () => { location.hash = task.href; } });
  for (const orch of state.fleet) if (orch.name !== state.current) list.push({ label: "Switch to " + nameOf(orch), hint: orch.status, run: () => requestSwitch(orch.name) });
  list.push({ label: "Check Orchestrators again", hint: "status", run: loadFleet });
  list.push({ label: "List earlier builds again", hint: "builds", run: loadBuilds });
  list.push({ label: "Switch light or dark theme", hint: "theme", run: toggleTheme });
  return list;
}

function renderPalette() {
  const query = $("palette-input").value.trim().toLowerCase();
  paletteItems = commands().filter((item) => item.label.toLowerCase().includes(query));
  paletteIndex = Math.min(paletteIndex, Math.max(paletteItems.length - 1, 0));
  $("palette-list").replaceChildren(...paletteItems.map((item, index) => {
    const option = el("li", { role: "option", id: "cmd-" + index, "aria-selected": String(index === paletteIndex) }, el("b", { text: item.label }), el("span", { text: item.hint }));
    option.addEventListener("click", () => runCommand(index));
    return option;
  }));
  const input = $("palette-input");
  if (paletteItems.length) input.setAttribute("aria-activedescendant", "cmd-" + paletteIndex); else input.removeAttribute("aria-activedescendant");
  const active = $("cmd-" + paletteIndex);
  if (active) active.scrollIntoView({ block: "nearest" });
}

function openPalette() {
  const dialog = $("palette");
  if (dialog.open) return;
  $("palette-input").value = "";
  paletteIndex = 0;
  renderPalette();
  dialog.showModal();
  $("palette-input").focus();
}

function runCommand(index) {
  const item = paletteItems[index];
  $("palette").close();
  if (item) item.run();
}

/* wiring */
applyTheme(store("ec-theme"));
$("theme").addEventListener("click", toggleTheme);
$("fleet-refresh").addEventListener("click", loadFleet);
$("builds-refresh").addEventListener("click", loadBuilds);
$("orch").addEventListener("change", (event) => requestSwitch(event.target.value));
$("palette-open").addEventListener("click", openPalette);
$("palette-input").addEventListener("input", () => { paletteIndex = 0; renderPalette(); });
$("palette-input").addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    paletteIndex = (paletteIndex + step + paletteItems.length) % Math.max(paletteItems.length, 1);
    renderPalette();
  } else if (event.key === "Enter") {
    event.preventDefault();
    runCommand(paletteIndex);
  }
});
$("switch-confirm").addEventListener("click", () => $("switch-dialog").close("switch"));
$("switch-cancel").addEventListener("click", () => $("switch-dialog").close("stay"));
$("switch-download").addEventListener("click", downloadFiles);
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    openPalette();
  }
});
window.addEventListener("hashchange", () => { render(); $("main").focus(); });

document.addEventListener("DOMContentLoaded", render);  // after the screens' own scripts have added themselves to SCREENS
loadFleet();
loadTasks();
loadBuilds();
setInterval(loadTasks, 5000);

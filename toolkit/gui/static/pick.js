// "Choose what changes per site" and "Compare with other sites" (docs/gui/BUILD_PLAN.md step 8 and 8b, D11 to D14). The check boxes
// are the picker rows the CLI's own `variables FOLDER --pick P --set ... --fix ... --dry-run` (or `compare FOLDER SITE... --set ...
// --fix ... --captures ... --dry-run`) records; after each flip the page runs that command again and draws
// what it returns. The browser only keeps which boxes the person flipped; every tick, column name, line and refusal comes from
// Python. Every value is written with textContent. Loaded after app.js, which owns el, api, post and the views.
"use strict";

// Where a pick starts. Each start is re-applied with the person's own ticks (--set) and unticks (--fix) on top. compare reads the
// sites once; every later run names that run (captures_from) so the server passes its sample captures back with --captures.
const PICK_STARTS = [
  { id: "top10", label: "Top 10" },
  { id: "top20", label: "Top 20" },
  { id: "all", label: "All" },
  { id: "compare", label: "Different between your sites", available: () => pick.sites.length > 0,
    request: () => ({ action: "compare", build: pick.build, sites: pick.sites, captures_from: pick.captures }) },
];
const FINAL_STATES = new Set(["done", "failed", "rejected", "expired", "cancelled"]);

// Run one task and follow its events; resolves with { task, result } once it has ended. onEvent gets every event in order.
async function runTask(request, onEvent) {
  const task = await post("/api/tasks", request);
  const ended = await new Promise((resolve) => {
    const source = new EventSource("/api/tasks/" + task.id + "/events");
    source.onmessage = (message) => {
      const event = JSON.parse(message.data);
      if (onEvent) onEvent(event);
      if (event.state && FINAL_STATES.has(event.state)) { source.close(); resolve(event.task); }
    };
    source.onerror = () => { source.close(); resolve(null); };  // the stream broke: ask for the result until it is there
  });
  for (;;) {
    const result = (await api("/api/tasks/" + task.id + "/result")).result;
    if (result) return { task: ended || task, result };
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
}

// The last "error: ..." line a failed command printed: the CLI's own reason (a refused --fix names the row and why it stays).
function errorLine(text) {
  const lines = String(text || "").split("\n").filter((line) => line.trim());
  return (lines.filter((line) => line.startsWith("error: ")).pop() || lines.pop() || "the command failed").replace(/^error: /, "");
}

const pick = {
  build: "", key: "", start: "top10", extras: new Set(), unticks: new Set(), good: null,
  rows: [], columns: [], ref: null, siteType: "", tab: "sections", query: "", onlyOn: false, closed: new Set(),
  pending: new Map(), notes: new Map(), busy: false, dirty: false, timer: 0, opened: 0, message: "", lineEls: [],
  sites: [], captures: null, orchestrator: "", finding: 0,
};
const GONE = /the earlier read is gone/;  // the server's refusal of a captures_from whose folders were cleared

function pickRequest(extra) {
  const start = PICK_STARTS.find((item) => item.id === pick.start) || PICK_STARTS[0];
  const base = start.request ? start.request(pick) : { action: "variables", build: pick.build, pick: start.id };
  return Object.assign(base, { set: [...pick.extras], fix: [...pick.unticks] }, extra || {});
}

/* the screen, built once */
const P = {};
(function buildPickView() {
  P.view = el("section", { id: "view-pick", class: "view", "aria-labelledby": "pick-title", hidden: "" });
  P.title = el("h1", { id: "pick-title", text: "Choose what changes per site" });
  P.eyebrow = el("div", { class: "eyebrow" });
  P.lede = el("p", { class: "lede" });
  P.status = el("p", { class: "muted", role: "status" });
  P.choose = el("ul", { class: "notes" });
  P.chooser = el("section", { class: "panel", "aria-labelledby": "pick-choose-title" }, el("h2", { id: "pick-choose-title", text: "Which build?" }), P.choose);
  P.starts = el("div", { class: "toolbar", role: "group", "aria-label": "Start from" });
  P.search = el("input", { type: "search", "aria-label": "Search the settings", placeholder: "Search settings, values or columns" });
  P.only = el("input", { type: "checkbox", id: "pick-only" });
  P.save = el("button", { class: "btn primary", type: "button", text: "Save template and CSV" });
  P.tabSections = el("button", { class: "btn", type: "button", role: "tab", id: "pick-tab-sections", "aria-controls": "pick-sections" , text: "By section" });
  P.tabTable = el("button", { class: "btn", type: "button", role: "tab", id: "pick-tab-table", "aria-controls": "pick-table", text: "Table" });
  P.sections = el("div", { id: "pick-sections", role: "tabpanel", "aria-labelledby": "pick-tab-sections", class: "pick-panels" });
  P.tbody = el("tbody");
  P.table = el("div", { id: "pick-table", role: "tabpanel", "aria-labelledby": "pick-tab-table", class: "table-wrap", hidden: "" },
    el("table", null, el("caption", { class: "sr-only", text: "Every setting, as the command line's tick list shows it" }),
      el("thead", null, el("tr", null, ...["#", "Per site", "Setting", "Value now", "% different", "Column"].map((name) => el("th", { scope: "col", text: name })))), P.tbody));
  P.yaml = el("ol", { class: "yaml" });
  P.yamlBox = el("div", { class: "yaml-box", tabindex: "0", role: "region", "aria-label": "reference.yaml, read only; secrets show as <SECRET>" }, P.yaml);
  P.work = el("div", { class: "pick-grid" },
    el("div", { class: "pick-left" },
      el("div", { class: "tabs", role: "tablist", "aria-label": "View" }, P.tabSections, P.tabTable),
      P.sections, P.table),
    el("section", { class: "panel pick-right", "aria-labelledby": "pick-yaml-title" },
      el("div", { class: "panel-head" }, el("h2", { id: "pick-yaml-title", text: "reference.yaml" }), el("span", { class: "muted", text: "ticked values carry their column" })),
      P.yamlBox));
  P.siteSearch = el("input", { type: "search", "aria-label": "Other sites: hostname, nePk, model or site contains", placeholder: "Other sites: hostname, nePk, model or site" });
  P.siteFind = el("button", { class: "btn", type: "submit", text: "Find sites" });
  P.siteList = el("tbody");
  P.siteText = el("p", { class: "muted", role: "status" });
  P.compareGo = el("button", { class: "btn primary", type: "button", text: "Compare with the ticked sites" });
  P.compare = el("details", { class: "pick-compare" },
    el("summary", { text: "Compare with other sites (1 to 4, GET only)" }),
    el("form", { class: "toolbar" }, P.siteSearch, P.siteFind), P.siteText,
    el("div", { class: "table-wrap" }, el("table", null, el("caption", { class: "sr-only", text: "Other sites on the build's Orchestrator" }),
      el("thead", null, el("tr", null, ...["Compare", "Hostname", "nePk", "Model", "Site"].map((name) => el("th", { scope: "col", text: name })))), P.siteList)),
    el("div", { class: "toolbar" }, P.compareGo));
  P.tools = el("div", { class: "panel" },
    P.compare,
    el("div", { class: "toolbar" }, el("span", { class: "muted", text: "Start from" }), P.starts),
    el("div", { class: "toolbar" }, P.search, el("label", { class: "check", for: "pick-only" }, P.only, "Show only ticked"), P.save),
    P.status);
  P.saveName = el("input", { type: "text", id: "pick-save-name", autocomplete: "off", spellcheck: "false" });
  P.saveText = el("p", { class: "muted" });
  P.saveLog = el("ol", { class: "log mono", "aria-label": "What the command printed" });
  P.saveGo = el("button", { class: "btn primary", type: "submit", text: "Save" });
  P.saveCancel = el("button", { class: "btn", type: "button", text: "Close" });
  P.dialog = el("dialog", { "aria-labelledby": "pick-save-title" },
    el("form", { method: "dialog", class: "gate" },
      el("h2", { id: "pick-save-title", text: "Save template and CSV" }),
      el("label", { for: "pick-save-name", text: "Site type name" }), P.saveName, P.saveText, P.saveLog,
      el("div", { class: "actions" }, P.saveGo, P.saveCancel)));
  P.view.append(el("a", { class: "back", href: "#/" }, "← All tasks"), P.eyebrow, P.title, P.lede, P.chooser, P.tools, P.work);
  document.querySelector("main .wrap").append(P.view);
  document.body.append(P.dialog);

  P.search.addEventListener("input", () => { pick.query = P.search.value.trim().toLowerCase(); draw(); });
  P.only.addEventListener("change", () => { pick.onlyOn = P.only.checked; draw(); });
  for (const tab of [P.tabSections, P.tabTable]) {
    tab.addEventListener("click", () => setTab(tab === P.tabTable ? "table" : "sections"));
    tab.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      setTab(pick.tab === "table" ? "sections" : "table");
      (pick.tab === "table" ? P.tabTable : P.tabSections).focus();
    });
  }
  P.save.addEventListener("click", openSave);
  P.siteFind.closest("form").addEventListener("submit", (event) => { event.preventDefault(); findSites(); });
  P.compareGo.addEventListener("click", startCompare);
  P.saveCancel.addEventListener("click", () => P.dialog.close());
  P.dialog.querySelector("form").addEventListener("submit", (event) => { event.preventDefault(); save(); });
})();

function setTab(tab) {
  pick.tab = tab;
  P.tabSections.setAttribute("aria-selected", String(tab === "sections"));
  P.tabTable.setAttribute("aria-selected", String(tab === "table"));
  P.tabSections.tabIndex = tab === "sections" ? 0 : -1;
  P.tabTable.tabIndex = tab === "table" ? 0 : -1;
  P.sections.hidden = tab !== "sections";
  P.table.hidden = tab !== "table";
}

/* opening a build */
async function openPick(encoded, comparing) {
  const build = decodeURIComponent(encoded);
  pick.route = comparing ? "compare" : "variables";
  P.compare.open = comparing;
  const orch = current();
  P.eyebrow.textContent = orch ? nameOf(orch) : "";
  P.chooser.hidden = !!build;
  P.tools.hidden = P.work.hidden = !build;
  if (!build) return listBuildsToPick();
  if (build === pick.build) return pick.rows.length ? draw() : undefined;  // open already, or still loading
  Object.assign(pick, { build, key: encodeURIComponent(build), start: "top10", extras: new Set(), unticks: new Set(), good: null, rows: [], columns: [],
    ref: null, pending: new Map(), notes: new Map(), message: "", siteType: "", sites: [], captures: null, orchestrator: "" });
  P.siteList.replaceChildren();
  P.siteText.textContent = "Find the other sites of this design on the build's Orchestrator, tick up to 4, then compare.";
  const opened = ++pick.opened;
  P.lede.textContent = "Build " + build + ". Tick the settings that differ from site to site: each becomes a column of the CSV. The rest stay as this appliance has them.";
  P.status.textContent = "Loading the settings…";
  P.yaml.replaceChildren();
  drawStarts();
  setTab(pick.tab);
  try {
    const [ref, builds] = await Promise.all([api("/api/builds/" + pick.key + "/reference"), api("/api/builds")]);
    if (opened !== pick.opened) return;
    pick.ref = ref;
    const found = builds.builds.find((item) => item.appliance === build);
    pick.siteType = found ? found.site_type : "";
    pick.orchestrator = found ? found.orchestrator : "";
    drawYaml();
  } catch (error) {
    P.status.textContent = "Could not open " + build + ": " + error.message;
    return;
  }
  preview();
}

async function listBuildsToPick() {
  P.lede.textContent = "Choose an earlier build. Build a template from an appliance first if there is none.";
  P.choose.replaceChildren(el("li", { class: "muted", text: "Looking for earlier builds…" }));
  try {
    const data = await api("/api/builds");
    P.choose.replaceChildren(...(data.builds.length ? data.builds.map((build) => el("li", null,
      el("a", { href: "#/" + pick.route + "/" + encodeURIComponent(build.appliance), text: build.appliance }),
      el("span", { class: "muted", text: " · " + build.orchestrator + " · " + String(build.built || "").replace("T", " ") })))
      : [el("li", null, el("a", { href: "#/build", text: "No builds yet: build a template from an appliance" }))]));
  } catch (error) {
    P.choose.replaceChildren(el("li", { class: "tone-bad", text: "Could not list the builds: " + error.message }));
  }
}

function drawStarts() {
  P.starts.replaceChildren(...PICK_STARTS.filter((start) => !start.available || start.available()).map((start) => {
    const button = el("button", { class: "btn", type: "button", "aria-pressed": String(start.id === pick.start), text: start.label });
    button.addEventListener("click", () => { pick.start = start.id; drawStarts(); preview(); });
    return button;
  }));
}

/* a flip, then the dry run (debounced; one at a time, the newest state wins) */
function flip(row, want) {
  const path = row.path;
  if (want) { if (pick.unticks.has(path)) pick.unticks.delete(path); else pick.extras.add(path); }
  else if (pick.extras.has(path)) pick.extras.delete(path);
  else pick.unticks.add(path);
  pick.pending.set(path, want);
  pick.notes.delete(path);
  pick.last = path;
  if (want) pick.scroll = path;  // scrolled to once the dry run gives its lines
  if (pick.busy) pick.dirty = true;  // the run in flight is older than this flip: its answer must not be drawn over it
  draw();
  clearTimeout(pick.timer);
  pick.timer = setTimeout(preview, 150);
}

async function preview() {
  if (pick.busy) { pick.dirty = true; return; }
  pick.busy = true;
  const opened = pick.opened;
  const sent = { start: pick.start, extras: new Set(pick.extras), unticks: new Set(pick.unticks), sites: pick.sites, captures: pick.captures };
  P.status.textContent = "Working out the columns…";
  let ran = null;
  try {  // the first compare reads every site (minutes): its "reading ..." lines say where it is
    ran = await runTask(pickRequest(), (event) => { if (event.line && event.line.startsWith("reading ")) P.status.textContent = event.line; });
  } catch (error) {
    pick.message = "Could not run the preview: " + error.message;
    if (GONE.test(error.message) && sent.captures === pick.captures) {
      Object.assign(pick, { captures: null, dirty: true });  // read the sites afresh, once: a run without captures_from is never refused so
      P.siteText.textContent = "The earlier read is gone; reading the sites again.";
    }
  }
  pick.busy = false;
  // only a run that read the sites itself: one sent with --captures records them even when they are gone
  if (ran && sent.start === "compare" && !sent.captures && sent.sites === pick.sites && ran.result.outputs.some((item) => item.kind === "capture")) {
    pick.captures = ran.task.id;  // also from a refused run: the sites were read before the flips were checked
  }
  if (pick.dirty) { pick.dirty = false; return preview(); }  // more flips (or another build) came in: only the newest state is drawn
  if (opened !== pick.opened) return;
  const result = ran && ran.result;
  const picked = result && result.status === "done" && result.outputs.find((item) => item.kind === "picker");
  if (picked) {
    Object.assign(pick, { rows: picked.rows, columns: picked.columns, good: sent, message: "" });
    const ticked = picked.rows.find((row) => row.path === pick.scroll);
    if (ticked) setTimeout(() => scrollYaml(ticked));
  } else if (result) {  // refused (a locked row, a follower): back to the last state the CLI accepted, with its reason at the row
    const why = errorLine(result.text);
    if (pick.last) pick.notes.set(pick.last, why);
    if (pick.good) Object.assign(pick, { start: pick.good.start, extras: new Set(pick.good.extras), unticks: new Set(pick.good.unticks) });
    pick.message = why;
    drawStarts();
  }
  pick.pending.clear();
  pick.scroll = null;
  draw();
}

/* drawing what the dry run returned */
function isOn(row) { return pick.pending.has(row.path) ? pick.pending.get(row.path) : row.on; }

function matches(row) {
  if (pick.onlyOn && !isOn(row)) return false;
  if (!pick.query) return true;
  return [row.label, row.path, row.value, row.column].some((text) => String(text || "").toLowerCase().includes(pick.query));
}

function groups() {
  const followers = new Map();
  for (const row of pick.rows) if (row.follows) followers.set(row.follows, (followers.get(row.follows) || []).concat(row));
  const locked = pick.rows.filter((row) => row.locked && !row.follows);
  const free = pick.rows.filter((row) => !row.locked && !row.follows).sort((a, b) => a.n - b.n);
  const names = [...new Set(free.map((row) => row.section))].sort();
  return { followers, sections: [{ name: "Always per site", rows: locked, locked: true }].concat(names.map((name) => ({ name, rows: free.filter((row) => row.section === name) }))) };
}

function draw() {
  const focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.pick : null;
  const { followers, sections } = groups();
  const total = pick.columns.length;
  P.status.textContent = pick.message ? pick.message : pick.rows.length
    ? total + " column" + (total === 1 ? "" : "s") + " per site: " + pick.columns.join(", ") : P.status.textContent;
  P.status.className = pick.message ? "tone-bad" : "muted";
  P.sections.replaceChildren(...sections.filter((section) => section.rows.length).map((section) => {
    const on = section.rows.filter(isOn).length;
    const shown = section.rows.filter((row) => matches(row) || (followers.get(row.path) || []).some(matches));
    const details = el("details", { class: "panel pick-section" + (section.locked ? " locked" : "") },
      el("summary", null, el("span", { class: "name", text: section.name }), el("span", { class: "muted", text: on + " of " + section.rows.length + " per site" })),
      shown.length ? el("ul", { class: "rows" }, ...shown.map((row) => rowItem(row, followers, "s"))) : el("p", { class: "muted", text: "Nothing here matches." }));
    details.open = !pick.closed.has(section.name);
    details.addEventListener("toggle", () => { if (details.open) pick.closed.delete(section.name); else pick.closed.add(section.name); });
    return details;
  }));
  const ordered = [];
  for (const section of sections) for (const row of section.rows) { ordered.push(row); for (const f of followers.get(row.path) || []) ordered.push(f); }
  P.tbody.replaceChildren(...ordered.filter(matches).map((row) => tableRow(row)));
  drawYamlMarks();
  if (focused) {
    const again = document.querySelector('[data-pick="' + CSS.escape(focused) + '"]');
    if (again) again.focus();
  }
}

function box(row, where) {
  const input = el("input", { type: "checkbox", "data-pick": where + ":" + row.path, "aria-label": row.label + (row.locked ? ", always per site" : "") });
  input.checked = isOn(row);
  input.addEventListener("change", () => flip(row, input.checked));
  return input;
}

function hoverable(node, row) {
  node.addEventListener("mouseenter", () => markHover(row, true));
  node.addEventListener("mouseleave", () => markHover(row, false));
  node.addEventListener("focusin", () => markHover(row, true));
  node.addEventListener("focusout", () => markHover(row, false));
  return node;
}

function follower(row) {
  const target = pick.rows.find((item) => item.path === row.follows);
  return "follows " + (target ? target.column || target.label : row.follows);
}

function rowItem(row, followers, where) {
  const on = isOn(row);
  const note = pick.notes.get(row.path);
  const item = el("li", { class: "row" + (on ? " on" : "") + (row.follows ? " follower" : "") },
    el("label", { class: "check" }, box(row, where), el("span", { class: "setting", text: row.label })),
    el("span", { class: "value mono", title: String(row.value), text: row.value === "" ? '""' : row.value }),
    el("span", { class: "pct", text: row.pct == null ? "" : row.pct + "% different" }),
    el("span", { class: "column mono", text: on ? (pick.pending.has(row.path) ? "…" : row.column) : "" }),
    row.locked ? el("span", { class: "lock", text: "🔒 always per site: " + row.locked_why }) : null,
    row.follows ? el("span", { class: "lock", text: follower(row) }) : null,
    note ? el("span", { class: "why tone-bad", role: "status", text: note }) : null,
    (followers.get(row.path) || []).length ? el("ul", { class: "rows" }, ...followers.get(row.path).filter(matches).map((f) => rowItem(f, followers, where))) : null);
  return hoverable(item, row);
}

function tableRow(row) {
  const on = isOn(row);
  const note = pick.notes.get(row.path);
  return hoverable(el("tr", { class: on ? "on" : "" },
    el("td", { class: "mono", text: row.n == null ? "-" : String(row.n) }),
    el("td", null, box(row, "t")),
    el("td", null, el("span", { text: (row.follows ? "↳ " : "") + row.label }),
      row.locked ? el("div", { class: "lock", text: "🔒 always per site: " + row.locked_why }) : null,
      row.follows ? el("div", { class: "lock", text: follower(row) }) : null,
      note ? el("div", { class: "why tone-bad", text: note }) : null),
    el("td", { class: "mono", text: row.value === "" ? '""' : row.value }),
    el("td", { text: row.pct == null ? "" : row.pct + "%" }),
    el("td", { class: "mono", text: on ? (pick.pending.has(row.path) ? "…" : row.column) : "" })), row);
}

/* the YAML viewer: read only, secrets already <SECRET> from the server */
function drawYaml() {
  pick.lineEls = pick.ref.text.split("\n").map((line, index) => el("li", { "data-n": String(index + 1) }, el("span", { class: "text", text: line })));
  if (pick.lineEls.length && pick.ref.text.endsWith("\n")) pick.lineEls.pop();
  P.yaml.replaceChildren(...pick.lineEls);
}

function linesOf(row) { return row.lines || null; }  // every row has its range from the dry run (R9a)

function drawYamlMarks() {
  for (const li of pick.lineEls) { li.classList.remove("on"); const tag = li.querySelector(".tag"); if (tag) tag.remove(); }
  const tags = new Map();
  for (const row of pick.rows) {
    if (!row.on || !row.lines || pick.pending.has(row.path)) continue;
    for (let n = row.lines[0]; n <= row.lines[1]; n++) if (pick.lineEls[n - 1]) pick.lineEls[n - 1].classList.add("on");
    tags.set(row.lines[0], (tags.get(row.lines[0]) || []).concat(row.column));
  }
  for (const [n, names] of tags) if (pick.lineEls[n - 1]) pick.lineEls[n - 1].append(el("span", { class: "tag", text: [...new Set(names)].join(", ") }));
}

function markHover(row, on) {
  const lines = linesOf(row);
  if (!lines) return;
  for (let n = lines[0]; n <= lines[1]; n++) if (pick.lineEls[n - 1]) pick.lineEls[n - 1].classList.toggle("hover", on);
  if (on) scrollYaml(row, true);
}

function scrollYaml(row, onlyIfHidden) {
  const lines = linesOf(row);
  const li = lines && pick.lineEls[lines[0] - 1];
  if (!li) return;
  const boxEl = P.yamlBox;
  const top = li.offsetTop, bottom = top + li.offsetHeight;
  if (onlyIfHidden && top >= boxEl.scrollTop && bottom <= boxEl.scrollTop + boxEl.clientHeight) return;
  boxEl.scrollTop = Math.max(0, top - boxEl.clientHeight / 2);
}

/* Save: the same command with --name N --yes; then the two files the person keeps are downloaded */
function openSave() {
  P.saveName.value = pick.siteType || pick.build.toLowerCase();
  P.saveText.textContent = "Writes site-<name>/ in " + pick.build + (pick.start === "compare"
    ? ", with a row per compared site, then Orchestrator /validate checks every row. Then "
    : ", only if Orchestrator /validate accepts this appliance's own row. Then ") + "your browser downloads <name>.jinja2 and sites.csv; they also stay in the site folder.";
  P.saveLog.replaceChildren();
  P.dialog.showModal();
  P.saveName.focus();
  P.saveName.select();
}

async function save() {
  const name = P.saveName.value.trim();
  const request = pickRequest({ save: true, name: name || null });
  P.saveLog.replaceChildren();
  P.saveText.textContent = "Saving: building the template and validating the example row…";
  let ran;
  try {
    ran = await runTask(request, (event) => { if (event.line != null) P.saveLog.append(el("li", { text: event.line })); });
  } catch (error) {
    P.saveText.textContent = "Not saved: " + error.message;
    if (GONE.test(error.message)) pick.captures = null;  // the next preview reads the sites again
    return;
  }
  const picked = ran.result.outputs.find((item) => item.kind === "picker" && item.folder);  // recorded once the site folder is written
  if (!picked) {
    P.saveText.textContent = "Not saved: " + errorLine(ran.result.text);
    return;
  }
  const site = picked.folder.split(/[\\/]/).pop().replace(/^site-/, "");
  const saved = ran.result.outputs.find((item) => item.kind === "saved");  // compare also validates every site's row
  const stopped = ran.result.exit_code === 2;  // render STOPPED (validator refused, network): the rows after it were not checked
  P.saveText.textContent = "Saved site-" + site + " in " + pick.build + ". Downloading " + site + ".jinja2 and sites.csv." +
    (saved && !saved.validated ? (stopped ? " Validation stopped, so not every row was checked: " : " Not every row passed validation: ") + errorLine(ran.result.text) : "");
  const at = encodeURIComponent(site);
  for (const href of ["/api/builds/" + pick.key + "/" + at + "/template", "/api/builds/" + pick.key + "/" + at + "/csv"]) {
    const link = el("a", { href, download: "" });
    document.body.append(link);
    link.click();
    link.remove();
  }
  if (typeof loadBuilds === "function") loadBuilds();
}

/* compare: the other sites come from `list` on the build's Orchestrator */
async function findSites() {
  if (!pick.orchestrator) { P.siteText.textContent = "This build names no Orchestrator."; return; }
  P.siteText.textContent = "Listing the appliances on " + pick.orchestrator + "…";
  const asked = ++pick.finding, opened = pick.opened;
  let ran;
  try {
    ran = await runTask({ action: "list", orchestrator: pick.orchestrator, search: P.siteSearch.value.trim() || null });
  } catch (error) {
    if (asked === pick.finding && opened === pick.opened) P.siteText.textContent = "Could not list them: " + error.message;
    return;
  }
  if (asked !== pick.finding || opened !== pick.opened) return;  // a newer Find, or another build: this answer is not for this screen
  const found = ran.result.outputs.filter((item) => item.kind === "appliance");
  P.siteText.textContent = ran.result.status !== "done" ? "Could not list them: " + errorLine(ran.result.text)
    : found.length + " appliance" + (found.length === 1 ? "" : "s") + ". Tick up to 4 of the same design; not " + pick.build + " itself.";
  P.siteList.replaceChildren(...found.map((item) => {
    const box = el("input", { type: "checkbox", "aria-label": "Compare with " + (item.hostname || item.nepk) });
    box.site = { hostname: item.hostname, nepk: item.nepk };
    return el("tr", null, el("td", null, box), ...["hostname", "nepk", "model", "site"].map((key) => el("td", { class: key === "nepk" ? "mono" : "", text: item[key] })));
  }));
}

function startCompare() {
  const chosen = [...P.siteList.querySelectorAll("input:checked")].map((box) => box.site);
  if (!chosen.length) { P.siteText.textContent = "Tick at least one other site first."; return; }
  Object.assign(pick, { sites: chosen, captures: null, start: "compare" });  // a new set of sites is read afresh
  P.siteText.textContent = "Comparing with " + chosen.map((site) => site.hostname || site.nepk).join(", ") + ".";
  drawStarts();
  preview();
}

SCREENS.push({ hash: /^#\/(variables|compare)(?:\/(.+))?$/, view: "view-pick", render: (match) => openPick(match[2] || "", match[1] === "compare") });

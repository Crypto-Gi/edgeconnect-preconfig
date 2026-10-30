// "Make the preconfig files" (docs/gui/BUILD_PLAN.md step 9), and what yaml.js shares with it: a task followed over its event
// stream, the approve dialog (automation 7af5e9d's, with preconfig's one gate: the nickname typed), the row table and the
// downloads. Load after app.js and tables.js. Python matches the template, checks every row and decides every refusal; the
// browser only sends the two files and shows what comes back, every value written with textContent.
"use strict";

// FINAL_STATES comes from pick.js, loaded before this file.
const STATE_WORDS = { queued: "Waiting for another run", running: "Running", awaiting_approval: "Waiting for your approval", uploading: "Uploading",
  done: "Done", failed: "Finished with problems", rejected: "Refused", expired: "Expired", cancelled: "Cancelled" };

// A screen section built here, so index.html only loads the script (steps 8 and 10 change that page too).
function screenSection(id, title, eyebrow, lede, ...panels) {
  const section = el("section", { id, class: "view", "aria-labelledby": id + "-title", hidden: "" },
    el("a", { class: "back", href: "#/", text: "← All tasks" }), eyebrow, el("h1", { id: id + "-title", text: title }), el("p", { class: "lede", text: lede }), ...panels);
  $("main").querySelector(".wrap").append(section);
  return section;
}

// A file picker as automation shows it: the label is the button, the input stays in the tab order; the text is kept for the request.
function fileField(id, text, accept, multiple, onFiles) {
  const input = el("input", { type: "file", id, accept, class: "sr-only" });
  input.multiple = Boolean(multiple);
  const name = el("span", { class: "muted", text: "No file chosen" });
  input.addEventListener("change", async () => {
    const files = [...input.files];
    const texts = await Promise.all(files.map((file) => file.text()));
    files.forEach((file, index) => { state.work.files[file.name] = texts[index]; });  // the switch dialog offers them back
    name.textContent = files.length ? files.map((file) => file.name).join(", ") : "No file chosen";
    onFiles(files.map((file, index) => ({ name: file.name, text: texts[index] })));
  });
  return { row: el("div", { class: "toolbar" }, el("label", { class: "btn", for: id, text }), input, name), clear: () => { input.value = ""; name.textContent = "No file chosen"; } };
}

/* the approve dialog: the server checks the typed nickname; the dialog only collects it */
const approveUi = (() => {
  const band = el("div", { class: "orch-band" });
  const text = el("p", { id: "approve-text" });
  const gates = el("div");
  const clock = el("p", { class: "muted", "aria-live": "off" });
  const later = el("button", { class: "btn", type: "button", text: "Not yet" });
  const form = el("form", { method: "dialog" }, gates, clock, el("div", { class: "actions" }, el("button", { class: "btn primary", type: "submit", text: "Approve and upload" }), later));
  const dialog = el("dialog", { id: "approve-dialog", "aria-labelledby": "approve-title", "aria-describedby": "approve-text" },
    band, el("h2", { id: "approve-title", text: "Approve this upload" }), text, form);
  document.body.append(dialog);
  let timer = null;
  let sending = null;
  later.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => clearInterval(timer));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const answers = [...gates.querySelectorAll("input")].map((input) => input.value);
    dialog.close();
    if (sending) await sending(answers);
  });
  return {
    open(view, what, send) {
      const orch = state.fleet.find((item) => item.name === view.orchestrator) || { name: view.orchestrator, url: "" };
      band.replaceChildren(el("span", { text: nameOf(orch) + " " }), el("span", { class: "mono", text: "(" + [orch.name, orch.url].filter(Boolean).join(", ") + ")" }));
      text.textContent = "You are approving the upload of exactly " + what + " to " + nameOf(orch) + ". The approval works once, for these only; nothing is applied.";
      gates.replaceChildren(...view.approval.gates.map((gate, index) => el("label", { class: "gate", for: "approve-gate-" + index },
        el("span", { text: gate.prompt.replace(/:\s*$/, "") }),
        el("input", { type: "text", id: "approve-gate-" + index, autocomplete: "off", spellcheck: "false", required: "" }))));
      const tick = () => {
        const left = Math.max(0, Math.round((view.deadline || 0) - Date.now() / 1000));
        clock.textContent = "This approval expires in " + Math.floor(left / 60) + ":" + String(left % 60).padStart(2, "0") + ".";
      };
      tick();
      clearInterval(timer);
      timer = setInterval(tick, 1000);
      sending = send;
      if (!dialog.open) dialog.showModal();
      gates.querySelector("input").focus();
    },
    close() { if (dialog.open) dialog.close(); },
  };
})();

// What approving a CSV upload takes, in words ("2 of 4 rows"), from the counts Python sent.
function approvalWhat(approval) {
  const counts = approval.counts || {};
  return counts.ready + " of " + counts.rows + " rows (the ones that are ready; invalid and conflicting rows are not sent)";
}

/* one run, followed: its state, Python's note, the approval, downloads, the row table and the command's own lines */
function taskPanel(id) {
  const status = el("p", { role: "status" });
  const note = el("p", { class: "callout", hidden: "" });
  const approveText = el("p");
  const approveAgain = el("button", { class: "btn primary", type: "button", text: "Approve the upload…" });
  const cancel = el("button", { class: "btn", type: "button", text: "Cancel the upload" });
  const approveBox = el("div", { class: "callout gate", hidden: "" }, approveText, el("div", { class: "actions" }, approveAgain, cancel));
  const zip = el("a", { class: "btn", download: "", text: "Download the files (.zip)" });
  const downloads = el("div", { class: "actions", hidden: "" }, zip);
  const rows = el("div");
  const log = el("pre", { class: "mono" });
  const panel = el("section", { class: "panel", "aria-labelledby": id + "-title", hidden: "" },
    el("h2", { id: id + "-title", text: "Result" }), status, note, approveBox, downloads, rows,
    el("details", null, el("summary", { text: "What the command printed" }), log));
  let view = null;
  let source = null;
  let shownState = "";
  const table = new PagedTable({
    id: id + "-rows",
    label: "rows of this run",
    container: rows,
    path: () => view && view.result && view.result.rows ? "/api/tasks/" + view.id + "/rows" : "",
    problems: "Problems only",
    empty: "No rows yet.",
    filters: [{ key: "result", label: "All results" }],
    columns: [
      { key: "row", label: "Row" },
      { key: "item", label: "Name" },
      { key: "result", label: "Result", cell: (row) => toneCell(row.result, row.tone) },
      { key: "column", label: "Column", mono: true },
      { key: "value", label: "Value", mono: true },
      { key: "fix", label: "What to fix" },
      { key: "file", label: "File", cell: (row) => row.file ? el("a", { href: "/api/tasks/" + view.id + "/file?name=" + encodeURIComponent(row.file), download: "", text: row.file }) : "" },
    ],
  });
  rows.hidden = true;

  function approve(answers) {
    return post("/api/tasks/" + view.id + "/approve", { answers }).catch((error) => { status.textContent = "Could not send the approval: " + error.message; });
  }

  function update(next) {
    view = next;
    status.className = next.state === "done" ? "tone-ok" : FINAL_STATES.has(next.state) && next.state !== "done" ? "tone-bad" : "";
    status.textContent = (STATE_WORDS[next.state] || next.state) + (next.message ? ": " + next.message : "");
    note.hidden = !next.note;
    note.textContent = next.note || "";
    const waiting = next.state === "awaiting_approval" && next.approval;
    approveBox.hidden = !waiting;
    if (waiting) approveText.textContent = "Ready to upload " + approvalWhat(next.approval) + " to " + nameOf(state.fleet.find((orch) => orch.name === next.orchestrator) || { name: next.orchestrator }) + ". Nothing is sent until you approve.";
    const files = next.result && next.result.files;
    downloads.hidden = !(files && (FINAL_STATES.has(next.state) || waiting));
    zip.href = "/api/tasks/" + next.id + "/files.zip";
    zip.textContent = "Download the " + (files === 1 ? "file" : files + " files") + " this run wrote (.zip)";
    if (next.state !== shownState) {  // the table and the dialog follow state changes, not every printed line
      shownState = next.state;
      rows.hidden = !(next.result && next.result.rows);
      table.reset();
      table.load();
      if (waiting) approveUi.open(next, approvalWhat(next.approval), approve);
      else approveUi.close();
    }
  }

  approveAgain.addEventListener("click", () => view && approveUi.open(view, approvalWhat(view.approval), approve));
  cancel.addEventListener("click", () => view && post("/api/tasks/" + view.id + "/cancel").catch((error) => { status.textContent = error.message; }));

  return {
    panel,
    async start(body) {
      await this.reset();
      panel.hidden = false;
      status.className = "";
      status.textContent = "Starting…";
      let created;
      try {
        created = await post("/api/tasks", body);
      } catch (error) {  // Python refused the request before anything ran: its words, as they are
        status.className = "tone-bad";
        status.textContent = "Not started: " + error.message;
        return;
      }
      update(created);
      source = new EventSource("/api/tasks/" + created.id + "/events");
      source.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.line != null) log.append(data.line + "\n");
        update(data.task);
        if (FINAL_STATES.has(data.task.state)) { source.close(); loadTasks(); }
      };
      source.onerror = () => source.close();  // the stream ends with the task; a reconnect would replay it
      loadTasks();
    },
    async reset() {
      if (source) source.close();
      approveUi.close();
      const open = view && OPEN_STATES.has(view.state) ? view.id : "";
      view = null;
      shownState = "";
      log.textContent = "";
      panel.hidden = true;
      // a task left open would keep its Orchestrator's upload lock until its approval expires; one already uploading cannot be stopped
      if (open) await post("/api/tasks/" + open + "/cancel").catch(() => {});
    },
  };
}

// The validators, for "Validate on": the switcher's Orchestrator first when it is one; Python checks the choice again.
function validatorOptions(select) {
  const list = state.fleet.filter((orch) => orch.validator).sort((a, b) => (b.name === state.current) - (a.name === state.current));
  select.replaceChildren(...(list.length ? list.map((orch) => el("option", { value: orch.name, text: nameOf(orch) }))
    : [el("option", { value: "", text: "No validator in EC_VALIDATORS" })]));
}

function uploadsText(orch, onText) {
  return !orch ? "" : orch.writes ? "Uploads are on for " + nameOf(orch) + ": " + onText
    : "Uploads are off for " + nameOf(orch) + ": set " + orch.name + "_gui_preconfig_uploads=on in .env to allow them. Nothing at all is sent there until then.";
}

/* the screen */
const make = (() => {
  const files = { template: null, csv: null };
  const template = fileField("make-template", "Choose template…", ".jinja2,.j2,text/plain", false, (picked) => { files.template = picked[0] || null; });
  const csv = fileField("make-csv", "Choose CSV…", ".csv,text/csv", false, (picked) => { files.csv = picked[0] || null; });
  const choices = [
    ["create", "Create the YAML files", "One file per row, checked on this computer; no Orchestrator is called."],
    ["validate", "Create and validate", "As above, and every row through /validate on a validator; a row is written only if it passes."],
    ["upload", "Create, validate and upload", "As above on the Orchestrator chosen at the top, with name and serial conflicts checked; the ready rows are uploaded after your approval."],
  ];
  const radios = choices.map(([value, label, hint]) => {
    const input = el("input", { type: "radio", name: "make-choice", id: "make-choice-" + value, value });
    const option = el("label", { class: "check", for: "make-choice-" + value }, input, el("span", null, el("b", { text: label }), " ", el("span", { class: "muted", text: hint })));
    option.style.whiteSpace = "normal";  // the hint wraps on a narrow screen
    return option;
  });
  radios[0].querySelector("input").checked = true;
  const validateOn = el("select", { id: "make-validate-on" });
  const validateRow = el("div", { class: "toolbar", hidden: "" }, el("label", { for: "make-validate-on", text: "Validate on" }), validateOn);
  const uploads = el("p", { class: "callout gate", hidden: "" });
  const message = el("p", { class: "muted", role: "status" });
  const run = el("button", { class: "btn primary", type: "submit", text: "Make the files" });
  const result = taskPanel("make-result");
  const form = el("form", { class: "panel", "aria-labelledby": "make-files-title" },
    el("h2", { id: "make-files-title", text: "1. Your template and your CSV" }),
    el("p", { class: "muted", text: "Both are needed: the Jinja2 template of the site type and the CSV with one row per site. Nothing runs without both." }),
    template.row, csv.row,
    el("h2", { id: "make-what-title", text: "2. What to do" }),
    el("fieldset", null, el("legend", { class: "sr-only", text: "What to do" }), ...radios.map((radio) => el("div", null, radio))),
    validateRow, uploads, el("div", { class: "actions" }, run), message);
  const eyebrow = el("div", { class: "eyebrow" });
  screenSection("view-make", "Make the preconfig files", eyebrow,
    "From a template and a filled CSV: one YAML file per row, in a new folder of its own under preconfig-output/runs. A CSV with problems is fixed in the file and chosen again.",
    form, result.panel);
  let orchestrator = null;

  const choice = () => form.querySelector("input[name=make-choice]:checked").value;
  function refresh() {
    const orch = current();
    validateRow.hidden = choice() !== "validate";
    uploads.hidden = choice() !== "upload";
    uploads.textContent = uploadsText(orch, "the ready rows are shown first, and nothing is sent until you approve them by typing the Orchestrator's nickname.");
  }
  form.addEventListener("change", refresh);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!files.template || !files.csv) {
      message.textContent = "Choose both files first: " + [!files.template && "the template", !files.csv && "the CSV"].filter(Boolean).join(" and ") + ".";
      return;
    }
    message.textContent = "";
    const body = { action: "make", orchestrator: state.current, choice: choice(), template_text: files.template.text, csv_text: files.csv.text };
    if (body.choice === "validate" && validateOn.value) body.validate_on = validateOn.value;
    run.disabled = true;  // a double click starts one run, not two
    result.start(body).finally(() => { run.disabled = false; });
  });

  return {
    render() {
      if (orchestrator !== state.current) {  // switching Orchestrator starts fresh (app.js has already asked)
        orchestrator = state.current;
        files.template = files.csv = null;
        template.clear();
        csv.clear();
        result.reset();
        message.textContent = "";
      }
      eyebrow.textContent = nameOf(current()) || "No Orchestrator configured";
      validatorOptions(validateOn);
      refresh();
    },
  };
})();

SCREENS.push({ hash: /^#\/make$/, view: "view-make", render: () => make.render() });

// "Build a template from an appliance" (docs/gui/BUILD_PLAN.md step 8): pick the appliance from `list --search` (its recorded
// rows, R8), the validator
// from the Orchestrators in EC_VALIDATORS, then `build --orchestrator N --appliance A [--validate-on V] [--target-release R]
// [--target-ecos E]`. Progress comes from the capture's own request log, then the build's printed lines; the result is the
// build's summary.md as text. Every value is written with textContent. Loaded after app.js and pick.js (runTask).
"use strict";

const B = { chosen: null, running: false };
(function buildBuildView() {
  B.view = el("section", { id: "view-build", class: "view", "aria-labelledby": "build-title", hidden: "" });
  B.eyebrow = el("div", { class: "eyebrow" });
  B.search = el("input", { type: "search", id: "build-search", "aria-label": "Hostname, nePk, model or site contains", placeholder: "Hostname, nePk, model or site" });
  B.find = el("button", { class: "btn", type: "submit", text: "Find appliances" });
  B.listText = el("p", { class: "muted", role: "status" });
  B.list = el("tbody");
  B.validator = el("select", { id: "build-validator" });
  B.validatorText = el("p", { class: "muted" });
  B.release = el("input", { type: "text", id: "build-release", autocomplete: "off", spellcheck: "false", placeholder: "e.g. 9.7.1.42046" });
  B.ecos = el("input", { type: "text", id: "build-ecos", autocomplete: "off", spellcheck: "false", placeholder: "e.g. 9.6.2.1" });
  B.go = el("button", { class: "btn primary", type: "submit", text: "Build" });
  B.goText = el("p", { class: "muted", role: "status" });
  B.progress = el("p", { class: "mono", role: "status" });
  B.log = el("ol", { class: "log mono", tabindex: "0", "aria-label": "What the build printed" });  // it scrolls: focusable, so a keyboard can scroll it
  B.run = el("section", { class: "panel", "aria-labelledby": "build-run-title", hidden: "" },
    el("h2", { id: "build-run-title", text: "Progress" }), B.progress, B.log);
  B.verdict = el("p", { class: "lede" });
  B.summary = el("pre", { class: "summary mono", tabindex: "0", role: "region", "aria-label": "summary.md" });  // a pre has no role: the region carries the name
  B.next = el("a", { class: "btn primary", href: "#/variables", text: "Choose what changes per site" });
  B.result = el("section", { class: "panel", "aria-labelledby": "build-result-title", hidden: "" },
    el("div", { class: "panel-head" }, el("h2", { id: "build-result-title", text: "Result" }), B.next), B.verdict, B.summary);
  const field = (id, label, input, hint) => el("div", { class: "field" }, el("label", { for: id, text: label }), input, hint ? el("p", { class: "muted", text: hint }) : null);
  B.view.append(
    el("a", { class: "back", href: "#/" }, "← All tasks"), B.eyebrow,
    el("h1", { id: "build-title", text: "Build a template from an appliance" }),
    el("p", { class: "lede", text: "Reads one appliance (GET only, about a minute), turns it into a preconfig template and keeps it only if Orchestrator /validate accepts it." }),
    el("section", { class: "panel", "aria-labelledby": "build-pick-title" },
      el("h2", { id: "build-pick-title", text: "1. Pick the appliance" }),
      el("form", { class: "toolbar", role: "search" }, B.search, B.find), B.listText,
      el("div", { class: "table-wrap" }, el("table", null, el("caption", { class: "sr-only", text: "Appliances on this Orchestrator" }),
        el("thead", null, el("tr", null, ...["Build from", "Hostname", "nePk", "Model", "ECOS", "Site", "Reachable"].map((name) => el("th", { scope: "col", text: name })))), B.list))),
    el("form", { class: "panel", "aria-labelledby": "build-options-title" },
      el("h2", { id: "build-options-title", text: "2. Validate and build" }),
      field("build-validator", "Validate on", B.validator), B.validatorText,
      field("build-release", "Target Orchestrator release (optional)", B.release, "Only when the validator's key may not read its release."),
      field("build-ecos", "Target appliance ECOS (optional)", B.ecos, "The ECOS release of the appliances the preconfig is for."),
      el("div", { class: "toolbar" }, B.go), B.goText),
    B.run, B.result);
  document.querySelector("main .wrap").append(B.view);
  B.find.closest("form").addEventListener("submit", (event) => { event.preventDefault(); findAppliances(); });
  B.go.closest("form").addEventListener("submit", (event) => { event.preventDefault(); startBuild(); });
})();

function openBuild() {
  const orch = current();
  B.eyebrow.textContent = orch ? nameOf(orch) : "No Orchestrator configured";
  const validators = state.fleet.filter((item) => item.validator);
  const keep = B.validator.value;
  B.validator.replaceChildren(...(validators.length ? validators.map((item) => el("option", { value: item.name, text: nameOf(item) }))
    : [el("option", { value: "", text: "No validator Orchestrator" })]));
  B.validator.value = validators.some((item) => item.name === keep) ? keep : orch && orch.validator ? orch.name : validators.length ? validators[0].name : "";
  B.validatorText.textContent = validators.length ? "Only Orchestrators listed in EC_VALIDATORS in .env are offered: /validate runs nowhere else."
    : "No Orchestrator is listed in EC_VALIDATORS in .env, so a build cannot be validated. Add a lab Orchestrator there.";
  if (!B.list.children.length) B.listText.textContent = "Search, or press Find appliances to list them all.";
}

async function findAppliances() {
  const orch = current();
  if (!orch) { B.listText.textContent = "Add an Orchestrator to .env first."; return; }
  B.listText.textContent = "Listing the appliances on " + nameOf(orch) + "…";
  let ran;
  try {
    ran = await runTask({ action: "list", orchestrator: orch.name, search: B.search.value.trim() || null });
  } catch (error) {
    B.listText.textContent = "Could not list them: " + error.message;
    return;
  }
  const found = ran.result.outputs.filter((item) => item.kind === "appliance");  // what `list` recorded for each row it printed
  B.chosen = null;
  B.listText.textContent = ran.result.status !== "done" ? "Could not list them: " + errorLine(ran.result.text)
    : found.length + " appliance" + (found.length === 1 ? "" : "s") + (B.search.value.trim() ? " match." : ".");
  B.list.replaceChildren(...found.map((item, index) => {
    const radio = el("input", { type: "radio", name: "build-appliance", id: "build-appliance-" + index, "aria-label": "Build from " + (item.hostname || item.nepk) });
    radio.addEventListener("change", () => { B.chosen = item; B.goText.textContent = "Will build from " + (item.hostname || item.nepk) + " (" + item.nepk + ")."; });
    return el("tr", null, el("td", null, radio), ...["hostname", "nepk", "model", "ecos", "site", "reachable"].map((key) => el("td", { class: key === "nepk" ? "mono" : "", text: item[key] })));
  }));
}

async function startBuild() {
  const orch = current();
  if (!B.chosen) { B.goText.textContent = "Pick an appliance in step 1 first."; return; }
  if (B.running) { B.goText.textContent = "A build is running; it cannot be stopped midway."; return; }
  const validator = B.validator.value;
  B.running = true;
  B.run.hidden = false;
  B.result.hidden = true;
  B.log.replaceChildren();
  B.progress.textContent = "Starting…";
  B.goText.textContent = "Building from " + (B.chosen.hostname || B.chosen.nepk) + ".";
  let ran;
  try {
    ran = await runTask({ action: "build", orchestrator: orch.name, hostname: B.chosen.hostname, nepk: B.chosen.nepk,
      validate_on: validator && validator !== orch.name ? validator : null, target_release: B.release.value.trim() || null, target_ecos: B.ecos.value.trim() || null },
    (event) => {
      if (event.progress != null) B.progress.textContent = event.done + " request" + (event.done === 1 ? "" : "s") + " read · " + event.progress;
      if (event.line != null) B.log.append(el("li", { text: event.line }));
    });
  } catch (error) {
    B.running = false;
    B.goText.textContent = "Not started: " + error.message;
    return;
  }
  B.running = false;
  const made = ran.result.outputs.find((item) => item.kind === "build");
  B.result.hidden = false;
  if (ran.result.status !== "done" || !made) {
    B.progress.textContent = "Stopped.";
    B.verdict.textContent = "Not saved: " + errorLine(ran.result.text);
    B.summary.textContent = "";
    B.next.hidden = true;
    return;
  }
  const build = made.folder.split(/[\\/]/).pop();
  B.progress.textContent = "Done.";
  B.verdict.textContent = "Saved " + build + ", validated on " + made.validator + ". Verdict: " + made.verdict;
  B.next.hidden = false;
  B.next.setAttribute("href", "#/variables/" + encodeURIComponent(build));
  B.summary.textContent = "Loading summary.md…";
  try {
    const key = encodeURIComponent(build);
    B.summary.textContent = (await api("/api/builds/" + key + "/summary")).text;
  } catch (error) {
    B.summary.textContent = "Could not read summary.md: " + error.message;
  }
  if (typeof loadBuilds === "function") loadBuilds();
}

SCREENS.push({ hash: /^#\/build$/, view: "view-build", render: openBuild });

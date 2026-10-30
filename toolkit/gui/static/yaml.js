// "Check or upload existing YAML files" (docs/gui/BUILD_PLAN.md step 9): validate --yaml, and upload --yaml as one button with
// no typed nickname (D9, decision 0017), refused by Python unless uploads are on for the Orchestrator. Load after make.js,
// whose task panel and file picker it uses. Python checks every file; every value is written with textContent.
"use strict";

const yamlScreen = (() => {
  let picked = [];
  const files = fileField("yaml-files", "Choose YAML files…", ".yml,.yaml,text/yaml", true, (chosen) => { picked = chosen; });
  const name = el("input", { type: "text", id: "yaml-name", autocomplete: "off", spellcheck: "false" });
  const serial = el("input", { type: "text", id: "yaml-serial", autocomplete: "off", spellcheck: "false" });
  const uploads = el("p", { class: "callout gate" });
  const message = el("p", { class: "muted", role: "status" });
  const check = el("button", { class: "btn", type: "button", text: "Check the files" });
  const send = el("button", { class: "btn primary", type: "button", text: "Upload the files" });
  const result = taskPanel("yaml-result");
  const eyebrow = el("div", { class: "eyebrow" });
  screenSection("view-yaml", "Check or upload existing YAML files", eyebrow,
    "Preconfig YAML files you already have: each is checked as a CSV row is (its shape, its <SECRET:...> placeholders, /validate, name and serial conflicts). Checking writes nothing; uploading sends every file that passes, never applied.",
    el("section", { class: "panel", "aria-labelledby": "yaml-files-title" },
      el("h2", { id: "yaml-files-title", text: "Your files" }),
      files.row,
      el("p", { class: "muted", text: "With one file you may give its preconfig name (default: the file name without .yml) and the appliance serial number." }),
      el("div", { class: "toolbar" }, el("label", { for: "yaml-name", text: "Preconfig name" }), name, el("label", { for: "yaml-serial", text: "Serial number" }), serial),
      uploads, el("div", { class: "actions" }, check, send), message),
    result.panel);
  let orchestrator = null;

  function start(action) {
    if (!picked.length) {
      message.textContent = "Choose at least one YAML file first.";
      return;
    }
    message.textContent = "";
    check.disabled = send.disabled = true;  // a double click starts one run, not two
    result.start({ action, orchestrator: state.current, files: picked.map((file) => file.text), name: name.value.trim(), serial: serial.value.trim() })
      .finally(() => { check.disabled = send.disabled = false; });
  }
  check.addEventListener("click", () => start("validate_yaml"));
  send.addEventListener("click", () => start("upload_yaml"));

  return {
    render() {
      if (orchestrator !== state.current) {  // switching Orchestrator starts fresh
        orchestrator = state.current;
        picked = [];
        files.clear();
        name.value = serial.value = "";
        result.reset();
        message.textContent = "";
      }
      const orch = current();
      eyebrow.textContent = nameOf(orch) || "No Orchestrator configured";
      uploads.textContent = [uploadsText(orch, "Upload sends every file that passes the checks, with no further question (decision 0017)."), orch && !orch.validator ? "Checking needs a validator: " + nameOf(orch) + " is not in EC_VALIDATORS." : ""].filter(Boolean).join(" ");
    },
  };
})();

SCREENS.push({ hash: /^#\/yaml$/, view: "view-yaml", render: () => yamlScreen.render() });

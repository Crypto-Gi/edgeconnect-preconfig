# Preconfig GUI build plan

The preconfig GUI (`edgeconnect-preconfig gui`) is built in the steps below, one pull request per step into the `gui`
branch. Each step starts only after the previous one is merged there. `main` stays untouched until the owner decides to
merge `gui` into it. This file lands as `docs/gui/BUILD_PLAN.md` in the step 0 pull request, the same name and shape as
the automation repo's, and each step gets a "Built as" note when it is merged.

Decision numbers (D1...) refer to the owner's rulings of 29 and 30 Oct 2026; D9 is one ruling that accepted a group of
recommendations, so it is cited for several of them. Numbered decisions (0006, 0017...) are the records in `docs/decisions/`.

## Decided

What the GUI is
- A local web GUI for edgeconnect-preconfig that does everything the CLI does: list, build, choose what changes per site,
  compare, check a sites CSV and write the YAML files, upload, and check or upload existing YAML files (D1).
- It lives in this repo and ships with preconfig (D2). The automation GUI is not changed. One GUI for all the tools may
  come later; nothing here may make that harder (same keys, same run-log format, same look).
- It works the way the automation GUI works (D3): every action builds a normal CLI command line and runs it through the
  CLI's own code, in the same process, with its own run context (`gui_tasks` -> `engine.run(argv)` -> the CLI). The GUI
  never calls inner functions, never re-implements a check, and the browser only shows what Python decided.
- The CLI's behaviour stays exactly the same: the GUI only adds. Golden copies of today's CLI output are recorded first
  and must stay byte-identical through every step.
- Same look and feel as the automation GUI, copied exactly (D4): its `static/` fonts (Bricolage Grotesque, IBM Plex Sans,
  IBM Plex Mono, latin woff2 with their OFL licences), its `app.css` colour tokens and components (dark first plus a
  full light theme, 14px rounded panels, pill buttons, sticky blurred top bar, stat tiles, status shown by shape and
  word: ● green, ▲ amber, ■ red), its command palette (Ctrl/Cmd+K) and keyboard behaviour. The brand reads
  "EdgeConnect **Preconfig**". Copied files name the automation commit they came from (7af5e9d).
- Every Orchestrator in `.env` appears in the switcher and the fleet panel, checked the same way the automation GUI checks
  them (D5): all at once, one try of 5 s each, green / amber / red with a plain reason. The preconfig GUI also shows
  whether `/validate` may run there (`EC_VALIDATORS`) and whether GUI uploads are on.

Writes
- The only Orchestrator writes are uploads, and nothing ever applies a preconfig (decision 0008); every upload sends
  `autoApply: false`. (`/validate` is a POST that changes nothing; it runs only where the rules below allow it.)
- GUI uploads have their own switch per Orchestrator, `<nickname>_gui_preconfig_uploads=on`, off by default (D6). It is not the
  automation GUI's `<nickname>_gui_writes`.
- A CSV upload needs the same approve dialog as the automation GUI: the Orchestrator's colour band and label, its nickname
  typed, single use, bound to the exact checked rows and the Orchestrator's identity, valid 10 minutes (D9). It is what lets
  the GUI add `--approve`. An upload of existing YAML files is one button, as `upload --yaml` needs no `--approve`
  (decision 0017); it still needs `<nickname>_gui_preconfig_uploads=on`.
- No Undo for uploads (D7).

Work and records
- Results live where the CLI puts them, in `preconfig-output/`, and stay after the GUI stops; the home page lists earlier
  builds and site types so a person can come back after filling a CSV (D9). Uploaded files (template, CSV) are saved in the run's
  own new folder (see "Engineering rules"), never over an earlier file.
- Coming back later (owner, 30 Oct): nothing the person made is lost when the GUI stops. Every earlier build and site type
  on the home page has [Download template] and [Download CSV] at any time, and every "make the files" run in "See what
  changed" has [Download the YAML files] (its .zip, rebuilt from `preconfig-output/runs/<run_id>/`) and its CSV and template.
  Unlike the automation GUI, the preconfig GUI keeps no work in a temporary folder.
- A run log in the automation format: one JSON line per run in `reports/<workflow>.jsonl`, with `run_id`, `timestamp`,
  `orchestrator`, `workflow`, `status`, `changes_written`, `row_outcomes`, `duration_seconds`, `source` (`cli` or `gui`)
  and per-row `row`, `item`, `scope`, `outcome`, `reason` (D9). The CLI writes it too. "See what changed" reads it.
- Logs and results are tables built on the server, sent a page at a time, with sort, filter, search, problems-only and CSV
  export.

Carried over from the automation GUI's decisions (26 Oct)
- Bulk only: the GUI never edits a CSV or a single setting in the browser. A CSV with problems is listed row by row with
  its column and fix, and the person fixes the file and imports it again.
- Switching Orchestrator resets the open work and asks first if something is loaded.
- Server: standard library only, 127.0.0.1 only, a launch link that works once and becomes an HttpOnly, SameSite=Strict
  session cookie (its name includes the port), an exact Host allowlist (localhost, 127.0.0.1, [::1] with the port), an
  Origin check and `application/json` on every POST, no CORS, strict security headers, no API key in any response.
- macOS, Linux and Windows through WSL (the Windows browser is opened via wslview or explorer.exe; the link is always printed).
- Zero new Python dependencies. Keyboard use and WCAG AA contrast in both themes.
- Do not build: API keys in the browser, a hosted or multi-user server, an AI chat box, anything the CLI cannot do.
- The GUI's files stay out of the agent skill copy (D9): they live in `toolkit/gui/`, which `tools/build_skill.py` does not copy.
- Lab runs use the lab Orchestrator and are driven by the owner's own browser automation (D9).

## The screens

Home: "What do you want to do?" task cards, the fleet panel, the tasks of this session, and the earlier builds in
`preconfig-output/` (appliance, Orchestrator, date, site types). Cards:

| Card | What it runs |
|---|---|
| Build a template from an appliance | pick an appliance (`list --search`), then `build --orchestrator N --appliance A [--validate-on V] [--target-release R] [--target-ecos E]`; the build result shows `summary.md`: the verdict and every item the YAML does not carry |
| Choose what changes per site | `variables FOLDER ...` (below) |
| Compare with other sites | `compare FOLDER SITE [SITE ...]`, then the same check-box screen, pre-ticked with what differs |
| Make the preconfig files | upload the template and the CSV, then create, create and validate, or create, validate and upload (below) |
| Check or upload existing YAML files | `validate --yaml ...` / `upload --yaml ... [--name N --serial S]` (one file) |
| Inventory | appliances (`list`) and the preconfigs already on the Orchestrator (`preconfigs`) |
| See what changed | the run log |

Choose what changes per site (D11 to D14)
- The check boxes are the rows of the build's `variables.csv`, through the CLI's own `Picker`: the same sections, the same
  Top 10 / Top 20 (from `references/variable-ranking.json`), the same locked rows and followers.
- Start from [Top 10] [Top 20] [All] (and [Different between your sites] after a compare). Top 10 is ticked when the screen
  opens, as in the CLI. Switching keeps the person's own extra ticks and unticks, re-applied on top of the new start.
- Left: one panel per section, headed by its name and "3 of 12 per site", collapsible, with search and "show only ticked".
  Each row: check box, setting, value now, "% different between sites", and the CSV column name once ticked. A follower
  sits under the row it follows ("follows lan0_ip").
- "Always per site" comes first: hostname, secrets and the Deployment page values (decision 0006), ticked and shown like
  every other row, not greyed (D14), with a lock mark and the reason. Unticking one says why it stays per site.
- A table tab shows the same rows like the CLI's tick list (D11); a flip in either view shows in the other.
- Right: a read-only YAML viewer of `reference.yaml` (secrets as `<SECRET>`). Ticked values are highlighted with their
  column name; ticking a box scrolls the viewer to its value; hovering a row highlights its line (D12). There is no
  selecting in the YAML.
- After each tick the GUI runs `variables FOLDER --pick P --set ... --fix ... --dry-run` (local, no Orchestrator call) and
  draws what it returns: ticks, column names, highlighted lines.
- [Save template and CSV] asks the site type name (default: the appliance's) and runs the same command with `--name N --yes`
  instead of `--dry-run`: `site-<name>/` is written only if Orchestrator `/validate` accepts the example row. Then the
  browser downloads the two files the person keeps (D16): `<name>.jinja2` and `sites.csv` (its first row is this appliance's
  own values with secrets blank, plus a row per compared site after a compare; the person adds one row per site). The same files also stay in `preconfig-output/<appliance>/site-<name>/`.

Make the preconfig files (D15, D16)
1. Upload both files: the Jinja2 template and the filled CSV. Both are required; without either nothing runs.
2. Choose what to do:
   - Create the YAML files: one `<preconfig_name>.yml` per row, checked offline, no Orchestrator call.
     Runs `render --template T --csv C --out RUN` (RUN: the run's own new folder).
   - Create and validate: as above, and every row through Orchestrator `/validate` (validators only); a row is written
     only if it passes. When the template matches a saved site folder this is the CLI's own `validate SITE --csv C`;
     otherwise `render --template T --csv C --out RUN --validate V --substitute`, V a validator, in lower case.
   - Create, validate and upload: as above, plus name and serial conflicts checked against the Orchestrator; the ready rows
     are shown, and only after the approve dialog (typed nickname, `<nickname>_gui_preconfig_uploads=on`) are they uploaded.
     The target is always the Orchestrator chosen in the switcher, passed as `--orchestrator P`, and it must have
     `<nickname>_gui_preconfig_uploads=on` before anything runs; that switch also allows the upload's own `/validate` check
     there, as the CLI's upload does (owner, 30 Oct, D20). With it off, nothing at all is sent to that Orchestrator. Runs `upload SITE --csv C --orchestrator P` for a matched template, else
     `upload --template T --csv C --out RUN --orchestrator P`; then the same with `--approve`.
3. A table, one line per row: written, invalid (with column, value and fix), conflict or uploaded; paged and filterable.
   A CSV with problems is fixed in the file and uploaded again.
4. Download one file or all as a .zip. The .zip holds only the files this run wrote (from its recorder), never older ones.

These are the CLI's own `render` and `upload` commands, which already take `--template` and `--csv` without a site folder.
The full offline checks (each cell's format and range, every value reaching the YAML) need the template's `columns.json`:
Python looks for a saved site folder in `preconfig-output/` whose template is byte-identical to the uploaded one and, when
found, passes its `columns.json` (and validates the way the build did). When none matches (a template edited by hand, or one
made on another computer), the files are still made and checked by rendering and YAML parsing, and the result says plainly
that the per-cell checks did not run.

Inventory (D8)
- Appliances, as `list` shows them, with search and paging.
- Preconfigs already on the Orchestrator: name, serial, tag, last change. Clicking one opens its YAML in a read-only viewer,
  secrets as `<SECRET>` (D13). Reading changes nothing.

## Engineering rules (from the gap review, 29 Oct)

- Folders. `gui` resolves `.env` (`EC_DOTENV`, as the launcher sets it), `preconfig-output/`, `preconfig-work/` and the run
  log folder to absolute paths, prints them at start and shows them on the page; every command gets an explicit `--out`.
  The automation GUI's "./.env only" rule is not copied: preconfig's launcher always sets `EC_DOTENV`. Every "make the files"
  run writes into its own new folder `preconfig-output/runs/<run_id>/`, never inside a build folder (a build folder's own
  `rendered/` holds the example row). Folder and file names come from Python, never from the browser's file name. Files the
  GUI saves (uploaded CSVs, rendered YAML) are mode 0600. The earlier-builds list skips `.building-*`, `.incomplete-*` and
  `*.previous-*`; matching an uploaded template to a saved folder ignores `*.previous-*`.
- One at a time where folders are shared. Runs on the same appliance, build folder or site folder queue behind each other
  (their staging folders are wiped at start and `keep_previous` names to the second), as well as uploads per Orchestrator.
- Secrets in text. Before any text reaches a task view, an issue table, the event stream or the run log, every literal value of
  a secret column from the CSV is replaced by `<SECRET:column>` (secret columns from `columns.json`, or secret-like headers
  when no folder matches); Jinja, YAML and `/validate` error text included. The `preconfigs` verb never returns `configData`
  in its list and never prints the raw text of a YAML that does not parse. Test: a secret holding `"` that breaks YAML parsing
  never appears in any response.
- No hidden prompts. The GUI always passes `--pick`, adds `--yes` to every run that writes, and `--dry-run` never asks.
- Long runs. A build reads every endpoint in the catalogue with a 0.2 s pause between requests (about a minute); a four-site compare several minutes. The
  page shows progress from the capture's own request log. A running read cannot be stopped midway (as in automation); a
  cancelled one is not offered as finished. Ctrl+C waits for an upload that is sending.
- Fleet panel versus the CLI. Profiles are the `orch_N` nicknames (a profile without an `orch_N` line is not listed, and the
  panel says so). Green: connected and the key works (a 403 on `/gms/versions` is still green, as `list` treats it); amber:
  slow (over 2 s) or busy (HTTP 429, which preconfig's client reports as a server error with status 429); red: the rest.
  Each check uses a 5 s timeout. A refused `GET /gmsserver/info` means "identity unknown, known by its address".
- The shared `.env`. The two repos read it differently (automation refuses an unnumbered `orch_` key such as the
  `orch_validators` alias preconfig documented up to 1.3.2, and nicknames with `-`; unterminated quotes; `_verify_tls` only in preconfig; a global CA
  bundle only in automation). Step 4 adds a table of these to `.env.example` and a test that one fixture `.env` gives the same
  profiles in both readers; the GUI warns about a key the automation tools would refuse.
- Run log. Workflows are named `preconfig <verb>` and logged to `reports/preconfig.jsonl`, with automation's full record
  (also `exit_code`, `orchestrator_url`, `command`, `tool_version`, `report_fingerprint`, the approval record), mode 0600,
  redacted. Row outcomes use automation's words: written -> VALID, invalid -> INVALID, conflict -> SKIPPED, ready -> PLANNED,
  uploaded -> CREATED, upload-failed -> FAILED. The automation GUI will list these runs too. Writing `reports/` is a new
  side effect of the CLI (also for agents using the skill), noted in the README.
- Templates from elsewhere. A template that matches no saved folder is the person's own file, rendered exactly as the CLI
  would render it; the result says the per-cell checks did not run.
- Where the code lives. `engine.py`, `approval.py`, `fleet.py`, `gui_server.py`, `gui_tasks.py`, `gui_tables.py` and `static/`
  go in `toolkit/gui/`; only the hooks the CLI needs (run context, recorder, approval hook, the new flags) go in
  `toolkit/scripts/`. Every step re-runs `tools/build_skill.py`; the `.env`-key test also scans `toolkit/gui/`.
- Copied from automation as is: browser CI job (Playwright pinned, `EDGECONNECT_BROWSER_TESTS=1`), `test_gui_design.py`,
  the security tests; `EC_DOTENV=/nonexistent/.env` in every CI job. Not copied: `gui_revert` and its endpoint (D7).

## Who changes what (owner, 30 Oct 05:19Z, D19)

Every change to the CLI or the toolkit scripts (`toolkit/scripts/`, fixes and the additions listed below alike) is made
separately from the GUI work and lands on `gui` first, as its own pull request with its own tests and review; the GUI work writes
the exact specification of each CLI addition it needs, with the tests that prove it. The GUI pull requests change only the GUI
itself (`toolkit/gui/`, its tests, CI and docs). A GUI step that needs a CLI addition starts after that addition is merged into `gui`.

## What the CLI gains (only additions)

| Addition | Step | Why |
|---|---|---|
| A run context instead of module values, and a recorder of each command's structured results | 1 | so two runs coexist in one process and the GUI reads results instead of text |
| The run log, `reports/<workflow>.jsonl` (`EDGECONNECT_REPORT_DIR` as in automation, default `reports/`) | 7 | "See what changed" |
| `variables ... --fix PATH,...` | 8 | untick a Top 10 / Top 20 setting (the opposite of `--set`); a locked one is refused with its reason |
| `variables ... --dry-run` | 8 | the check-box preview: writes nothing, no `/validate` |
| `preconfigs --orchestrator N [--show NAME]` | 10 | list the preconfigs on an Orchestrator, or print one as YAML with secrets redacted; GET only |
| `gui [--port P] [--no-browser]` | 5 | start the GUI; in the skill copy (no `toolkit/gui/`) it prints that the GUI comes with the full install |
| An approval hook in `upload.run_batch`, just before anything is sent | 2 | so the GUI's approval is checked against the rows this run is about to send (in the terminal, `--approve` is the approval, as now) |
| `compare ... --dry-run`, `--set`, `--fix`, and reuse of the sample captures already read (`--captures`) | 8 | so compare runs as command lines and does not read every site again after each click |
| Structured problems in the recorder (row, column, value, message) where each is made; the printed text unchanged | 1 | the GUI's problem tables |

Text printed by every existing command stays byte-identical.

## Existing CLI bugs found in the gap review (fixed as 1.3.3, PR #8, merged into `gui`; golden copies are recorded in step 1)

1. `validate --yaml` / `upload --yaml` on an Orchestrator not in `EC_VALIDATORS` crash with a traceback instead of the
   one-line refusal: the stage runs as `__main__`, so it raises `__main__.Stop`, which `upload.yaml_main` (catching
   `validate.Stop`) misses; `run_stage` also runs outside `cli.main`'s error handling.
2. The stage form's validator check is case-sensitive (`render ... --validate LAB` is refused while `lab` is allowed);
   the verbs lower-case the nickname.
3. The documented `orch_validators=` key in the shared `.env` makes every `edgeconnect-auto` command refuse to start
   ("Orchestrator slots must be numbered"). 1.3.3 drops the alias, so `EC_VALIDATORS` is the only key.
4. The validator refusal names a stale requirement ("edgeconnect_automation ApiClient.request has no read_only flag ...").

## Step 0: CI and the gui branch
Create `gui` from `main`. CI runs the tests on pushes and pull requests to `main` and `gui`, on Python 3.9 and 3.13, on Linux
and macOS (today: Linux only). Add this file as `docs/gui/BUILD_PLAN.md`.
Done when: CI is green on `gui`.

## Step 1: Golden outputs and run context
Record the CLI's output for representative commands against a fake Orchestrator (list, build, variables with each start,
compare, validate with valid and invalid rows, upload with and without `--approve`, validate --yaml, upload --yaml) as golden
files. Then replace the module values the commands share (`VALIDATORS` read at import, the `.env` path, the work folder)
with a run context passed through, holding a recorder that keeps each command's structured result (the build folder and
verdict, the picker rows, the per-row results `upload.run_batch` already builds).
How (from the gap review): the run context is a `contextvars` value, because stage scripts run inside the same process as the verbs;
the GUI installs one thread-routing `sys.stdout`/`sys.stderr` so each run's printed lines go to that run only (today's
`quiet()` swaps the whole process's stdout); module values read at import (`VALIDATORS`, `WORK_DIR`,
`translate.IGNORE_OWNERSHIP`) and current-folder defaults are read from the context; `sys.stdin` is an empty stream in the
GUI, so a prompt fails at once instead of hanging. Since 1.3.3 (PR #8) `run_stage` imports the stage module and calls its `main(argv)`, so stage runs no longer touch
`sys.argv` or `sys.modules['__main__']`; stage modules are imported once per process, so their import-time state (`VALIDATORS`
from `.env`) must move into the run context like the verbs'.
Golden outputs mask timestamps, absolute paths and the toolkit commit.
Done when: tests pass, golden outputs are byte-identical, two run contexts with different `.env` files coexist in one process,
two builds and a render in parallel each get only their own lines, and every verb run through the engine with stdin at end of
file finishes without waiting.

Built as: PRs #10 (golden outputs), #11 (run context), #12 (per-run output, prompts at the end of stdin, recorder results and
structured problems) and #13 (the approval hook and batch fingerprint in `upload.run_batch`), all made as CLI additions.

## Step 2: Approval module (approval.py)
The automation module's GUI front end: a single-use approval bound to a fingerprint of the template, CSV and `columns.json`
bytes, each ready row's name, serial and the SHA-256 of its rendered YAML, and the Orchestrator's identity read in that same
run; 10-minute expiry. It is redeemed by the hook in `run_batch` just before the first upload, so a row that changed between
the check and the approval (it failed before and passes now, or a conflict cleared) is refused, not sent. The terminal path is unchanged: the CLI still
uploads only with `--approve`.
Done when: tests prove an approval cannot be reused, expires, and is refused for different rows or another Orchestrator.
Built as: `toolkit/gui/approval.py` (`GuiApprovals`, `upload_gates`, and `approver(token, nickname)` for the run context),
tested in `tests/test_gui_approval.py` through `cli.main` with a fake Orchestrator.

## Step 3: Engine (engine.py)
`engine.run(argv, context)` runs one CLI command line in-process and returns a Result: exit code, status, issues (row, column,
value, message, fix), per-row outcomes, counts, the files written, the command's own text (shown as the run's log), and what
approving it takes. Thin helpers build the argv for each action (list, build, variables, compare, validate, upload, validate
--yaml, upload --yaml, preconfigs).
Done when: golden outputs are unchanged and every Result field has a test.
Built as: `toolkit/gui/engine.py`: `run(argv, approver, **RunContext fields)` returns a `Result` (`exit_code`, `status` done / failed /
rejected, `issues`, `rows` with the run log's outcome words, `counts`, `files`, `text`, `approval`, `outputs`), and `approve()` returns
the approver for the upload run. Before the CLI runs, the engine refuses an upload with `--approve` and no approver, and any upload
whose own `--orchestrator` lacks `<nickname>_gui_preconfig_uploads=on` in the run's .env. Each run reads an empty stdin through a
per-thread stand-in, and secret values from the CSV (`columns.json` or secret-like headers) or any YAML file the command names are
scrubbed from every string, in each form the CLI prints them (escaped, `tojson`, cut short); the parser's message for a YAML file that
does not parse and every PyYAML source snippet are withheld, and a run without a GUI approval gets an approver that always refuses.
`run` refuses every command line whose shape no helper builds (other stages, a typo, the `validate` stage form that sends its files
unparsed). Helpers cover
list, build, variables, validate, upload, validate / upload --yaml and render; compare and preconfigs wait for R6 and R7.
Tested in `tests/test_gui_engine.py` through `cli.main` with a fake Orchestrator.

## Step 4: Orchestrator status and identity (fleet.py)
`check_all()` as in automation, over preconfig's own `.env` reader (so the GUI sees exactly what the CLI sees): every profile in
parallel, one try of 5 s; green = connected and the key works; amber = slow (over 2 s) or busy (429); red = bad settings, no
answer, certificate, DNS, refused, key refused, server error. Each status also says validator yes/no and uploads on/off. Identity:
the serial from `GET /gmsserver/info`; the cloud placeholder `00-00-00-00-00-00` counts as none, and the Orchestrator is known
by its address. An upload refuses if the identity changed since its rows were checked. `.env.example` gains
`<nickname>_gui_label`, `<nickname>_gui_color` (the same keys the automation GUI reads, so an Orchestrator looks the same in both)
and `<nickname>_gui_preconfig_uploads` (off by default).
Done when: fixture tests cover each status and the identity refusal, and a read-only check of the lab Orchestrators passes.
Built as: `toolkit/gui/fleet.py`, `check_all(dotenv_path)` -> `Fleet(orchestrators, not_listed, note, warnings)`, each a `Status` with
automation's fields (`name`, `url`, `status`, `reason`, `release`, `id`, `label`, `color`, `writes`, `seconds`) plus `validator`; `writes`
is `<nickname>_gui_preconfig_uploads`, read with the values automation accepts for `_gui_writes`. It runs `common.client_for` with a
5 s timeout inside a run context holding the given `.env`, reads `GET /gms/versions`, and takes the identity from
`upload.orchestrator_identity`, the same read the approval fingerprint uses. `.env.example` has the three keys and the table of
reading differences; `tests/test_gui_fleet.py` checks the fixture `.env` against a recorded automation reading, and against
automation's own reader when its checkout is found (`EDGECONNECT_AUTOMATION_SRC`, else skipped). The `.env`-key test also scans
`toolkit/gui/`. An upload to an Orchestrator whose identity changed is already refused by step 2 (the batch fingerprint holds the
identity read in the upload run). `Fleet.warnings` names, by key only and never its value, each `.env` key that makes every
edgeconnect-auto command stop: an unnumbered `orch_` key, an unclosed quote, a nickname with `-` or another character outside
letters, digits and `_`, a reserved nickname (default, ec, edgeconnect, orchestrator), and a nickname used by two `orch_N` lines.
Not yet: the read-only lab check, which needs the owner's machine.

## Step 5: GUI server and tasks
`gui_server.py` copied from automation (security unchanged) and `gui_tasks.py` over `engine.py`. Reads (list, build,
variables preview and save, compare, validate, validate --yaml, preconfigs) run in parallel; an upload holds its Orchestrator's
lock, so a second one waits as queued. States as in automation: queued, running, done, awaiting_approval, uploading, failed,
rejected, expired, cancelled; automation's `applying` is named `uploading` here, because nothing in this toolkit applies
(decision 0008). Progress over server-sent events; the command's printed lines stream as they come. Files a person
imports arrive in the request body.
Done when: security tests cover a rebinding Host, a missing cookie, a cross-origin POST, a non-JSON POST and keys never appearing
in a response, and the page loads on macOS, Linux and WSL.
Built as: `toolkit/gui/gui_server.py` (automation's server, security unchanged; `python3 -m toolkit.gui [--port P] [--no-browser]`
prints the absolute `.env`, output, work and run-log folders and fixes `EDGECONNECT_REPORT_DIR` once at launch), `toolkit/gui/gui_tasks.py`
(actions list, build, variables preview/save, validate, render, validate_yaml, upload, upload_yaml; an appliance lock for a build folder
and its site folders, an Orchestrator lock per upload; imports saved 0600 in `preconfig-output/runs/<run_id>/`), `engine.run(on_line=...)`
for the streamed lines, and `GET /api/builds` for the earlier builds; its placeholder page gave way to the step 6 shell. Tested in
`tests/test_gui_tasks.py` and `tests/test_gui_server.py`. Not yet: compare and preconfigs tasks (R6, step 10), the page on macOS and WSL.

## Step 6: Design and shell
The exact copy of the automation `static/` design (fonts, `app.css`, favicon style), the top bar with the Orchestrator switcher,
colour stripe and label, the fleet panel, the home cards, the earlier-builds list, the command palette and reset on switch.
Done when: usable with the keyboard only; `tests/test_gui_design.py` (copied) passes WCAG AA for every text pair in both themes.
Built as: `toolkit/gui/static/`, the automation `static/` of 7af5e9d with fonts, licences, `app.css` and `favicon.svg` unchanged,
and `index.html` / `app.js` cut to the home page: switcher with the colour stripe and label, fleet panel (`validator`, uploads,
`note`, `warnings`), the seven cards (each opens a page that says the screen comes later and gives the CLI line), tasks of this
session, earlier builds, palette, and reset on switch. Nothing is disabled. The script calls `GET /api/orchestrators` (the
`Fleet` as `dataclasses.asdict`), `GET /api/tasks`, `POST /api/tasks/<id>/cancel` and `GET /api/builds`, which returns
`{folder, builds: [{appliance, orchestrator, built, site_types: [{name, template_url, csv_url}]}]}`, newest first, the download
URLs named by the server. `tests/test_gui_shell.py` pins that list and that every element the script uses is on the page.

## Step 7: Run log and tables
The CLI writes the run log (format above); `source` is `cli` or `gui`. "See what changed": runs table (time, Orchestrator,
workflow, status, changes written, row outcomes, duration, source, run ID) with sort, filter, search, problems-only and CSV
export; a run's page with its per-row table. `gui_tables.py` copied.
Done when: 10,000 logged runs stay responsive and a run's row CSV matches the run log.
Built as: `toolkit/gui/gui_tables.py`, automation's of 7af5e9d without its inventory, change and issue tables: `select` / `page` (search,
exact-match filters, problems only, sort with blanks last, page sizes 25 to 200, facets), `RunLog` over `reports/preconfig.jsonl` only
(re-read when its size or time changes; a line that is not a record with a `summary` is counted as unreadable; every line is
redacted again on load with `runlog.redact`, after an `Authorization: Bearer|Basic` token is masked, which that redactor misses;
a run ID outside `[A-Za-z0-9_-]` gets a `<log>-line-<n>` ID), `run_detail`, and
automation's formula-safe `csv_text`. Routes, GET only behind the session cookie: `/api/runs[.csv]` and `/api/runs/<run_id>[/rows.csv]`,
the run ID matched against `[A-Za-z0-9_-]{1,80}` before the log is searched for it (it never names a file). The screen is
`static/tables.js` (automation's `PagedTable`, unchanged) and `static/runs.js` (the runs table, the run page with its facts, notes,
approval record and rows); `app.js` gained only `SCREENS`, where a screen's own script adds its hash and view, so the home card
opens it. Tested in `tests/test_gui_tables.py` (CLI and GUI runs read back as logged, each row CSV equal to the log's rows, the approval
record, 10,000 runs, the routes' refusals) and `tests/test_gui_shell.py`.

## Step 8: Build, choose what changes per site, compare
The build card (appliance picker from `list`, validator choice from the validators only), the check-box screen with the YAML
viewer (above), and compare feeding the same screen. The CLI gains `--fix` and `--dry-run`.
Done when: Save with Top 10 plus extras gives byte-identical files to the same `variables` command in the CLI; `--fix` undoes
`--set`; a locked row is refused with its reason; `--dry-run` writes nothing and calls no Orchestrator; a lab build
matches the CLI's.
Built as (without compare, which waits for R6): `static/build.js` (the build card: appliances from the `list` task's recorded
rows (R8); validators only; progress from the capture's `requests.jsonl`, method, path and
status only; `summary.md` as text) and `static/pick.js` / `pick.css` (the check-box screen, the table tab, and the YAML viewer from
`GET /api/builds/<build>/reference`, secrets as `<SECRET>` through `engine.hide_secrets`). The browser keeps only the person's
ticks (`--set`) and unticks (`--fix`), runs the dry run after each flip (debounced, one at a time, the newest state drawn), and on a
refusal goes back to the last accepted state with the CLI's reason at the row. `PICK_STARTS` is the hook for "Different between
your sites". Screens in their own files push to `SCREENS` in `app.js`. Tested in `tests/test_gui_pick.py`. Not yet: the lab build.
Built as (8b, compare): `engine.compare(folder, sites, name, settings, fixed, captures, dry_run)` and exactly its shapes in
`_built_here` (1 to 4 sites, `--dry-run` or `--yes`, one `--captures` folder per site); the READ task `compare` locks the build
folder and every site it reads by hostname and nePk on the build's Orchestrator. "Compare with other sites" on the pick screen lists
the build's Orchestrator, takes up to 4 ticked sites, and adds the start "Different between your sites"; the first preview reads the
sites (GET only) and every later run, Save too, names that task (`captures_from`) so the server passes its recorded captures back
with `--captures`; the browser never sends a folder. Save is `compare --name N --yes`, which validates on the build's validator and
says when a row did not pass. Every row's `lines` lights the YAML on hover. Since R10 the Table tab has a column per compared site
(`sites`, `site_values`) beside the base value: a dash where the site lacks the setting, a mark where it differs, a secret as
its placeholder, and a site in `no_row` says "no row" with its Note above the table; the table scrolls, never the page. Tested in
`tests/test_gui_compare.py`. Not yet: progress from the reads' request logs (the status shows the "reading ..." lines), and the
lab compare.

## Step 9: Make the preconfig files, upload, existing YAML
Upload the template and the CSV, the three choices (create; create and validate; create, validate and upload), the matching of
the template to a saved site folder for the full checks, the row table, downloads (one file or a .zip), upload behind the
approval and `<nickname>_gui_preconfig_uploads`, and validate / upload of existing YAML files.
Done when: the same template and CSV give byte-identical YAML files through the GUI and the CLI for each of the three choices;
a template that matches no saved folder still renders and says the cell checks did not run; every gate is tested (switch off, wrong
nickname, expired, reused, other Orchestrator); a lab upload matches the CLI's.
Built as: the task action `make` in `toolkit/gui/gui_tasks.py` (choice `create`, `validate` or `upload`; the template and CSV saved 0600 in
the task's own new `preconfig-output/runs/<run_id>/`). `_match` finds a saved site folder whose `<name>.jinja2` is byte-identical (no
`*.previous-*` build or site folder, `columns.json` and `build.json` present): then `render --site-dir S --out RUN/rendered` (with `--validate V`,
`--substitute` when V is not the build's Orchestrator, as `validate SITE`), or `upload --site-dir S --out RUN`; otherwise `render --template T`
(`--validate V --substitute`) or `upload --template T --out RUN`, and the task's `note` says the per-cell checks did not run. V is the one named,
else the switcher's Orchestrator when it is a validator, else the build's validator; anything not in `EC_VALIDATORS` is refused. Every upload
(`upload`, `upload_yaml`, `make` with `upload`) is refused with 403 before a task or folder exists unless `<nickname>_gui_preconfig_uploads=on`
(`engine.uploads_off`). The task view carries `approval` (`gates`, `orchestrator`, `counts`: `ready` of `rows`) while a CSV upload awaits it.
Routes, GET behind the cookie: `/api/tasks/<id>/rows` (one line per row: written, invalid with column, value and fix, conflict, uploaded;
`gui_tables.page`), `/files.zip` and `/file?name=` (only `Result.files` of that run, inside its folder). `upload --yaml` is one button with no
typed nickname (D9, decision 0017): only the files chosen, to the switcher's Orchestrator, with that switch on. Screens: `static/make.js` (with
the shared task panel, file picker and approve dialog) and `static/yaml.js`, which build their own sections. Tested in
`tests/test_gui_make.py` (GUI and CLI files byte-identical for each choice, matched and unmatched; the gates; a secret CSV cell in no
response, event, row table or zip listing).
Not yet: the lab upload check, which needs the owner's machine.

## Step 10: Inventory
Appliances and the preconfigs on the Orchestrator, with the read-only YAML viewer; the CLI gains `preconfigs`.
Done when: secrets in a fixture preconfig never reach the page unredacted, and a read-only lab check passes.
Built as: `engine.preconfigs(orchestrator, show=None)` runs `preconfigs --orchestrator N [--show NAME|ID]`, and `run` allows exactly
those two shapes; the CLI redacts what it prints and records (`preconfig` rows without `configData`, `preconfig-yaml` text), and a
YAML that does not parse ends failed with the CLI's one line naming where it broke. The appliances are the `appliance` results `list`
records (R8, #23), never its printed table. The `preconfigs` task (with `show`) joins the reads in `gui_tasks.py`. The screen is
`static/inventory.js`, loaded last, which adds `#/inventory` to step 7's `SCREENS`: appliances with `list --search` and 50 rows a page
in the browser (`PagedTable` pages a server endpoint, and these rows are one task's result), the preconfigs (name, serial, tag, last
change, id), and the clicked one's YAML as text in a `<pre>`, read only, fetched by its id.
Tested in `tests/test_gui_inventory.py` over HTTP with the golden preconfigs (list, show and task JSON, the event stream, the YAML whose
`"` breaks parsing), and `tests/test_gui_shell.py`. Not yet: the read-only lab check (which also confirms the `modifiedTime` field
name, `cli.LAST_CHANGE`).

## Step 11: Polish and release
Accessibility pass (axe-core on every screen, both themes), a Playwright smoke test in its own CI job (from an appliance to
written YAML files), README and docs GUI sections, a WSL check by hand, version bump, release as a GitHub pre-release from `gui`
(`main` and its Latest release untouched).
Done when: CI is green, the WSL check has passed and the pre-release is published.

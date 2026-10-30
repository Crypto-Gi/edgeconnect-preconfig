# The local web GUI

Back to the [README](../README.md). The command line it runs: [COMMAND_LINE.md](COMMAND_LINE.md). Install and `.env`: [INSTALL.md](INSTALL.md).

The GUI does what the command line does, in a browser on this computer. Every button builds a normal `edgeconnect-preconfig` command line and runs it through the command line's own code, so the checks, the files written and the run log are the same as in the terminal. The browser only shows what that run decided. It is on the `gui` branch until it is released.

## Start it

From the repository folder, in its Python environment (the one with `requirements.txt` installed):

```bash
python3 -m toolkit.gui                          # opens your browser
python3 -m toolkit.gui --port 8765 --no-browser # a fixed port; only prints the link
```

- `--port P` listens on that port (default: any free port); `--no-browser` prints the link without opening a browser.
- At start it prints the folders it uses, as absolute paths: the `.env`, `preconfig-output/`, the work folder (captures) and the run log folder. The `.env` is found as the command line finds it; set `EC_DOTENV=/path/to/.env` to name another.
- Then it prints a sign-in link, `http://127.0.0.1:<port>/?token=...`. It works once: it signs this browser in and then stops working, so another account on the computer cannot reuse it. Do not share it. Opening the GUI in a second browser takes a restart.
- Press Ctrl+C in the terminal to stop it. An upload that is sending finishes first.
- On Windows through WSL it opens the Windows browser (`wslview`, else `explorer.exe`). Over SSH, forward the port (`ssh -L 8765:127.0.0.1:8765 ...`), start it with `--port 8765 --no-browser` and open the printed link on your own computer. Use the same port number on both ends: through `ssh -L 9000:127.0.0.1:8765` the browser names `localhost:9000`, and the GUI refuses it ("this server only answers to 127.0.0.1, localhost or [::1] on its own port").

It listens on 127.0.0.1 only and answers only to `127.0.0.1`, `localhost` or `[::1]` on its own port. API keys stay in the Python process: no page or response carries them.

## Screens

The top bar has the Orchestrator switcher, its status, **Commands** (Ctrl+K or Cmd+K: every task, switch Orchestrator, check again, theme) and **Theme** (light or dark). Everything works with the keyboard.

Switching Orchestrator starts fresh: files you chose and open tasks are dropped (open tasks are cancelled), and it asks first when there is something to lose. Checks and approvals are never carried to another Orchestrator. Files already written to `preconfig-output/` stay there.

### Home

- **What do you want to do?** One card per task below. Each says whether it only reads, or can upload on the chosen Orchestrator.
- **Orchestrators.** Every `orch_N` profile in `.env`, checked at once with one try of 5 seconds each: green (connected and the key works), amber (slow, over 2 seconds, or busy), red (with the reason). Each also says `validator yes` (listed in `EC_VALIDATORS`, so `/validate` may run there) or `no`, and `uploads on` or `off`. A profile with settings but no `orch_N` line is named as not listed, and `.env` keys the automation tools would refuse are named, by key only.
- **Tasks in this session.** What ran since the GUI started, with its state.
- **Earlier builds.** Every build in `preconfig-output/`, newest first, with **Download template** and **Download CSV** for each site type saved there. They stay after the GUI stops, so you can come back after filling a CSV.

### Build a template from an appliance

1. **Find appliances** lists the Orchestrator's appliances (`list --search`); pick one.
2. **Validate on** offers only the Orchestrators in `EC_VALIDATORS`. Target release and target ECOS are optional, as for `build`.
3. **Build** runs `build --orchestrator N --appliance A`. It reads the appliance with GET only, about a minute; the progress shows each request. The folder is kept only if `/validate` accepts it. The result shows the verdict and `summary.md`: every item the YAML does not carry.

### Choose what changes per site

Open it from the build result or from an earlier build. The check boxes are the rows of the build's `variables.csv`, the same as the command line's tick list.

- **Start from** Top 10 (ticked when the screen opens), Top 20 or All. Your own ticks and unticks are kept when you switch.
- **By section** shows one panel per section with "3 of 12 per site"; **Table** shows the same rows as the command line's tick list. Search, and **Show only ticked**, filter both.
- **Always per site** comes first: hostname, secrets and the Deployment page values ([decision 0006](decisions/0006-deployment-page-values-are-always-per-site.md)), ticked, with a lock and the reason. Unticking one says why it stays per site.
- Each tick runs `variables FOLDER --pick P --set ... --fix ... --dry-run` (nothing written, no Orchestrator call) and draws what it returns: the ticks, each column name, and the lines of `reference.yaml` on the right that each ticked value fills. Hovering a row highlights its lines. Secrets show as `<SECRET>`. A refused flip goes back, with the command line's reason at the row.
- **Save template and CSV** asks the site type name and runs the same command with `--name N --yes`. `site-<name>/` is written only if `/validate` accepts this appliance's own row. Your browser then downloads `<name>.jinja2` and `sites.csv` (this appliance's row, secrets blank). Add one row per site to the CSV. Both files also stay in `preconfig-output/<appliance>/site-<name>/`.

### Compare with other sites

On the same screen, **Compare with other sites** lists the appliances on the build's Orchestrator. Tick 1 to 4 sites of the same design and press **Compare with the ticked sites**. The first preview reads each site with GET only, which takes some minutes; later ticks reuse what was read. The start **Different between your sites** ticks every setting that differs. Save runs `compare ... --name N --yes`: `sites.csv` gets a row per compared site, and every row is validated.

### Make the preconfig files

1. Choose your template (`.jinja2`) and your filled CSV. Both are needed.
2. Choose what to do:
   - **Create the YAML files:** one `<preconfig_name>.yml` per row, checked on this computer; no Orchestrator is called.
   - **Create and validate:** as above, and every row through `/validate` on the validator you choose; a row is written only if it passes.
   - **Create, validate and upload:** as above on the Orchestrator chosen at the top, with name and serial conflicts checked. The ready rows are shown, and nothing is sent until you approve them (see Uploads below).
3. A table shows one line per row: written, invalid (with the column, the value and what to fix), conflict, or uploaded. **Problems only** and the filter narrow it. A CSV with problems is fixed in the file and chosen again; the GUI never edits a CSV.
4. Download one file from the table, or all the files this run wrote as a .zip.

Each run writes into its own new folder, `preconfig-output/runs/<run_id>/`. When your template is byte for byte the one saved in a site folder, its `columns.json` is used and every cell is checked, as `validate SITE` does. A template edited by hand or made on another computer is still rendered as the command line would, and the result says the per-cell checks did not run.

### Check or upload existing YAML files

Choose one or more preconfig YAML files. **Check the files** runs `validate --yaml` (shape, `<SECRET:...>` placeholders, `/validate`, name and serial conflicts) and writes nothing. **Upload the files** runs `upload --yaml` and uploads every file that passes. With one file you may give its preconfig name and the appliance serial number.

### Inventory

The appliances on the chosen Orchestrator, as `list` shows them, with search and paging, and the preconfigs already there (name, serial, tag, last change, id). Open one to read its YAML: read only, secrets as `<SECRET:path>`. Every request here is a GET.

### See what changed

The run log, `reports/preconfig.jsonl`, which the command line writes too: time, Orchestrator, workflow, result, changes made, row outcomes, duration, from (`cli` or `gui`) and run ID. Sort, filter, search, **Problems only** and CSV export. A run's page shows its facts, its approval when it had one, and every row it touched. `EDGECONNECT_REPORT_DIR` names another folder; empty turns the log off.

## Uploads

- Uploads are off for every Orchestrator until `.env` has `<nickname>_gui_preconfig_uploads=on` for it (for example `lab_gui_preconfig_uploads=on`). Only `on` (any case) turns them on, and `off` or blank leaves them off. Any other value, `true` or `yes` too, shows that Orchestrator red with `<nickname>_gui_preconfig_uploads must be on or off`, and uploads stay off. With it off, no upload goes to that Orchestrator, and none of an upload's own requests either: not its name and serial check, not its `/validate`. Everything else still runs there: the fleet check, lists, inventory, builds, and `/validate` when it is a validator. It is not the automation GUI's `<nickname>_gui_writes`.
- **A CSV upload** (Create, validate and upload) shows the ready rows first. The approve dialog shows the Orchestrator's colour band and label; type its nickname to approve. The approval works once, only for exactly those rows and that Orchestrator, and expires after 10 minutes. A row that changed since it was checked is refused, not sent. **Not yet** closes the dialog, and **Cancel the upload** drops it.
- **An upload of existing YAML files** is one button, with no typed nickname, as `upload --yaml` needs no `--approve` ([decision 0017](decisions/0017-upload-yaml-needs-no-approve.md)). The switch above must still be on.
- There is no Undo. An uploaded preconfig is removed in Orchestrator.
- The GUI never applies a preconfig. Every upload is staged with `autoApply` off, and a person applies it in Orchestrator ([decision 0008](decisions/0008-the-toolkit-cannot-apply.md)).

The other `.env` keys the GUI reads, `<nickname>_gui_label` and `<nickname>_gui_color`, set the name and colour an Orchestrator has in the switcher. Every key and its values: [`.env.example`](../.env.example).

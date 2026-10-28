# The command line

Back to the [README](../README.md). Install first: [INSTALL.md](INSTALL.md).

`setup.sh` installs the `edgeconnect-preconfig` command (in `~/.local/bin`; it runs `scripts/cli.py` with the toolkit's Python and the shared `.env` of the install folder, unless `EC_DOTENV` names another). Six commands cover the whole job; `edgeconnect-preconfig --help` lists them, each takes `--help`, and `edgeconnect-preconfig --version` prints the toolkit version.

```bash
edgeconnect-preconfig list --orchestrator lab                      # test the .env profile and list appliances: hostname, nePk, model, ECOS, site, reachable
edgeconnect-preconfig build --orchestrator lab --appliance Branch-01     # read it (GET only), convert, check; kept only if /validate accepts it
edgeconnect-preconfig variables ./preconfig-output/Branch-01             # choose the per-site settings: Top 10, Top 20 or all, by section
edgeconnect-preconfig compare ./preconfig-output/Branch-01 Branch-07 Branch-12   # or let sites of the same design choose; a CSV row per site
edgeconnect-preconfig validate ./preconfig-output/Branch-01/site-branch --csv sites.csv
edgeconnect-preconfig upload ./preconfig-output/Branch-01/site-branch --csv sites.csv            # table only
edgeconnect-preconfig upload ./preconfig-output/Branch-01/site-branch --csv sites.csv --approve  # uploads the valid, non-conflicting rows
edgeconnect-preconfig validate --yaml ./known-good/ --orchestrator lab                           # check existing preconfig YAML files; never writes
edgeconnect-preconfig upload --yaml ./known-good/*.yml --orchestrator lab                        # same checks, then uploads every file that passes
```

- **list** `--orchestrator NICK [--search TEXT]`: logs in, reads the release and lists the appliances; `--search` keeps those whose hostname, nePk, model or site contains the text. The first line says whether `/validate` may run on that Orchestrator.
- **build** `--orchestrator NICK --appliance NAME|nePk [--out DIR] [--validate-on NICK] [--target-release RELEASE] [--target-ecos ECOS]` writes `<out>/<hostname>/` (default `./preconfig-output/<hostname>/`): `reference.yaml`, the template `<hostname>.jinja2`, `<hostname>.csv` (empty sheet, headers only, for new sites), `example.csv` (this appliance's own values, secrets blank: copy it to start your `sites.csv`), `variables.csv`, `columns.json`, `columns.md`, `ledger.json`, `summary.md` with the verdict, `checks.json`, `rendered/` and `build.json`. The folder is kept only if Orchestrator's `/validate` accepts the example row; the capture itself stays in the work folder (see below). Validation runs on the Orchestrator the appliance came from; `--validate-on <lab>` names another, and then object names (groups, labels, segments) are swapped for ones that exist there, for the check only. Either way the validator must be allowed in `.env` (see `EC_VALIDATORS` in [INSTALL.md](INSTALL.md#2-configure-env)). An earlier folder of the same name is renamed to `<name>.previous-<UTC stamp>`, never overwritten. `--target-release` is only needed when the validator's API key may not read its release (`GET /gms/versions`); build then says so. `--target-ecos` (for example `9.6.2.1`) is the ECOS release of the appliances the preconfig is for; it is kept in `build.json` and passed on by later `variables` and `compare` runs (see translate below).
- **variables** `FOLDER [--pick top10|top20|all] [--set COL,...] [--name NAME] [--yes]` shows the settings as a tick list. Hostname, serial, secrets and every Deployment-page and appliance-info value (addresses, next hops, bandwidths, DHCP, location) are always per site, no questions; interface names, labels, zones, segments, firewall and deployment mode stay fixed unless you flip them. The Top 10 and Top 20 are the settings that differed most often between appliances of the same design in the captured corpus (`references/variable-ranking.json`); "All" opens one section at a time. Type numbers to flip a setting (`4 12`), `s1`.. to open a section, `a` for all sections, `t` for the Top list, `l` for the locked ones, a word to search, Enter when done, `q` to quit. A gateway that equals an interface address follows that address. `--pick top10 --yes` (or `top20`, or `all` to make every setting per site) and `--set column,...` do the same without questions; `--yes` needs `--pick`. Top 10 and Top 20 never tick template groups or overlays; flip them yourself if sites differ.
- **compare** `FOLDER SITE [SITE ...] [--name NAME] [--yes]` reads 1 to 4 other sites on the same Orchestrator (GET only; 2 recommended), makes every setting whose value differs between them per site, and asks you to confirm (`Y`), stop (`n`) or `edit` the tick list. Its `sites.csv` has this site's row and a row for every other site with that site's own values (`preconfig_name` is `<hostname>_preconfig`; serials and secrets blank), and every row is validated. A site whose interface names differ at the same place (`wan1` where this site has `wan0`) gets no row: a note names it, because its values would land on the wrong interfaces. Build a separate template for that design.
- **validate** `FOLDER --csv FILE [--orchestrator NICK]` renders and validates every row on the build's validator (or the one named); rows that pass are written to `FOLDER/rendered/`.
- **upload** `FOLDER --csv FILE [--orchestrator NICK] [--approve]` validates every row again, skips a row whose name or serial is already on the Orchestrator, and uploads only with `--approve`. It uploads to the build's validator unless `--orchestrator` names another, and runs `/validate` there itself (upload does not check `EC_VALIDATORS`).
- **validate --yaml / upload --yaml** `FILE|DIR [FILE|DIR ...] --orchestrator NICK [--name NAME] [--serial SERIAL]` take existing preconfig YAML files instead of a template and CSV (`--yaml` and `--csv` exclude each other). A folder gives every `*.yml` and `*.yaml` directly in it, sorted; shell globs work. Each file is refused if it is empty, not a mapping of sections, not YAML, or still a Jinja template (`refused: not a preconfig YAML`), or if it holds `<SECRET:...>` placeholders (`refused: secret placeholder`, with the path of each one to fill in); then it goes to `/validate` and the name and serial conflict checks. The preconfig name is the file name without `.yml` unless `--name` gives one; `--name` and `--serial` need exactly one file, and the serial is normalised like the CSV's `serial_number`. A route with next hop `0.0.0.0` or `::` and an interface prints a warning (ECOS 9.7+ rejects the routes step at apply; `docs/decisions/0016`) and does not stop the file. `validate --yaml` never writes and needs a validator in `EC_VALIDATORS`. `upload --yaml` uploads every file that passed with no `--approve` (`docs/decisions/0017`; `--approve` is accepted and ignored), reads the list back, and prints one line per file: `uploaded`, `skipped: name conflict`, `skipped: serial conflict`, `refused: ...` or `validate failed` with Orchestrator's message. A bad or conflicting file is skipped and the others continue.
- **Nothing applies.** The command line stops at upload, and every upload has `autoApply` off. Applying a preconfig to an appliance is done by a person in Orchestrator; no script here can do it (`docs/decisions/0008`).
- **variables** and **compare** write `site-<name>/` inside the build folder (an earlier one is renamed, as above); the build's own files never change. Give `validate` and `upload` the build folder or a `site-<name>` folder.
- **Put FOLDER first** after `validate` and `upload`. If the first word after them is an option (`upload --csv ...`), the old stage script runs instead and asks for its own options.
- **The stage scripts still run by name:** `cache-check`, `capture`, `check`, `generate`, `options`, `render`, `translate`, and `validate` / `upload` in their stage form (for example `edgeconnect-preconfig capture --orchestrator lab --appliance 0.NE --role reference`). The old live-versus-cached stage `compare` is now `cache-check`. For the help of the `validate` and `upload` stages run the script itself (`python3 scripts/upload.py --help`), since `edgeconnect-preconfig upload --help` shows the command. `cells` and `synth` are listed too, but they are libraries with no command line.
- **Captures** go to the work folder: `$PRECONFIG_WORK`, else `preconfig-work/` in the install folder when it exists, else `./preconfig-work/` (`reference/<orchestrator>/<nePk>/<UTC stamp>/` for build, `samples/...` for compare).
- **Exit codes:** 0 done; 1 when some rows failed or you stopped at a question; 2 when the command stopped, with one `error:` line; 130 after Ctrl-C.

## When a command stops

| You see | What it means | What to do |
|---|---|---|
| `... is not a validator Orchestrator: /validate runs only on the ones listed in EC_VALIDATORS ...` | The validator is not allowed in `.env`. | Add it to `EC_VALIDATORS`, or pass `--validate-on <lab>`. |
| `PermissionDeniedError (HTTP 403) from /gms/appliance/preconfiguration/validate` | The key's roles do not allow `/validate`; a read-only key is refused, although nothing is saved. Nothing was kept. | Validate on an Orchestrator whose key may validate: `--validate-on <lab>`. |
| `unknown or incomplete Orchestrator profile 'x' (configured: ...)` | No `x_base_url` and `x_api_key` in the `.env` that was found. | Fix `.env`, or point `EC_DOTENV` at the right file. |
| `AuthenticationError`, `TransportError` (401, redirect, TLS, network) | The capture stops; no folder is left behind. | Check the URL, the key and, for a self-signed certificate, `<nick>_tls_sha256`. |
| `GET /subnets refused (HTTP 403) ...` (or `/subnets/all`) | The key may not read the routes, so the preconfig would lose them (`docs/decisions/0010`). | Give the key read rights for them and run again. |
| `... lacks appliance or deployment.sysConfig data (appliance may be offline with no Orchestrator cache)` | The appliance is unreachable and Orchestrator holds no cached copy. | Bring it online, or pick another appliance. |
| `appliance 'x': 0 matches by nePk or exact hostname` | The name is not on that Orchestrator (hostnames match exactly). | Run `list` and copy the hostname or nePk. |
| `... did not give its release (GET /gms/versions refused): pass --target-release` | The key may not read the release. | Pass `--target-release` with one of the releases it names. |

A read the API key's roles refuse (HTTP 403) on any other endpoint does **not** stop the run (`docs/decisions/0009`). The capture records it and `summary.md` lists it under "Not readable: the format has a place for it, but a GET cannot read it; check it by hand", with the note "Orchestrator refused GET <path> (HTTP 403): the API key's RBAC roles do not allow it, so these sections may lack values; check them by hand or widen the key's read roles and capture again". The verdict then reads "Complete for preconfig; N item(s) to check by hand." Business hours (`availabilityTimeSettings`) are always in that group, because Orchestrator returns them only through a POST.

## The stage scripts, step by step

The six commands above run these stages for you. To run them yourself (`SKILL_SCRIPTS` is `toolkit/scripts`, or the `scripts/` folder of an installed skill):

### Capture a Reference Appliance (Read-Only `GET`)
```bash
SKILL_SCRIPTS="toolkit/scripts"

python3 "$SKILL_SCRIPTS/capture.py" --orchestrator prod --appliance 2.NE --role reference
```
> Prints the capture folder at the end. Captures live and cached appliance state via `GET` only, redacts all passwords/PSKs/RADIUS keys/certificates to `<SECRET:path>` before touching disk (free-text credentials in banners or notes may not all be caught; review captures before sharing them), and automatically downloads `GET /gms/appliance/preconfiguration/default` into the work folder's `reference/default-templates/orchestrator-<release>.yml` (`PRECONFIG_WORK`; never inside the toolkit) if the Orchestrator release is new (not with `--only`, and not when the key may not read `GET /gms/versions`). Use `--role sample` for the other sites of the same design (written under `samples/`). A read the key's roles refuse is recorded and reported, not fatal; see [When a command stops](#when-a-command-stops).

### Generate the Site-Type Jinja2 & CSV Template
```bash
python3 "$SKILL_SCRIPTS/translate.py" \
  --capture ./preconfig-work/reference/prod/2.NE/<UTC_STAMP> \
  --site-type retail-branch \
  --target-release 9.7.1.42046 \
  --target-ecos 9.7.1.0
```
> `--target-ecos` (optional) is the ECOS release of the target appliance(s). It is recorded for the report (`ledger.json`, `summary.md`); no rule depends on it at present. A static route with next hop `0.0.0.0` or `::` and a named interface, which ECOS 9.7 rejects on apply, is written without the interface for any ECOS (`docs/decisions/0016`).
> `--target-release` (default `9.7.1.42046`) must be the release of the Orchestrator that validates and applies. Its default template comes from `references/default-templates/` (bundled) or the work folder's `reference/default-templates/` (downloaded by capture; bundled wins): the exact build, else the newest template of the same major.minor (any 9.7.x uses 9.7.1.42046), else the nearest older release, marked untested (`docs/decisions/0014`); `summary.md` and `ledger.json` (`default_template`) say which and why. Writes `./preconfig-output/retail-branch/` containing `retail-branch.jinja2`, `retail-branch.csv` (blank headers), `example.csv` (pre-filled reference row with `preconfig_name` and `serial_number`), `columns.json`, `columns.md` (what to enter in each column), `reference.yaml`, `variables.csv`, `ledger.json`, and `summary.md` (the toolkit version, then every item the YAML does not carry, grouped: toolkit errors, gaps, not readable, outside preconfig, owned by templates or Orchestrator, then the verdict). Edit `variables.csv` and re-run with `--variables variables.csv`; a run without `--variables` writes a fresh sheet and keeps the old one as `variables.previous.csv`.

### Validate Rows, Emit `.yml` Files Immediately & Upload on Approval
Copy `example.csv` to `sites.csv` and add a row per site (`preconfig_name`, optional `serial_number`, and the columns `columns.md` explains). `--csv` is required.

```bash
# Step 5a: Check each cell offline (address format, ranges, subnets), validate each row in memory and immediately write rendered/<preconfig_name>.yml for valid rows (0 uploads)
python3 "$SKILL_SCRIPTS/upload.py" \
  --site-dir ./preconfig-output/retail-branch \
  --csv ./preconfig-output/retail-branch/sites.csv \
  --orchestrator lab

# Step 5b: After reviewing the validation & conflict table, upload valid non-conflicting rows to Orchestrator
python3 "$SKILL_SCRIPTS/upload.py" \
  --site-dir ./preconfig-output/retail-branch \
  --csv ./preconfig-output/retail-branch/sites.csv \
  --orchestrator lab \
  --approve
```

## Output Directory Structure

The stage scripts write to `./preconfig-output/<site-type>/` in your current working directory (`--out` names another) and keep everything on disk:

```text
./preconfig-output/<site-type>/
├── <site-type>.jinja2      # Reusable Jinja2 template (uses data['col'] for pyedgeconnect compatibility)
├── <site-type>.csv         # Blank CSV header template (includes preconfig_name and serial_number)
├── example.csv             # Single-row CSV populated from the reference appliance (secrets left blank)
├── reference.yaml          # Translated preconfig YAML of the reference appliance
├── variables.csv           # One row per field: decision (variable / fixed), column name, linked column, value, path, reason; edit it and re-run with --variables
├── variables.previous.csv  # The sheet a re-run replaced (your earlier edits)
├── proposal.md / .json, decisions.yaml   # Only with --decisions that sets per-section modes (the older way)
├── ledger.json             # Complete audit trail (mapped, unresolved = gap, outside-preconfig, template-group, runtime, ...)
├── summary.md              # Toolkit version and the grouped items to report
├── columns.json            # Column metadata (YAML path, scalar type, address format, optional/secret flags)
├── columns.md              # What to enter in each column: format, allowed values, the reference value, the fields it fills
├── checks.json             # check.py results
├── validation.json         # validate.py results (for files in rendered/)
├── upload-report.json      # Per-row validation, conflict, and upload report
└── rendered/
    ├── Store-010-Primary.yml
    └── Store-020-Primary.yml
```

The command line keeps the same files per build, plus its own:

```text
./preconfig-output/<hostname>/
├── build.json              # source and validator Orchestrator, capture folder, target release, toolkit version
├── <hostname>.jinja2, <hostname>.csv, example.csv, reference.yaml, variables.csv, ledger.json, summary.md, columns.*, checks.json, rendered/
├── site-<name>/            # from variables or compare: the same files for the chosen columns, plus sites.csv
└── <hostname>.previous-<UTC stamp>/   # an earlier build of the same appliance
```

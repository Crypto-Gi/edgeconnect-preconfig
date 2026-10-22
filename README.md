# EdgeConnect Preconfiguration Skill & Toolkit (`edgeconnect-preconfig`)

Turn any existing HPE Aruba EdgeConnect SD-WAN appliance into a verified Jinja2 + CSV preconfiguration cookie-cutter — and safely validate, render, and stage per-site preconfigs across Orchestrator `9.6.x`, `9.7.x`, and `10.0.x`.

---

## What & Why

Writing EdgeConnect preconfiguration YAML files by hand is slow and brittle: there are 41 sections, strict cross-field dependencies, `replace` vs. `merge` post-approval behaviors, and standard YAML 1.1 parsers silently corrupt values like BGP route targets (`1:1` -> `61`) or leading-zero ZIP codes (`08034` -> octal error).

This repository packages a **lean, self-contained AI skill (`skills/building-edgeconnect-preconfig-templates/`)** plus a **deterministic Python toolkit and test suite (`tests/`)**:
- **Works standalone or with any AI coding agent:** Run the Python scripts directly from your terminal, or let your AI agent drive them conversationally, translate customer spreadsheets into `<site>.csv`, adapt site topologies, and self-heal validation errors.
- **Zero bloat when installed:** Installing `skills/building-edgeconnect-preconfig-templates/` copies only `SKILL.md`, `references/`, and the 13 runtime `scripts/` into your agent (no repository READMEs or test harnesses).
- **Verified against real hardware & virtual fabrics:** Proven by option and mutation tests, captures of real appliances from several Orchestrators (`9.3.7`–`10.0.0`), and live appliance apply-and-diff runs, including every section that `/validate` alone could not prove (IPv6 and router advertisements, DHCP failover, NAT pool references, AppExpress groups, all four IDS/IPS modes).

---

## The EdgeConnect toolkit

Three repositories that work together:

| Repository | What it does | Agent skill |
|---|---|---|
| [edgeconnect-orchestrator-automation](https://github.com/Crypto-Gi/edgeconnect-orchestrator-automation) | `edgeconnect-auto` CLI: deploys and removes firewall policies, zones, address, service and application groups, application definitions and template-group ACLs from reviewed CSVs, with a dry run, a typed `APPLY` and readback verification. | `managing-edgeconnect-firewall-and-objects` |
| [edgeconnect-preconfig](https://github.com/Crypto-Gi/edgeconnect-preconfig) (this repository) | Turns an existing appliance into a Jinja2 + CSV preconfiguration template, then renders, validates and uploads one preconfig per site. | `building-edgeconnect-preconfig-templates` |
| [edgeconnect-mcp](https://github.com/Crypto-Gi/edgeconnect-mcp) | MCP server that lets Claude or any MCP client read, and with your approval change, an Orchestrator through its REST API. | `edgeconnect-guide` |

**Install the CLI, the preconfig scripts and their two skills** (macOS and Linux; needs git, Python 3.9 or later and, for the skills, Node.js 18 or later):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Crypto-Gi/edgeconnect-preconfig/main/setup.sh)
```

The same `setup.sh` is in all three repositories. It asks a few questions, then does everything else:

1. **Which repositories**: a table of the three, with what each does and the skill it installs; type numbers separated by commas (for example `1,3`) or `all` (the default).
2. **The MCP server** (only if you pick 3): run it locally (stdio) for your agents, or in Docker over HTTPS, which you install by hand with the [MCP install guide](https://github.com/Crypto-Gi/edgeconnect-mcp/blob/main/docs/install.md), or not at all.
3. **Which agents**, once, from a table of 20 (Claude Code, Codex, Cursor, GitHub Copilot, Gemini CLI, OpenCode, Kilo Code, Kiro CLI, Zed, Windsurf, Cline, Goose, Antigravity, Devin, Roo Code, Continue, Amp, Claude Desktop, and any agent that reads `.agents/skills`). Every picked skill is installed for them, also when it was already installed, and the local MCP server is added to them: through [`add-mcp`](https://www.npmjs.com/package/add-mcp) (without auto-approving tool calls), through `devin mcp add` for Devin, or as a printed config to paste for agents neither covers. Before adding the server it asks to continue, because an existing server named `edgeconnect` in those agents is replaced; with `--yes` it adds the server only when you also pass `--mcp stdio`. It prints each agent's MCP settings file as it goes.
4. **The install folder** (default: the folder you run it from, or `~/edgeconnect` from your home folder).
5. **Where the skills and the MCP server go**, one answer for both: only that folder (the default) or your user account (`~`). Agents that keep MCP servers only for the whole user account (Windsurf, Cline, Goose, Antigravity, Claude Desktop) are asked about one by one when you chose the folder: yes adds the server user-wide for that agent, no skips it. With `--yes` they are skipped, and the last screen lists the command to add each one.

Everything lands in the one folder, from `main`: the repositories, for 1 and 2 a Python environment and the `edgeconnect-auto` (1) and `edgeconnect-preconfig` (2) commands in `~/.local/bin`, for a local MCP server its launcher `<folder>/bin/edgeconnect-mcp`, and one shared `.env`. The last screen shows how to fill in `.env` for what you picked: the Orchestrator lines, the API key, a self-signed certificate, `EC_VALIDATORS` for preconfig and MCP writes. It also writes `AGENTS.md` (which tool an agent should use for which task, and what this folder has installed) and a `CLAUDE.md` that points to it, so start your agent there; an `AGENTS.md` that `setup.sh` wrote and you never edited is kept up to date on every run, and one you have edited is never overwritten (the new text goes to `AGENTS.md.new`). Run it again to update. Options for answering without prompts: `--repos 1,3` (or `all`), `--mcp stdio|docker|no`, `--agents 1,2,15` (numbers or names such as `claude-code,codex`), `--dir PATH`, `--skills folder|home`, `--yes`.

Picking 3 installs the MCP guide skill, `edgeconnect-guide`, and, if you choose local, the stdio MCP server for your agents; a Docker server is installed by hand, as the [edgeconnect-mcp README](https://github.com/Crypto-Gi/edgeconnect-mcp#install) covers.

---

## Use It Without an AI: the Command Line

`setup.sh` installs the `edgeconnect-preconfig` command. Six verbs cover the whole job; `edgeconnect-preconfig --help` lists them, and each takes `--help`.

```bash
edgeconnect-preconfig list --orchestrator lab                      # test the .env profile and list appliances: hostname, nePk, model, ECOS, site, reachable
edgeconnect-preconfig build --orchestrator lab --appliance Branch-01     # read it (GET only), convert, check; kept only if /validate accepts it
edgeconnect-preconfig variables ./preconfig-output/Branch-01             # choose the per-site settings: Top 10, Top 20 or all, by section
edgeconnect-preconfig compare ./preconfig-output/Branch-01 Branch-07 Branch-12   # or let sites of the same design choose; a CSV row per site
edgeconnect-preconfig validate ./preconfig-output/Branch-01/site-branch --csv sites.csv
edgeconnect-preconfig upload ./preconfig-output/Branch-01/site-branch --csv sites.csv            # table only
edgeconnect-preconfig upload ./preconfig-output/Branch-01/site-branch --csv sites.csv --approve  # uploads the valid, non-conflicting rows
```

- **build** writes `preconfig-output/<hostname>/`: `reference.yaml`, the template, `example.csv`, `summary.md` with the verdict, and `build.json`. Nothing is kept unless Orchestrator's `/validate` accepts the result. Validation runs on the Orchestrator the appliance came from; `--validate-on <lab>` names another. Either way it must be listed in `EC_VALIDATORS`. An earlier folder of the same name is renamed, never overwritten.
- **variables** shows the settings as a tick list. Hostname, serial, secrets and every Deployment-page and appliance-info value (addresses, next hops, bandwidths, DHCP, location) are always per site, no questions; interface names, labels, zones, segments, firewall and deployment mode stay fixed unless you flip them. The Top 10 and Top 20 are the settings that differed most often between appliances of the same design in the captured corpus (`references/variable-ranking.json`); "All" opens one section at a time. Type numbers to flip a setting, a word to search, Enter when done. A gateway that equals an interface address follows that address. `--pick top10 --yes` and `--set column,...` do the same without questions.
- **compare** reads the other sites (GET only), makes every setting whose value differs between them per site, and asks you to confirm or edit. Its `sites.csv` has a row for every site with that site's own values (serials and secrets blank), and every row is validated.
- **variables** and **compare** write `site-<name>/` inside the build folder; the build's own files never change. The old stage scripts still run by name (`edgeconnect-preconfig capture ...`); the live-versus-cached check is now `cache-check`.

---

## Quick Start (5 Steps)

> **Everything at once:** see [The EdgeConnect toolkit](#the-edgeconnect-toolkit) above for the one-line `setup.sh` install.

### 1. Clone & Install Python Dependencies
Requires **Python 3.9+** and two packages (`Jinja2` and `PyYAML`; the EdgeConnect Orchestrator REST client is bundled in `scripts/common.py` using Python's standard library):

```bash
git clone https://github.com/Crypto-Gi/edgeconnect-preconfig.git
cd edgeconnect-preconfig
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure `.env`
Copy `.env.example` to `.env` in your working directory (or set `EC_DOTENV=/path/to/.env`):

```dotenv
# Read-only reference Orchestrator profile
orch_1=prod
prod_base_url=https://orchestrator.example.com
prod_api_key=YOUR_READ_ONLY_API_KEY

# Lab / staging Orchestrator profile (used for /validate and optional upload)
orch_2=lab
lab_base_url=https://lab-orchestrator.example.com
lab_api_key=YOUR_LAB_API_KEY
# Self-signed lab certificate: pin it instead of turning checks off.
# lab_tls_sha256=<SHA-256 fingerprint, e.g. from: openssl x509 -noout -fingerprint -sha256>
EC_VALIDATORS=lab
```

### 3. Capture a Reference Appliance (Read-Only `GET`)
```bash
SKILL_SCRIPTS="skills/building-edgeconnect-preconfig-templates/scripts"

python3 "$SKILL_SCRIPTS/capture.py" --orchestrator prod --appliance 2.NE --role reference
```
> Captures live and cached appliance state via `GET` only, redacts all passwords/PSKs/RADIUS keys/certificates to `<SECRET:path>` before touching disk, and automatically downloads `GET /gms/appliance/preconfiguration/default` into `references/default-templates/orchestrator-<release>.yml` if the Orchestrator release is new.

### 4. Generate the Site-Type Jinja2 & CSV Template
```bash
python3 "$SKILL_SCRIPTS/translate.py" \
  --capture ./preconfig-work/reference/prod/2.NE/<UTC_STAMP> \
  --site-type retail-branch
```
> Writes `./preconfig-output/retail-branch/` containing `retail-branch.jinja2`, `retail-branch.csv` (blank headers), `example.csv` (pre-filled reference row with `preconfig_name` and `serial_number`), `columns.json`, `columns.md` (what to enter in each column), `reference.yaml`, `variables.csv`, `ledger.json`, and `summary.md` (the toolkit version, then every item the YAML does not carry, grouped: toolkit errors, gaps, outside preconfig, owned by templates or Orchestrator).

### 5. Validate Rows, Emit `.yml` Files Immediately & Upload on Approval
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

---

## Installing & Using the Skill in AI Coding Agents

This skill follows the open [Agent Skills specification](https://agentskills.io) (`SKILL.md` + `scripts/` + `references/`).

```bash
# One-command install via npx skills (Claude Code, Codex, Cursor, Windsurf, OpenCode, etc.)
npx skills add Crypto-Gi/edgeconnect-preconfig
```

| Platform | Global Skill Path | Project-Level Skill Path | How to Invoke |
|---|---|---|---|
| **Claude Code (CLI)** | `~/.claude/skills/building-edgeconnect-preconfig-templates/` | `.claude/skills/building-edgeconnect-preconfig-templates/` | `/building-edgeconnect-preconfig-templates` or ask naturally in chat |
| **Claude Cowork / Desktop** | Upload `dist/building-edgeconnect-preconfig-templates.zip` (or `.skill`) in **Settings -> Capabilities -> Skills** | Attach skill folder to Cowork workspace | Automatically triggered when you ask to build or validate EdgeConnect preconfigs |
| **OpenAI Codex (CLI & App)** | `~/.codex/skills/building-edgeconnect-preconfig-templates/` or `~/.agents/skills/building-edgeconnect-preconfig-templates/` | `.agents/skills/building-edgeconnect-preconfig-templates/` | `$building-edgeconnect-preconfig-templates` or ask naturally |
| **Devin (CLI & IDE)** | `~/.config/devin/skills/building-edgeconnect-preconfig-templates/` | `.devin/skills/building-edgeconnect-preconfig-templates/` or `.agents/skills/building-edgeconnect-preconfig-templates/` | `/building-edgeconnect-preconfig-templates` or automatic skill matching |
| **Windsurf** | `~/.codeium/windsurf/skills/building-edgeconnect-preconfig-templates/` | `.windsurf/skills/building-edgeconnect-preconfig-templates/` or `.agents/skills/building-edgeconnect-preconfig-templates/` | `@building-edgeconnect-preconfig-templates` or automatic trigger in Cascade |
| **Google Antigravity** | `~/.gemini/antigravity/skills/building-edgeconnect-preconfig-templates/` | `.agent/skills/building-edgeconnect-preconfig-templates/` or `.agents/skills/building-edgeconnect-preconfig-templates/` | Automatically loaded from `.agent/skills/` or `.agents/skills/` |
| **OpenCode** | `~/.config/opencode/skills/building-edgeconnect-preconfig-templates/` | `.opencode/skills/building-edgeconnect-preconfig-templates/` or `.agents/skills/building-edgeconnect-preconfig-templates/` | Loaded automatically via OpenCode's native `skill` tool |
| **VS Code (Copilot Agent)** | `~/.claude/skills/building-edgeconnect-preconfig-templates/` | `.github/skills/building-edgeconnect-preconfig-templates/` | Enable `"chat.useAgentSkills": true` in VS Code Settings, then reference in Copilot Chat |

### Manual Copy Commands (Installs Only the Lean Skill Folder)

```bash
# Universal project-level install (Codex, Devin, Windsurf, Antigravity, OpenCode)
mkdir -p .agents/skills && cp -R skills/building-edgeconnect-preconfig-templates .agents/skills/

# Claude Code (global)
mkdir -p ~/.claude/skills && cp -R skills/building-edgeconnect-preconfig-templates ~/.claude/skills/

# OpenAI Codex (global)
mkdir -p ~/.codex/skills && cp -R skills/building-edgeconnect-preconfig-templates ~/.codex/skills/

# Devin CLI (global)
mkdir -p ~/.config/devin/skills && cp -R skills/building-edgeconnect-preconfig-templates ~/.config/devin/skills/

# Windsurf (global)
mkdir -p ~/.codeium/windsurf/skills && cp -R skills/building-edgeconnect-preconfig-templates ~/.codeium/windsurf/skills/

# Google Antigravity (global)
mkdir -p ~/.gemini/antigravity/skills && cp -R skills/building-edgeconnect-preconfig-templates ~/.gemini/antigravity/skills/

# OpenCode (global)
mkdir -p ~/.config/opencode/skills && cp -R skills/building-edgeconnect-preconfig-templates ~/.config/opencode/skills/

# VS Code GitHub Copilot (workspace)
mkdir -p .github/skills && cp -R skills/building-edgeconnect-preconfig-templates .github/skills/
```

---

## Conversational Example (With Any Supported AI Agent)

1. **Capture & propose:**
   > *"Read appliance `branch-nyc-01` (or `2.NE`) from Orchestrator `prod` and build a preconfig Jinja2 and CSV template named `retail-branch`."*
2. **Review the variables:**
   The agent presents `variables.csv`: every field with the proposed decision (`variable` or `fixed`) and the reason; variables come first, then the rows marked `(check)` that need an owner decision. Reply in plain English:
   > *"Make the DNS servers per site, keep the region fixed, and use `core_ip` as the name of the LAN address."*
   The agent edits the sheet and re-runs `translate.py --variables variables.csv`. To replace the guesses with evidence, capture two or three more appliances of the same type and run `translate.py ... --compare <capture> ...`: fields that differ between the sites become variables and fields equal everywhere stay fixed.
3. **Validate rows & generate YAMLs:**
   Add rows for your new sites in `sites.csv` (or ask the agent to convert your IP plan spreadsheet into `sites.csv`), then say:
   > *"Validate `sites.csv` against `lab` and generate the YAML files."*
4. **Approve upload to Orchestrator:**
   After reviewing the validation and conflict table, say:
   > *"Approved — upload the ready preconfigs to `lab`."*

---

## Output Directory Structure

All generated artifacts are written to `./preconfig-output/<site-type>/` in your current working directory and kept permanently on disk:

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
├── upload-report.json      # Per-row validation, conflict, and upload report
└── rendered/
    ├── Store-010-Primary.yml
    └── Store-020-Primary.yml
```

---

## Repository Structure

```text
edgeconnect-preconfig/
├── README.md                                 # Repository overview, install guide & CLI usage
├── LICENSE                                   # Apache License 2.0
├── NOTICE                                    # Copyright, trademarks, bundled templates
├── requirements.txt                          # Jinja2>=3.1.0, PyYAML>=6.0
├── setup.sh                                  # One-step install of the toolkit and skills (see above)
├── .env.example                              # Sanitized Orchestrator profile template
├── docs/
│   ├── TESTING_AND_VERIFICATION.md           # Verification report: unit tests, captures, live applies
│   └── decisions/                            # Decision records
├── skills/
│   └── building-edgeconnect-preconfig-templates/   # <-- Lean installable skill (23 files: zero README/test bloat)
│       ├── SKILL.md
│       ├── references/                       # 9.6/9.7/10.0 default templates, endpoint-catalogue, known-constraints, lessons, validate-behaviour
│       └── scripts/                          # 16 runtime scripts (cli, capture, cache_check, translate, mappers, decisions, variables, generate, cells, render, check, validate, upload, synth, options, common)
├── tests/                                    # offline unit tests & maintainer lab harnesses
│   ├── test_capture.py
│   ├── test_cells.py
│   ├── test_decisions.py
│   ├── test_generate.py
│   ├── test_mappers.py
│   ├── test_raw_diff.py
│   ├── test_variables.py
│   └── harnesses/                            # apply_and_diff.py, corpus.py, probes.py, raw_diff.py, scan.py, suite.py
└── dist/                                     # Pre-built archives for Claude Desktop / Cowork upload
    ├── building-edgeconnect-preconfig-templates.zip
    └── building-edgeconnect-preconfig-templates.skill
```

---

## Safety & Scope Boundaries

- **What this project does:** Read-only appliance configuration capture (`GET` only), secret redaction, Jinja2 + CSV preconfig template generation, YAML 1.2 validation, non-saving Orchestrator `/validate` checks, immediate local `.yml` emission for valid rows, and approval-gated preconfig staging (`POST /gms/appliance/preconfiguration`).
- **What this project does NOT do:** It never writes to read-only reference Orchestrators, never writes `.yml` files for rows that fail validation, never uploads without `--approve`, never overwrites existing Orchestrator `preconfig_name` or `serial_number` entries (conflicting rows are skipped while valid non-conflicting rows proceed), and never modifies shared day-2 Template Groups or Business Intent Overlays directly.

---

## Running Tests & Verification History

See [docs/TESTING_AND_VERIFICATION.md](docs/TESTING_AND_VERIFICATION.md) for the complete verification report (option tests, multi-Orchestrator corpus appliances `9.3.7`–`10.0.0`, live apply-and-diff runs), with the apply-time behaviours that `/validate` does not catch.

```bash
python3 -m unittest discover -s tests
```

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

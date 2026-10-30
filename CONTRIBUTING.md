# Contributing

Issues and pull requests are welcome. Report a security problem privately: the **Report a vulnerability** button under the repository's **Security** tab.

## Ground rules

- **No customer data.** Never commit captures, `.env` files, appliance names, site names, serials, API keys or real IP addresses; tests and docs use placeholders (`example.com`, `192.0.2.0/24`). Captures and output stay in the git-ignored `preconfig-work/` and `preconfig-output/`.
- **The toolkit never applies.** `tests/test_no_apply.py` fails if any script or test calls the apply API or sets `autoApply` true ([decision 0008](docs/decisions/0008-the-toolkit-cannot-apply.md)).
- **Decisions** that the code must keep live in [docs/decisions/](docs/decisions/); read the ones for the area you change, and add one when you make a rule.
- **Every `.env` key** the scripts read is documented in [`.env.example`](.env.example); `tests/test_env_example.py` fails otherwise.
- **Edit `toolkit/`, never the skill's copy.** `toolkit/` (`VERSION`, `scripts/`, `references/`, and `gui/`, which the skill does not copy) is the only source; after changing it, run `python3 tools/build_skill.py` to copy it into `skills/building-edgeconnect-preconfig-templates/` and rebuild `dist/`. `tests/test_layout.py` fails while the copy or `dist/` is out of date ([decision 0012](docs/decisions/0012-toolkit-is-the-source-the-skill-ships-a-copy.md)).

## Running the tests

Offline, no Orchestrator needed; CI runs them on Python 3.9 and 3.13, on Linux and macOS:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
EC_DOTENV=/nonexistent/.env python3 -m unittest discover -s tests
```

`EC_DOTENV=/nonexistent/.env` is what CI sets: no `.env` of yours is read, so no test can reach an Orchestrator. `tests/test_golden.py` pins what each command prints and writes (`tests/golden/`); after a deliberate change of output, regenerate with `EDGECONNECT_UPDATE_GOLDEN=1 python3 -m unittest tests.test_golden` and review the diff of `tests/golden/`.

The browser tests in `tests/e2e/` (the GUI from an appliance to written YAML files, and axe-core on every screen in both themes) are skipped there. CI's browser job runs them; to run them yourself:

```bash
pip install playwright==1.64.1 && python -m playwright install chromium
npm install --prefix /tmp/axe axe-core@4.10.3
EDGECONNECT_BROWSER_TESTS=1 AXE_CORE_JS=/tmp/axe/node_modules/axe-core/axe.min.js python3 -m unittest discover -s tests -p test_browser.py
```

`EDGECONNECT_BROWSER_PATH` names a Chromium binary to use instead of Playwright's own, and `AXE_REPORT=FILE` writes the axe-core results to that file. One fleet test in `tests/test_gui_fleet.py` also compares with an `edgeconnect-orchestrator-automation` checkout: it looks for one beside this repository, or at `EDGECONNECT_AUTOMATION_SRC` (that checkout's `src/` folder), and is skipped when there is none. `python3 tests/e2e/gui_launcher.py` starts the GUI over the same fake Orchestrator and prints its link, for a look by hand.

The lab harnesses in `tests/harnesses/` need a lab Orchestrator and are run by maintainers; [docs/TESTING_AND_VERIFICATION.md](docs/TESTING_AND_VERIFICATION.md) records what they proved.

## Repository structure

```text
edgeconnect-preconfig/
├── README.md                                 # Overview, quick start and links
├── CONTRIBUTING.md                           # This file
├── CHANGELOG.md                              # What changed in each release
├── .github/workflows/test.yml                # CI: the offline tests on Python 3.9 and 3.13, Linux and macOS; the GUI in Chromium
├── LICENSE                                   # Apache License 2.0
├── NOTICE                                    # Copyright, trademarks, bundled templates
├── requirements.txt                          # Jinja2>=3.1.0, PyYAML>=6.0
├── setup.sh                                  # One-step install of the toolkit and skills (docs/INSTALL.md)
├── .env.example                              # Sanitized Orchestrator profile template
├── docs/
│   ├── INSTALL.md                            # setup.sh walkthrough, manual install, .env
│   ├── COMMAND_LINE.md                       # every command, errors, stage scripts, output folders
│   ├── GUI.md                                # the local web GUI: starting it, each screen, uploads
│   ├── AGENT_SKILLS.md                       # installing the skill in each AI agent
│   ├── TESTING_AND_VERIFICATION.md           # Verification report: unit tests, captures, live applies
│   ├── assets/demo.gif                       # README demo (a lab run, names and addresses replaced)
│   ├── gui/BUILD_PLAN.md                     # the GUI's decisions and build steps (built on the gui branch)
│   └── decisions/                            # Decision records
├── toolkit/                                  # <-- the code: edit here; runs without any agent
│   ├── VERSION                               # toolkit version
│   ├── references/                           # 9.6/9.7/10.0 default templates, endpoint-catalogue, known-constraints, lessons, validate-behaviour, variable-ranking.json
│   ├── scripts/                              # 17 runtime scripts (cli, capture, cache_check, translate, mappers, decisions, variables, generate, cells, render, check, validate, upload, synth, options, runlog, common)
│   └── gui/                                  # the local web GUI (python3 -m toolkit.gui): server, tasks, engine, fleet status, approval, static/
├── skills/
│   └── building-edgeconnect-preconfig-templates/   # <-- installable skill: SKILL.md + a copy of toolkit/ (tools/build_skill.py)
├── tools/build_skill.py                      # copies toolkit/ into the skill and rebuilds dist/; --check reports drift
├── tests/                                    # offline unit tests & maintainer lab harnesses
│   ├── test_*.py                             # one file per script or area (capture, cells, cli, mappers, translate_robust, upload_yaml, ...)
│   ├── test_gui_*.py                         # the GUI: approval, engine, fleet, server, shell, tasks, tables, pick, compare, make, inventory, design
│   ├── test_golden.py                        # what each command prints and writes, against golden/ (EDGECONNECT_UPDATE_GOLDEN=1 regenerates)
│   ├── test_no_apply.py                      # fails if any script or test calls the apply API or sets autoApply true
│   ├── test_validators.py                    # /validate runs only on nicknames listed exactly in EC_VALIDATORS
│   ├── test_env_example.py                   # every .env key the scripts and the GUI read is documented in .env.example
│   ├── test_layout.py                        # the skill copy and dist/ match toolkit/; the work folder is found from both
│   ├── golden/                               # the pinned outputs
│   ├── fixtures/                             # synthetic captures for compare (compare/)
│   ├── e2e/                                  # the GUI in a browser (Playwright, axe-core); the CI browser job runs it
│   └── harnesses/                            # corpus.py, lab_diff.py, probes.py, rank_variables.py, raw_diff.py, scan.py, suite.py
└── dist/                                     # Pre-built archives for Claude Desktop / Cowork upload
    ├── building-edgeconnect-preconfig-templates.zip
    └── building-edgeconnect-preconfig-templates.skill
```

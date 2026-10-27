# Contributing

Issues and pull requests are welcome. Report a security problem privately: the **Report a vulnerability** button under the repository's **Security** tab.

## Ground rules

- **No customer data.** Never commit captures, `.env` files, appliance names, site names, serials, API keys or real IP addresses; tests and docs use placeholders (`example.com`, `192.0.2.0/24`). Captures and output stay in the git-ignored `preconfig-work/` and `preconfig-output/`.
- **The toolkit never applies.** `tests/test_no_apply.py` fails if any script or test calls the apply API or sets `autoApply` true ([decision 0008](docs/decisions/0008-the-toolkit-cannot-apply.md)).
- **Decisions** that the code must keep live in [docs/decisions/](docs/decisions/); read the ones for the area you change, and add one when you make a rule.
- **Every `.env` key** the scripts read is documented in [`.env.example`](.env.example); `tests/test_env_example.py` fails otherwise.
- **The skill folder ships as is:** after changing anything under `skills/building-edgeconnect-preconfig-templates/`, rebuild `dist/` (`python3 -c "import shutil; shutil.make_archive('dist/building-edgeconnect-preconfig-templates', 'zip', root_dir='skills', base_dir='building-edgeconnect-preconfig-templates')"`, then copy the zip to `dist/building-edgeconnect-preconfig-templates.skill`).

## Running the tests

Offline, no Orchestrator needed; CI runs them on Python 3.9 and 3.13:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 -m unittest discover -s tests
```

The lab harnesses in `tests/harnesses/` need a lab Orchestrator and are run by maintainers; [docs/TESTING_AND_VERIFICATION.md](docs/TESTING_AND_VERIFICATION.md) records what they proved.

## Repository structure

```text
edgeconnect-preconfig/
├── README.md                                 # Overview, quick start and links
├── CONTRIBUTING.md                           # This file
├── .github/workflows/test.yml                # CI: the offline tests on Python 3.9 and 3.13
├── LICENSE                                   # Apache License 2.0
├── NOTICE                                    # Copyright, trademarks, bundled templates
├── requirements.txt                          # Jinja2>=3.1.0, PyYAML>=6.0
├── setup.sh                                  # One-step install of the toolkit and skills (see above)
├── .env.example                              # Sanitized Orchestrator profile template
├── docs/
│   ├── INSTALL.md                            # setup.sh walkthrough, manual install, .env
│   ├── COMMAND_LINE.md                       # every command, errors, stage scripts, output folders
│   ├── AGENT_SKILLS.md                       # installing the skill in each AI agent
│   ├── TESTING_AND_VERIFICATION.md           # Verification report: unit tests, captures, live applies
│   ├── assets/demo.gif                       # README demo (a lab run, names and addresses replaced)
│   └── decisions/                            # Decision records
├── skills/
│   └── building-edgeconnect-preconfig-templates/   # <-- Lean installable skill (26 files: zero README/test bloat)
│       ├── SKILL.md
│       ├── VERSION                           # toolkit version (1.2.5)
│       ├── references/                       # 9.6/9.7/10.0 default templates, endpoint-catalogue, known-constraints, lessons, validate-behaviour, variable-ranking.json
│       └── scripts/                          # 16 runtime scripts (cli, capture, cache_check, translate, mappers, decisions, variables, generate, cells, render, check, validate, upload, synth, options, common)
├── tests/                                    # offline unit tests & maintainer lab harnesses
│   ├── test_capture.py
│   ├── test_cells.py
│   ├── test_cli.py
│   ├── test_common_tls.py
│   ├── test_decisions.py
│   ├── test_generate.py
│   ├── test_mappers.py
│   ├── test_no_apply.py                      # fails if any script or test calls the apply API or sets autoApply true
│   ├── test_raw_diff.py
│   ├── test_report.py
│   ├── test_validators.py                    # /validate runs only on nicknames listed exactly in EC_VALIDATORS
│   ├── test_env_example.py                   # every .env key the scripts read is documented in .env.example
│   ├── test_variables.py
│   └── harnesses/                            # corpus.py, lab_diff.py, probes.py, rank_variables.py, raw_diff.py, scan.py, suite.py
└── dist/                                     # Pre-built archives for Claude Desktop / Cowork upload
    ├── building-edgeconnect-preconfig-templates.zip
    └── building-edgeconnect-preconfig-templates.skill
```

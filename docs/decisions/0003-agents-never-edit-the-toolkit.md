# Agents using the skill never edit the toolkit; they name gaps

Status: accepted (owner, 2026-10-21). Supersedes the "follows the self-healing playbook for ledger gaps" part of 0002.

An agent that uses the installed skill changes only inputs (`variables.csv`, the site CSV, a `--decisions` file) and re-runs the scripts. It never edits `scripts/`, `references/`, `SKILL.md` or `VERSION`; when the toolkit falls short it reports the gap or toolkit error with its evidence, and the maintainers fix it in this repository with a test. The owner's words: "installed agents never edit the toolkit, they name gaps; and the report states the toolkit version and says to refresh if it's old."

Consequences: `summary.md` opens with the toolkit version and flags local edits (`common.toolkit_version`; cached default templates do not count); the diagnosis playbook in `references/lessons.md` section 4 reports instead of fixing.

Checked by: `tests/test_report.py`.

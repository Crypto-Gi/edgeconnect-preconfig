# Other sites decide the variables; the agent never writes output

Status: accepted (owner, 2026-10-19); the self-healing playbook sentence is superseded by 0003

`translate.py --compare <capture> ...` classifies a field by what the other captured sites of the same type show: it differs, so it is a variable; it is equal everywhere, so it is fixed; the others lack it, so it is fixed and flagged as a different site type. This replaces the name-based guesses (bandwidth, peers, "address outside the subnet") whenever other sites are given, because evidence beats a guess. The site's own interface addresses, secrets and the hostname stay columns, and the comparison always writes a fresh sheet.

The skill (SKILL.md) now says the agent runs the scripts in order, turns the owner's words into edits of `variables.csv` and follows the self-healing playbook for ledger gaps; it never writes `reference.yaml`, a template or a rendered file by hand. Every output comes from code, so the same inputs give the same files.

Considered: forcing leaves that exist at only one site into the sheet (a third WAN is a different site type, the same reasoning as 0001); matching list items by name instead of position (more code, and the same-type sites in use share their layout).

Checked by: `tests/test_variables.py`.

# toolkit/ is the only source; the skill ships a generated copy

Status: accepted (owner, 2026-10-23)

The scripts, their reference data and `VERSION` lived only inside the skill folder, although the command line works without any agent. The owner chose to move them out and keep both: `toolkit/` (`VERSION`, `scripts/`, `references/`) is the only place they are edited, and `skills/building-edgeconnect-preconfig-templates/` keeps `SKILL.md` plus a copy of `toolkit/`, so the skill still works where it is installed alone (`npx skills add`, a Claude Desktop upload of `dist/`). `tools/build_skill.py` makes the copy and rebuilds `dist/`; the layout inside both folders is the same, so the scripts find `references/` and `VERSION` from either. `setup.sh` runs the command from `toolkit/scripts`, and from the skill's copy for a checkout older than 1.3.0.

Checked by: `tests/test_layout.py` (the copy and `dist/` match `toolkit/`; the install folder's `preconfig-work/` is found from both layouts).

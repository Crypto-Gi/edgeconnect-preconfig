# The skill holds instructions only; the code stays in toolkit/

Status: accepted (owner, 2026-11-06); supersedes [0012](0012-toolkit-is-the-source-the-skill-ships-a-copy.md)

The skill shipped a generated copy of `toolkit/` (`scripts/`, `references/`, `VERSION`) and `dist/*.zip` and `*.skill`, so it could run where it was installed alone. The owner asked to "decouple code from the skill and keep skills as separate ... we should not change or make our code in any shape or form". So `skills/building-edgeconnect-preconfig-templates/` now holds `SKILL.md` only: it runs the `edgeconnect-preconfig` command and reads `<install folder>/edgeconnect-preconfig/toolkit/references/`, and it needs the toolkit installed by `setup.sh` (or a clone with `requirements.txt` installed and the command on the PATH). `tools/build_skill.py` and `dist/` are gone, and no release ships a skill archive. Nothing under `toolkit/` changed. A skill installed on its own, such as a Claude Desktop upload, no longer runs anything.

Checked by: `tests/test_layout.py` (every `skills/*/` folder holds only `SKILL.md`; the install folder's `preconfig-work/` is found from both layouts).

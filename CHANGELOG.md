# Changelog

## 1.3.0 (2026-10-27)

Combines three changes that were open as separate pull requests (#4, #5, #6).

- **Code out of the skill folder** (`docs/decisions/0012`): `toolkit/` (`VERSION`, `scripts/`, `references/`) is the only source. The skill ships a copy made by `tools/build_skill.py`, which also rebuilds `dist/`. `tests/test_layout.py` fails while the copy or `dist/` differs from `toolkit/`.
- **Only keys the target template knows** (`docs/decisions/0013`): a preconfig holds a key path only if the target release's default template has it, or `TEMPLATE_OMISSIONS` lists it (10 paths `/validate` accepted on the corpus). Every dropped path is one `outside-preconfig` ledger line, and a dropped key never fails the build. This fixes `HTTP 400 Unrecognized field: "dot1x_acct_server_group"` from ECOS 9.5.x NAC.
- **ECOS 9.5.x payloads**:
  - The IP SLA and VRRP mappers no longer crash when `routeLabelId` or `tracker` is missing.
  - An HA group with `v6HaMask: 0` gets Orchestrator's default IPv6 pool.
  - A bonded `blanN`/`bwanN` with an empty speed counts as the default.
- **Capture**: reachability state 5 (sync in progress) counts as reachable, so live reads run.
- **Release families** (`docs/decisions/0014`): a release without its own default template uses the newest template of the same major.minor. Failing that, it uses the nearest older release (marked untested), then the nearest newer one. Only an empty template folder stops the build. `summary.md` and `ledger.json` (`default_template`) say which template was used and why.
- **Downloaded templates go to the work folder**: capture saves an Orchestrator's own default template in `<PRECONFIG_WORK>/reference/default-templates/`, never inside `toolkit/` or the skill. Template lookup searches both folders, and a bundled template wins over a download.
- **Missing PyYAML**: prints how to install it instead of a traceback.

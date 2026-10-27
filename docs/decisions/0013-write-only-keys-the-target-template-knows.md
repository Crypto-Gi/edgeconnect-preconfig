# A preconfig holds only keys the target release's default template knows

Status: accepted (owner, 2026-10-27)

An appliance can report settings the target Orchestrator's preconfig format rejects. ECOS 9.5.x returns `dot1x_acct_server_group` and `mac_acct_server_group` in each NAC `aaaProfile`; no default template (9.6.4, 9.7.1, 10.0) has them, and `/validate` answers `HTTP 400 Unrecognized field`. The earlier rule (`gated_paths`) only dropped keys that another release's template had, so a key no template knew went through. On 2026-10-27 the owner ruled: accept only the configuration the default template supports, and do not worry about anything else. `translate.prune` keeps a key path only if it is in `references/default-templates/orchestrator-<target>.yml` or in `TEMPLATE_OMISSIONS`, and writes one `outside-preconfig` ledger line for each path it drops, so nothing is dropped silently and the build never fails because of it.

`TEMPLATE_OMISSIONS` lists key paths the converter writes, the templates do not show, and `/validate` accepted on the corpus run of 2026-10-27 (83 appliances). Add a path there only with a `/validate` result that accepts it.

Checked by: `tests/test_mappers.py` (`Release`, `Ecos95`).

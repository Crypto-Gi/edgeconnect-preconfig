# /validate runs only on nicknames listed exactly in EC_VALIDATORS

Status: accepted (owner, 2026-10-23)

`common.configured_validators` used to allow, besides the nicknames in `EC_VALIDATORS`, any `orch_N` profile whose nickname contained `lab`, `vlab`, `test` or `sandbox`, and `lab` and `vlab` when nothing matched. A substring lets a production profile through by accident (`prod_testbed`, `lab_prod`). On 2026-10-23 the owner ruled that exact-name matching replaces substring matching: a nickname may run `/validate` only when `EC_VALIDATORS` in `.env` lists it, and with it unset no Orchestrator may. A refusal tells the operator to add the nickname to `EC_VALIDATORS` in `.env`. The auto-allow came with the first commit, reason not recorded; no earlier decision required it. `upload` is unchanged: it does not read `EC_VALIDATORS` and sends only with `--approve`.

Checked by: `tests/test_validators.py`.

Update 2026-10-30 (1.3.3): the `orch_validators` alias is no longer read. The automation CLI refuses unnumbered `orch_` keys, so the alias broke every `edgeconnect-auto` command when both tools share one `.env`.

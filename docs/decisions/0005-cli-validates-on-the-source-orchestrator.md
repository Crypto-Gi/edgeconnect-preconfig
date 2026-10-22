# The command line validates on the Orchestrator it read from

Status: accepted (owner, 2026-10-22)

`edgeconnect-preconfig` has six verbs for people working without an AI (appliances, build, variables, compare, validate, upload). The owner ruled that `build` validates on the same Orchestrator the appliance was read from unless the user names another (`--validate-on`), so names of groups, labels and segments match without substitution. The `EC_VALIDATORS` guard still applies: a source Orchestrator that is not listed stops `build` before anything is read, with a message naming both ways out. The folder records the validator in `build.json`, and `validate` and `upload` use it by default. Listing appliances also tests the `.env` profile (no separate check command), and the old live-versus-cached stage `compare` is now `cache-check`, because `compare` names the site comparison.

Considered: always validating on a lab Orchestrator (`--validate-on lab` as the default), rejected by the owner; a separate `orchestrators` check command and a `status` command, dropped to keep the tool minimal.

Checked by: `tests/test_cli.py::Build`, `tests/test_cli.py::Dispatch`.

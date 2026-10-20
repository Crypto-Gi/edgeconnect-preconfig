# Variables are chosen per field in variables.csv, not per section

Status: accepted (owner, 2026-10-19)

The section modes in `decisions.yaml` could only be steered with path regexes and made a column for every IP-looking value (about a third of the columns on 60 real appliances were interface names, `any` or addresses every site shares), and the same address in several places became several columns typed separately. `translate.py` now writes `variables.csv`, one row per field with a rule-based decision and reason; the owner edits the sheet and `--variables` re-reads it. Code produces every output byte; an LLM may only help edit the sheet.

Considered: an LLM choosing variables per run (different sheet each run, cannot be unit-tested); templates that compute DHCP networks and ranges from the interface address (breaks pyedgeconnect's plain `data['col']` rendering); a third WAN or extra route as a CSV cell (the shape of the site type is fixed by the reference; a different shape is a different site type).

Consequences: an address equal to an interface's address renders as `data['col'].split('/')[0]` (plain Jinja, still pyedgeconnect-compatible). Section modes keep working through `--decisions` for now.

Checked by: `tests/test_variables.py`.

Open: comparing several sites of one type (phase 4) will replace the "(check)" guess for addresses outside the site's subnets.

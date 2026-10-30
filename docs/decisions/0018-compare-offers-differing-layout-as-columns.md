# compare offers a differing zone, label, segment or firewall mode as a column

Status: accepted (owner, 2026-10-30); amends 0006 when other sites are compared

Decision 0006 keeps interface labels, zones, segments and firewall mode fixed. When the sites given to `compare` (or `translate.py --compare`) differ on one of these, the other sites silently got the base's value. The owner's ruling: "let the user decide". Such a setting is listed in compare's table with the other differing settings and is ticked as a column by default; the user may untick it in the existing tick list (answer `edit`), and compare then prints a one-line warning that the other sites get the base's value. `--yes` keeps the default (column). When the sites agree, or only one site is given, the setting stays fixed exactly as 0006 says. Interface names, interface type and deployment mode are not covered: a different interface name is a different design (no row for that site).

Checked by: `tests/test_cli.py::CompareLayoutChoice`.

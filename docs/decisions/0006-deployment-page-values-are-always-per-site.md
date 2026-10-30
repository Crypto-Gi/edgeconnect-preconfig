# Deployment page and appliance info values are always per site

Status: accepted (owner, 2026-10-22); supersedes the applianceInfo and deploymentInfo guesses of 0001; amended by 0018 for compared sites

Every value in `applianceInfo` and `deploymentInfo` (the appliance's initial settings and its Deployment page: addresses, next hops, bandwidths, DHCP, HA, group, site, location, contact) is a CSV column, and the CLI locks it: no question is asked. Only the layout stays fixed unless the user flips it: interface names (also a DHCP item's interface), labels, interface type, zones, segments, firewall mode and deployment mode, plus the template-group and overlay lists. A value that is blank in the source stays fixed, since there is nothing to vary. The owner's reason: these are exactly what changes from one site to the next, so asking about them only slows the user down.

Interface type is on the fixed list by our ruling: it follows the interface name.

Checked by: `tests/test_variables.py::Classify`, `tests/test_cli.py::PickerChoice`.

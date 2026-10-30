# compare makes a route-map name or IDS/IPS setting that differs a column, not a reason for no row

Status: accepted (owner, 2026-10-30)

The owner ruled that a differing default route-map name (`redistToSDwanFabricRouteMap` of a segment) or a differing IDS/IPS profile or mode (`idsIpsConfig.profile`, `idsIpsConfig.mode`) becomes a ticked column in `compare`, not a dropped site. A capture holds these only when the source could read them, so one site often has them and another does not. When both sites have one, a different value is already a ticked column. When the base has one and a partner does not, it is now a ticked column too: that partner keeps its row with a blank cell, and a `Note:` asks the user to fill the cell in, or untick it to give the partner the base's value (the untick warning names that partner). A cell left blank renders as an empty string, and the toolkit's cell check does not stop it, so the Note is the only prompt. We do not guess the partner's value: the capture does not say what it is.

Considered: filling in the release's default name for the partner (we cannot tell that a missing value means the default; it can mean the read failed). Not covered: a site that has one of these settings when the base does not still gets no row, because the base's template has no field for it. Run compare with that site as the base.

Checked by: `tests/test_cli.py::CompareShape::test_a_release_default_set_at_only_some_sites_is_a_ticked_column`.

# A branch NAT map listed by name only means no rules

Status: accepted (owner, 2026-10-21)

When `/nat/maps` answers `{"map1": "map1"}` (live and cached), the appliance has a map with no rules: no `branchNatMapsConfig` is written and the ledger says "no branch NAT rules configured", naming the map. The owner ruled this after a live round trip on ECOS 9.6.2.1 and 9.7.1: applying `branchNatRules: []` deletes the rules but keeps `map1`, and every read path (`/nat`, `/nat/maps`, `/natMapsDynamic`, the appliance's `nat/maps/map1`) then shows the name only. When the cached copy is names-only but the live copy has rules, the live rules are written.

Checked by: `tests/test_mappers.py` (names-only, live over cached).

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
- **Lab replicate fixes**:
  - Capture redaction no longer reaches across a line break: an empty CLI secret (`authentication-key` with no value) used to replace the first word of the next running-config line with `<SECRET:text>`.
  - A segment whose OSPF is switched off but still configured on interfaces is written with `enable: false` instead of being dropped.
  - Lease times kept in the inactive DHCP server block of a relay interface are ledgered as outside-preconfig (the relay entry has no lease fields).
- **One bad item no longer drops its section**: list mappers (VRRP groups, BGP neighbors and segments, OSPF segments, local and segment routes, IP SLA rules, port forwarding, branch NAT rules, loopbacks, LAG and bridge groups, BFD, tags, multicast) skip an item they cannot read, ledger it as `toolkit-error` naming the item, and still write the rest. A section whose every item fails is not written, so a replace-on-apply section never goes out empty. The section-level catch stays as the outer net. NAT pools stay all-or-nothing: branch NAT rules refer to pools by list position. The "does this segment run BGP" probe is guarded too: a segment it cannot read is passed to the per-segment pass, which writes the others and ledgers it.
- **Item isolation, second pass** (review of the change above):
  - A list section whose every item fails is not written: multicast, BGP neighbors (default and per segment), local and segment routes, OSPF interfaces and areas. An empty list replaces the target's items on apply.
  - A null or non-dict item is skipped and ledgered like any other unreadable item; filters and ledger loops around the items (BFD, tags, bridge and LAG groups, segment OSPF) no longer trip on it.
  - Virtual tunnel interfaces, default-segment OSPF interfaces and areas, and each segment's OSPF interfaces are isolated per item.
  - Ledger lines name the item exactly: port-forwarding rules by their position in the captured list, branch NAT rules with their map name, segment BGP neighbors and segment routes under their own section. No "disabled" or "no tag has a name" note is written for items that failed.
  - A segment whose OSPF is switched off with no interfaces but keeps a router ID or redistribution map is ledgered as outside-preconfig (visible in summary.md) instead of default-omitted.
- **No silent drops found by the lab replicate**:
  - VXLAN VNI maps on a source with no VTEP interface are ledgered as outside-preconfig, naming each segment and VNI (the format requires `vtepSrcInterface`). They used to vanish without a ledger line.
  - Routes that could not be read are never written as an empty list (routes replace on apply). With `/subnets/all` unreadable (an unreachable appliance), the Default segment's routes come from `/subnets` and the other segments are left out and ledgered as not-readable. With `/subnets` unreadable too, `localRoutes` is not written and is ledgered the same way. A 403 still stops the capture (decision 0010).
- **Isolation, third pass** (second independent review):
  - A per-segment block that is present but null (routes, BGP neighbors, OSPF interfaces) is unreadable, not empty: the segment is ledgered and left out instead of written with an empty list. BGP settings that cannot be read while the segment has neighbors are a failure, not "no BGP".
  - With `/subnets/all` unreadable and no per-segment routes otherwise known, the non-Default segments are ledgered not-readable instead of "0 segments with routes".
  - A disabled OSPF segment is reported as keeping settings only for a redistribution map of its own: ECOS fills in a router ID on disabled segments and `default_rtmap_to_ospf` is the default.
  - Ledger accuracy: segment route notes name segmentLocalRoutes; no "mapped 0 segments" line; no localRoutes note pointing to a segment section that is not written; branch NAT priorities that are not numbers fail alone; VNI maps whose VTEP could not be read are not-readable, and VNI maps in an unexpected shape are unresolved. An empty multicast list in the source is written as before.
- **Reads that could not be done** (third independent review): an item whose source read failed is ledgered `not-readable` (check by hand), not `toolkit-error` and never "nothing configured". BGP enabled with an unread neighbors read, OSPF enabled with an unread interfaces read, a null segment's BGP settings, and a routes read without a table are left out and ledgered instead of written with an empty list. VNI maps are not-readable only when neither `/vxlan` nor the running-config was read. Numeric-id sorting never raises (tags, branch NAT priorities, VNI segments), and a branch NAT section whose every rule failed gets no "no rules configured" note.
- **Credentials in free text are redacted at capture**: a password written into a banner or other free text ("pass: x", "password=x", "pwd: x") is saved as `<SECRET:text>`. A seteam banner template carried a demo login in clear, and capture saved it verbatim in the template-groups read. Field-level redaction was unchanged and covered keyed secrets only.
- **Missing PyYAML**: prints how to install it instead of a traceback.

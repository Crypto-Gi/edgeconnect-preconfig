# A route with next hop 0.0.0.0 or :: is written without its interface

Status: accepted (owner, 2026-10-28)

The owner decided: "if an EC on a version before 9.7 has a local route with next hop 0.0.0.0 and an interface, set the interface to blank — we don't need it, just remove it. It has no effect, and it was not supposed to be an option in Orchestrator itself, which was fixed in 9.7 onwards."

So every local route (`localRoutes` and `segmentLocalRoutes`) with next hop `0.0.0.0` or `::` and an interface other than `default` is written with no `interfaceName`, the way an `interface: default` route is written, and ledgered `mapped` with prefix, interface and segment. The route's `zone` is written as the source has it, never changed. If that makes two routes of a segment the same (prefix and next hop), it is written once and the merge is ledgered. This replaces the 1.3.0 rule that left such routes out for a target on ECOS 9.7 or later, or of unknown ECOS. `--target-ecos` stays: it is recorded in `build.json`, `ledger.json` and `summary.md`, and no rule depends on it at present.

Ruling (coordinator, same day): the rule applies whatever the source and target ECOS, because an appliance upgraded from 9.6 to 9.7 can still carry such a route and 9.7 rejects it on apply ("Interface cannot be specified when Next Hop is 0.0.0.0"). Next hop `::` is included because 9.7 rejected that form too and the owner's reason applies equally.

Zone (coordinator, same day, from live apply): with the interface, ECOS 9.6 shows the route with the interface's zone (for example zone 2) while its config zone is none (65534). The same route with the interface blank shows zone none; blank with an explicit `zone` in the preconfig still shows none, on ECOS 9.6.2.1 and 9.7.1.0: ECOS ignores a zone on a route without an interface. So the converter does not try to carry the interface's zone, and the ledger line says the zone shows as none on the appliance.

Checked by: `tests/test_mappers.py` `InterfaceRouteNextHopZero`.

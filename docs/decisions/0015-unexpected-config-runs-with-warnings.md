# New or unexpected configuration runs with warnings, never errors

Status: accepted (owner; date not recorded, relayed 2026-10-28)

A capture read of an unexpected shape (a release with new payloads, a null item, a missing key) must never fail the whole translation. `translate.build()` reads each independent block in a guard: a whole read it cannot use is ledgered `not-readable`, one item of another shape `toolkit-error`, the block is skipped and everything else is written. A replace-on-apply list (interfaces, DHCP, HA peers, template groups, overlays) is never written short without a ledger line, and core interfaces that cannot be read are not invented: `deploymentInfo` is then left out. Only an appliance or deployment read with no usable data stops the build (`NothingToConvert`).

Checked by: `tests/test_translate_robust.py`; a seeded fuzzer on real captures (private, outside the repository) finds no build crash.

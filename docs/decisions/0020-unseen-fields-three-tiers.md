# A field never seen in the field is written only when it is plain and safe, otherwise left out and reported

Status: accepted (owner, 2026-10-31)

A new or never-seen setting must not turn into a new `/validate` bug. The old rule was to write only fields in the target release's default template or on the verified extra list (decision 0013). The owner first asked for a stricter rule, "template AND seen in the field", then for a middle ground, because the strict rule would also drop real configuration. So every written key path now falls into one of three tiers.

1. **Seen:** the path is in `references/observed-fields.yaml`. `observed` lists the paths written by a build of a real capture that a real `/validate` accepted. `verified` lists paths added by hand, each with its evidence. These paths are written as before.
2. **Not seen, plain and safe:** the path is written, with a check-by-hand ledger line (`unverified`, which counts towards "to check by hand", not as a gap), when all of these hold:
   - it is a single plain value in an object that is itself seen;
   - it is not inside a list entry;
   - it is not empty or null;
   - it is a documented word where the template lists them (`template_enums`);
   - it is not in a section that can affect management access, interfaces or licensing (`NO_UNSEEN`).
3. **Everything else is not written:** an `unresolved` ledger line names the reason.

The yes/no type check (`template_types`) runs before the tiers and applies to every path.

The tiers judge what the YAML would write. A new setting can also sit in a read no mapper takes in, so translate also compares every field the capture holds with `references/known-source-fields.yaml` (1,255 read-and-field pairs from the same 98 captures) and writes one `new-source-field` line per read with fields none of them had. That line counts towards "to check by hand". How "seen" is decided, so it is never widened by accident:
- Each ok read (live, cached, orchestrator, backup) is walked to field paths: `[]` for a list entry, `*` for a key that is not lower camel or snake case (`^[a-z][a-zA-Z_]*$`: names, addresses, ids) and for every key of a record map (two or more non-empty objects with the same keys). `record_maps` lists, per read, the paths that were record maps on any appliance, so they are `*` everywhere.
- Reads of Orchestrator objects and state (`NOT_SETTINGS`: template groups, overlays, segments, zones, regions, the appliance list and the like) are not compared.
- Only a commit widens either file, regenerated from captures whose builds validated; the commit names them.
- Leave-one-out on the corpus: a box from an Orchestrator the others did not see would show 0 to 122 new fields (122 on the Orchestrator running 10.0); a box like those already seen shows none.

Considered:
- **Template only (decision 0013 alone):** the 10.0 `flowProcess` null came through this way.
- **Template AND seen, nothing else:** the first site using any setting our captures lack would lose it with no try. The owner judged this too aggressive.
- **An allowed-values check on every field:** the documented word lists are incomplete. `ikeEncryptAlgorithm: default` is undocumented, but `/validate` accepts it. So the check applies only to tier 2.

Consequences:
- Adding a field to `verified` once a site with it validates is how a tier-2 or tier-3 field becomes tier 1.
- `observed` is regenerated from the corpus, field names only.

Checked by: `tests/test_mappers.py` (`test_a_field_never_seen_in_the_field_is_written_only_when_plain_and_safe`), `tests/test_translate_robust.py` (`NothingUnknownIsWritten`, `NewSourceFields`), and the corpus diff and fuzz runs recorded in the PR.

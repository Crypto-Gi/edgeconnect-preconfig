# Decisions

One file per decision: `NNNN-short-title.md`, numbered in order. Find the highest number and add one.

## Format

```md
# Short title of the decision

One to three sentences: the context, what we decided, and why.
```

That is a complete record. Add these only when they earn their place:

- `Status:` proposed, accepted, rejected, or superseded by NNNN. Records are never edited to change the decision; write a new one and mark the old one superseded. Rejected ideas are kept so they are not proposed again.
- `Considered:` alternatives worth remembering, each with why it lost.
- `Consequences:` downstream effects that are not obvious.
- `Checked by:` the test or lint rule that enforces it, if one exists.
- `Open:` assumptions not yet confirmed.

## When to write one

Write one when the code must follow a rule from outside it: someone's ruling, a regulation, a contract, a budget. A reader cannot infer that from the code, and a well-meant cleanup will undo it.

Otherwise, all three must hold:

1. **Hard to reverse.** Changing it later costs real work.
2. **Surprising without context.** A reader of the code would wonder why.
3. **A real trade-off.** There were alternatives and one was picked for a reason.

Typical cases: the shape of the system, a technology that would take months to replace, where a boundary sits and what is deliberately excluded, a deviation from the obvious approach, an alternative rejected for a non-obvious reason.

Anything smaller still deserves its reason, in the commit message of the change that made it. Records here are for the few decisions a future reader must not miss.

## Rules

- A record states what a person decided. If the reason was never given, write "reason not recorded"; do not supply one.
- A record written after the fact says so: `Status: accepted (reconstructed YYYY-MM-DD)`.
- Reading: check the records for the area you are changing. If your change contradicts one, raise it before proceeding.

## Records

- [0001](0001-variable-review-sheet.md): Variables are chosen per field in variables.csv, not per section
- [0002](0002-compare-sites-and-code-owns-output.md): Other sites decide the variables; the agent never writes output
- [0003](0003-agents-never-edit-the-toolkit.md): Agents using the skill never edit the toolkit; they name gaps
- [0004](0004-names-only-branch-nat-map-means-no-rules.md): A branch NAT map listed by name only means no rules
- [0005](0005-cli-validates-on-the-source-orchestrator.md): The command line validates on the Orchestrator it read from
- [0006](0006-deployment-page-values-are-always-per-site.md): Deployment page and appliance info values are always per site
- [0007](0007-apply-only-behind-two-human-gates.md): Apply only behind two human gates (superseded by 0008)
- [0008](0008-the-toolkit-cannot-apply.md): The toolkit cannot apply a preconfig
- [0009](0009-a-refused-read-is-reported-not-fatal.md): A read the API key may not make is reported, not fatal
- [0010](0010-a-refused-routes-read-stops-the-capture.md): A refused routes read stops the capture
- [0011](0011-validators-are-exact-names-in-ec-validators.md): /validate runs only on nicknames listed exactly in EC_VALIDATORS
- [0012](0012-toolkit-is-the-source-the-skill-ships-a-copy.md): toolkit/ is the only source; the skill ships a generated copy
- [0013](0013-write-only-keys-the-target-template-knows.md): A preconfig holds only keys the target release's default template knows
- [0014](0014-a-release-uses-its-family-template.md): A release without its own template uses its family's
- [0015](0015-unexpected-config-runs-with-warnings.md): New or unexpected configuration runs with warnings, never errors
- [0016](0016-next-hop-zero-routes-lose-their-interface.md): A route with next hop 0.0.0.0 or :: is written without its interface
- [0017](0017-upload-yaml-needs-no-approve.md): upload --yaml uploads without --approve; validate --yaml is the dry run
- [0018](0018-compare-offers-differing-layout-as-columns.md): compare offers a differing zone, label, segment or firewall mode as a column
- [0019](0019-compare-release-defaults-are-columns.md): compare makes a route-map name or IDS/IPS setting that differs a column, not a reason for no row

---
Format adapted from mattpocock/skills, skills/engineering/domain-modeling/ADR-FORMAT.md (MIT).

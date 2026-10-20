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

---
Format adapted from mattpocock/skills, skills/engineering/domain-modeling/ADR-FORMAT.md (MIT).

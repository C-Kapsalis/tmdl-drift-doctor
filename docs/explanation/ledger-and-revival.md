# The ledger and revival model

**The rule: every consequential act leaves an immutable line, and every
correction is a new line — never an erasure.**

## Why an append-only ledger

Fleet remediation is bulk mutation of other people's models. The minimum
standard for that is *auditability*: six months later, "why doesn't tenant
alpha have the evening-peak KPI?" must be answerable from the record —
retired from the template on this date, removed from alpha by this sync run,
these files touched. The ledger (`ledger.jsonl`, one JSON event per line)
is that record. Three event types cover it:

- `retired` — the template dropped an object (recorded at recapture);
- `revived` — a retirement was canceled (by return-to-template or by hand);
- `remediated` — a fix was applied to a model (kind, ref, action, files).

Append-only is what makes it evidence. A ledger you can edit is a ledger
you can quietly rewrite; a corrupt line therefore fails every read loudly
rather than being skipped — a half-readable ledger silently mis-targeting a
delete is the worst outcome available.

## Retirement: deletion needs provenance

The template is a core, not a superset, so "absent from the template" can
never justify deleting from a derived model — that description also matches
every legitimate tenant customization. The `retired` events carve out the
one deletable population: objects **provably ex-template**, witnessed by
the recapture diff (previous baseline had it, fresh snapshot doesn't). The
`retire.*` channel acts only on active `retired` events. No event, no
removal — however suspicious an object looks.

## Revival: the escape hatch that keeps history

Retirement is a strong claim ("this object should not exist in derived
models") and reality overrides it in two ways:

- **The template takes it back.** At recapture, an actively-retired object
  found in the fresh snapshot is auto-stamped `revived` (fleet-wide). The
  retirement stands down; ordinary drift comparison resumes.
- **A human overrides.** `drift-doctor ledger --revive` records that a
  deliberate re-add — usually one tenant keeping an object the fleet
  dropped — must not be re-removed. Scoped with `--model`, it protects
  exactly that model while the removal stays live for everyone else.

Both are *events*, so the fold is simple and the history stays complete:
retired → in force; revived → canceled (globally or per-model); retired
again later → in force again. You can always reconstruct what the fleet
believed at any point in time.

## Design consequences worth noticing

- **Undo is re-do.** Restoring a wrongly-removed object is: put the block
  back, revive the ref ([how-to](../how-to/recover-from-unwanted-remediation.md)).
  The mistake and the correction are both on the record.
- **Dry runs leave no trace** — `--dry-run` writes neither models nor
  ledger, so the ledger never claims things that didn't happen.
- **The ledger is shared state.** Commit it with the models: retirement
  history *is* fleet policy, and two machines with different ledgers will
  disagree about what may be deleted.
- **Duplicate suppression, not deduplication.** Recapture never re-records
  an already-active retirement, and drops covered by a bigger drop (columns
  of a retired table) are represented by the one covering entry.

# Object retirement (measures and tables)

*Kinds: `retire.measure`, `retire.table`*

## What it is

An object the template *used to* define was removed from it — superseded,
dead, or replaced. Derived models that still carry it hold stale surface: a
measure nobody maintains, a table whose upstream feed is gone. Retirement is
the channel that cascades template **removals**, the mirror image of the
missing-object cascade.

## Why removals need their own channel

Additions are easy: presence in the template is proof enough. Removals are
not symmetric, because the template is a core, not a superset — an object's
absence from the template is exactly what a legitimate tenant customization
looks like ([Extra objects](extra-objects.md)). Deleting on absence would
destroy tenant work.

The retirement ledger resolves the ambiguity with provenance: at every
recapture, objects present in the *previous* baseline but absent from the
fresh snapshot are recorded as `retired` events. Only that recorded
population is ever deletable. The proof is "this WAS template surface and
the template dropped it", never "I can't find it".

## Detection logic

For each active (non-revived) ledger retirement, the detector checks whether
the derived model still carries the ref:

- `retire.measure` — the measure still exists on the shared table;
- `retire.table` — the table file still exists in the derived model.

Objects subsumed by a bigger drop are not double-tracked: when a whole table
is retired, its measures and columns are covered by the single table entry.

## Remediation semantics

Deletions are double-gated: the kind must be allowlisted **and** the run
must pass `--sync`. A default `remediate` pass skips every retirement with
an explanatory reason — you cannot delete anything by accident.

- `retire.measure` removes the measure's raw block from its host table file.
- `retire.table` deletes the table file and removes its `ref table`
  registration from `model.tmdl`.

Both are idempotent, and both append `remediated` events to the ledger.

## When NOT to auto-fix

- **Something in the derived model still references the object.** A
  derived-only measure whose DAX reads the retired table, a report visual
  bound to the retired measure. drift-doctor removes model objects; it does
  not rewrite your reports or your custom DAX. Sweep references first —
  removals that orphan references should be sequenced (dependents first),
  not forced.
- **A tenant is contractually still using it.** Revive it for that model
  (`ledger --revive ... --model NAME`) so the sync passes leave it alone,
  and keep everyone else on the removal path.
- **The retirement itself was a mistake.** Restore the object in the
  template and recapture — the ledger auto-stamps a fleet-wide revival and
  the channel stands down.

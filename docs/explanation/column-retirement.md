# Column retirement

*Kind: `retire.column`*

## What it is

A single column was dropped from a template table that itself survives.
Derived models that still carry the column hold a stale field — often one
whose upstream source no longer exists, so it is one refresh away from
breaking, or already broken.

## Why columns deserve their own kind

It is easy to build a retirement inventory of tables, measures and other
whole objects and believe you are done — and then a template column drop
falls straight through it. This exact gap has a characteristic, nasty
failure signature worth understanding:

1. The template drops column `X` from table `T`. The inventory tracks
   tables (T survives) and measures — the *column* drop is recorded nowhere.
2. If the baseline is also stale (not recaptured), detection still lists
   `X` as template truth and flags every derived model *without* it as
   `column.missing` — and the cascade then tries to splice `X` **from a
   template that no longer has it**, and crashes (or worse, resurrects the
   column from an old copy).
3. Models still carrying `X` are meanwhile reported as carrying an
   [extra](extra-objects.md) — advisory, so the debris never gets cleaned.

The fix is structural: the retirement inventory must enumerate
**`Table[Column]` pairs**, not just object names. Recapture then records a
dropped column as its own `retired` event — unless the whole table was
dropped, in which case the table entry covers it and no per-column entries
are written (one retirement, not thirty).

## Detection logic

For each active `column` retirement `T[X]`: if the derived model has table
`T` and `T` still has column `X`, emit `retire.column`. Models without the
host table are silently fine (nothing to remove; if `T` itself is missing
that is a different finding).

## Remediation semantics

Allowlist + `--sync` gated, like every retirement. The column's raw block
(declaration, properties, annotations) is removed from the derived table
file; the write guards validate the result before it is persisted.
Idempotent — the next run finds no column to remove.

## When NOT to auto-fix

- **Derived-only DAX still references the column.** Template measures that
  used it were retired with it, but a tenant's custom measure reading
  `T[X]` will break when the column goes. Search the model for `[X]`
  references before syncing.
- **The column still exists in that tenant's source and they use it.** Then
  it isn't debris for them — revive it for that model and move on.
- **Ordering matters in mixed sweeps.** When a retired measure references a
  retired column, remove the measure first (the suite orders measure
  removals before column removals within a sync pass for exactly this
  reason — keep that ordering if you script your own).

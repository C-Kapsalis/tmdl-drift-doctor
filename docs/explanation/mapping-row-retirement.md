# Mapping-row retirement

*Kind: `retire.mapping_row` (companion: `mapping_row.missing`)*

## What it is

Fleets commonly carry small **mapping tables** — lookup tables whose rows
are data-as-model, defined inline in a calculated partition (a DAX
`DATATABLE` literal): plan catalogs, category-to-metric groupings, slicer
configurations. The fixture fleet's `Plan Map` is one: each row maps a plan
key to a display name and a fee.

Mapping-row retirement is the surgical case: **one row** was dropped from a
mapping table that itself survives — the template discontinues the
`legacy-gold` plan while `basic`, `plus` and `elite` live on. Derived models
still carrying the row keep offering a dead option.

## Why whole-table thinking fails here

Coarser retirement kinds can't express this:

- *Table retirement* would delete the survivors along with the dead row.
- Treating the whole partition source as one comparable blob turns any row
  difference into "the table drifted", with only an all-or-nothing rewrite
  as the fix — which would also flatten tenant-custom rows.

The row is the real unit of change, so the suite makes it the unit of
detection, retirement and remediation. Rows are identified by a **row key**
(the first column value of the tuple), and each row's raw tuple text is
tracked in the baseline.

## Detection logic

- At recapture, a key present in the previous baseline's row set and absent
  from the fresh one is recorded as a `mapping_row` retirement
  (`Table/row_key`) — unless the whole table was dropped, which stays a
  single table entry.
- At detect time, each active row retirement is checked against the derived
  model's parsed rows; carriers get a `retire.mapping_row` finding.
- Derived-only rows are **not** findings — custom rows are tenant
  customization, hands-off, same doctrine as [extras](extra-objects.md).

## Remediation semantics

Allowlist + `--sync` gated. Exactly the ledger-listed row tuple is removed
from the `DATATABLE` literal, separator comma included; **sibling rows are
kept** — keeping them is the entire point of the kind. The inverse operation
(`mapping_row.missing`) inserts the template's tuple after the last existing
row. Both edits pass the standard write guards and are idempotent.

## When NOT to auto-fix

- **Tenant data still references the key.** If a tenant's facts still
  arrive tagged `legacy-gold`, removing the mapping row orphans that data in
  every visual that joins through the mapping table. Check usage first;
  revive per-model where the key must live on.
- **The "row" is load-bearing config, not reference data.** If downstream
  logic switches on the key's presence, coordinate the removal with the
  logic change instead of letting a sync pass race it.
- **Ambiguous keys.** Row identity is the first tuple element. If a mapping
  table's first column is not unique, fix the table design before trusting
  row-level tooling with it.

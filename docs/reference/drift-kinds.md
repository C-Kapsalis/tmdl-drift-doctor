# Drift-kind catalog

Every finding has a dotted `kind`. The catalog below gives, for each kind:
what triggers it, what remediation does, and the gates it must pass.

Gates legend — **A**: must be in `allowlist.kinds`; **S**: additionally
requires `--sync`; **–**: advisory, never remediated.

## Missing objects (template → derived)

| Kind | Trigger | Remediation | Gates |
|---|---|---|---|
| `table.missing` | A baseline table is absent from the derived model | Copy the template's table file (all lineage tags regenerated), register `ref table` in `model.tmdl`. The partition source is the template's — review it. | A |
| `column.missing` | A baseline column is absent from a shared table | Splice the template's raw column block (fresh lineage tag) | A |
| `measure.missing` | A baseline measure is absent from a shared table | Splice the template's raw measure block, `///` description included (fresh lineage tag) | A |
| `mapping_row.missing` | A baseline mapping-table row (by row key) is absent | Insert the template's row tuple after the last existing row | A |

## Divergence on shared objects

| Kind | Trigger | Remediation | Gates |
|---|---|---|---|
| `measure.expression_drift` | Normalized DAX differs (comments, fences and whitespace never count) | Overwrite the derived block with the template's raw block; the derived lineage tag is preserved | A |
| `measure.property_drift` | Expressions match but compared properties differ (`formatString`, `isHidden`, `displayFolder`, …) | Same block overwrite | A |
| `column.property_drift` | A shared column's compared properties differ (`dataType`, `formatString`, `summarizeBy`, …) | Same block overwrite | A |
| `expression.drift` | An **allowlist-named** shared expression differs or is absent | Replace/append exactly that expression block; everything else in `expressions.tmdl` stays byte-untouched | A (and named in `allowlist.expressions` — gates detection too) |

Notes: when a measure's expression *and* properties both drift, one
`measure.expression_drift` finding is emitted — the block overwrite fixes
both. Lineage tags and annotations never participate in comparison.

## Extras (derived → template) — advisory

| Kind | Trigger | Remediation | Gates |
|---|---|---|---|
| `extra.measure` | A derived measure on a shared table with no template counterpart | None — reported only | – |
| `extra.column` | A derived column on a shared table with no template counterpart | None — reported only | – |

Derived-only **tables** (and their contents) are not findings at all, and
neither are derived-only mapping rows — extending the template is
legitimate. See [Extra objects](../explanation/extra-objects.md).

## Retirements (ledger-driven deletions)

Emitted only for objects with an **active `retired` ledger event** (recorded
automatically at recapture, cancelable by revival) that a derived model
still carries.

| Kind | Trigger | Remediation | Gates |
|---|---|---|---|
| `retire.table` | Ledger-retired table still in the derived model | Delete the table file + deregister from `model.tmdl` | A + S |
| `retire.column` | Ledger-retired column still on a shared table | Remove the column block | A + S |
| `retire.measure` | Ledger-retired measure still on a shared table | Remove the measure block | A + S |
| `retire.mapping_row` | Ledger-retired row still in a mapping table | Remove exactly that row tuple; sibling rows are kept | A + S |

## Exit-code semantics

`detect` exits 1 when any non-advisory finding exists; advisories alone exit
0. `remediate` exits 1 only when an apply failed.

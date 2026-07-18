# Configuration: fleet.yml and the allowlist

One file configures a fleet. All paths are resolved relative to the file's
directory.

## Full schema

```yaml
version: 1                                  # required, must be 1

template: template/GymChain.SemanticModel   # required — the golden model
models:                                     # required, non-empty map
  alpha: franchises/alpha/GymChain.SemanticModel
  bravo: franchises/bravo/GymChain.SemanticModel

mapping_tables:                             # optional — tables whose DATATABLE
  - Plan Map                                #   rows are diffed row-by-row

state_dir: .drift-doctor                    # optional, default .drift-doctor
                                            #   holds baseline.json + ledger.jsonl

allowlist:                                  # optional — EMPTY means nothing
  kinds: [...]                              #   is ever remediated
  expressions: [...]
```

Every model path (template included) must contain a `definition/` directory;
`load_fleet` fails loudly otherwise.

## `allowlist.kinds`

The drift kinds `remediate` may apply. Anything not listed is detected and
reported but never fixed. Valid values:

```
table.missing            column.missing          column.property_drift
measure.missing          measure.expression_drift  measure.property_drift
expression.drift         mapping_row.missing
retire.table             retire.column           retire.measure
retire.mapping_row
```

`extra.measure` / `extra.column` are not valid here — advisories have no
applier by design.

Notes:

- `retire.*` kinds are additionally gated behind the `--sync` flag; listing
  them here is necessary but not sufficient.
- Prefix filtering on the command line (`--kind measure.`) intersects with
  this list — the allowlist is the outer boundary.

## `allowlist.expressions`

The *named* shared expressions (`expressions.tmdl` blocks) that participate
in drift at all. This list gates **detection as well as remediation**: an
expression not named here is never compared, so it can never be flagged or
cascaded.

Why so strict: `expressions.tmdl` is a mixed file. It typically holds
per-tenant connection parameters (warehouse names, project ids, currencies)
alongside a small set of genuinely shared parameters (reporting window
dates, feature toggles). Cascading the whole file would repoint every
tenant's data source. Name exactly the parameters that are canonical in the
template, and nothing else.

## `mapping_tables`

Tables listed here get row-level treatment: their partition source is parsed
as a DAX `DATATABLE` literal and each row (keyed by its first column value)
becomes a diffable unit. This enables:

- `mapping_row.missing` — a template row a derived model lacks;
- `retire.mapping_row` — a row retired from the template, removed from
  derived models while sibling rows survive.

Rows that exist only in a derived model are left alone — custom rows are
tenant customization, same doctrine as extra measures.

## State files

| File | Role | Commit it? |
|---|---|---|
| `<state_dir>/baseline.json` | The committed template truth. | Yes |
| `<state_dir>/ledger.jsonl` | Append-only audit + retirement record. | Yes |

Both are shared fleet state: detection is only meaningful when everyone
(and CI) reads the same baseline and the same retirement history.

# fleet.yml reference

`fleet.yml` describes one template model and the models derived from it.
Paths are relative to the file. Every command validates the whole file
first and exits 2 listing every problem; nothing is half-loaded.

## Top level

| Key | Shape | Default | Meaning |
|---|---|---|---|
| `version` | `1` | required | Schema version. |
| `template` | path | required | The template `*.SemanticModel` folder. |
| `models` | mapping | required | Fleet entries (see below). |
| `state_dir` | path | `.drift-doctor` | Baseline, ledger, floors, receipts. |
| `mapping_tables` | list of table names (or `{name, encoding}`) | `[]` | Tables whose rows are diffed row by row. A table whose partition calls `Binary.FromText` (Desktop's "Enter data") is decoded automatically; its rows are compared by (category, key). |
| `category_annotations` | `{baseline, table, property_suffixes}` | none; the first mapping table; `_RequiredServices`, `_HealthPolicy` | `Category_*` annotations in model.tmdl must match the categories a model's mapping adds on top of the template's. `baseline` names a JSON file `{"orphaned": {<folder>/<model>: [...]}, "undeclared": {...}}` of accepted exceptions. |
| `allowlist.kinds` | list of dotted prefixes | `[]` | Kinds `remediate` may apply. `measure` matches every `measure.*` kind; `measure.miss` matches nothing. Never decides what `detect` or the ratchet count. |
| `allowlist.expressions` | list of names | `[]` | Shared expressions compared for content (`expression.drift`). Must not name a connection parameter. |
| `connection_parameters` | list of names | `[]` | Expressions that hold per-model values. Reported by `expression.missing` with a hint, never written. |
| `connection.project` | string | `""` | Fleet-level default project for the connection contract. |
| `overlays.dir` | path | `overlays` | Per-model overlay folders (`<dir>/<model id>/{policy,dq,mapping}.yml`). |
| `families.growth.suffixes` | list (two or more) | `Growth.SemanticModel`, `insights.SemanticModel` | Folder suffixes that make a model a growth-family member. |
| `metadata.total` | integer | none | When present, must equal the number of model entries. |
| `count_floor.family_prefixes` | list of table-name prefixes | `x-Selector`, `x-Plot Specific`, `y-Selector`, `y-Plot Specific` | Axis-table families. A missing axis table fails `detect` whatever `allowlist.kinds` says. |
| `fieldparam_tables` | list of table-name globs | auto | Field-parameter tables. When set, schema modernization runs on them; when unset, every calculated table built from `NAMEOF` tuples counts as one. |
| `fieldparam.families` | list of `{prefixes, role}` | `x-Selector`/`x-Plot Specific` (metric), `y-Selector`/`y-Plot Specific` (dimension) | The first prefix names the numbered levels (`<prefix> <n>`); the others are variant tables. |
| `fieldparam.placeholder_table` | table name | `#Report Measures` | Rows pointing at this table are placeholders, ignored by the cross-level check. |
| `fieldparam.blank_row` | `{category, tables}` | `Blank Dimension_y`, `y-*` | The "no legend" option row each legend field parameter must carry, copied from the template per table. |
| `fieldparam.skip_tables` | list of table names | `[]` | Field-parameter tables the template row merge leaves alone. |
| `source_identity` | `{function, db_parameter}` | none (detection off) | How a table's source view is read: the first string argument of the source-function call (bare or `#"quoted"`), or the view folded into an inline query after the database parameter. With the block, a table reading another view than the template's is source-divergent. |
| `notify_command` | shell command | none | Receives the run's view-enrichment entries as JSON on stdin, once per `remediate` run that produced any. |
| `incremental_refresh` | `{enabled, fact_pattern, exempt}` | on; fact tables by role; `[]` | Which fact tables follow the template's incremental-refresh policy and window. |
| `renames.measures` | mapping old -> new | `{}` | Reviewed measure renames for `renames apply` / `renames check`. An empty value is a to-do and is skipped. |
| `renames.columns` | mapping `Table.Old` -> `Table.New` | `{}` | Column renames (split at the last dot; cross-table moves are ignored). |
| `renames.profiles` | mapping name -> `{measures, columns}` | `{}` | Alternative maps; a model selecting one (`rename_profile`) uses it INSTEAD of the default maps. |
| `renames.pairing` | `{prefix_sigils, exclude_prefixes}` | `- _ ! # ^` (plus one letter and a dash); `i-` | How `measure.renamed` pairs a missing template measure with a derived one: same name after stripping one prefix, trimmed, lower-cased. |
| `renames.format_table` | table name | none | The format-selector table `rename measure` checks for a precision loss. |
| `renames.format_family` | string | `cost` | The literal whose test a `--format-arm` is inserted in front of. |

## Model entries

An entry is a bare path or a mapping:

| Key | Shape | Meaning |
|---|---|---|
| `path` | path | The model's `*.SemanticModel` folder (required; "no model_path" otherwise). |
| `gating` | `enforced` \| `advisory` | Advisory models are reported but never fail a gate. |
| `gating_reason` | string | Why the model is advisory and what re-gates it. |
| `secondary_models` | list of `{name, path, deviations}` | More semantic models of the same entry. `deviations` is required (`[]` = exact clone). They inherit the entry's gating, overrides and connection block. |
| `connection` | `{project, billing_project, dataset}` | Declared warehouse target. Dataset defaults to `<model id>_db`; project is empty unless declared. |
| `family` | string | Explicit family membership. |
| `workspace` | `{name, guid, tenant_id, verified}` | GUIDs are checked; `verified` is a date or `false`; no two entries share a workspace guid. |
| `overrides` | list or typed sections | Declared, reviewed divergence (below). |
| `rename_profile` | string | The `renames.profiles` entry this model uses. |
| `source_divergent` | list of table names | Tables declared source-divergent when the view cannot be read. |

## Overrides

A `refresh_windows` section (`{table, kind: window | absent, reason, ...}`)
declares a fact table's own refresh window (any of `rollingWindowGranularity`,
`rollingWindowPeriods`, `incrementalGranularity`, `incrementalPeriods`,
`incrementalPeriodsOffset`, layered over the template's) or that it has no
policy on purpose.

Typed sections also include `mapping_rows` (`{category, key, reason}`: the
model keeps a row the template retired) and `mapping_categories`
(`{category, reason}`: the model deliberately lacks a template category).


Every entry needs a non-empty `reason`; `owner` and `added` are optional.

The flat list form takes `{object, kind, reason}` with `kind` one of
`expression`, `absent`, `retain`, `extension`.

Typed sections:

| Section | Entry keys | Effect |
|---|---|---|
| `measures` | `object`, `kind` (`expression` needs `Table[Measure]`; `absent` takes a bare name too) | Acknowledges expression drift or a missing measure. |
| `columns` | `object` (`Table[column]`), `kind` (`absent`, `extension`) | `absent`: never spliced; `extension`: a declared derived-only column. |
| `expressions` | `object`, `kind` | Same for shared expressions. |
| `source_views` | `table`, `view` | The model's table reads its own view on purpose. |
| `tables` | `name` | Retained table. |
| `relationships` | `signature` | Retained relationship. |
| `fieldparam_entries` | `category`, `entry` | Retained field-parameter entry. |
| `mapping_rows` | `category`, `key` | Retained mapping row. |
| `files` | `path` (glob, relative to the folder holding the model) | `remediate` never writes the file; findings whose fix would touch it are reported as overridden. |

`expression`/`absent` objects must resolve in the template (checked at
detect time). Declarations on an entry cover its secondary models.

## Commands that read only the configuration

- `drift-doctor fleet check [--stamps FILE]`: validates the file and the
  overlay folders (exit 2 on a problem) and, with `--stamps`, a
  connection-parameter stamping table (exit 1 on a mismatch).
- `drift-doctor fleet connection MODEL`: `project<TAB>billing<TAB>dataset`.
- `drift-doctor fleet family NAME`: members, sorted, comma-separated (exit 1
  when empty).
- `drift-doctor overrides check`: stale file exemptions (exit 1).
- `drift-doctor overrides files --model M [--item ITEM]`: exempt patterns.
- `drift-doctor overrides is-exempt --model M PATH`: exit 0 or 3.

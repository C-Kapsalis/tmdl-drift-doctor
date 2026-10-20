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
| `mapping_tables` | list of table names | `[]` | Tables whose rows are diffed row by row. |
| `allowlist.kinds` | list of dotted prefixes | `[]` | Kinds `remediate` may apply. `measure` matches every `measure.*` kind; `measure.miss` matches nothing. Never decides what `detect` or the ratchet count. |
| `allowlist.expressions` | list of names | `[]` | Shared expressions compared for content (`expression.drift`). Must not name a connection parameter. |
| `connection_parameters` | list of names | `[]` | Expressions that hold per-model values. Reported by `expression.missing` with a hint, never written. |
| `connection.project` | string | `""` | Fleet-level default project for the connection contract. |
| `overlays.dir` | path | `overlays` | Per-model overlay folders (`<dir>/<model id>/{policy,dq,mapping}.yml`). |
| `families.growth.suffixes` | list (two or more) | `Growth.SemanticModel`, `insights.SemanticModel` | Folder suffixes that make a model a growth-family member. |
| `metadata.total` | integer | none | When present, must equal the number of model entries. |
| `count_floor.family_prefixes` | list of table-name prefixes | `x-Selector`, `x-Plot Specific`, `y-Selector`, `y-Plot Specific` | Axis-table families. A missing axis table fails `detect` whatever `allowlist.kinds` says. |
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

## Overrides

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

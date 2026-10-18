# Release notes

## Unreleased

### Added

- **`expression.missing`.** Every expression the template declares and a
  model does not is reported, whether or not the name is listed under
  `allowlist.expressions` (that list only scopes content comparison).
  When the kind is allowlisted, `remediate` adds the template block after
  the existing ones, in alphabetical order, with a fresh lineage tag. Names
  listed under the new top-level `connection_parameters` key are reported
  with a hint ("copy the declaration from the template and set this
  model's own value") and are never written.
- **`tmdl_edit.replace_measure_expression`.** Replaces one measure's DAX
  body and nothing else (single-line, bare multi-line and fenced shapes),
  behind a guard that refuses any result in which the measure's
  description, declaration, properties or the following objects changed.

- **Secondary models.** A fleet entry may list `secondary_models: [{name,
  path, deviations}]`. Each is detected as a model of its own and inherits
  the entry's gating, overrides and connection block.
- **Typed override sections.** `overrides` may be a mapping of sections
  (`measures`, `columns`, `expressions`, `source_views`, `tables`,
  `relationships`, `fieldparam_entries`, `mapping_rows`, `files`) in place of
  the flat list, which keeps working. A `columns` entry with `kind:
  extension` acknowledges a derived-only column; a bare measure name with
  `kind: absent` covers that measure on any table.
- **File exemptions (`overrides.files`).** `remediate` never writes a file a
  model declares exempt, and `detect` reports drift whose fix would touch it
  as overridden. `drift-doctor overrides check|files|is-exempt` inspect the
  declarations.
- **`drift-doctor fleet check|connection|family`.** Validates the
  configuration, per-model overlay files and a connection-parameter stamping
  table; resolves a model's declared warehouse target; lists a family's
  members.
- **More validation.** Duplicate model paths, malformed workspace blocks,
  `metadata.total` mismatches, secondary models without `deviations`, a
  growth family with fewer than two suffixes and a connection parameter in
  `allowlist.expressions` are configuration errors. Model entries accept
  `gating_reason`, `connection`, `family` and `workspace`.

### Changed

- **Configuration errors exit 2 (breaking).** A malformed or unresolvable
  `fleet.yml`, and an unknown `--model`, now exit 2 instead of 1, so CI can
  tell a broken configuration from drift.
- **Allowlist entries are dotted prefixes.** `measure` allows every
  `measure.*` kind and `measure.missing` also allows `measure.missing.*`;
  `measure.miss` allows nothing. `--kind` filters match the same way.
- **Shared expressions are compared with their comments (stricter).**
  `expression.drift` used to strip comments and collapse whitespace; it now
  drops only lineage-tag lines and puts the `meta [...]` bag in canonical
  order, so a changed doc comment or `//` comment is drift. A name declared
  more than once is compared on every declaration, and the fix rewrites
  each differing declaration with its own lineage tag kept.
- **Baseline snapshot format 3.** Run `drift-doctor capture` once after
  upgrading; the ledger is kept as is.

### Fixed

- **A table's last column no longer carries its refresh policy.** Copying
  or removing the last column of a table also copied or removed the
  table's `refreshPolicy` block, because the block boundary stopped only at
  columns, measures, partitions, hierarchies and annotations. `remediate`
  adding a missing last column copied the template's policy into the
  spinout; removing one deleted the spinout's policy. `refreshPolicy`,
  `calculationGroup`, `changedProperty` and `extendedProperty` now end a
  block too.
- **A doc comment stays with its own expression.** Replacing a shared
  expression carried the `///` comment of the NEXT expression along with
  it, so that comment ended up documenting an unrelated parameter. A run of
  column-0 `///` lines now belongs to the declaration below it.
- **A stray `table` line ends a block**, so a malformed concatenated file
  can no longer make one object swallow the next table. Inserting a measure
  block written at column 0 now indents it to object depth.

## v1.1.0 (2026-10-12)

Relationships, cross-host measures, guarded retirements, and the governance
commands that keep a fleet from drifting back.

Version numbering: the package metadata said `0.1.0` while the first GitHub
release was tagged `v1.0.0-stable`. This release aligns the two: the package
version is now `1.1.0`, the release after `v1.0.0-stable`.

### Changed

- **`detect --json` emits a versioned report (breaking).** The output is now
  an object with `schema_version: 2`, `generated_at`, `template_tree` (the
  git tree hash of the template directory at `HEAD`, or `null` outside git),
  `scope` (`fleet`, `models`, `kind_filter`), `errors`, and per-model
  `findings`, `overridden` and `counts`. Consumers of the old top-level array
  must read `models.<name>.findings` instead.
- **`detect` fails only on actionable findings.** A finding is actionable
  when its kind is allowlisted, it is not advisory, and no override
  acknowledges it. Findings of kinds the allowlist does not name are still
  printed (tagged `[not allowlisted]`) but no longer set exit code 1. The
  same predicate drives the ratchet and `review`.
- **A model that cannot be parsed no longer aborts `detect`.** It is reported
  under `errors` and `detect` exits 3.
- **Baseline snapshot format 2.** The baseline now records relationships and
  uses the new normalization; the first `detect` after upgrading asks for one
  `drift-doctor capture`. The ledger is unchanged and kept as is.
- **Remediation runs in dependency order** (tables, columns, expressions,
  mapping rows, measures, relationships, then retirements), so a fix that
  needs another one usually lands in the same pass.
- `remediate` prints and counts the new statuses `blocked`,
  `auto_skipped_precondition` and `overridden`.
- `capture` records the objects a retired table held, and records retired
  relationships even when their table was retired too.

### Added

- **Relationships.** `relationships.tmdl` is parsed and compared on the
  field signature `FromTable[col] -> ToTable[col]` (block names and GUIDs
  never count; omitted properties equal their engine defaults). New kinds:
  `relationship.missing` (appended once both endpoints exist),
  `relationship.modified` (template properties, derived block name kept),
  `relationship.extra` (advisory) and `retire.relationship`.
- **Cross-host measures.** Measure names are model-global, so template
  measures are looked up by name across the whole derived model.
  `measure.wrong_host` moves the derived block verbatim to the template's
  host (DAX and lineage tag untouched, destination written first);
  `measure.duplicate_host` is advisory; `measure.missing` never inserts a
  name that already exists, and a misplaced measure no longer shows up as a
  missing one plus an extra one.
- **Coupling guard for retirements.** `retire.table`, `retire.column` and
  `retire.measure` are refused with status `blocked` (and the reason) while
  derived-only DAX, derived-only relationships, a `sortByColumn` or a
  hierarchy level depends on the object, or while a retired table still
  hosts derived-only objects. Template-owned and ledger-retired
  relationships are removed together with the table or column.
- **Preconditions.** Fixes declare what must be true when they run:
  relationship endpoints present; the columns, measures and tables an
  inserted or overwritten measure references present or added earlier in
  the same run; no duplicated measure name from a table copy; no qualified
  reference left dangling by a move; retirement dependents already fixed.
  Unmet preconditions are reported as `auto_skipped_precondition`.
- **`remediate --converge`** re-detects and re-applies until a pass changes
  nothing, at most four passes.
- **Per-model overrides** under each model in `fleet.yml`:
  `{object, kind: expression | absent | retain, reason, owner, added}`.
  Loading fails on unknown keys, an empty reason or an unknown kind;
  detection fails on an object that resolves to nothing. Overridden findings
  are never remediated and never fail `detect` or the ratchet; they print as
  an ACK block with reason, owner and date, and appear under `overridden`
  in the JSON report.
- **Per-model `gating: advisory`**: findings print but never set the exit
  code of `detect` or `ratchet check`.
- **`drift-doctor ratchet check|tighten`** with `.drift-doctor/floors.json`:
  per-model floors (total and per kind) that only go down. `retire.*`
  findings are resurrections: hard zero, never floored. Floors are only
  written from a full-fleet, unfiltered, error-free report.
- **`drift-doctor audit`** re-verifies every `remediated` ledger event
  (added objects still present, retired objects not back) and exits 4 on a
  revert.
- **`drift-doctor review --out REVIEW.md`** writes a per-model checklist of
  everything routed to a human: blocked retirements, precondition skips,
  wrong and duplicate hosts, advisories and overrides in force.
- The example and fixture fleets gained relationships, a cross-host
  measure, a derived-only measure that depends on a column, a reordered
  `meta [...]` bag and an `https://` parameter.

### Fixed

- `//` and `--` comments are stripped only outside string literals, so a
  changed `"https://..."` parameter or label is detected instead of being
  read as the start of a comment.
- `meta [k = v, ...]` bags are canonicalized (split, trimmed, sorted) before
  expressions are compared, so a Power BI Desktop re-save that reorders them
  is no longer reported as `expression.drift`.
- The legacy-console CLI test decoded the child's cp1252 output as UTF-8 and
  failed on every non-Windows host.

### Validation

- 130 automated tests (60 before this release).

## v0.1.0 — 2026-07-18

First release.

### Features

- **`capture`** — snapshot a template semantic model (TMDL folder format)
  into a committed baseline; automatically maintain the retirement record by
  diffing successive captures (drops become `retired` ledger events, returns
  become `revived` stamps).
- **`detect`** — read-only, typed drift findings for every derived model in
  the fleet, compared against the baseline plus the retirement record.
  Fourteen kinds across four families: missing objects (`table.missing`,
  `column.missing`, `measure.missing`, `mapping_row.missing`), divergence on
  shared objects (`measure.expression_drift`, `measure.property_drift`,
  `column.property_drift`, `expression.drift`), advisories (`extra.measure`,
  `extra.column`), and ledger-driven retirements (`retire.table`,
  `retire.column`, `retire.measure`, `retire.mapping_row`). CI-ready exit
  codes; `--json` output; `--model`/`--kind` scoping.
- **`remediate`** — cascade template truth into derived models via raw-block
  TMDL surgery (lineage tags regenerated on insert, preserved on overwrite;
  annotations and formatting survive). Gated three ways: the fleet allowlist
  (nothing cascades by default), the `--sync` flag (deletions are double-gated),
  and pre-write validation guards including the never-inject-into-DAX
  invariant. `--dry-run` renders unified diffs and writes nothing. All
  appliers are idempotent.
- **`ledger`** — inspect the append-only JSONL audit trail; record manual
  revivals (`--revive`, fleet-wide or per-model) so deliberate re-adds are
  never re-removed.
- **Stale-baseline guard** — `detect` and `remediate` refuse to act when the
  template no longer matches the committed baseline (`--allow-stale`
  overrides).
- Diátaxis documentation: a getting-started tutorial, two how-to guides
  (managing a fleet, running in CI) and two reference pages (CLI and
  configuration; drift kinds and ledger), published from the separate
  `tmdl-drift-doctor-documentation` repository. (An earlier version of these
  notes listed four how-to guides, four reference pages and eleven
  explanation pages; that described an in-repository `docs/` folder that was
  consolidated and moved out before the release was tagged.)

### Validation

- 60 automated tests, including guard/injection safety tests and CLI
  end-to-end runs on the bundled fixture fleet.
- Exercised against a production-scale fleet (108-table / 614-measure
  template, three derived client models, ~780 findings): detection,
  fleet-wide dry-run, a scoped real apply, idempotent re-run, and ledger
  audit all verified.

### Known limitations

- Compares tables, columns, measures, allowlist-named shared expressions and
  mapping-table rows. Relationships, hierarchies, perspectives, cultures,
  roles and the report layer are not compared.
- `table.missing` copies the template's partition source verbatim — review
  connection details after cascading a whole table.
- Operates on TMDL folder-format files only; no Power BI workspace or
  XMLA/service integration.

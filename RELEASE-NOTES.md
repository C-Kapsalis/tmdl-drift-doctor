# Release notes

## 1.2.1 (2026-10-22)

### Fixed

- BUGFIX (kept): a model with Windows (CRLF) line endings is handled like
  the same model with LF endings. Found by running the suite on Windows
  in CI; `tests/test_crlf_models.py` proves each case.
  - A CRLF table's refresh policy was reported as `refresh_policy.invalid`
    ("the refreshPolicy block is not at table level"), so a fixed policy
    never cleared.
  - `groups drop` and `groups drop-table` left a CRLF `model.tmdl` with a
    blank line where the `ref table` line was and the table still in
    `PBI_QueryOrder`, and
    skipped a CRLF report JSON file as "not reproducible as JSON". The
    JSON file now keeps its CRLF endings when it is rewritten.
  - `remediate --ref` failed on a CRLF table ("does not read back as the
    block that was written"). On Windows it also rewrote every file with
    CRLF, so a second run applied the same change again. It now writes
    each file with its own line endings.
  - After a failed read-back, the restored file on Windows got `\r\r\n`
    line endings. It now gets back its exact text.

## 1.2.0 (2026-10-22)

### Added

- **The visibility hygiene rules.** A measure whose name says it is for
  report authors must not be hidden, and a measure whose name says it is
  plumbing must be. The class of a measure is decided by its name alone, the
  way tmdl-preflight's O009 and O011 rules decide it, so a measure hidden by
  accident still reads as user-facing. Two findings: `visibility.hidden_user_facing`
  ("`<table>[<name>]` has a user-facing NAME but is hidden. Either it was
  hidden by mistake (show it) or it is really a helper (rename with a helper
  prefix — a cascade rename).") and `visibility.helper_visible` ("`<table>[<name>]`
  has a helper NAME but is visible and is not on the always-visible allowlist
  — auto-hiding."). The first repair removes the hidden flag, the second adds
  it. A scaffolding name that looks user-facing ("Max Date", "Date Filter")
  is never reported, and the always-visible infrastructure measures
  (`f-All`, `f-Slicer`, the `i-` set) are never reported either. A fleet may
  add names to the allowlist (`visibility.always_visible`) and replace the
  built-in helper test with its own prefixes (`visibility.helper_prefixes`).
  **STRICTER:** neither repair is batch-safe: the kind has to be in
  `allowlist.kinds` and the run has to ask.
- **The applied-filters coverage check.** `i-Applied Filters` lists every
  dimension filter the model currently applies. A dimension the model offers
  in a y-Selector table but the measure does not expose is reported as
  `ifilter.coverage_gap` ("N visible y-Selector dimension(s) are NOT exposed
  via i-Applied Filters in `<model>`: + `<table>[<column>]` … AUTO-FIX
  regenerates the 'i-Applied Filters' measure from the full visible
  y-Selector dimension set"), and the repair regenerates the measure through
  the generator in pbip-model-forge.
- **The applied-filters measure is rebuilt after a removal.** A retirement or
  a purge that removes an object the measure names leaves a stale branch
  behind — the coverage check never re-fires on one. Two findings ask for the
  rebuild: `retire.ifilter_regen` and `purge.ifilter_regen` ("'i-Applied
  Filters' references object(s) this run retires (…)" / "… this run purges
  (…)"). **STRICTER:** a retired column counts too: the rebuild is asked for
  when the measure names a column the run removes, not only a table or a
  measure.
- **The f-filter coverage check.** Every fact a user-facing measure reads
  needs a category gate, and every analysis category needs a gate in the
  all-filters measure. A fact with no gate, and a category with no gate, are
  reported as `ffilter.coverage_gap` ("f-measure coverage gap in `<model>`
  (N to create, M to extend, K f-All gate(s) to add, U category(ies) ungated
  with no resolvable f- measure) — every fact consumed by user-facing measures
  needs an f-<Category> gate, AND every mapping category must be GATED in
  f-All"), with the creates, the extends, the gates and the ungated
  categories listed, and the repair adds them through the generator in
  pbip-model-forge. A fact no category can be found for is reported as
  `ffilter.uncategorized` ("N fact table(s) can't be auto-covered by the
  f-filter generator (no resolvable analysis category): … NO AUTO-FIX:
  assign the fact an analysis category (or add its f-measure by hand), then
  re-run."); that one is report-only. **BUGFIX:** a gate line that tests a
  measure the ledger retired goes with that measure, so the all-filters
  measure never tests a measure that no longer exists.

  The two generator-backed repairs need
  [pbip-model-forge](https://github.com/C-Kapsalis/pbip-model-forge)
  installed next to drift-doctor; without it the checks report nothing (and a
  run says so on standard error) and the repairs refuse to run. The
  visibility rules need nothing else.
- **The ratchet counts by category head.** A floor is a `total` plus a count
  per head (the text before the first dot: `measure.missing` and
  `measure.expression_drift` both roll up to `measure`). What counts is every
  finding a sync could act on, whatever `allowlist.kinds` says, minus
  overridden findings, advisory and report-only kinds, and model-intrinsic
  regenerations (`ifilter.*`, `visibility.*`, `ffilter.coverage_gap`). The
  allowlist decides what `remediate` writes, never what the ratchet counts.
  A legacy floors file keyed by kind is read as-is and rolled up on load;
  `tighten` rewrites it in head form.
- **The ratchet trusts its report.** `detect --json` records what the run
  covered in a `scope` block (`fleet`, `models`, `alternate_model`,
  `targets`, `kind_filter`, `overrides`). `ratchet check` refuses a
  category-filtered report, a report with detection errors, and (when
  writing) a run against an alternate model or a non-default overrides
  file. `ratchet tighten` also refuses a report with no scope block, and
  `--prune` needs a fleet report. A report with no scope block is still
  accepted by `check`, compared as it stands, with no prune suggestion.
- **`ratchet tighten` on a scoped report** lowers only the models it
  names; the other floors are carried forward byte for byte.
- **A resurrection is a hard zero.** A `retire.*` finding is never floored
  and always fails, in its own `RESURRECTION` block, by name, with the
  banner `DRIFT RATCHET RED — RESURRECTION`. A floors file that grants a
  `retire` allowance is itself refused. `--include-advisory` extends the
  hard zero to a retirement the sync cannot apply.
- **`ratchet check|tighten --total-only`** compare each model's drift total
  with `<state_dir>/drift_totals.json` (written by `tighten --total-only`),
  a separate mode from the head ratchet.
- **The sync receipt.** After a `remediate` run that applied something,
  `<state_dir>/last_sync.json` records, per model, what was applied
  (`kind` and `ref`) with that model's own stamps. Models the run did not
  touch keep their entries verbatim, so a one-model run never re-attests the
  rest of the fleet. A run that applied nothing leaves the file
  byte-identical.
- **`drift-doctor diff`.** Compares two `detect --json` reports and writes
  a Markdown report of the blast radius: the objects the change adds to
  the fleet's drift and the ones it resolves, how far each reaches, and
  what it means per model (added, removed, net, and a per-kind-head
  breakdown). The report is written for a CI comment: a hidden marker line
  comes first, so a later run edits the same comment in place, and the
  footer names the two reports it compared. A scoped or kind-filtered
  report, and a fleet whose committed baseline no longer matches the live
  template, each raise a warning in the report; neither fails the run.
- **`drift-doctor freshness`.** Warns that a branch predates retirements
  the main line has made, by comparing the ledger the branch was cut from
  with the ledger on the main line. A retirement that is still present in
  a derived model is reported as at risk, with the models that still carry
  it; a kind the tool cannot check is listed as not checked rather than
  guessed. Advisory by construction: the command always exits 0, because
  the ratchet is the blocking gate. `--body-file` writes a Markdown body
  for a CI comment and `--stale-flag-file` writes `1` when something is at
  risk.
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

- **Count floors (`model.count_below_template`).** `capture` stores
  template metrics in baseline.json (`metrics`: measures by class, fact
  tables, DAX expressions, NAMEOF references, relationships, many-to-one
  relationships, axis-family tables with their column and reference
  counts). `detect` reports a model that holds fewer of any of them than the
  template, with `data` = `{metric, client, template, adjustment, floor}`.
  The NAMEOF floor is lowered by template references to columns the model
  declares absent. Count floors fail a gate whatever `allowlist.kinds` says.
  A reference to `x-Selector 10` counts only toward `x-Selector 10` (not
  toward `x-Selector 1`).
- **`relationship.casing_mismatch`.** A relationship endpoint whose table
  or column exists in the template under another casing is reported once
  per mismatching part, instead of as a missing plus an extra
  relationship. The fix rewrites the endpoint to the template's spelling.
- **`retire.stranded_ref`.** Live DAX that still names a table the ledger
  retired and the model no longer declares is reported (comments are
  ignored); it fails a gate whatever the allowlist says. Requesting the
  check with an empty ledger is an error (exit 2).
- **Baseline provenance.** baseline.json carries `provenance`
  (`template_tree`, `template_commit`, `captured_at`, `generator`), plus
  `fact_tables` and `table_source_views`.
- **Cascade summary.** `detect` ends with one `[OK  ]`/`[GAP ]` line per
  model and `Summary: <passing>/<total> models in cascade`; `detect --csv`
  prints one row per model (`model,tables,measures,fact_tables,
  filter_measures,user_facing,helpers,gaps,flagged,allowed,expr_drift,
  col_drift,rel_drift`).
- **`detect --json` findings carry `remediable` and `data`.**

- **`remediate --yes`, `--interactive` and `--confirm-deletions`.** On a
  terminal every fix is confirmed (`y`/`n`, `a` = yes to the rest, `q` =
  stop). `--yes` applies without asking but only batch-safe kinds and only
  together with at least one `--kind`. `--sync --confirm-deletions` is the
  explicit second confirmation that lets deletions run in a scheduled job.
  `--kind` may be repeated.
- **`tmdl_drift_doctor.interactive`** exposes the confirmation runner
  (`run_interactive`, `ask`, `Item`, `Action`); `remediate.BATCH_SAFE_KINDS`
  and `remediate.DERIVED_PHASE_KINDS` list the batch-safe kinds and the
  order of the field-parameter phase.
- **More remediation outcomes.** `manual` (no automated fix),
  `skipped_override` (an override found at apply time, so a stale finding
  never writes a declared divergence), `skipped_divergent` (a modified
  relationship on a source-divergent table) and `skipped_destructive`.
- **More write guards.** A property after an object's first annotation or
  `changedProperty` line, and a DAX `VAR` name declared twice in one scope,
  are refused before anything is written. Every inserted, overwritten or
  moved block is read back after the edit and must equal what was intended.
- **Derived-only files are protected for the whole run.** `remediate` hashes
  every file a model has and the template does not before it writes; an
  ordinary fix that would write one fails with `OVERLAY`, and a change to
  one that no fix declared fails the run (exit 1).
- **`remediate --backup`.** One `<file>.bak_<YYYYMMDD_HHMMSS>` copy per file
  before its first write in the run; removed (and counted) when the run
  succeeds, kept when anything failed. Never in a dry run.
- **Clearer confirmations.** Each question shows its position (`[3/12]`)
  and flags deletions as destructive; `--dry-run` says per fix whether it
  would be asked, is batch-safe under `--yes`, or is destructive. Items a
  non-terminal run declines count under the new `skipped_noninteractive`
  counter (and in `skipped`).

- **`measure.renamed`.** A missing template measure and a derived-only
  measure that share a core name (the name with one leading `-`, `_`, `!`,
  `#`, `^` or letter-and-dash stripped, trimmed, lower-cased) are reported
  as one rename instead of a missing plus an extra (or a retirement). The
  fix renames the derived declaration, rewrites DAX references in every
  table file (field-parameter NAMEOF rows included) and, when
  pbi-plot-styler is installed, every report binding below the model's
  folder. Ambiguous cores and override-declared names are never paired.
- **`drift-doctor rename measure OLD NEW`.** Renames a measure in the
  template and every fleet model: picker labels whose NAMEOF points at it,
  the declaration (any quoting), DAX references including text comparisons
  inside string literals, and report bindings. A configured format-selector
  table (`renames.format_table`) is checked for a precision loss; exit 2
  until `--format-arm MODEL=FORMAT` resolves it.
- **Rename map (`renames:` in fleet.yml).** `renames check` verifies every
  target exists in each model; `renames apply` rewrites references in
  measure bodies, calculated columns, partition sources with or without
  fences, calculation items and format-string definitions, and column
  endpoints in relationships.tmdl. Per-model `rename_profile`.
  `tmdl_drift_doctor.renames.rewrite_measure_references(path, map)` is the
  one-file entry point.
- **`remediate --ref NAME [--add-missing]`.** Pushes exactly the named
  template measures to every model, editing each model's own host table
  and keeping its lineage tag; override-declared models are skipped; an
  unknown name exits 2 before anything is written.
- **`measure.duplicate_host` is fixable.** The stale copy on the other
  table is removed (the copy on the template's host stays). It is no longer
  advisory; allowlist the kind to apply it.
- **Table copies register their query order.** `table.missing` appends a
  Power Query table to the model's `PBI_QueryOrder` annotation (calculated
  tables and models without the annotation are left alone); a table
  removal drops the entry and the quoted or bare `ref table` line.
- **Missing axis tables always gate.** A missing `x-Selector`,
  `x-Plot Specific`, `y-Selector` or `y-Plot Specific` table fails `detect`
  whatever `allowlist.kinds` says (writing it still needs the allowlist).

- **Field-parameter tables.** `tmdl_drift_doctor.fieldparams` reads and
  rewrites the tuple constructor of a field parameter losslessly: rows are
  re-sorted by ordinal and grouped under regenerated `-- <group>` headers,
  while parked (commented-out) rows, annotation comments, inline trailers
  and wide tuples survive, the last tuple never keeps a comma, and a second
  write changes nothing. New kinds built on it:
  `fieldparam.row_missing` / `fieldparam.row_drift` (template rows),
  `fieldparam.cross_level.<category>` and
  `fieldparam.plot_specific_extra.<category>` (dimension levels that
  disagree), `fieldparam.blank_row` (the "no legend" row; `detect --kind
  fieldparam.blank_row` exits 2 when the template has none),
  `fieldparam.schema_modernize` / `fieldparam.schema_blocked` (column
  schema; a table with its own extra columns is never touched) and
  `fieldparam.dangling_row` (rows whose NAMEOF points at nothing).
  `drift-doctor fieldparam retag` moves rows between categories.

- **Encoded mapping tables.** Mapping tables stored as Desktop "Enter data"
  strings (`Binary.FromText` of raw-deflated JSON) are decoded
  (`tmdl_drift_doctor.mapping_codec`), captured under `encoded_mapping`,
  and compared by (category, key): `mapping_category.missing`,
  `mapping_row.missing`, advisory `extra.mapping_row`, and the retirements
  `retire.mapping_row` (only the retired keys go, siblings stay) and
  `retire.mapping_category` (`blocked` when the model added its own keys).
  `capture` records row retirements with `category` and `row_key`, a whole
  category once (never its rows again), and stamps revivals with the
  template tree. Every write is verified before it lands and read back
  after it; a failed read-back restores the file. `drift-doctor mapping
  decode|diff|union` inspect and merge tables. Typed override section
  `mapping_categories`.
- **Category annotations.** `category_annotation.orphaned` (report-only) and
  `category_annotation.undeclared` (fix: add the annotation, valued from
  git history or `"<Category> metrics"`; a name that cannot become a key is
  left for a person) keep model.tmdl's `Category_*` annotations in step
  with the categories a model adds to the template's mapping, with a
  shrink-only baseline of accepted exceptions.
- **The ledger refuses a retired mapping row without its target.** A
  `mapping_row` retirement without `row_key` (or with an empty `category`)
  stops every read.

- **Incremental-refresh parity.** For fact tables the template decides
  whether an incremental-refresh policy exists and what its window is:
  `refresh_policy.missing` (the fix builds the policy from the model's own
  partition source, never the template's; report-only when the table
  cannot take one), `refresh_policy.window_drift`, `refresh_policy.invalid`
  (duplicated or misplaced block, out-of-domain scalars, a
  `sourceExpression` that differs from the partition source) and the
  report-only `refresh_policy.client_only`. None is batch-safe; every
  applied fix adds a "full refresh owed" entry to the run digest. Typed
  override section `refresh_windows` and the `incremental_refresh` key.

- **Source divergence.** With a `source_identity` block in fleet.yml, a table
  that reads another source view than the template's is source-divergent
  (also `source_divergent: [...]` per model). `detect` prints the set and
  `detect --json` reports `source_divergent_tables` per model. A column or
  measure spliced onto such a table queues a view-enrichment notice: the run
  merges them into `<state_dir>/view_enrichment_outstanding.json` (this
  run's tables replace their own entries, others pass through untouched),
  writes `view_enrichment_report.json` with this run's entries only and
  pipes them to `notify_command`. `drift-doctor enrichment rescan|report`.
  A modified relationship or a missing refresh policy on a divergent table
  is `skipped_divergent` in batch mode.
- **`retire.source_view`.** `capture` records a template table re-pointed
  to another view once (`source_view_change` in the ledger); `remediate
  --sync` re-points models still on the old view in every spelling (bare
  or quoted helper call; inline queries in the partition and the refresh
  policy together). A model on a view it declares (`source_views`
  override) is retained and one on some other view is left for a person;
  both are written to the decision digest (`remediate --digest-json FILE`).

- **Pristine-category purge (`purge.*`, sync mode).** With `purge.enabled`,
  `detect --sync` / `remediate --sync` remove model-only field-parameter
  rows, measures, columns, mapping keys, relationships and tables of an
  analysis category the model never customised. A category is customised
  when the model reads one of its tables through its own source view and
  wires that table to a table of its own. Protected names
  (`protected_name_pattern`), override retentions and data extensions are
  kept. Deletions: never under `--yes`.
- **Decisions.** Calls routed to a person (a retained source view, a kept
  row in a customised category, a measure still referenced) are
  `decision` items: `detect` prints them, `detect --json` lists them per
  model under `decisions`, and `remediate` collects them in the run digest
  (`--digest-json`). They never fail a gate.
- **`detect` works before the first capture**, comparing against the live
  template without writing anything (a note says so).

- **Analysis groups.** `drift-doctor groups plan` builds the graph of
  groups, field groups, selector measures and the fact tables they read, and
  splits a drop set's objects into exclusive (safe to remove) and shared
  (kept, naming the survivors); dead fact tables (no data reference, only
  sensor calls) are always listed. `groups drop` runs the layers in
  dependency order (selector-branch lines, the measure purge with its
  liveness rules, selector rows, the model annotation, gate measures and
  their aggregate branches, pinned bookmarks, dead fact tables re-derived
  after the purge, mapping rows last), each available alone with `--layer`
  and a dry run unless `--apply`. `groups drop-table` removes a table from
  every surface it occupies, keeping report JSON byte-identical apart from
  the pruned references (float literals included) and listing what it
  cannot remove safely for manual review.

- **Selector arms.** `fieldparam.dangling_selector` reports SWITCH arms that
  call a measure the model lacks and removes those lines (batch-safe);
  `drift-doctor add-branch` clones a sibling's arms for a new measure.
- **Named measure removals.** `drift-doctor purge --measure` removes measures
  from chosen models on any host table (blocked while anything calls them;
  two hosts need `--table`); `drift-doctor retire OLD --new NEW` moves report
  bindings and then deletes the superseded declaration (`--drop` deletes
  only the declaration).
- **`groups annotate`** writes one `Category_*` annotation per mapping group,
  keeping existing human-written descriptions.
- **`column.purge_listed`** (sync mode): configured bookkeeping columns on
  shared tables are removed, with a source-cleanup notice.
- **Opt-in deletes of model-only objects.** On a terminal, `remediate` offers
  to delete an extra measure, column or relationship (one destructive
  question, naming how many report references would break); batch runs and
  `--yes` never do.
- **Retirement channel.** A model measure whose core name matches a retired
  measure is retired with it; protected names are never retired (blocked);
  a revived object the model still carries is a digest line.

### Changed

- **The ratchet reports in its own words.** A rise prints `TOTAL rose A ->
  B` and `category '<head>' rose A -> B` (it used to print the per-kind
  names), advisory models print under an `ADVISORY` heading, and a clean
  run prints `no ledger-retired object resurrected` next to the per-model
  status. The failure banner is `DRIFT RATCHET RED`.
- **A source-divergent table keeps its own columns.** A table that reads
  the model's own source view (rather than the template's) is
  acknowledged once: its own columns and relationships are not reported
  as extras, with the reason in the finding's override. Its missing
  columns still count.
- **`measure.wrong_host` is one finding per measure.** A template measure
  missing from its host but present on several other tables is one move
  from the alphabetically first of them (it used to be a duplicate-host
  finding per table), and the detail says whether the body matches the
  template.
- **`remediate` without a terminal applies nothing (breaking).** When
  stdin is not a terminal, questions are disabled, every item is reported
  as skipped ("non-interactive") and nothing is written, unless `--yes` is
  given with at least one `--kind`. Deletions (`retire.*` removals, purges)
  are never applied under `--yes`: they need an interactive yes or `--sync
  --confirm-deletions`. CI jobs that ran a bare `remediate` must add `--yes
  --kind ...`.
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

### Changed

- **A run prints one line per model and a fleet total.** A fleet run
  printed one line per finding; an operator watching a sweep could not read
  one line per model out of it, and there was no fleet total to compare
  with the previous sweep. A remediation run now ends with a
  `── Sync summary (DRY-RUN|APPLIED):` block: one line per model and a
  `FLEET TOTAL` line, each with the same six counters.
- **The cascade report carries the shape of each model.** Each model's
  block now carries a shape line (`fact_tables=N f_measures=N
  user_facing=N helpers=N`), so a reader can see that one model has grown
  helper measures.
- **The decision digest is persisted.** The digest entries — the items that
  need a person's decision — were printed and could be written on request,
  but a needs-decision item had no trace after the run that found it. A
  run that produced at least one entry now writes
  `<state_dir>/decision_digest.json`. A run with no entry never writes the
  file and never removes an existing one.

### Fixed

- **A model the run could not check is marked as such.** The cascade
  report said a model was in the fleet; a reader could not tell a model
  that is behind from a model that could not be looked at. A model whose
  detection failed is now marked `[FAIL ]` with the reason, and the summary
  counts it apart from the models that were checked.
- **An interrupted run says what happened.** The run now catches Ctrl-C,
  prints "any edits already accepted are written and valid; re-run to
  resume from the remaining items" on standard error, and exits with the
  interrupt code 130.
- **A field-parameter row that points at a retired object is now removed.**
  A measure retired from the template leaves its field-parameter rows behind
  in the derived models: the row's `NAMEOF(...)` still names the measure
  the template deliberately dropped. The tool removed a row that points at
  nothing at all, but not a row that points at a retired object. The
  finding `retire.fieldparam_entry` now names the table and the category,
  and the repair removes exactly those rows — every other row, the row
  order and the table's own formatting stay as they were.
- **A missing analysis category is reported.** A field-parameter row names
  the mapping key it serves (`Account_y`); the mapping table maps that key
  to an analysis category (`Account`). When the template's field parameters
  used a category the derived model's mapping table did not carry, the tool
  said nothing. It now reports `fieldparam.category_missing`, naming the
  category and the derived model. It is report-only: the mapping table is
  written by hand.
- **A mapping table that cannot be decoded fails loudly.** The decode error
  was swallowed and the whole mapping comparison quietly disappeared, so a
  model with a corrupt mapping table read as free of mapping drift. The
  model is now recorded as unreadable: the run names the model and the
  table on standard error and exits with the parse-failure code.
- **A reference to a retired table can be repaired.** When a table is
  retired from the template and a derived model still reads it, the tool
  reported the finding `retire.stranded_ref` but had no repair for it: the
  kind was permanent, and a model that reads a table that no longer exists
  kept reading it. A reference that lives inside a dead sensor operand —
  `ISFILTERED('Old Campaigns')` and friends — is now stripped, and the rest
  of the chain keeps working. A measure that reads the retired table for
  its rows is left alone: removing the reference would change what the
  measure means, so the tool says a person must decide.
- **A client-only table can be purged.** The tool knew how to remove a
  client-only table from a derived model and listed the kind among the
  removable ones, but no detection ever produced the finding, so the kind
  was unreachable and a table nothing read any more stayed in the model
  forever. The purge now reports it, and removing it deletes the table
  file, deregisters the table from `model.tmdl` and from the query order,
  and sweeps the relationships that wired it.
- **A no-op fleet tighten now records the measurement.** A tighten that
  lowers no floor still measures the fleet, so the floors file now records
  that: the update timestamp and the template tree at the top of the file
  are refreshed and the last-tighten block is rewritten with the report's
  scope, the models it covered, the report's template tree and the time.
  Before, a CI job asking "when was this floor last measured against the
  live template?" read a stale answer. A scoped tighten still leaves those
  two top-level values exactly as they were, and the floors themselves are
  unchanged either way.
- **A ledger line that is not an event object is refused.** The freshness
  command reads two ledgers, and the ledger command reads one. A line that
  holds valid JSON but is not an object — an array, a string, a number —
  raised a Python error. Both now refuse such a line the way they refuse a
  malformed one: exit 2, a message on standard error, no traceback.
- **`diff` refuses a detection report it cannot read.** The two reports it
  compares are artifacts: they may be hand-edited, truncated by a pipeline or
  written by another version of the tool. A report whose models block, a
  model's findings list or a single finding has the wrong shape raised a
  Python error. The command now refuses such a report as a configuration
  problem: exit 2, a message on standard error, no traceback.
- **A saved detection report the ratchet cannot read is refused, not
  crashed on.** A CI job hands `ratchet check --from-json` an artifact, and
  the artifact may be hand-edited, truncated by a pipeline or written by
  another version of the tool. A report whose top level is not an object, or
  whose scope block, models block, findings list or a single finding has the
  wrong shape, raised a Python error. The ratchet now refuses such a report
  the way it refuses any other configuration problem: exit 2, a message on
  standard error, no traceback.
- **A retirement no sync can apply is now named.** The ratchet counts the
  findings a sync could act on, so a retirement kind that no sync can apply
  was not printed at all: a report said nothing about an object the ledger
  proves was retired from the template and that is back in a derived model.
  Every retirement finding is now named in the `ratchet check` output, under
  a `RETIREMENT REPORTED` block. Such a finding does not fail the check
  unless `--include-advisory` is given, which makes it fail.
- **A floors file that grants a retirement allowance is refused, not
  reported as a rise.** Resurrections are hard zero, so a floor may never
  license one, and the tool never writes such a key: one in the file is a
  hand edit. `ratchet check` and `ratchet tighten` now exit 2 and name the
  model and the value on standard error, leaving the file byte-for-byte
  alone, instead of exiting 1 with a `[RISE]` line on standard output —
  which read as ordinary backlog growth. The legacy per-kind form of the
  floors file is refused the same way.
- **A stale baseline now exits 2, not 1.** When the committed baseline no
  longer matches the live template, the findings computed against it are
  misclassified (a retired object looks missing and gets resurrected). The
  commands that need the baseline — `detect`, `remediate`, `ratchet check`,
  `ratchet tighten` and `review` — refused, but they exited 1, the code
  reserved for actionable findings: a CI job read a configuration problem
  as a verdict about a model. They now exit 2, say so on standard error and
  print no findings summary. `--allow-stale` still proceeds.
- **An object whose name holds a square bracket can be remediated.** The
  reference of an object is a table name and an object name joined with
  brackets, and a name may legitimately hold a bracket of its own
  (`Sales[Joe's Ünï [Proj]]`). The reference was read at its last bracket, so
  the table became `Sales[Joe's Ünï` and the object became `Proj]`; the fix
  then looked for a table file whose name was built from the middle of the
  reference, and the drift could never be fixed. The reference is now read
  at its first bracket, so the table and the object are found as they are
  written, and the cascade copies the block into the model as it should.
- **A fixer no longer changes a model's line endings.** A fleet committed
  from Windows comes back from a fix with Unix line endings, and the diff
  shows the whole file instead of the change that was asked for. The
  cascade, the field-parameter retag, the field-parameter row writer,
  `retire`, `purge`, the rename and the annotation writers now read each
  file's own line endings and write them back; only the text that actually
  changed differs. A model file that uses Windows line endings comes out of
  a remediation run with the same line endings it went in with.
- **A file keeps its trailing newline.** `purge` dropped the final newline
  when the object it removed was the last one in the file, so the next diff
  showed a spurious "\ No newline at end of file". A file that ended with a
  newline still ends with one; a file that did not still does not.
- **A byte-order mark is ignored, not reported as a broken model.** A model
  file saved from Windows may start with a UTF-8 byte-order mark. The
  parser now skips it and reads the model like any other; before, the tool
  reported "no `table` declaration found" for a file that is valid TMDL.
- **`freshness` no longer claims a merge is safe for a retirement kind it
  could not check.** When the head ledger holds a retirement of a kind the
  command does not check (a `mapping_row`, for example), it printed
  "nothing at risk: none of the unseen retirements is present in a derived
  model" and wrote "None of them is present in any derived model, so nothing
  can be resurrected by this merge" — a safety claim about a kind it never
  looked at. The output now says the check is incomplete and that the merge
  is not known to be safe, and the report body says the same instead of
  claiming that nothing can be resurrected. The "nothing at risk" line is
  printed only when every unseen retirement was checked.
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

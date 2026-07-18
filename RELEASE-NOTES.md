# Release notes

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
- Diátaxis documentation set: tutorial, four how-to guides, four reference
  pages, and eleven explanation pages including the four operating
  principles.

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

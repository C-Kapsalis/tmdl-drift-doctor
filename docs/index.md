# tmdl-drift-doctor documentation

The documentation follows the [Diátaxis](https://diataxis.fr/) framework:
tutorials teach, how-to guides solve, reference informs, explanation deepens.

## Tutorials

- [Getting started](tutorials/getting-started.md) — set up a fleet, capture a
  baseline, seed a drift, detect it, remediate it.

## How-to guides

- [Add a model to the fleet](how-to/add-a-model.md)
- [Cascade a column retirement](how-to/cascade-a-column-retirement.md)
- [Recover from an unwanted remediation (revival)](how-to/recover-from-unwanted-remediation.md)
- [Run drift-doctor in CI](how-to/run-in-ci.md)

## Reference

- [CLI reference](reference/cli.md)
- [Configuration: fleet.yml and the allowlist](reference/configuration.md)
- [Drift-kind catalog](reference/drift-kinds.md)
- [Ledger format](reference/ledger-format.md)

## Explanation

Topic guides — one per thing the suite looks for:

- [Missing objects](explanation/missing-objects.md) — tables, columns,
  measures the template has and a derived model lacks
- [Expression drift](explanation/expression-drift.md) — DAX bodies and shared
  parameters that silently diverged
- [Property drift](explanation/property-drift.md) — format strings, hidden
  flags and friends
- [Extra objects](explanation/extra-objects.md) — why derived-only additions
  are advisory, never deletions
- [Object retirement](explanation/object-retirement.md) — measures and whole
  tables removed from the template
- [Column retirement](explanation/column-retirement.md) — the finer-grained
  drop that whole-object inventories miss
- [Mapping-row retirement](explanation/mapping-row-retirement.md) — removing
  one row from a lookup table that survives

Operating principles — why the suite behaves the way it does:

- [Baseline-recapture discipline](explanation/baseline-recapture-discipline.md)
- [Allowlist-only cascading](explanation/allowlist-only-cascading.md)
- [The ledger and revival model](explanation/ledger-and-revival.md)
- [Safe TMDL surgery](explanation/safe-tmdl-surgery.md)

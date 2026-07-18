# Examples

A ready-made fleet of TMDL-format semantic models to run the tool against —
a fictional gym chain with one template ("golden") model and two franchise
models, drift of every major kind pre-seeded so each command has something
real to find.

```
examples/fleet/
├── fleet.yml                              # fleet config — relative paths, starter allowlist
├── template/GymChain.SemanticModel/       # the canonical model you maintain
└── derived/
    ├── alpha/GymChain.SemanticModel/      # franchise model with seeded drift
    └── bravo/GymChain.SemanticModel/      # franchise model with different seeded drift
```

## What is seeded where

| Model | Drift | Object | Story |
|---|---|---|---|
| alpha | `measure.missing` | `Members[New Members #]` | Never received the template's new-joiners KPI |
| alpha | `measure.expression_drift` | `Visits[Total Visits]` | Still uses the old formula (filters out zero-minute visits) |
| alpha | `measure.property_drift` | `Visits[Avg Visit Duration]` | Someone changed the format string to `0.00` |
| alpha | `expression.drift` | `Reporting Start Date` | Shared reporting-window parameter left at an old date |
| alpha | `extra.measure` | `Members[Alpha Loyalty Score]` | Alpha's own KPI — **advisory**, never remediated |
| bravo | `table.missing` | `Classes` | The template gained a class-schedule table bravo never got |
| bravo | `column.missing` | `Members[MembershipTier]` | Bravo deleted the tier column |
| bravo | `column.property_drift` | `Members[JoinDate]` | Hand-edited to `dd/mm/yyyy` |
| bravo | `mapping_row.missing` | `Plan Map/elite` | Bravo's plan lookup lacks the Elite tier row |

The `Data Source` parameter differs in every model too — and is never
flagged, because only allowlist-**named** shared expressions are compared.
That is the point: per-tenant connection parameters cannot be repointed by a
careless cascade.

The starter allowlist in `fleet.yml` permits every non-destructive kind
(inserts and overwrites). Retirement — the only deletion — stays
double-gated: `retire.measure` is listed so act two below works, but a
removal still requires `--sync` at run time.

## The five-command walkthrough

Running the tool writes state (`.drift-doctor/`) and fixes the derived
models, so work on a copy and keep the shipped fleet pristine:

```console
$ pip install .            # from the repository root
$ cp -r examples/fleet ~/gym-fleet && cd ~/gym-fleet
```

**1. Capture** the template baseline:

```console
$ drift-doctor capture
baseline written: .drift-doctor/baseline.json
  tables=5 columns=16 measures=8 mapping_rows=4 expressions=2
```

**2. Detect** — read-only; exits 1 because remediable drift exists:

```console
$ drift-doctor detect

── alpha: 5 finding(s)
  measure.missing              Members[New Members #]
  measure.expression_drift     Visits[Total Visits]
      template: COUNTROWS(Visits)
      derived:  COUNTROWS(FILTER(Visits, Visits[DurationMinutes] > 0))
  measure.property_drift       Visits[Avg Visit Duration]
      template: formatString=0.0
      derived:  formatString=0.00
  expression.drift             Reporting Start Date
  extra.measure                Members[Alpha Loyalty Score]  [advisory]

── bravo: 4 finding(s)
  table.missing                Classes
  column.property_drift        Members[JoinDate]
      template: dataType=dateTime, formatString=yyyy-mm-dd, ...
      derived:  dataType=dateTime, formatString=dd/mm/yyyy, ...
  column.missing               Members[MembershipTier]
  mapping_row.missing          Plan Map/elite
```

**3. Preview** the cascade — unified diffs, zero writes (not even the ledger):

```console
$ drift-doctor remediate --dry-run
[would apply] alpha: measure.missing Members[New Members #] — insert measure block from template (fresh lineage tag)
--- a/.../derived/alpha/GymChain.SemanticModel/definition/tables/Members.tmdl
+++ b/.../derived/alpha/GymChain.SemanticModel/definition/tables/Members.tmdl
@@ -35,6 +35,18 @@
...
── summary: applied=0 would-apply=8 skipped=1 failed=0
```

**4. Apply** — the advisory extra is skipped, everything else cascades:

```console
$ drift-doctor remediate
...
[skipped] alpha: extra.measure Members[Alpha Loyalty Score] — advisory — this object exists only in the derived model; extensions are reported for review, never auto-remediated. ...
── summary: applied=8 would-apply=0 skipped=1 failed=0
```

A second `remediate` applies nothing (`applied=0 skipped=1`) — every applier
is idempotent. `detect` now exits 0 with only the advisory left.

**5. Ledger** — the append-only audit trail of what just happened:

```console
$ drift-doctor ledger
2026-07-18T09:59:11+00:00  remediated  measure.missing  model=alpha  Members[New Members #]  — insert measure block from template (fresh lineage tag)
2026-07-18T09:59:11+00:00  remediated  measure.expression_drift  model=alpha  Visits[Total Visits]  — overwrite measure block from template (lineage tag preserved)
...
2026-07-18T09:59:12+00:00  remediated  mapping_row.missing  model=bravo  Plan Map/elite  — add mapping row 'elite' from template
```

## Act two: retirement and revival

The removal side of the workflow. Retire a measure from the template, watch
the retirement cascade — and protect one franchise that wants to keep it.

1. Delete the whole `measure 'Peak Hour Visits' = ...` block (through its
   `lineageTag:` line) from
   `template/GymChain.SemanticModel/definition/tables/Visits.tmdl`.

2. Recapture — the drop is recorded as ledger proof, the only thing that
   ever authorizes a deletion downstream:

   ```console
   $ drift-doctor capture
     tables=5 columns=16 measures=7 mapping_rows=4 expressions=2
     [ledger] retired measure: Visits[Peak Hour Visits]
   ```

3. Detect — both franchises still carry the retired measure:

   ```console
   $ drift-doctor detect
   ── alpha: 2 finding(s)
     extra.measure                Members[Alpha Loyalty Score]  [advisory]
     retire.measure               Visits[Peak Hour Visits]
   ── bravo: 1 finding(s)
     retire.measure               Visits[Peak Hour Visits]
   ```

4. A plain run refuses to delete — retirement is double-gated (allowlist
   **and** `--sync`):

   ```console
   $ drift-doctor remediate --kind retire.
   [skipped] alpha: retire.measure Visits[Peak Hour Visits] — retirement removals require --sync (deletions never run in a default pass) — re-run with --sync to apply this removal
   [skipped] bravo: ...
   ```

5. Alpha wants to keep the KPI — revive it for alpha only, then sync:

   ```console
   $ drift-doctor ledger --revive "Visits[Peak Hour Visits]" --kind measure --model alpha --note "alpha keeps this KPI on its dashboards"
   revived measure Visits[Peak Hour Visits] for model 'alpha' — it will not be re-removed.

   $ drift-doctor remediate --kind retire. --sync
   [applied] bravo: retire.measure Visits[Peak Hour Visits] — remove retired measure block
   ── summary: applied=1 would-apply=0 skipped=0 failed=0
   ```

6. `detect` exits 0. Alpha's kept measure is now reported as an advisory
   `extra.measure` (a deliberate extension), bravo is clean, and the ledger
   holds the whole exchange:

   ```console
   $ drift-doctor ledger
   ...
   2026-07-18T09:59:44+00:00  retired  measure  Visits[Peak Hour Visits]
   2026-07-18T09:59:45+00:00  revived  measure  model=alpha  Visits[Peak Hour Visits]  (alpha keeps this KPI on its dashboards)
   2026-07-18T09:59:45+00:00  remediated  retire.measure  model=bravo  Visits[Peak Hour Visits]  — remove retired measure block
   ```

Done experimenting? Delete your copy and re-copy `examples/fleet` — the
shipped fleet is always in its pristine pre-retirement state.

## Going deeper

- The step-by-step narrated version of this walkthrough:
  [docs/tutorials/getting-started.md](../docs/tutorials/getting-started.md)
- Every drift kind, detection rule and gate:
  [docs/reference/drift-kinds.md](../docs/reference/drift-kinds.md)
- The same fleet also lives at `tests/fixtures/fleet/` where it doubles as
  the test suite's specification.

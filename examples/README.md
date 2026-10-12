# Examples

A ready-made fleet of TMDL-format semantic models to run the tool against —
a fictional gym chain with one template ("golden") model and two franchise
models, drift of every major kind pre-seeded so each command has something
real to find.

```
examples/fleet/
├── fleet.yml                              # fleet config — relative paths, starter allowlist
├── template/
│   ├── GymChain.pbip                      # ← open this in Power BI Desktop
│   ├── GymChain.SemanticModel/            # the canonical model you maintain
│   └── GymChain.Report/                   # thin report bound to the model (blank canvas)
└── derived/
    ├── alpha/
    │   ├── GymChain.pbip                  # ← open this to SEE the drift
    │   ├── GymChain.SemanticModel/        # franchise model with seeded drift
    │   └── GymChain.Report/
    └── bravo/
        ├── GymChain.pbip
        ├── GymChain.SemanticModel/        # franchise model with different seeded drift
        └── GymChain.Report/
```

Each model folder now ships a **PBIP wrapper** (`GymChain.pbip` plus a
`.Report` sibling and the `.platform`/`.pbism`/`.pbir` metadata files) so you
can open it directly in Power BI Desktop and *see* the drift with your own
eyes — before and after remediation. The report is intentionally an empty
canvas: you inspect the **model**, not a report. The drift tool ignores every
one of these wrapper files — it reads only `GymChain.SemanticModel/definition/`
— so detection and remediation behave exactly as they did without the wrapper.

## What is seeded where

| Model | Drift | Object | Story |
|---|---|---|---|
| alpha | `measure.missing` | `Members[New Members #]` | Never received the template's new-joiners KPI |
| alpha | `measure.expression_drift` | `Visits[Total Visits]` | Still uses the old formula (filters out zero-minute visits) |
| alpha | `measure.property_drift` | `Visits[Avg Visit Duration]` | Someone changed the format string to `0.00` |
| alpha | `expression.drift` | `Reporting Start Date` | Shared reporting-window parameter left at an old date |
| alpha | `extra.measure` | `Members[Alpha Loyalty Score]` | Alpha's own KPI — **advisory**, never remediated |
| alpha | `measure.wrong_host` | `Visits[Visits per Member]` | Alpha keeps this template measure on `Members`; the fix moves it verbatim |
| alpha | `expression.drift` | `Portal URL` | The report-portal link still points at the old path (a change inside an `https://` string) |
| alpha | `extra.measure` | `Classes[Alpha Seats Left]` | Alpha's own KPI that reads `Classes[Capacity]`: retire that column and the retirement is **blocked** |
| bravo | `table.missing` | `Classes` | The template gained a class-schedule table bravo never got |
| bravo | `column.missing` | `Members[MembershipTier]` | Bravo deleted the tier column |
| bravo | `column.property_drift` | `Members[JoinDate]` | Hand-edited to `dd/mm/yyyy` |
| bravo | `mapping_row.missing` | `Plan Map/elite` | Bravo's plan lookup lacks the Elite tier row |
| bravo | `relationship.missing` | `Members[MembershipTier] -> Plan Map[PlanKey]` | Needs the missing column first; the same run adds both, in order |
| bravo | `relationship.modified` | `Visits[MemberId] -> Members[MemberId]` | Someone switched it to filter in both directions |
| bravo | `relationship.extra` | `Members[JoinDate] -> Promotions[StartDate]` | Bravo's own relationship: **advisory** |

Bravo also re-saved its `Reporting Start Date` parameter with the `meta [...]`
bag in a different order. That is not drift, and it is not flagged.

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

```bash
# bash / macOS / Linux
pip install .                              # from the repository root
cp -r examples/fleet /tmp/gym-fleet && cd /tmp/gym-fleet
```

```powershell
# PowerShell / Windows
pip install .                              # from the repository root
Copy-Item -Recurse examples\fleet $env:TEMP\gym-fleet ; Set-Location $env:TEMP\gym-fleet
```

The `drift-doctor` commands in the rest of this walkthrough are byte-identical
in every shell — only the copy step above differs between bash and PowerShell.

**1. Capture** the template baseline:

```console
$ drift-doctor capture
baseline written: .drift-doctor/baseline.json
  tables=5 columns=17 measures=9 relationships=3 mapping_rows=4 expressions=3
```

**2. Detect** — read-only; exits 1 because remediable drift exists:

```console
$ drift-doctor detect

── alpha: 8 finding(s)
  measure.missing              Members[New Members #]
  measure.expression_drift     Visits[Total Visits]
      template: COUNTROWS(Visits)
      derived:  COUNTROWS(FILTER(Visits, Visits[DurationMinutes] > 0))
  measure.property_drift       Visits[Avg Visit Duration]
      template: formatString=0.0
      derived:  formatString=0.00
  measure.wrong_host           Visits[Visits per Member]
      template: Visits
      derived:  Members
  expression.drift             Portal URL
  expression.drift             Reporting Start Date
  extra.measure                Classes[Alpha Seats Left]  [advisory]
  extra.measure                Members[Alpha Loyalty Score]  [advisory]

── bravo: 7 finding(s)
  table.missing                Classes
  column.property_drift        Members[JoinDate]
      template: dataType=dateTime, formatString=yyyy-mm-dd, ...
      derived:  dataType=dateTime, formatString=dd/mm/yyyy, ...
  column.missing               Members[MembershipTier]
  mapping_row.missing          Plan Map/elite
  relationship.missing         Members[MembershipTier] -> Plan Map[PlanKey]
  relationship.modified        Visits[MemberId] -> Members[MemberId]
      template: (engine defaults)
      derived:  crossFilteringBehavior=bothDirections
  relationship.extra           Members[JoinDate] -> Promotions[StartDate]  [advisory]

── summary: actionable=12 advisory=3 overridden=0 errors=0
```

**3. Preview** the cascade — unified diffs, zero writes (not even the ledger):

```console
$ drift-doctor remediate --dry-run
[would apply] alpha: measure.missing Members[New Members #] — insert measure block from template (fresh lineage tag)
--- a/.../derived/alpha/GymChain.SemanticModel/definition/tables/Members.tmdl
+++ b/.../derived/alpha/GymChain.SemanticModel/definition/tables/Members.tmdl
@@ -35,6 +35,18 @@
...
── summary: applied=0 would-apply=12 skipped=3 blocked=0 auto_skipped_precondition=0 overridden=0 failed=0
```

**4. Apply**: the advisories are skipped, everything else cascades in
dependency order (tables, columns, expressions, rows, measures, then
relationships):

```console
$ drift-doctor remediate
...
[skipped] alpha: extra.measure Members[Alpha Loyalty Score] — advisory — this object exists only in the derived model; extensions are reported for review, never auto-remediated. ...
── summary: applied=12 would-apply=0 skipped=3 blocked=0 auto_skipped_precondition=0 overridden=0 failed=0
```

A second `remediate` applies nothing (`applied=0 skipped=3`): every applier
is idempotent. `detect` now exits 0 with only the advisories left. When one
fix can only be seen after another lands, `remediate --converge` re-detects
and re-applies until nothing changes (at most four passes).

**5. Ledger** — the append-only audit trail of what just happened:

```console
$ drift-doctor ledger
2026-07-18T09:59:11+00:00  remediated  measure.missing  model=alpha  Members[New Members #]  — insert measure block from template (fresh lineage tag)
2026-07-18T09:59:11+00:00  remediated  measure.expression_drift  model=alpha  Visits[Total Visits]  — overwrite measure block from template (lineage tag preserved)
...
2026-07-18T09:59:12+00:00  remediated  mapping_row.missing  model=bravo  Plan Map/elite  — add mapping row 'elite' from template
```

## Open the models in Power BI Desktop (see the drift with your own eyes)

Don't take the CLI's word for it — watch a drifted measure change in Power BI
Desktop itself, before and after remediation. This is the skeptic's loop.

> **Platform note.** Power BI Desktop is **Windows-only**, so opening a model
> to eyeball it needs Windows + a Power BI Desktop install. Everything else —
> `capture`, `detect`, `remediate` — runs anywhere Python does (Windows,
> macOS, Linux). The visual check is a convenience, not a dependency.

Work on a fresh copy so the shipped fleet stays pristine:

```powershell
# PowerShell / Windows
Copy-Item -Recurse examples\fleet $env:TEMP\gym-demo ; Set-Location $env:TEMP\gym-demo
```

```bash
# bash / macOS / Linux (capture/detect/remediate work here; opening needs Windows)
cp -r examples/fleet /tmp/gym-demo && cd /tmp/gym-demo
```

**1. Open the drifted model — BEFORE.** In Power BI Desktop, open
`derived/alpha/GymChain.pbip` (double-click it, or *File → Open*). It loads to
a blank report canvas with the model attached. Switch to the **Model** view
and find the `Total Visits` measure on the `Visits` table — its DAX still
reads the old, drifted formula:

```dax
Total Visits = COUNTROWS(FILTER(Visits, Visits[DurationMinutes] > 0))
```

That is the divergence: the template long ago simplified this to
`COUNTROWS(Visits)`, but franchise alpha never got the fix. Close Power BI
Desktop.

**2. Cascade the template's truth onto this copy.** Capture the baseline, then
remediate just that drift kind:

```console
$ drift-doctor capture
$ drift-doctor remediate --model alpha --kind measure.expression_drift
[applied] alpha: measure.expression_drift Visits[Total Visits] — overwrite measure block from template (lineage tag preserved)
```

**3. Re-open the same model — AFTER.** Open `derived/alpha/GymChain.pbip`
again, back to the **Model** view, and look at `Total Visits`. Its DAX now
matches the template exactly:

```dax
Total Visits = COUNTROWS(Visits)
```

Same lineage tag, same measure, corrected formula — the drift is gone, and you
watched it happen in the tool your analysts actually use. (Try the same
before/after with bravo's `Members[JoinDate]` column format, or run a full
`drift-doctor remediate` and re-open both franchises.)

Done? Delete the copy (`Remove-Item -Recurse $env:TEMP\gym-demo` /
`rm -rf /tmp/gym-demo`) and re-copy `examples/fleet` when you want a clean
slate.

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
     tables=5 columns=17 measures=8 relationships=3 mapping_rows=4 expressions=3
     [ledger] retired measure: Visits[Peak Hour Visits]
   ```

3. Detect — both franchises still carry the retired measure:

   ```console
   $ drift-doctor detect
   ── alpha: 3 finding(s)
     extra.measure                Classes[Alpha Seats Left]  [advisory]
     extra.measure                Members[Alpha Loyalty Score]  [advisory]
     retire.measure               Visits[Peak Hour Visits]
   ── bravo: 2 finding(s)
     relationship.extra           Members[JoinDate] -> Promotions[StartDate]  [advisory]
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
   ── summary: applied=1 would-apply=0 skipped=0 blocked=0 auto_skipped_precondition=0 overridden=0 failed=0
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

## Act three: the governance commands

On the same copy, after the walkthrough above:

```console
$ drift-doctor ratchet tighten        # seed .drift-doctor/floors.json
$ drift-doctor ratchet check          # exit 1 if any model's drift rises
$ drift-doctor audit                  # exit 4 if an applied fix was undone
$ drift-doctor review --out REVIEW.md # the checklist of human decisions
```

To see the coupling guard, retire `Classes[Capacity]` from the template
(delete the column block, and change `SUM(Classes[Capacity])` in
`Class Fill Rate %` to `COUNTROWS(Classes)`), add `retire.column` to the
allowlist, recapture, and run `remediate --sync`. Bravo's copy of the column
is removed; alpha's is `blocked`, because alpha's own `Alpha Seats Left`
still reads it.

## Going deeper

- The step-by-step narrated version of this walkthrough, every drift kind,
  and the configuration reference are in the documentation
  (`tmdl-drift-doctor-documentation`, published on Read the Docs).
- The same fleet also lives at `tests/fixtures/fleet/`, where it doubles as
  the test suite's specification (with a few more seeded cases, such as a
  measure stored on two tables, which would stop the model from opening in
  Power BI Desktop and so is left out of this example).

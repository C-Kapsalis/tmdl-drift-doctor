# Getting started

In this tutorial you will take the example fleet that ships with the
repository, capture a baseline, watch the detector find nine seeded drifts,
seed one more by hand, and cascade the fixes back. It takes about ten
minutes.

## Prerequisites

- Python 3.10+ (the CLI runs on Windows, macOS and Linux alike).
- Nothing else — the repository ships a runnable example fleet under
  `examples/fleet/` (a fictional gym chain), and this tutorial uses it.
- *Optional:* Power BI Desktop (Windows-only) if you want to open the models
  and see the drift visually — see
  [examples/README.md](../../examples/README.md#open-the-models-in-power-bi-desktop-see-the-drift-with-your-own-eyes).

Install the package from a checkout of this repository (identical in every
shell):

```console
$ pip install .
$ drift-doctor --version
```

## 1. Lay out the fleet

Running the tool writes state and fixes models in place, so copy the example
fleet somewhere writable and keep the shipped one pristine:

```bash
# bash / macOS / Linux
cp -r examples/fleet /tmp/gym-fleet
cd /tmp/gym-fleet
```

```powershell
# PowerShell / Windows
Copy-Item -Recurse examples\fleet $env:TEMP\gym-fleet
Set-Location $env:TEMP\gym-fleet
```

Every `drift-doctor` command below is byte-identical in both shells — only the
copy step above differs.

The layout is one template model and two derived ("franchise") models, each
wrapped as a Power BI Desktop project (`GymChain.pbip`) you can open directly:

```
fleet.yml
template/GymChain.pbip                       # open in Power BI Desktop
template/GymChain.SemanticModel/definition/...
derived/alpha/GymChain.pbip
derived/alpha/GymChain.SemanticModel/definition/...
derived/bravo/GymChain.pbip
derived/bravo/GymChain.SemanticModel/definition/...
```

`fleet.yml` names the template, the derived models, the mapping tables, and —
crucially — the **allowlist** of drift kinds that may be auto-fixed. The
example's starter allowlist permits every non-destructive kind and lists
`retire.measure`, which still needs `--sync` at run time — deletions are
double-gated:

```yaml
version: 1
template: template/GymChain.SemanticModel
models:
  alpha: derived/alpha/GymChain.SemanticModel
  bravo: derived/bravo/GymChain.SemanticModel
mapping_tables:
  - Plan Map
state_dir: .drift-doctor
allowlist:
  kinds:
    - measure.missing
    - measure.expression_drift
    # ... every non-destructive kind, plus retire.measure
  expressions:
    - Reporting Start Date
```

## 2. Capture the baseline

```console
$ drift-doctor capture
baseline written: .drift-doctor/baseline.json
  tables=5 columns=16 measures=8 mapping_rows=4 expressions=2
```

The baseline is the committed statement of what the template contains. Every
`detect` and `remediate` run compares derived models against it — and refuses
to run if the template has changed since (see
[Baseline-recapture discipline](../explanation/baseline-recapture-discipline.md)).

## 3. Detect the seeded drift

The example franchises ship with drift already seeded, so:

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
      template: dataType=dateTime, formatString=yyyy-mm-dd, sourceColumn=JoinDate, summarizeBy=none
      derived:  dataType=dateTime, formatString=dd/mm/yyyy, sourceColumn=JoinDate, summarizeBy=none
  column.missing               Members[MembershipTier]
  mapping_row.missing          Plan Map/elite
      template: {"elite", "Elite", 89}
      derived:  None
```

`detect` exited 1 — remediable drift exists, which is what makes it a CI
gate. Two more things to notice:

- `extra.measure` is tagged **advisory**: alpha's own `Alpha Loyalty Score`
  is a legitimate franchise extension. The suite reports it and will never
  delete it (see [Extra objects](../explanation/extra-objects.md)).
- The `Data Source` parameter also differs in both franchises — and is *not*
  flagged, because only allowlist-named shared expressions are compared.
  Cascading connection parameters would repoint every tenant's warehouse.

## 4. Seed one more drift yourself

Open `derived/bravo/GymChain.SemanticModel/definition/tables/Visits.tmdl`
and change the `Total Visits` DAX to anything else. Run `drift-doctor detect`
again — a new `measure.expression_drift` appears for bravo. (Undo it, or
carry it through the next step — either works.)

## 5. Preview the fix, then apply it

Always preview first. `--dry-run` renders unified diffs and writes nothing —
not even the ledger:

```console
$ drift-doctor remediate --dry-run
[would apply] alpha: measure.missing Members[New Members #] — insert measure block from template (fresh lineage tag)
--- a/.../derived/alpha/GymChain.SemanticModel/definition/tables/Members.tmdl
+++ b/.../derived/alpha/GymChain.SemanticModel/definition/tables/Members.tmdl
@@ -35,6 +35,18 @@
 		lineageTag: aaaa1111-1111-4111-8111-000000000008

+	/// New joiners inside the reporting window.
+	measure 'New Members #' =
+
+			VAR _start = DATE(2024, 1, 1)
+			RETURN
...
── summary: applied=0 would-apply=8 skipped=1 failed=0
```

Then apply:

```console
$ drift-doctor remediate
...
[skipped] alpha: extra.measure Members[Alpha Loyalty Score] — advisory — this object exists only in the derived model; extensions are reported for review, never auto-remediated. ...
── summary: applied=8 would-apply=0 skipped=1 failed=0
```

The skipped item is the advisory extra. Run `drift-doctor remediate` a second
time: `applied=0` — every applier is idempotent. And `drift-doctor detect`
now exits 0, with only the advisory left:

```console
$ drift-doctor detect

── alpha: 1 finding(s)
  extra.measure                Members[Alpha Loyalty Score]  [advisory]
```

## 6. Inspect the audit trail

Every applied fix left a line in the append-only ledger:

```console
$ drift-doctor ledger
2026-07-18T09:59:11+00:00  remediated  measure.missing  model=alpha  Members[New Members #]  — insert measure block from template (fresh lineage tag)
2026-07-18T09:59:11+00:00  remediated  measure.expression_drift  model=alpha  Visits[Total Visits]  — overwrite measure block from template (lineage tag preserved)
2026-07-18T09:59:12+00:00  remediated  measure.property_drift  model=alpha  Visits[Avg Visit Duration]  — overwrite measure block from template (lineage tag preserved)
2026-07-18T09:59:12+00:00  remediated  expression.drift  model=alpha  Reporting Start Date  — cascade shared expression 'Reporting Start Date' from template (allowlist-named; everything else untouched)
2026-07-18T09:59:12+00:00  remediated  table.missing  model=bravo  Classes  — copy table from template + register in model.tmdl (review the partition source — it is the template's)
2026-07-18T09:59:12+00:00  remediated  column.property_drift  model=bravo  Members[JoinDate]  — overwrite column block from template (lineage tag preserved)
2026-07-18T09:59:12+00:00  remediated  column.missing  model=bravo  Members[MembershipTier]  — insert column block from template (fresh lineage tag)
2026-07-18T09:59:12+00:00  remediated  mapping_row.missing  model=bravo  Plan Map/elite  — add mapping row 'elite' from template
```

## Where to go next

- [examples/README.md](../../examples/README.md) — "act two" of this same
  fleet: retire a measure from the template, cascade the removal with
  `--sync`, and revive it on one franchise. It also has an
  [open-in-Power-BI-Desktop loop](../../examples/README.md#open-the-models-in-power-bi-desktop-see-the-drift-with-your-own-eyes)
  so you can watch a drifted measure change before and after remediation
  (Windows + Power BI Desktop).
- [Cascade a column retirement](../how-to/cascade-a-column-retirement.md) —
  the removal side of the workflow, step by step.
- [Run drift-doctor in CI](../how-to/run-in-ci.md) — make drift a build signal.
- [Drift-kind catalog](../reference/drift-kinds.md) — every kind, its
  detection rule and its remediation.

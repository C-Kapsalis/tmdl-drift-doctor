# tmdl-drift-doctor

Drift detection and auto-remediation for **fleets of TMDL-format Power BI
semantic models derived from one template**.

You maintain one template ("golden") model and a copy per client. Last month
you fixed the `Total Visits` measure in the template, and fourteen client
models still carry the old DAX. Someone hand-edited client 7's format strings and
nobody remembers why. A table you retired in March still haunts three tenants,
and "are the clients up to date?" is an afternoon of eyeballing folder diffs.

tmdl-drift-doctor turns that into three commands: **capture** a baseline of
the template, **detect** typed drift findings in every derived model, and
**remediate**: an auditable, allowlist-gated cascade of template truth back
out to the fleet. Deletions require double opt-in, every applied fix lands in
an append-only ledger, and `--dry-run` shows you the exact diffs first.

## How it works

- **Template**: the one model you actually maintain (`*.SemanticModel`
  folder in TMDL format). The canonical *core*, not a superset: derived
  models may legitimately extend beyond it.
- **Fleet**: the derived per-client models, declared in `fleet.yml`.
- **Baseline**: a committed JSON snapshot of the template
  (`.drift-doctor/baseline.json`), written by `capture`. Detection compares
  each derived model against the baseline, and refuses to run if the
  template changed since the last capture, because a stale baseline
  misclassifies drift (a retired object looks "missing" and gets
  resurrected).
- **Ledger**: an append-only JSONL audit trail
  (`.drift-doctor/ledger.jsonl`). Recapture records objects *retired from
  the template* here; that record is the only thing that ever authorizes a
  deletion downstream.

```
template model ──capture──▶ baseline.json ──┐
                                            ├─detect──▶ typed findings ──remediate──▶ fleet
derived models ─────────────────────────────┘              (allowlist + --sync gates,
                                                            ledger-audited, dry-runnable)
```

## 60-second demo

The repository ships a runnable example fleet, a fictional gym chain with
one template and two franchises, drift of every kind pre-seeded. Copy it
somewhere writable and point the CLI at it
([examples/README.md](examples/README.md) has the guided walkthrough,
retirement and revival included):

```bash
# bash / macOS / Linux
pip install -e .
cp -r examples/fleet /tmp/gym-fleet && cd /tmp/gym-fleet
```

```powershell
# PowerShell / Windows
pip install -e .
Copy-Item -Recurse examples\fleet $env:TEMP\gym-fleet ; Set-Location $env:TEMP\gym-fleet
```

The `drift-doctor` commands themselves are identical in every shell; only the
copy step above differs between bash and PowerShell:

```console
$ drift-doctor capture
baseline written: .drift-doctor/baseline.json
  tables=5 columns=17 measures=9 relationships=3 mapping_rows=4 expressions=3
```

Detect (read-only; exits 1 when actionable drift exists, CI-ready):

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
  column.missing               Members[MembershipTier]
  mapping_row.missing          Plan Map/elite
  relationship.missing         Members[MembershipTier] -> Plan Map[PlanKey]
  relationship.modified        Visits[MemberId] -> Members[MemberId]
      template: (engine defaults)
      derived:  crossFilteringBehavior=bothDirections
  relationship.extra           Members[JoinDate] -> Promotions[StartDate]  [advisory]

── summary: actionable=12 advisory=3 overridden=0 errors=0
```

Note what is *not* flagged: the franchises' own measures and relationships
are advisory (franchise extensions are legitimate); the `Data Source`
connection parameter differs in every franchise but is never compared
(only allowlist-named shared expressions participate); and bravo's
`Reporting Start Date`, re-saved with its `meta [...]` bag in a different
order, is not drift. Alpha's `Portal URL` is: the change sits inside an
`https://` string, which is content, not a comment.

Preview the cascade (real unified diffs, zero writes):

```console
$ drift-doctor remediate --dry-run
[would apply] alpha: measure.missing Members[New Members #] — insert measure block from template (fresh lineage tag)
--- a/derived/alpha/GymChain.SemanticModel/definition/tables/Members.tmdl
+++ b/derived/alpha/GymChain.SemanticModel/definition/tables/Members.tmdl
@@ -35,6 +35,18 @@
 		lineageTag: aaaa1111-1111-4111-8111-000000000008

+	/// New joiners inside the reporting window.
+	measure 'New Members #' =
+
+			VAR _start = DATE(2024, 1, 1)
+			RETURN
...
```

Apply it, and read the audit trail:

```console
$ drift-doctor remediate
[applied] alpha: measure.missing Members[New Members #] — insert measure block from template (fresh lineage tag)
[applied] alpha: measure.wrong_host Visits[Visits per Member] — move measure block verbatim from 'Members' to the template's host 'Visits' (DAX and lineage tag untouched)
[applied] alpha: measure.expression_drift Visits[Total Visits] — overwrite measure block from template (lineage tag preserved)
[skipped] alpha: extra.measure Members[Alpha Loyalty Score] — advisory — this object exists only in the derived model; extensions are reported for review, never auto-remediated. ...
[applied] bravo: table.missing Classes — copy table from template + register in model.tmdl (review the partition source — it is the template's)
[applied] bravo: mapping_row.missing Plan Map/elite — add mapping row 'elite' from template
[applied] bravo: relationship.missing Members[MembershipTier] -> Plan Map[PlanKey] — append relationship block from template
...
── summary: applied=12 would-apply=0 skipped=3 blocked=0 auto_skipped_precondition=0 overridden=0 failed=0

$ drift-doctor ledger
2026-07-18T04:25:10+00:00  remediated  measure.missing  model=alpha  Members[New Members #]  — insert measure block from template (fresh lineage tag)
...
```

Fixes run in dependency order (bravo's new relationship lands after the
column it needs, in the same run). A second `remediate` applies nothing:
every applier is idempotent. A second `detect` exits 0 with only the
advisories left. When one fix only becomes visible after another lands,
`remediate --converge` re-detects and re-applies until nothing changes
(at most four passes).

The removal side: retire a measure from the template, recapture, and the drop
is recorded as ledger proof. `remediate` still skips it (deletions
additionally need `--sync`), and a revival protects any tenant that
deliberately keeps the object:

```console
$ drift-doctor capture
  [ledger] retired measure: Visits[Peak Hour Visits]

$ drift-doctor remediate --kind retire.
[skipped] alpha: retire.measure Visits[Peak Hour Visits] — retirement removals require --sync (deletions never run in a default pass) — re-run with --sync to apply this removal

$ drift-doctor ledger --revive "Visits[Peak Hour Visits]" --kind measure --model alpha --note "alpha kept this KPI"
$ drift-doctor remediate --kind retire. --sync
[applied] bravo: retire.measure Visits[Peak Hour Visits] — remove retired measure block
── summary: applied=1 would-apply=0 skipped=0 blocked=0 auto_skipped_precondition=0 overridden=0 failed=0
```

Alpha keeps its measure; bravo is cleaned up; the whole exchange is in the
ledger. A retirement that derived work still depends on is refused: retire
`Classes[Capacity]` and alpha's removal is `blocked`, because alpha's own
`Alpha Seats Left` still reads that column.

## Drift kinds

| Kind | What it means | What remediation does | Gated by |
|---|---|---|---|
| `table.missing` | Template table absent from a derived model | Copy the table file (all lineage tags regenerated) + register `ref table` in `model.tmdl` | allowlist |
| `column.missing` | Template column absent from a shared table | Splice the template's raw column block (fresh lineage tag) | allowlist |
| `measure.missing` | Template measure absent from a shared table | Splice the template's raw measure block, `///` description included | allowlist |
| `mapping_row.missing` | Template lookup-table row absent | Insert the row tuple after the last existing row | allowlist |
| `measure.expression_drift` | Shared measure's DAX diverged (whitespace/comments never count) | Overwrite the block from the template; derived lineage tag preserved | allowlist |
| `measure.property_drift` | Format string, hidden flag, display folder… diverged | Same block overwrite | allowlist |
| `column.property_drift` | Shared column's type/format/summarization diverged | Same block overwrite | allowlist |
| `expression.drift` | An allowlist-**named** shared parameter diverged or is absent | Replace exactly that expression block; the rest of `expressions.tmdl` stays byte-untouched | allowlist (name gates detection too) |
| `measure.wrong_host` | A template measure lives on a different table in the derived model | Move the derived block verbatim to the template's host (DAX and lineage tag untouched; destination written first) | allowlist |
| `measure.duplicate_host` | The same measure name sits on more than one table | Nothing: advisory (the model will not load until a copy is removed by hand) | never remediated |
| `relationship.missing` | Template relationship (matched on `From[col] -> To[col]`, never on its GUID) absent | Append the template's raw block, once both endpoint columns exist | allowlist |
| `relationship.modified` | Same endpoints, different cardinality, direction, or active flag | Overwrite with the template's block, keeping the derived block name | allowlist |
| `relationship.extra` | Derived-only relationship between two shared tables | Nothing: advisory | never remediated |
| `extra.measure` / `extra.column` | Derived-only object on a shared table | Nothing: advisory, never auto-deleted | never remediated |
| `retire.table` / `retire.column` / `retire.measure` / `retire.mapping_row` / `retire.relationship` | Object retired from the template (ledger-proven), still carried by a derived model | Remove the block / file / row; a table or column takes its template-owned or ledger-retired relationships with it | allowlist **+ `--sync`**, refused (`blocked`) while derived work depends on it |

Derived-only tables and mapping rows are not findings at all; extending the
template is legitimate. Measure names are model-global, so `measure.missing`
never inserts a name that already exists anywhere in the model. Full detection rules and gates are in the
[documentation](#documentation) below.

## Safety model

- **Nothing cascades by default.** A drift kind the fleet allowlist does not
  name is detected and reported, never applied. Shared expressions are
  compared only if individually named, so your per-tenant connection
  parameters cannot be repointed by a careless cascade.
- **Deletions are double-gated.** The only removals ever proposed are of
  objects *provably retired from the template*, recorded in the ledger at
  recapture, and applying them requires both the allowlist entry and the
  `--sync` flag. A default run is purely additive/overwriting.
- **Dry-run everywhere.** `remediate --dry-run` computes every edit and
  prints unified diffs without writing anything: not the models, not the
  ledger.
- **Append-only ledger with revival.** Every retirement, revival and applied
  fix is one immutable JSONL line; a corrupt ledger fails reads loudly rather
  than silently mis-targeting a removal. Reviving (`ledger --revive`,
  fleet-wide or per-model) cancels a retirement without erasing its history,
  so a deliberate re-add is never re-removed.
- **Retirements respect derived work.** `retire.table`, `retire.column` and
  `retire.measure` are refused (`blocked`, with the reason) while derived-only
  DAX, derived-only relationships, a `sortByColumn` or a hierarchy level still
  depend on the object. Template-owned dependents are re-checked when the
  removal runs: it waits until their own fix has landed.
- **Preconditions, checked at apply time.** Every fix states what must
  already be true (relationship endpoints exist, the objects an inserted
  measure references exist or are being added in this run, no measure name
  would be duplicated). An unmet one is `auto_skipped_precondition`, never
  forced.
- **Declared divergence is data.** A per-model override in `fleet.yml`
  (`expression`, `absent` or `retain`, each with a required reason) is never
  remediated and never fails CI, and every run prints it as an ACK with its
  reason, owner and date.
- **Stale baselines are refused.** If the template changed since the last
  `capture`, `detect` and `remediate` stop with an error (override:
  `--allow-stale`). Acting on a stale baseline is how retired objects get
  resurrected.
- **Guarded TMDL surgery.** Edits splice raw text blocks (never re-serialized
  from parsed fields, so annotations and formatting survive), lineage tags
  are regenerated on insert and preserved on overwrite, and every new file
  must pass validation before it touches disk. That validation includes the
  invariant that a property line is never inserted into a DAX expression
  body.

The write guards exist because of a real failure mode: inserting `isHidden`
after `measure X =` when the expression is bare multi-line puts the property
*inside* the DAX, and the engine reads it as the start of the expression, so
every affected measure fails to compile, fleet-wide. See "Design: safe TMDL
surgery" in the CLI and configuration reference of the
[documentation](#documentation) below.

## Configuration

One file per fleet; all paths relative to it:

```yaml
# fleet.yml
version: 1
template: template/Product.SemanticModel
models:
  acme:   clients/acme/Product.SemanticModel
  globex:                                   # long form
    path: clients/globex/Product.SemanticModel
    gating: advisory                        # findings print, never fail CI
    overrides:                              # declared, reviewed divergence
      - object: Sales[Net Revenue]
        kind: expression                    # expression | absent | retain
        reason: globex nets out partner rebates by contract
        owner: bi-team
        added: 2026-10-01

mapping_tables:          # tables whose DATATABLE rows are diffed row-by-row
  - Plan Map

state_dir: .drift-doctor # baseline.json, ledger.jsonl, floors.json: commit all three

allowlist:               # empty allowlist = nothing is ever remediated
  kinds:
    - measure.missing
    - measure.expression_drift
    - column.missing
    - relationship.missing
    - retire.measure     # still needs --sync at run time
  expressions:
    - Reporting Start Date   # ONLY named expressions are ever compared
```

Full schema and the reasoning behind each gate are in the
[documentation](#documentation) below.

## CI usage

`detect` is read-only and exits 0 (clean, or only advisories, overridden or
not-allowlisted findings, or findings on `gating: advisory` models), 1
(actionable drift), or 3 (a model could not be parsed); a stale baseline is
an error too, so a PR that edits the template but forgets to recapture fails
loudly. `detect --json` writes a versioned report (`schema_version: 2`) with
the scope it covered, any errors, and per-model findings and counts.

Drift is often an accepted backlog, so the usual gate is the ratchet rather
than a clean `detect`:

```yaml
- run: pip install ./tools/tmdl-drift-doctor   # or from your package index
- name: Drift may only go down
  run: drift-doctor ratchet check --fleet fleet.yml
- name: Applied fixes stayed applied
  run: drift-doctor audit --fleet fleet.yml
```

| Command | What it does | Exit codes |
|---|---|---|
| `ratchet tighten` | Seeds or lowers `.drift-doctor/floors.json` to `min(floor, current)` per model, total and per kind; never raises a floor; only from a full-fleet, unfiltered, error-free run | 0, or 2 for an untrusted report |
| `ratchet check` | Fails if any model's actionable count rises above its floor, or any `retire.*` finding exists (a resurrection: hard zero, never floored) | 0 / 1 / 2 |
| `audit` | Re-verifies every `remediated` ledger event: added objects still present, retired objects not back | 0, or 4 on a revert |
| `review --out REVIEW.md` | Writes a per-model checklist of everything routed to a human: blocked retirements, precondition skips, wrong or duplicate hosts, advisories, overrides in force | 0 |

Keep `remediate` on a maintainer's machine where the diffs get reviewed and
committed; if you want automated fix-up PRs, post the `--dry-run` diff for
review instead. The CI how-to in the [documentation](#documentation) covers
warn-only versus gate and scheduled audits.

## Documentation

The full documentation, organized with the [Diátaxis](https://diataxis.fr/)
framework (a getting-started tutorial, how-to guides for managing a fleet
and running in CI, and the CLI, configuration, drift-kind and ledger
reference), is published from the separate `tmdl-drift-doctor-documentation`
repository on Read the Docs at
<https://tmdl-drift-doctor-documentation.readthedocs.io/en/latest/>.

Version history is in [RELEASE-NOTES.md](RELEASE-NOTES.md).

## Part of a small family of Power BI tools

tmdl-drift-doctor is one of a few related, independently installable tools;
a team adopts whichever ones it needs, not all of them.

- [`tmdl-preflight`](https://github.com/C-Kapsalis/tmdl-preflight): a linter
  and auto-fixer for a single TMDL-format model.
- [`pbip-model-forge`](https://github.com/C-Kapsalis/pbip-model-forge):
  builds a sample Power BI model from a plain-language description.
- [`pbi-plot-styler`](https://github.com/C-Kapsalis/pbi-plot-styler): keeps
  report visuals on-brand as measures are added.
- [`BI-report-template-site`](https://github.com/C-Kapsalis/BI-report-template-site):
  not a tool, a documentation paradigm demonstrating what Diátaxis-style
  documentation looks like applied to a Power BI report.

## Scope and non-goals

- Reads/writes the **TMDL folder format** (`definition/tables/*.tmdl`, …).
- Diffs tables, columns, measures, relationships, named shared expressions
  and mapping-table rows. Perspectives, cultures, roles, field parameters and
  the report layer are out of scope (today); hierarchies and `sortByColumn`
  are read only to guard retirements.
- Operates on files only: no workspace or service integration, so it slots
  into any git-based deployment pipeline. Deletions that would orphan report
  references are yours to sequence.

## Development

```console
$ pip install -e ".[dev]"
$ pytest
```

The fixture fleet under `tests/fixtures/fleet/` doubles as a readable,
runnable specification: every drift kind the suite detects is seeded there.

## Contributing

Contributions are welcome: new drift kinds, bug fixes, documentation, and
example fleets. See [CONTRIBUTING.md](CONTRIBUTING.md) for the development
setup and the recipe for adding a new drift kind.

## License

MIT: see [LICENSE](LICENSE).

# tmdl-drift-doctor

Drift detection and auto-remediation for **fleets of TMDL-format Power BI
semantic models derived from one template**.

You maintain one template ("golden") model and a copy per client. Last month
you fixed the `Total Visits` measure in the template — fourteen client models
still carry the old DAX. Someone hand-edited client 7's format strings and
nobody remembers why. A table you retired in March still haunts three tenants,
and "are the clients up to date?" is an afternoon of eyeballing folder diffs.

tmdl-drift-doctor turns that into three commands: **capture** a baseline of
the template, **detect** typed drift findings in every derived model, and
**remediate** — an auditable, allowlist-gated cascade of template truth back
out to the fleet. Deletions require double opt-in, every applied fix lands in
an append-only ledger, and `--dry-run` shows you the exact diffs first.

## How it works

- **Template** — the one model you actually maintain (`*.SemanticModel`
  folder in TMDL format). The canonical *core*, not a superset: derived
  models may legitimately extend beyond it.
- **Fleet** — the derived per-client models, declared in `fleet.yml`.
- **Baseline** — a committed JSON snapshot of the template
  (`.drift-doctor/baseline.json`), written by `capture`. Detection compares
  each derived model against the baseline — and refuses to run if the
  template changed since the last capture, because a stale baseline
  misclassifies drift (a retired object looks "missing" and gets
  resurrected).
- **Ledger** — an append-only JSONL audit trail
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

The repository ships a runnable example fleet — a fictional gym chain with
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

The `drift-doctor` commands themselves are identical in every shell — only the
copy step above differs between bash and PowerShell:

```console
$ drift-doctor capture
baseline written: .drift-doctor/baseline.json
  tables=5 columns=16 measures=8 mapping_rows=4 expressions=2
```

Detect (read-only; exits 1 when remediable drift exists — CI-ready):

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
  column.missing               Members[MembershipTier]
  mapping_row.missing          Plan Map/elite
```

Note what is *not* flagged: alpha's own `Alpha Loyalty Score` is advisory
(franchise extensions are legitimate), and the `Data Source` connection
parameter differs in every franchise but is never compared — only
allowlist-named shared expressions participate.

Preview the cascade — real unified diffs, zero writes:

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
[applied] alpha: measure.expression_drift Visits[Total Visits] — overwrite measure block from template (lineage tag preserved)
[skipped] alpha: extra.measure Members[Alpha Loyalty Score] — advisory — this object exists only in the derived model; extensions are reported for review, never auto-remediated. ...
[applied] bravo: table.missing Classes — copy table from template + register in model.tmdl (review the partition source — it is the template's)
[applied] bravo: mapping_row.missing Plan Map/elite — add mapping row 'elite' from template
...
── summary: applied=8 would-apply=0 skipped=1 failed=0

$ drift-doctor ledger
2026-07-18T04:25:10+00:00  remediated  measure.missing  model=alpha  Members[New Members #]  — insert measure block from template (fresh lineage tag)
...
```

A second `remediate` applies nothing — every applier is idempotent. A second
`detect` exits 0 with only the advisory left.

The removal side: retire a measure from the template, recapture, and the drop
is recorded as ledger proof. `remediate` still skips it — deletions
additionally need `--sync` — and a revival protects any tenant that
deliberately keeps the object:

```console
$ drift-doctor capture
  [ledger] retired measure: Visits[Peak Hour Visits]

$ drift-doctor remediate --kind retire.
[skipped] alpha: retire.measure Visits[Peak Hour Visits] — retirement removals require --sync (deletions never run in a default pass) — re-run with --sync to apply this removal

$ drift-doctor ledger --revive "Visits[Peak Hour Visits]" --kind measure --model alpha --note "alpha kept this KPI"
$ drift-doctor remediate --kind retire. --sync
[applied] bravo: retire.measure Visits[Peak Hour Visits] — remove retired measure block
── summary: applied=1 would-apply=0 skipped=0 failed=0
```

Alpha keeps its measure; bravo is cleaned up; the whole exchange is in the
ledger.

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
| `extra.measure` / `extra.column` | Derived-only object on a shared table | Nothing — advisory, never auto-deleted | never remediated |
| `retire.table` / `retire.column` / `retire.measure` / `retire.mapping_row` | Object retired from the template (ledger-proven), still carried by a derived model | Remove the block / file / row | allowlist **+ `--sync`** |

Derived-only tables and mapping rows are not findings at all — extending the
template is legitimate. Full detection rules and gates:
[docs/reference/drift-kinds.md](docs/reference/drift-kinds.md).

## Safety model

- **Nothing cascades by default.** A drift kind the fleet allowlist does not
  name is detected and reported, never applied. Shared expressions are
  compared only if individually named — your per-tenant connection
  parameters cannot be repointed by a careless cascade.
- **Deletions are double-gated.** The only removals ever proposed are of
  objects *provably retired from the template* — recorded in the ledger at
  recapture — and applying them requires both the allowlist entry and the
  `--sync` flag. A default run is purely additive/overwriting.
- **Dry-run everywhere.** `remediate --dry-run` computes every edit and
  prints unified diffs without writing anything — models, ledger, nothing.
- **Append-only ledger with revival.** Every retirement, revival and applied
  fix is one immutable JSONL line; a corrupt ledger fails reads loudly rather
  than silently mis-targeting a removal. Reviving (`ledger --revive`,
  fleet-wide or per-model) cancels a retirement without erasing its history,
  so a deliberate re-add is never re-removed.
- **Stale baselines are refused.** If the template changed since the last
  `capture`, `detect` and `remediate` stop with an error (override:
  `--allow-stale`) — acting on a stale baseline is how retired objects get
  resurrected.
- **Guarded TMDL surgery.** Edits splice raw text blocks (never re-serialized
  from parsed fields, so annotations and formatting survive), lineage tags
  are regenerated on insert and preserved on overwrite, and every new file
  must pass validation before it touches disk — including the invariant that
  a property line is never inserted into a DAX expression body.

The write guards exist because of a real failure mode: inserting `isHidden`
after `measure X =` when the expression is bare multi-line puts the property
*inside* the DAX, and the engine reads it as the start of the expression —
every affected measure fails to compile, fleet-wide. See
[docs/explanation/safe-tmdl-surgery.md](docs/explanation/safe-tmdl-surgery.md).

## Configuration

One file per fleet; all paths relative to it:

```yaml
# fleet.yml
version: 1
template: template/Product.SemanticModel
models:
  acme:   clients/acme/Product.SemanticModel
  globex: clients/globex/Product.SemanticModel

mapping_tables:          # tables whose DATATABLE rows are diffed row-by-row
  - Plan Map

state_dir: .drift-doctor # baseline.json + ledger.jsonl — commit both

allowlist:               # empty allowlist = nothing is ever remediated
  kinds:
    - measure.missing
    - measure.expression_drift
    - column.missing
    - retire.measure     # still needs --sync at run time
  expressions:
    - Reporting Start Date   # ONLY named expressions are ever compared
```

Full schema and the reasoning behind each gate:
[docs/reference/configuration.md](docs/reference/configuration.md).

## CI usage

`detect` is read-only and exits 0 (clean or advisories only), 1 (remediable
drift), or errors on a stale baseline — so a PR that edits the template but
forgets to recapture fails loudly:

```yaml
- run: pip install ./tools/tmdl-drift-doctor   # or from your package index
- name: Detect drift (read-only)
  run: drift-doctor detect --fleet fleet.yml --json > drift.json
```

Keep `remediate` on a maintainer's machine where the diffs get reviewed and
committed; if you want automated fix-up PRs, post the `--dry-run` diff for
review instead. Patterns (warn-only vs gate, scoped nightly retirement
checks): [docs/how-to/run-in-ci.md](docs/how-to/run-in-ci.md).

## Documentation

[Diátaxis](https://diataxis.fr/)-organized under [docs/](docs/index.md):

- **Tutorial** — [Getting started](docs/tutorials/getting-started.md): the
  fixture fleet, end to end.
- **How-to** — [add a model](docs/how-to/add-a-model.md) ·
  [cascade a column retirement](docs/how-to/cascade-a-column-retirement.md) ·
  [recover from an unwanted remediation](docs/how-to/recover-from-unwanted-remediation.md) ·
  [run in CI](docs/how-to/run-in-ci.md)
- **Reference** — [CLI](docs/reference/cli.md) ·
  [configuration](docs/reference/configuration.md) ·
  [drift-kind catalog](docs/reference/drift-kinds.md) ·
  [ledger format](docs/reference/ledger-format.md)
- **Explanation** — a topic guide per drift kind, plus the four operating
  principles: [baseline-recapture discipline](docs/explanation/baseline-recapture-discipline.md),
  [allowlist-only cascading](docs/explanation/allowlist-only-cascading.md),
  [the ledger and revival model](docs/explanation/ledger-and-revival.md),
  [safe TMDL surgery](docs/explanation/safe-tmdl-surgery.md).

Version history: [RELEASE-NOTES.md](RELEASE-NOTES.md).

## Scope and non-goals

- Reads/writes the **TMDL folder format** (`definition/tables/*.tmdl`, …).
- Diffs tables, columns, measures, named shared expressions and
  mapping-table rows. Relationships, perspectives, cultures and the report
  layer are out of scope (today).
- Operates on files only — no workspace or service integration, so it slots
  into any git-based deployment pipeline. Deletions that would orphan report
  references are yours to sequence.

## Development

```console
$ pip install -e ".[dev]"
$ pytest
```

The fixture fleet under `tests/fixtures/fleet/` doubles as a readable,
runnable specification: every drift kind the suite detects is seeded there.

## License

MIT — see [LICENSE](LICENSE).

# Contributing to tmdl-drift-doctor

`tmdl-drift-doctor` is a Python CLI that detects and auto-remediates drift
across a **fleet of TMDL-format Power BI semantic models derived from one
template**: you maintain one "golden" model, and the tool cascades its truth
back out to the per-client copies, safely and auditably. Contributions of all
kinds are welcome: new drift kinds, bug fixes, docs, and example fleets.

## Development setup

Requires Python 3.10+.

```bash
git clone <your-fork-url> tmdl-drift-doctor
cd tmdl-drift-doctor
python -m venv .venv
# bash / macOS / Linux
source .venv/bin/activate
# PowerShell / Windows
.venv\Scripts\Activate.ps1

pip install -e ".[dev]"    # editable install + pytest
pip install -e ../../pbi-plot-styler/pbi-plot-styler   # report-side tests
pytest                     # the full suite should pass before you start
```

Two sync tests rename measures inside a Power BI report and count the
report's references; that report logic belongs to
[pbi-plot-styler](https://github.com/C-Kapsalis/pbi-plot-styler), so install
it next to drift-doctor (any checkout path works) before running the suite.

The editable install puts the `drift-doctor` console command on your PATH, so
your code changes are picked up immediately. Try it against the bundled
example fleet (copy it somewhere writable first: running the tool writes
state and edits models):

```bash
cp -r examples/fleet /tmp/gym-fleet && cd /tmp/gym-fleet
drift-doctor capture
drift-doctor detect
```

See [`examples/README.md`](examples/README.md) for the full guided
walkthrough (capture → detect → remediate → ledger, plus retirement/revival).

## Project layout

```
src/tmdl_drift_doctor/
├── cli.py          # Click entry point: capture / detect / remediate / ratchet /
│                   #   audit / review / ledger
├── fleet.py        # fleet.yml loader + Fleet (paths, allowlist, gating, overrides)
├── overrides.py    # resolve per-model overrides; which findings each acknowledges
├── tmdl.py         # TMDL parser -> Model (tables, measures, relationships,
│                   #   hierarchies) + string-aware normalization, DAX references
├── baseline.py     # `capture`: snapshot the template, maintain retirements
├── detect.py       # `detect`: typed Findings, coupling guard, actionable
│                   #   predicate, the JSON report (schema v2)
├── remediate.py    # `remediate`: appliers, preconditions, dependency order,
│                   #   the gate engine, --converge
├── tmdl_edit.py    # guarded raw-block TMDL surgery + write validators
├── ledger.py       # append-only JSONL audit trail (retire/revive/remediated)
├── ratchet.py      # `ratchet check|tighten`: per-model floors, hard-zero retire.*
├── audit.py        # `audit`: re-verify applied fixes from the ledger
└── review.py       # `review`: the human review checklist

tests/              # pytest suite + tests/fixtures/fleet/ (the seeded fleet)
examples/fleet/     # runnable gym-chain fleet with drift pre-seeded
```

The full Diátaxis documentation (a tutorial, how-to guides and reference
pages) lives in the separate `tmdl-drift-doctor-documentation` repository,
published on Read the Docs.

## The mental model

Three commands form a pipeline, and every design decision follows from it:

1. **`capture`** snapshots the template into a committed baseline
   (`.drift-doctor/baseline.json`) and maintains the retirement record:
   anything dropped from the template since the last capture becomes a
   `retired` ledger event.
2. **`detect`** compares each derived model (parsed live from disk) against
   the baseline plus the retirement record and emits typed `Finding`s. It is
   read-only and CI-friendly (exit 1 when actionable drift exists: the kind
   is allowlisted, the finding is not advisory and no override acknowledges
   it; `detect.is_actionable` is the one predicate the exit code, the
   ratchet and `review` share).
3. **`remediate`** consumes those findings and applies a per-kind fix,
   cascading template truth back into the derived models, in dependency
   order (`remediate.PHASES`).

Around the pipeline, `ratchet` keeps drift from rising, `audit` checks that
applied fixes stayed applied, and `review` collects what only a human can
decide.

### Safety gates (do not weaken these without discussion)

- **Allowlist**: nothing cascades by default. A drift kind the fleet's
  `allowlist.kinds` does not name is detected and reported, never applied.
  Shared expressions are compared only if individually named under
  `allowlist.expressions`, so per-tenant connection parameters can never be
  repointed by a careless cascade.
- **`--sync` (double-gated deletions)**: the only removals ever proposed are
  of objects *provably retired from the template* (`retire.*` kinds), and
  applying them requires **both** the allowlist entry **and** the `--sync`
  flag. A default `remediate` run is purely additive/overwriting.
- **Ledger**: every retirement, revival, and applied fix is one immutable
  append-only JSONL line. A retirement recorded at recapture is the *only*
  thing that ever authorizes a downstream deletion; a revival cancels a
  retirement without erasing history, so a deliberate re-add is never
  re-removed.
- **Baseline-recapture discipline**: `detect` and `remediate` refuse to run
  on a stale baseline (template changed since the last `capture`), because a
  stale baseline misclassifies drift: a retired object looks "missing" and
  gets resurrected. `--allow-stale` overrides, at your own risk. **Recapture
  the moment you change the template.**
- **Guarded TMDL surgery**: edits splice *raw text blocks* (never
  re-serialize from parsed fields, so annotations and formatting survive),
  lineage tags are regenerated on insert and preserved on overwrite, and
  every new file content passes `tmdl_edit.assert_tmdl_valid` before it
  touches disk, including the invariant that a property line is never
  injected into a DAX expression body (which would break every affected
  measure fleet-wide). See "Design: safe TMDL surgery" in the CLI and
  configuration reference in `tmdl-drift-doctor-documentation`.
- **Coupling guard and preconditions**: a retirement that derived-only work
  depends on is `blocked` at detect time (`detect.coupling_blockers`), and
  every fix's precondition (`remediate.PRECONDITIONS`) is evaluated at
  apply time against the model as it is now plus what this run already did
  (`RunState`). Never let a fix "succeed" by breaking a dependent.

## How to add a new drift kind

Drift kinds are named `family.detail` (e.g. `measure.expression_drift`) and
that string is what users put in their allowlist, so choose it carefully.
Work through these files in order:

1. **`detect.py`**: emit a `Finding(kind="your.kind", …)` in `detect_model`
   where the divergence is discovered. Register the kind in the right tuple at
   the top of the module: `REMEDIABLE_KINDS` (has an automated fix),
   `ADVISORY_KINDS` (report-only, never applied), and/or
   `RETIREMENT_FINDING_KINDS` (a deletion, automatically `--sync`-gated).
   Stash any surgery-time extras on `Finding.data`. Match objects by stable
   identity (names, field signatures), never by GUID or lineage tag.
2. **`tmdl_edit.py`**: if the fix needs a new text operation, add a *guarded*
   raw-block helper there. Splice raw blocks; never rebuild TMDL from parsed
   dataclasses. Make sure `assert_tmdl_valid` still accepts your output.
3. **`remediate.py`**: write an applier `_apply_your_kind(fleet, f)` that
   returns `(label, [FileChange])` (a `FileChange` with `old=None` creates,
   `new=None` deletes) and register it in the `APPLIERS` dict. The engine
   handles overrides, the allowlist, `--sync`, `blocked`, dry-run, and
   pre-write validation for you; appliers must be **idempotent** (a second
   run finds nothing to do). If the fix relies on something else existing,
   add a precondition to `PRECONDITIONS` and place the kind in `PHASES`. If
   an override should be able to acknowledge it, add it to
   `overrides.SUPPRESSES`.
4. **Fixtures + tests**: seed the new drift in `tests/fixtures/fleet/` (the
   fixture fleet doubles as the suite's readable specification) and add
   coverage under `tests/` (detection, allowlist gating, remediation,
   idempotence; safety-critical surgery goes in `tests/test_tmdl_edit_safety.py`).
5. **Docs**: document the kind in the drift-kind catalog reference page in
   `tmdl-drift-doctor-documentation` (with a "do not cascade blindly when"
   note), and add it to the drift-kinds table in `README.md`.

## Before you open a PR

Run the suite and confirm it is green:

```bash
pytest
```

For changes that touch detection, remediation, or TMDL surgery, also exercise
the CLI end-to-end against a fresh copy of the example fleet (`capture` →
`detect` → `remediate --dry-run` → `remediate` → second `remediate` for
idempotence → `ledger` → `audit`) and confirm the output is what you expect. New
behavior needs new tests.

## Commit and PR conventions

- Write focused commits with imperative, present-tense subjects
  (e.g. "Add hierarchy.missing drift kind", not "Added…"). Keep unrelated
  changes in separate commits.
- Reference the issue you are addressing in the commit body or PR description.
- Open one PR per logical change. In the description, explain **what** changed
  and **why**, and note any impact on the safety gates or the ledger/baseline
  formats. Confirm `pytest` passes and describe how you exercised the change.
- Docs-only or example-only changes are welcome and should say so.

## Issues

When filing a bug, include: the `drift-doctor` version, your Python version
and OS, the command you ran, and the full output (findings, diffs, or the
error). A minimal `fleet.yml` and the smallest TMDL snippet that reproduces
the problem help enormously; the fixture fleet under `tests/fixtures/fleet/`
is a good template for a reproduction. For feature requests, describe the
drift you need caught or cascaded and why the existing kinds do not cover it.

Please do not report security-sensitive issues (e.g. a way to make the tool
write malformed or unintended TMDL past the guards) in a public issue; raise
them privately with the maintainer first.

## License

By contributing, you agree that your contributions are licensed under the
MIT License, the same as the rest of the project. See [`LICENSE`](LICENSE).

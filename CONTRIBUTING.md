# Contributing to tmdl-drift-doctor

`tmdl-drift-doctor` is a Python CLI that detects and auto-remediates drift
across a **fleet of TMDL-format Power BI semantic models derived from one
template** — you maintain one "golden" model, and the tool cascades its truth
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
pytest                     # the full suite should pass before you start
```

The editable install puts the `drift-doctor` console command on your PATH, so
your code changes are picked up immediately. Try it against the bundled
example fleet (copy it somewhere writable first — running the tool writes
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
├── cli.py          # Click entry point: capture / detect / remediate / ledger
├── fleet.py        # fleet.yml loader + Fleet dataclass (paths, allowlist)
├── tmdl.py         # TMDL folder-format parser -> Model / tables / measures / …
├── baseline.py     # `capture`: snapshot the template, maintain retirements
├── detect.py       # `detect`: typed Finding objects, one per divergence
├── remediate.py    # `remediate`: appliers per kind + the gate engine
├── tmdl_edit.py    # guarded raw-block TMDL surgery + write validators
└── ledger.py       # append-only JSONL audit trail (retire/revive/remediated)

tests/              # pytest suite + tests/fixtures/fleet/ (the seeded fleet)
docs/               # Diátaxis docs: tutorials / how-to / reference / explanation
examples/fleet/     # runnable gym-chain fleet with drift pre-seeded
```

## The mental model

Three commands form a pipeline, and every design decision follows from it:

1. **`capture`** snapshots the template into a committed baseline
   (`.drift-doctor/baseline.json`) and maintains the retirement record —
   anything dropped from the template since the last capture becomes a
   `retired` ledger event.
2. **`detect`** compares each derived model (parsed live from disk) against
   the baseline plus the retirement record and emits typed `Finding`s. It is
   read-only and CI-friendly (exit 1 when remediable drift exists).
3. **`remediate`** consumes those findings and applies a per-kind fix,
   cascading template truth back into the derived models.

### Safety gates (do not weaken these without discussion)

- **Allowlist** — nothing cascades by default. A drift kind the fleet's
  `allowlist.kinds` does not name is detected and reported, never applied.
  Shared expressions are compared only if individually named under
  `allowlist.expressions`, so per-tenant connection parameters can never be
  repointed by a careless cascade.
- **`--sync` (double-gated deletions)** — the only removals ever proposed are
  of objects *provably retired from the template* (`retire.*` kinds), and
  applying them requires **both** the allowlist entry **and** the `--sync`
  flag. A default `remediate` run is purely additive/overwriting.
- **Ledger** — every retirement, revival, and applied fix is one immutable
  append-only JSONL line. A retirement recorded at recapture is the *only*
  thing that ever authorizes a downstream deletion; a revival cancels a
  retirement without erasing history, so a deliberate re-add is never
  re-removed.
- **Baseline-recapture discipline** — `detect` and `remediate` refuse to run
  on a stale baseline (template changed since the last `capture`), because a
  stale baseline misclassifies drift: a retired object looks "missing" and
  gets resurrected. `--allow-stale` overrides, at your own risk. **Recapture
  the moment you change the template.**
- **Guarded TMDL surgery** — edits splice *raw text blocks* (never
  re-serialize from parsed fields, so annotations and formatting survive),
  lineage tags are regenerated on insert and preserved on overwrite, and
  every new file content passes `tmdl_edit.assert_tmdl_valid` before it
  touches disk — including the invariant that a property line is never
  injected into a DAX expression body (which would break every affected
  measure fleet-wide). See
  [`docs/explanation/safe-tmdl-surgery.md`](docs/explanation/safe-tmdl-surgery.md).

## How to add a new drift kind

Drift kinds are named `family.detail` (e.g. `measure.expression_drift`) and
that string is what users put in their allowlist, so choose it carefully.
Work through these files in order:

1. **`detect.py`** — emit a `Finding(kind="your.kind", …)` in `detect_model`
   where the divergence is discovered. Register the kind in the right tuple at
   the top of the module: `REMEDIABLE_KINDS` (has an automated fix),
   `ADVISORY_KINDS` (report-only, never applied), and/or
   `RETIREMENT_FINDING_KINDS` (a deletion — automatically `--sync`-gated).
   Stash any surgery-time extras on `Finding.data`.
2. **`tmdl_edit.py`** — if the fix needs a new text operation, add a *guarded*
   raw-block helper there. Splice raw blocks; never rebuild TMDL from parsed
   dataclasses. Make sure `assert_tmdl_valid` still accepts your output.
3. **`remediate.py`** — write an applier `_apply_your_kind(fleet, f)` that
   returns `(label, [FileChange])` (a `FileChange` with `old=None` creates,
   `new=None` deletes) and register it in the `APPLIERS` dict. The engine
   handles the allowlist, `--sync`, dry-run, and pre-write validation for you
   — appliers must be **idempotent** (a second run finds nothing to do).
4. **Fixtures + tests** — seed the new drift in `tests/fixtures/fleet/` (the
   fixture fleet doubles as the suite's readable specification) and add
   coverage under `tests/` (detection, allowlist gating, remediation,
   idempotence; safety-critical surgery goes in `tests/test_tmdl_edit_safety.py`).
5. **Docs** — document the kind in
   [`docs/reference/drift-kinds.md`](docs/reference/drift-kinds.md), add it to
   the drift-kinds table in `README.md`, and add an explanation page if it
   introduces new behavior.

## Before you open a PR

Run the suite and confirm it is green:

```bash
pytest
```

For changes that touch detection, remediation, or TMDL surgery, also exercise
the CLI end-to-end against a fresh copy of the example fleet (`capture` →
`detect` → `remediate --dry-run` → `remediate` → second `remediate` for
idempotence → `ledger`) and confirm the output is what you expect. New
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
the problem help enormously — the fixture fleet under `tests/fixtures/fleet/`
is a good template for a reproduction. For feature requests, describe the
drift you need caught or cascaded and why the existing kinds do not cover it.

Please do not report security-sensitive issues (e.g. a way to make the tool
write malformed or unintended TMDL past the guards) in a public issue; raise
them privately with the maintainer first.

## License

By contributing, you agree that your contributions are licensed under the
MIT License, the same as the rest of the project. See [`LICENSE`](LICENSE).

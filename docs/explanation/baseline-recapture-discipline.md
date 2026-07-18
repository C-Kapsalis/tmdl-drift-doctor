# Baseline-recapture discipline

**The rule: recapture the baseline the moment the template changes. Same
commit, same sitting, no exceptions.**

## Why the baseline exists at all

Detection could, in principle, diff derived models against the live
template directly. It deliberately doesn't, for two reasons:

1. **Retirement provenance.** The ledger's `retired` events come from the
   *recapture diff* — previous baseline vs fresh snapshot. That diff is the
   only trustworthy proof that an object used to be template surface. No
   committed baseline, no proof, no automatable removals (see
   [The ledger and revival model](ledger-and-revival.md)).
2. **A reviewable truth.** The baseline is a committed artifact: what the
   fleet compares against is in version control, changes to it show up in
   PRs, and CI on any machine reproduces the same findings.

## What goes wrong when you skip it

The failure is not "slightly outdated results" — a stale baseline inverts
meanings. Concretely, suppose the template drops column `cleaned` from a
table and nobody recaptures:

- The baseline still lists `cleaned` as template truth.
- Derived models *without* it get flagged `column.missing` — a false
  positive that reports the CORRECT state as drift.
- The missing-object cascade tries to splice `cleaned` **from a template
  that no longer carries it** and crashes mid-sweep — or, resurrection-
  style, re-adds the column from an old copy, undoing the template edit
  fleet-wide.
- Meanwhile the drop is never recorded as a retirement, so models still
  carrying the column are never cleaned.

Every one of those is worse than a missed detection: the tool actively
fights the maintainer. This scenario is why the discipline is framed as
part of the *template edit itself* — an edit isn't finished until the
baseline says so.

## How the tool enforces it

Discipline backed by machinery, not memory:

- **The stale-baseline guard.** `detect` and `remediate` re-snapshot the
  live template and compare it to the committed baseline before doing
  anything; on mismatch they refuse with a clear message. `--allow-stale`
  exists for forensics (comparing against a historical baseline on
  purpose), and its flag name says exactly what you are accepting.
- **Capture is atomic bookkeeping.** One `drift-doctor capture` writes the
  fresh baseline *and* maintains the ledger (retirements appended, returns
  revived). There is no partial state where the baseline knows something
  the ledger doesn't.
- **CI catches the forgetful.** A PR that edits the template without a
  recapture fails the `detect` step loudly ([Run in CI](../how-to/run-in-ci.md)).

## Practice

- Template edit → `drift-doctor capture` → commit both, together. The
  reviewer sees the model change, the baseline change, and any new ledger
  events as one story.
- Read capture's output. `[ledger] retired ...` lines are the removals you
  just authorized for cascade; if one surprises you, investigate before
  anyone runs `--sync`.
- Never hand-edit `baseline.json` to "fix" a finding — the baseline is a
  *capture*, not a config file. If the baseline is wrong, the template is
  wrong; fix that and recapture.

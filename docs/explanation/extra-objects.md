# Extra objects

*Kinds: `extra.measure`, `extra.column` — advisory, never remediated*

## What it is

A derived model carries an object on a **shared table** that the template
does not define: a custom KPI added to the shared measures table, an extra
column on a template fact table. The suite reports these — and does nothing
about them, ever.

## The doctrine: core, not superset

Early fleet tooling is tempted to treat the template as a *superset*:
anything not in the template is cruft, delete it. That model is wrong the
first time a tenant pays for a custom KPI. The sustainable contract is:

> The template is the canonical **core**. Every derived model must contain
> it; derived models may legitimately **extend beyond** it.

From that one sentence, two consequences follow mechanically:

1. **Absence from the template is not evidence of wrongness.** An extra
   object might be tenant customization (keep), an experiment (their call),
   or debris from a template retirement (remove) — and presence alone cannot
   distinguish the three.
2. **The only automatable deletions are provably-ex-template objects** —
   the ones the retirement ledger records. Everything else stays a human
   decision. This is why `extra.*` has no applier at all: not "off by
   default", *structurally absent*. See
   [The ledger and revival model](ledger-and-revival.md).

## How extras arise

- Genuine tenant customization (the healthy case).
- A measure renamed locally — the template name shows as missing, the local
  name as extra. Resolving the pair is a rename, not an add + delete.
- Retirement debris on models that predate the ledger's history.

## Detection logic

Objects in the derived model's shared tables are checked against the
baseline. Anything absent from the baseline is bucketed:

- an **active ledger retirement** for that ref → a `retire.*` finding
  (the one automatable removal);
- otherwise → `extra.*`, advisory.

Two things are deliberately *not* findings at all: **derived-only tables**
(with everything on them) and **derived-only mapping rows** — whole-surface
extensions are the clearest form of legitimate customization, and flagging
them would train users to ignore the report.

## What to do with advisories

- Nothing, usually. A stable set of extras per model is that tenant's
  custom surface; `detect` exits 0 when only advisories remain.
- If an extra should become standard, add it to the *template* and
  recapture — the finding disappears because the object is now core.
- If an extra is truly debris (never template-tracked, so the ledger can't
  know it), delete it by hand. The suite will not do it for you, by design.

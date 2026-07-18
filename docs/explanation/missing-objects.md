# Missing objects

*Kinds: `table.missing`, `column.missing`, `measure.missing`,
`mapping_row.missing`*

## What it is

The template defines an object — a table, a column on a shared table, a
measure, a row of a mapping table — and a derived model does not have it.
The fleet contract is that the template is a **canonical core**: every
template object must exist in every derived model. A gap means some tenant
is missing functionality the template promises.

## How it arises

- **The template moved on.** A new KPI or dimension landed in the template
  after a derived model was cloned, and nobody back-filled it. This is the
  overwhelmingly common case in any fleet older than a few months.
- **A model joined the fleet late**, cloned from an old copy of the template.
- **Someone deleted the object in the derived model** — by accident, or
  "cleaning up" something they didn't recognize.
- **A partial hand-cascade** — a maintainer pushed a change to nine models
  and got interrupted before the tenth.

## Detection logic

The detector walks the committed baseline (not the live template — see
[Baseline-recapture discipline](baseline-recapture-discipline.md)) and
checks each object's presence in the derived model, matched by name:

- tables by table name;
- columns and measures by name within the same-named table;
- mapping rows by row key (the first column value of the `DATATABLE` tuple).

Presence is all that's checked here; shared objects that exist but diverge
are the drift kinds ([expression](expression-drift.md) /
[property](property-drift.md)).

## Remediation semantics

Missing objects are the safest cascade — purely additive:

- **Measures and columns** are spliced as the template's *raw text block*
  (descriptions, format hints and annotations ride along; parsed-and-
  re-serialized TMDL would silently drop them). The spliced block gets a
  fresh lineage tag: keeping the template's tag can collide with an existing
  tag in the derived model, which the engine rejects at load time.
- **Tables** are copied whole (every lineage tag regenerated) and registered
  in `model.tmdl`.
- **Mapping rows** are inserted after the last existing row.

## When NOT to auto-fix

- **`table.missing` when data sources differ.** The copied partition carries
  the *template's* source query. If the tenant's warehouse layout differs,
  cascade the table, then fix its partition before deploying.
- **A column the tenant's source cannot supply.** The model will validate
  but the next refresh fails. Check the source before cascading
  `column.missing` for source-backed (non-calculated) columns.
- **The object is missing because the tenant deliberately removed it.**
  That is a policy conversation, not a bug. If the decision is "this tenant
  opts out", the honest states are: leave the finding standing as a known
  exception, or narrow the fleet so the model isn't compared. Don't
  remediate-and-revert in a loop.

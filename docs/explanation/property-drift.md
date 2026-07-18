# Property drift

*Kinds: `measure.property_drift`, `column.property_drift`*

## What it is

A shared object's *presentation and typing metadata* differs from the
template while its logic matches: format strings, hidden flags, display
folders, data types, summarization defaults. Individually small; in
aggregate this is what makes a fleet look unmaintained — the same KPI shows
two decimals in one tenant and none in another, a helper measure is visible
in one model's field list and hidden everywhere else, a date column sorts
as text in exactly one place.

## How it arises

- **Desktop editing side effects.** Opening a model in a BI desktop tool and
  touching an object can rewrite properties (summarization defaults are
  notorious).
- **Local cosmetic preferences** — someone "just fixed the formatting" in
  one tenant.
- **Partial template updates**: the template gained a `displayFolder`
  reorganization or a format-string standard, and only some models followed.
- **Type coercion at the source**: a warehouse column changed type and one
  model's `dataType` was hand-adjusted to match.

## Detection logic

For every shared object, a fixed set of single-value properties is compared
as a dict: `dataType`, `formatString`, `displayFolder`, `summarizeBy`,
`sourceColumn`, `sortByColumn`, `isHidden`. Everything else — lineage tags,
annotations, changed-property markers — is identity/tooling metadata and is
deliberately excluded: a model resaved on another machine must not light up
the fleet.

Property drift on measures is only reported when the expression matches; if
both diverge, one `measure.expression_drift` finding is emitted and its
remediation (block overwrite) fixes both.

## Remediation semantics

Same mechanism as expression drift: the template's raw block replaces the
derived block, lineage tag preserved. Property fixes are never assembled by
patching individual lines into an existing block — that is exactly the edit
shape that once injected a property *into* a DAX expression body and broke
every affected measure (see [Safe TMDL surgery](safe-tmdl-surgery.md)). The
block replacement is atomic and passes the same write guards.

## When NOT to auto-fix

- **`dataType` disagreements rooted in the source.** If the tenant's
  warehouse really serves a different type, cascading the template's
  `dataType` breaks refresh. Fix the source or accept the divergence
  explicitly — don't let the cascade paper over a data contract problem.
- **`sourceColumn` differences.** These usually mean the tenant's view has
  a renamed column — a source-mapping question, not a cosmetic one.
- **Deliberate per-tenant visibility.** If one tenant hides a measure for
  contractual reasons, an auto-cascade will keep unhiding it. Treat it like
  any deliberate divergence: either tolerate the standing finding or
  restructure so the difference lives outside the shared surface.

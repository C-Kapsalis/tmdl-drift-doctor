# Expression drift

*Kinds: `measure.expression_drift`, `expression.drift`*

## What it is

A shared object exists in both the template and a derived model, but its
*logic* differs: a measure's DAX body, or the value of a shared parameter in
`expressions.tmdl`. This is the most dangerous drift kind because it is
invisible on the surface — the object is there, the report renders, the
number is simply *different* from what the template's definition would
produce. Two tenants comparing "the same KPI" are comparing different
formulas.

## How it arises

- **A local hotfix that never flowed back.** Someone patched a measure in
  one tenant's model under time pressure and the template never learned
  about it (or the template got the proper fix and the hotfixed tenant kept
  the hack).
- **The template's definition was improved** — a divide-by-zero guard, a
  changed business rule — and only some models got the update.
- **Experimentation residue**: a maintainer tried a variant in a live model
  and forgot to revert.
- For shared parameters: a reporting window or feature toggle was advanced
  in the template and stragglers still carry the old value.

## Detection logic

Expressions are compared **normalized**: fence markers stripped, DAX/M
comments (`//`, `--`, `/* */`) removed, whitespace collapsed. Reformatting a
measure — reindenting, adding a comment, converting to a fenced block — never
registers as drift; only semantic-text changes do.

Two deliberate scope limits:

- **Shared parameters are allowlist-named only.** `expressions.tmdl` mixes
  genuinely shared parameters with per-tenant connection settings; only the
  expressions named in `allowlist.expressions` are compared at all (see
  [Allowlist-only cascading](allowlist-only-cascading.md)).
- **Comparison keys on `Table[Measure]`.** A measure that moved to a
  differently-named host table in a derived model shows up as
  missing + extra, not as expression drift.

## Remediation semantics

The derived block is overwritten with the template's **raw text block** —
formatting, description lines and annotations included — via block-level
text surgery (see [Safe TMDL surgery](safe-tmdl-surgery.md)). The derived
object's existing lineage tag is preserved, so its identity is stable and
re-running the cascade is a no-op.

For `expression.drift`, exactly the named expression block is replaced;
every other line of `expressions.tmdl` stays byte-untouched.

## When NOT to auto-fix

- **The derived version is the correct one.** Half of all expression drift
  is an unback-ported fix. Read the diff (`--dry-run`) before cascading: if
  the derived model is right, fix the *template*, recapture, and cascade in
  the other direction.
- **Genuinely tenant-specific logic.** If one tenant's business rule truly
  differs, an auto-cascade will keep reverting it. Model that properly — a
  tenant-specific measure under a different name, or a parameter the
  template reads — rather than fighting the fleet tooling.
- **Blind bulk-accept.** Overwriting is not destructive in the file sense
  (it's versioned), but it destroys *information* about why the divergence
  existed. The diff is the review artifact; look at it.

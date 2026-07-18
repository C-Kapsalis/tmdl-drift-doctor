# Cascade a column retirement

**Goal:** you removed a column from the template, and every derived model
that still carries it should drop it too.

This walkthrough uses a column, but the flow is identical for measures,
whole tables and mapping rows — they are all retirement kinds.

## 1. Retire the column in the template

Edit the template's table file and delete the column block (declaration line
plus its two-tab properties and annotations). Also remove any template
measures that referenced the column — the template must be self-consistent.

## 2. Recapture IMMEDIATELY

```console
$ drift-doctor capture
baseline written: .drift-doctor/baseline.json
  ...
  [ledger] retired column: Classes[Capacity]
```

This step does two things at once:

1. The baseline stops listing the column — without this, `detect` would flag
   every derived model as `column.missing` and the missing-object cascade
   would resurrect the column from a template that no longer has it.
2. The recapture diff (previous baseline vs fresh snapshot) proves the column
   used to be in the template, and appends a `retired` event to the ledger.
   That proof is what authorizes the removal — absence alone never does (see
   [Allowlist-only cascading](../explanation/allowlist-only-cascading.md)).

## 3. See who still carries it

```console
$ drift-doctor detect --kind retire.
── alpha: 1 finding(s)
  retire.column                Classes[Capacity]
```

## 4. Cascade the removal

Retirements are deletions, so they are double-gated: the kind must be in the
allowlist (`retire.column`) **and** the run must opt in with `--sync`:

```console
$ drift-doctor remediate --kind retire.column --sync --dry-run   # preview
$ drift-doctor remediate --kind retire.column --sync
[applied] alpha: retire.column Classes[Capacity] — remove retired column block
```

A plain `drift-doctor remediate` (no `--sync`) skips every `retire.*` finding
with an explanatory reason — a default pass is purely additive/overwriting.

## 5. Verify and note the edge cases

```console
$ drift-doctor detect --kind retire.
$ drift-doctor ledger | tail
```

- The run is idempotent: a second `--sync` pass finds nothing to remove.
- A derived model that never had the column is untouched (no finding).
- If a derived model's own measures still reference the dropped column,
  removing the column will break them at refresh time. drift-doctor does not
  parse derived-only DAX for you — grep the model for `[Capacity]` before
  syncing if you are unsure.
- If one franchise must keep the column, revive it for that model first —
  see [Recover from an unwanted remediation](recover-from-unwanted-remediation.md).

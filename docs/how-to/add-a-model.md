# Add a model to the fleet

**Goal:** bring a new derived model under drift management.

## 1. Place the model

The model must be a TMDL-format `.SemanticModel` folder with a `definition/`
directory (`tables/*.tmdl`, `model.tmdl`, optionally `expressions.tmdl`).
Location is up to you — paths in `fleet.yml` are resolved relative to the
fleet file.

## 2. Register it

Add one line under `models:` in `fleet.yml`:

```yaml
models:
  alpha: franchises/alpha/GymChain.SemanticModel
  bravo: franchises/bravo/GymChain.SemanticModel
  charlie: franchises/charlie/GymChain.SemanticModel   # new
```

`load_fleet` validates the path on the next run; a missing `definition/`
directory fails loudly rather than silently skipping the model.

## 3. Take stock before you fix anything

Run a scoped, read-only detection first:

```console
$ drift-doctor detect --model charlie
```

A model that has lived away from the template for a while can carry a lot of
drift. Read the findings before remediating:

- **Missing objects** are usually safe to cascade.
- **Expression/property drift** overwrites the derived version — check the
  diffs with `--dry-run` in case the divergence was deliberate. If it was,
  either leave it (it will keep being reported) or narrow the allowlist.
- **Extras** are advisory; nothing will touch them.

## 4. Remediate incrementally

Cascade one kind at a time rather than everything at once:

```console
$ drift-doctor remediate --model charlie --kind measure.missing --dry-run
$ drift-doctor remediate --model charlie --kind measure.missing
$ drift-doctor remediate --model charlie --kind measure.expression_drift --dry-run
...
```

## 5. Retirements need a decision

If objects were retired from the template *before* charlie joined the fleet,
the ledger already lists them, and `detect` will flag any that charlie still
carries as `retire.*`. Decide per object:

- retired debris → `drift-doctor remediate --model charlie --sync`
- deliberately kept → revive it for charlie only:

```console
$ drift-doctor ledger --revive "Visits[Peak Hour Visits]" --kind measure --model charlie
```

## Caveats

- `table.missing` copies the template's partition source verbatim. If the
  new model reads a different source, fix the partition after the cascade.
- Adding a model never requires a recapture — the baseline describes the
  template, not the fleet.

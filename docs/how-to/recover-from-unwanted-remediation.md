# Recover from an unwanted remediation (revival)

**Goal:** a `--sync` pass removed an object that one derived model was
supposed to keep. Put it back — and make sure no future pass removes it
again.

## 1. Find out exactly what happened

The ledger records every applied action with the files it touched:

```console
$ drift-doctor ledger | grep "Peak Hour Visits"
2026-...  retired     measure  Visits[Peak Hour Visits]
2026-...  remediated  retire.measure  model=alpha  Visits[Peak Hour Visits] — remove retired measure block
```

## 2. Restore the object

Re-add the object to the derived model. Version control is the natural
source (`git checkout -- <table file>` or copy the block from history);
otherwise paste the block back by hand.

At this point `detect` will flag it again — the retirement is still active
in the ledger:

```console
$ drift-doctor detect --kind retire.
── alpha: 1 finding(s)
  retire.measure               Visits[Peak Hour Visits]
```

## 3. Revive it

A revival is an append-only ledger event that cancels the retirement without
erasing its history. Scope it to the one model that keeps the object:

```console
$ drift-doctor ledger --revive "Visits[Peak Hour Visits]" --kind measure \
    --model alpha --note "alpha keeps its evening-peak KPI"
revived measure Visits[Peak Hour Visits] for model 'alpha' — it will not be re-removed.
```

Omit `--model` to revive fleet-wide (for example when the retirement itself
was a mistake).

## 4. Verify

```console
$ drift-doctor detect --kind retire.     # no finding for alpha
$ drift-doctor remediate --sync          # will not re-remove it
```

Other models still carrying the object remain flagged — a model-scoped
revival protects exactly one model.

## Notes

- If the object later returns to the template, the next `capture`
  auto-revives it fleet-wide and normal drift comparison resumes.
- Never edit or delete ledger lines to "undo" — the ledger is append-only by
  design (see [The ledger and revival model](../explanation/ledger-and-revival.md)).
  Every correction is a new event.

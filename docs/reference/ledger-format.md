# Ledger format

`<state_dir>/ledger.jsonl` — one JSON object per line, append-only. Reads
fold the stream in order; nothing is ever rewritten or deleted. A malformed
line makes every read fail loudly: a corrupt ledger must never silently
disable or mis-target a removal.

## Common fields

| Field | Type | Meaning |
|---|---|---|
| `ts` | string | UTC ISO-8601 timestamp, appended automatically |
| `event` | string | `retired` \| `revived` \| `remediated` |
| `kind` | string | See per-event tables below |
| `ref` | string | Object reference (formats below) |

Reference formats: tables — `TableName`; columns/measures —
`Table[Object]`; mapping rows — `Table/row_key`.

## `retired`

Appended by `capture` when an object present in the previous baseline is
absent from the fresh snapshot. This is the *proof of ex-template existence*
that authorizes later removal.

```json
{"ts": "2026-07-17T09:30:11+00:00", "event": "retired", "kind": "measure",
 "ref": "Visits[Peak Hour Visits]", "source": "capture"}
```

`kind` is one of `table`, `column`, `measure`, `mapping_row`. Mapping-row
events additionally carry `table` and `row_key`.

## `revived`

Cancels an active retirement without erasing it.

```json
{"ts": "...", "event": "revived", "kind": "measure",
 "ref": "Visits[Peak Hour Visits]", "source": "manual",
 "model": "alpha", "note": "alpha keeps its evening-peak KPI"}
```

| Field | Meaning |
|---|---|
| `source` | `capture` (the object returned to the template) or `manual` (`ledger --revive`) |
| `model` | Present on model-scoped revivals only; absent means fleet-wide |
| `note` | Optional human reason |

## `remediated`

Appended by `remediate` for every applied action (never in `--dry-run`).

```json
{"ts": "...", "event": "remediated", "kind": "retire.measure",
 "model": "alpha", "ref": "Visits[Peak Hour Visits]",
 "action": "remove retired measure block",
 "files": [".../tables/Visits.tmdl"]}
```

Here `kind` is the *finding* kind (e.g. `measure.missing`,
`retire.column`), `action` is the human-readable label of what was done, and
`files` lists every file the action touched.

## The fold (how state is derived)

`active_retirements(model)` walks the stream in order:

1. `retired` puts `(kind, ref)` in force.
2. `revived` removes it — if the event has no `model`, for everyone; if it
   has one, only when it matches the model being asked about.
3. A later `retired` for the same `(kind, ref)` puts it in force again.

`remediated` events never affect the fold — they are pure audit.

# CLI reference

All commands take `--fleet PATH` (default `fleet.yml`).

## `drift-doctor capture`

Snapshot the template into `<state_dir>/baseline.json` and maintain the
retirement record.

```console
$ drift-doctor capture [--fleet fleet.yml]
```

Behavior:

1. Parses the live template and writes the fresh snapshot as the baseline.
2. If a previous baseline exists, diffs it against the fresh snapshot:
   - objects present before and absent now → `retired` ledger events
     (kinds: `table`, `column`, `measure`, `mapping_row`); objects covered by
     a larger drop (e.g. a column of a dropped table) are not double-recorded;
   - actively-retired objects present again → `revived` ledger events.

Exit code: 0 on success.

## `drift-doctor detect`

Read-only drift detection for every derived model.

```console
$ drift-doctor detect [--json] [--model NAME] [--kind PREFIX] [--allow-stale]
```

| Option | Effect |
|---|---|
| `--json` | Emit findings as a JSON array (`kind`, `model`, `ref`, `template_value`, `derived_value`, `detail`, `advisory`). |
| `--model NAME` | Only the named derived model. |
| `--kind PREFIX` | Only findings whose kind starts with the prefix (e.g. `measure.`, `retire.`). |
| `--allow-stale` | Proceed even if the template changed since the last capture. |

Exit codes: **0** no remediable findings (advisories alone don't fail);
**1** remediable drift exists; error if the baseline is missing or stale
(unless `--allow-stale`).

## `drift-doctor remediate`

Cascade template truth into the derived models.

```console
$ drift-doctor remediate [--kind PREFIX] [--dry-run] [--sync] [--model NAME] [--allow-stale]
```

| Option | Effect |
|---|---|
| `--kind PREFIX` | Only remediate findings whose kind starts with the prefix. |
| `--dry-run` | Compute every edit and print unified diffs; write nothing — models, ledger, nothing. |
| `--sync` | Also apply `retire.*` removals. Without it, every retirement finding is skipped with a reason. |
| `--model NAME` | Only the named derived model. |
| `--allow-stale` | Proceed on a stale baseline (misclassification risk). |

Every finding passes three gates before an edit happens: the **allowlist**
(kind must be named in `fleet.yml`), the **sync gate** (`retire.*` needs
`--sync`), and the **write guards** (the new content must be valid TMDL —
tab-indented, balanced fences, no duplicate properties, no property inside a
DAX body). Applied actions are appended to the ledger.

Exit codes: **0** on success (including "nothing to do"); **1** if any apply
failed.

## `drift-doctor ledger`

Inspect the ledger, or record a manual revival.

```console
$ drift-doctor ledger [--json]
$ drift-doctor ledger --revive REF --kind KIND [--model NAME] [--note TEXT]
```

| Option | Effect |
|---|---|
| `--json` | Print raw JSONL entries. |
| `--revive REF` | Cancel the active retirement of `REF` (e.g. `"Visits[Peak Hour Visits]"`, `"Plan Map/legacy-gold"`). Requires `--kind`. |
| `--kind KIND` | Retirement kind: `table`, `column`, `measure`, `mapping_row`. |
| `--model NAME` | Scope the revival to one derived model. Default: fleet-wide. |
| `--note TEXT` | Human reason, stored on the event. |

Reviving a ref with no active retirement is an error (exit 1) — it usually
means a typo, and a silent no-op would hide it.

"""Baseline capture — snapshot the template, maintain the retirement record.

The baseline (``<state_dir>/baseline.json``) is the committed statement of
what the template contains. Detection reads it as template truth, so the
one iron discipline is: **recapture the moment the template changes**. A
stale baseline makes a retired object look "missing from the derived model"
and the missing-object cascade will resurrect it from a template that no
longer carries it — or crash trying.

Capture is also the retirement maintainer: anything present in the PREVIOUS
committed baseline but absent from the fresh snapshot was retired from the
template, and is appended to the ledger (kinds: table, column, measure,
mapping_row). Objects covered by a larger retirement are not double-recorded
(a column or measure whose whole table was dropped is covered by the table
entry; a mapping row whose whole table was dropped likewise). An object that
RETURNED to the template gets a ``revived`` stamp instead of having its
history erased.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import StaleBaselineError
from .fleet import Fleet
from .ledger import Ledger
from .tmdl import Model, normalize_expression, parse_mapping_rows, parse_model

SNAPSHOT_VERSION = 1


def build_snapshot(model: Model, mapping_tables: list) -> dict:
    """Reduce a parsed model to its comparable JSON form."""
    tables: dict = {}
    mapping_rows: dict = {}
    for tname, table in sorted(model.tables.items()):
        tables[tname] = {
            "columns": {c.name: dict(sorted(c.properties.items()))
                        for c in table.columns.values()},
            "measures": {m.name: {
                "expression": normalize_expression(m.expression),
                "properties": dict(sorted(m.properties.items())),
            } for m in table.measures.values()},
        }
        if tname in mapping_tables:
            rows: dict = {}
            for part in table.partitions.values():
                rows.update(parse_mapping_rows(part.source))
            mapping_rows[tname] = rows
    return {
        "version": SNAPSHOT_VERSION,
        "tables": tables,
        "expressions": dict(sorted(model.expressions.items())),
        "mapping_rows": mapping_rows,
    }


def _comparable(snapshot: dict) -> dict:
    """The snapshot minus capture metadata — what equality is judged on."""
    return {k: v for k, v in snapshot.items() if not k.startswith("captured")}


def load_baseline(fleet: Fleet) -> dict:
    path = fleet.baseline_path
    if not path.exists():
        raise StaleBaselineError(
            f"no baseline at {path} — run `drift-doctor capture` first")
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_baseline_fresh(fleet: Fleet, baseline: dict) -> None:
    """Raise StaleBaselineError if the live template no longer matches the
    committed baseline. Acting on a stale baseline misclassifies drift."""
    live = build_snapshot(parse_model(fleet.template_definition),
                          fleet.mapping_tables)
    if _comparable(live) != _comparable(baseline):
        raise StaleBaselineError(
            "the template has changed since the last capture — the committed "
            "baseline is stale, and findings computed against it can be "
            "misclassified (a retired object would look missing and be "
            "resurrected). Run `drift-doctor capture` before detecting or "
            "remediating; --allow-stale proceeds anyway if you accept that "
            "risk.")


# ───────────────────── retirement maintenance ─────────────────────

def kind_inventory(snapshot: dict) -> dict:
    """{kind: set(refs)} for one snapshot. Refs:

    * table        — ``TableName``
    * column       — ``Table[Column]``
    * measure      — ``Table[Measure]``
    * mapping_row  — ``Table/row_key``
    """
    tables = snapshot.get("tables") or {}
    return {
        "table": set(tables),
        "column": {f"{t}[{c}]" for t, body in tables.items()
                   for c in (body.get("columns") or {})},
        "measure": {f"{t}[{m}]" for t, body in tables.items()
                    for m in (body.get("measures") or {})},
        "mapping_row": {f"{t}/{k}"
                        for t, rows in (snapshot.get("mapping_rows") or {}).items()
                        for k in rows},
    }


def _host_table(kind: str, ref: str) -> str:
    if kind in ("column", "measure"):
        return ref[:ref.rindex("[")]
    if kind == "mapping_row":
        return ref[:ref.rindex("/")]
    return ref


def update_retirements(prev: dict, fresh: dict, ledger: Ledger) -> dict:
    """Compare the previous baseline to the fresh snapshot; append ``retired``
    events for template drops and ``revived`` stamps for returns.

    Returns {"retired": [events], "revived": [events]}.
    """
    prev_inv = kind_inventory(prev)
    fresh_inv = kind_inventory(fresh)
    known = ledger.retired_refs()
    dropped_tables = prev_inv["table"] - fresh_inv["table"]

    retired = []
    for kind in ("table", "column", "measure", "mapping_row"):
        for ref in sorted(prev_inv[kind] - fresh_inv[kind]):
            if (kind, ref) in known:
                continue
            # an object whose whole table was dropped is covered by the
            # table's own retirement entry — never double-record it
            if kind != "table" and _host_table(kind, ref) in dropped_tables:
                continue
            extra = {}
            if kind == "mapping_row":
                table, key = ref.rsplit("/", 1)
                extra = {"table": table, "row_key": key}
            retired.append(ledger.record_retirement(kind, ref, source="capture",
                                                    **extra))

    revived = []
    for (kind, ref), event in ledger.active_retirements(model=None).items():
        if kind in fresh_inv and ref in fresh_inv[kind]:
            revived.append(ledger.record_revival(kind, ref, source="capture",
                                                 note="object returned to the template"))
    return {"retired": retired, "revived": revived}


# ───────────────────────── capture ─────────────────────────

def capture(fleet: Fleet) -> dict:
    """Snapshot the template, update the retirement record, write the baseline.

    Returns a summary dict for the CLI."""
    fresh = build_snapshot(parse_model(fleet.template_definition),
                           fleet.mapping_tables)
    ledger = Ledger(fleet.ledger_path)
    changes = {"retired": [], "revived": []}
    path = fleet.baseline_path
    if path.exists():
        prev = json.loads(path.read_text(encoding="utf-8"))
        changes = update_retirements(prev, fresh, ledger)

    fresh["captured_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fresh, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")

    inv = kind_inventory(fresh)
    return {
        "baseline": str(path),
        "tables": len(inv["table"]),
        "columns": len(inv["column"]),
        "measures": len(inv["measure"]),
        "mapping_rows": len(inv["mapping_row"]),
        "expressions": len(fresh.get("expressions") or {}),
        "retired": changes["retired"],
        "revived": changes["revived"],
    }

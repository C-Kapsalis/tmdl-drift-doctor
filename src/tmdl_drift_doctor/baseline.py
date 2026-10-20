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
mapping_row, relationship). Objects covered by a larger retirement are not
double-recorded (a column or measure whose whole table was dropped is covered
by the table entry; a mapping row whose whole table was dropped likewise).
Relationships are the exception: they live in their own file, and the ledger
entry is what later authorizes ``retire.table``/``retire.column`` to sweep
them out of a derived model, so a dropped relationship is always recorded. An object that
RETURNED to the template gets a ``revived`` stamp instead of having its
history erased.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import StaleBaselineError, __version__
from .fleet import Fleet
from .ledger import Ledger
from .mapping_codec import is_encoded
from .tmdl import Model, normalize_expression, parse_mapping_rows, parse_model

SNAPSHOT_VERSION = 3


def build_snapshot(model: Model, mapping_tables: list, config=None,
                   with_metrics: bool = True, encoded: bool = True) -> dict:
    """Reduce a parsed model to its comparable JSON form (plus, for the
    template, the metrics the count floors and reports are built on)."""
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
        if tname in mapping_tables and not is_encoded(
                "\n".join(p.source for p in table.partitions.values())):
            rows: dict = {}
            for part in table.partitions.values():
                rows.update(parse_mapping_rows(part.source))
            mapping_rows[tname] = rows
    snap = {
        "version": SNAPSHOT_VERSION,
        "tables": tables,
        "expressions": dict(sorted(model.expressions.items())),
        "mapping_rows": mapping_rows,
        "relationships": {sig: dict(rel.properties)
                          for sig, rel in sorted(model.relationships.items())},
    }
    if encoded:
        from .mappings import snapshot_encoded
        enc = snapshot_encoded(model, mapping_tables)
        if enc:
            snap["encoded_mapping"] = enc
    if with_metrics:
        from .metrics import compute_metrics
        from .sources import identity, table_source_views
        metrics = compute_metrics(model, config)
        snap["metrics"] = metrics
        snap["fact_tables"] = list(metrics["fact_tables"])
        snap["table_source_views"] = table_source_views(model,
                                                        identity(config))
    return snap


def _comparable(snapshot: dict) -> dict:
    """The snapshot minus capture metadata — what equality is judged on."""
    return {k: v for k, v in snapshot.items()
            if not k.startswith("captured") and k != "provenance"}


def _git(path: Path, *args) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(path), *args],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def provenance(fleet: Fleet, captured_at: str) -> dict:
    """Where the baseline came from. ``template_tree`` (the git tree of the
    template folder at HEAD) survives squash merges and is what freshness
    checks may assert; ``template_commit`` is informational only."""
    return {
        "template_tree": _git(fleet.template, "rev-parse", "HEAD:./"),
        "template_commit": _git(fleet.template, "log", "-1", "--format=%H",
                                "--", "."),
        "captured_at": captured_at,
        "generator": f"tmdl-drift-doctor {__version__}",
    }


def load_baseline(fleet: Fleet) -> dict:
    path = fleet.baseline_path
    if not path.exists():
        raise StaleBaselineError(
            f"no baseline at {path} — run `drift-doctor capture` first")
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_baseline_fresh(fleet: Fleet, baseline: dict) -> None:
    """Raise StaleBaselineError if the live template no longer matches the
    committed baseline. Acting on a stale baseline misclassifies drift."""
    if baseline.get("version") != SNAPSHOT_VERSION:
        raise StaleBaselineError(
            f"the committed baseline uses snapshot format "
            f"{baseline.get('version')!r}, and this drift-doctor writes "
            f"format {SNAPSHOT_VERSION} (it now compares shared expressions "
            f"with their comments and records template metrics). Run "
            f"`drift-doctor "
            f"capture` once to upgrade it; the ledger is kept as is.")
    live = build_snapshot(parse_model(fleet.template_definition),
                          fleet.mapping_tables, fleet.raw)
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
    * relationship — ``From[col] -> To[col]``
    """
    from .mappings import inventory
    tables = snapshot.get("tables") or {}
    enc_rows, enc_cats = inventory(snapshot)
    return {
        "mapping_category": set(enc_cats),
        "table": set(tables),
        "column": {f"{t}[{c}]" for t, body in tables.items()
                   for c in (body.get("columns") or {})},
        "measure": {f"{t}[{m}]" for t, body in tables.items()
                    for m in (body.get("measures") or {})},
        "mapping_row": {f"{t}/{k}"
                        for t, rows in (snapshot.get("mapping_rows") or {}).items()
                        for k in rows} | set(enc_rows),
        "relationship": set(snapshot.get("relationships") or {}),
    }


def _host_table(kind: str, ref: str) -> str:
    if kind in ("column", "measure"):
        return ref[:ref.rindex("[")]
    if kind == "mapping_row":
        return ref[:ref.rindex("/")]
    return ref


def update_retirements(prev: dict, fresh: dict, ledger: Ledger,
                       template_tree: str | None = None) -> dict:
    """Compare the previous baseline to the fresh snapshot; append ``retired``
    events for template drops and ``revived`` stamps for returns.

    Returns {"retired": [events], "revived": [events]}.
    """
    from .mappings import inventory
    prev_inv = kind_inventory(prev)
    fresh_inv = kind_inventory(fresh)
    known = ledger.retired_refs()
    dropped_tables = prev_inv["table"] - fresh_inv["table"]
    enc_rows, enc_cats = inventory(prev)
    dropped_cats = prev_inv["mapping_category"] - fresh_inv["mapping_category"]

    retired = []
    for kind in ("table", "column", "measure", "mapping_category",
                 "mapping_row", "relationship"):
        for ref in sorted(prev_inv[kind] - fresh_inv[kind]):
            if (kind, ref) in known:
                continue
            if kind == "mapping_category":
                table, cat, keys = enc_cats[ref]
                if table in dropped_tables:
                    continue
                retired.append(ledger.record_retirement(
                    kind, ref, source="capture", table=table, category=cat,
                    keys=sorted(set(keys))))
                continue
            if kind == "mapping_row" and ref in enc_rows:
                table, cat, key = enc_rows[ref]
                if table in dropped_tables or f"{table}/{cat}" in dropped_cats:
                    continue
                retired.append(ledger.record_retirement(
                    kind, ref, source="capture", table=table, category=cat,
                    row_key=key))
                continue
            # an object whose whole table was dropped is covered by the
            # table's own retirement entry — never double-record it
            if (kind not in ("table", "relationship")
                    and _host_table(kind, ref) in dropped_tables):
                continue
            extra = {}
            if kind == "table":
                body = (prev.get("tables") or {}).get(ref) or {}
                extra = {"objects": sorted(
                    [f"{ref}[{c}]" for c in (body.get("columns") or {})]
                    + [f"{ref}[{m}]" for m in (body.get("measures") or {})])}
            if kind == "mapping_row":
                table, key = ref.rsplit("/", 1)
                extra = {"table": table, "row_key": key}
            retired.append(ledger.record_retirement(kind, ref, source="capture",
                                                    **extra))

    revived = []
    for (kind, ref), event in ledger.active_retirements(model=None).items():
        if kind in fresh_inv and ref in fresh_inv[kind]:
            extra = {"template_tree": template_tree} if template_tree else {}
            revived.append(ledger.record_revival(
                kind, ref, source="capture",
                note="object returned to the template", **extra))
    return {"retired": retired, "revived": revived}


# ───────────────────────── capture ─────────────────────────

def capture(fleet: Fleet) -> dict:
    """Snapshot the template, update the retirement record, write the baseline.

    Returns a summary dict for the CLI."""
    fresh = build_snapshot(parse_model(fleet.template_definition),
                           fleet.mapping_tables, fleet.raw)
    ledger = Ledger(fleet.ledger_path)
    changes = {"retired": [], "revived": []}
    path = fleet.baseline_path
    if path.exists():
        prev = json.loads(path.read_text(encoding="utf-8"))

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prov = provenance(fleet, stamp)
    if path.exists():
        changes = update_retirements(prev, fresh, ledger,
                                     prov.get("template_tree"))
    fresh["captured_at"] = stamp
    fresh["provenance"] = prov
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
        "relationships": len(inv["relationship"]),
        "retired": changes["retired"],
        "revived": changes["revived"],
    }

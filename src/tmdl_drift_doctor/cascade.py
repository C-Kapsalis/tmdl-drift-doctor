"""Fleet cascade summary: is each model a clean superset of the template?

One row per model with its shape (tables, measures by class, fact tables)
and its drift by bucket. A model is "in cascade" when it has no hard drift:
no missing template tables or measures, no measure expression drift, no
missing or modified relationships. Flagged extras (derived-only objects on
shared tables) are shown but do not take a model out of cascade.
"""

from __future__ import annotations

from .roles import Roles
from .tmdl import parse_model

CSV_HEADER = ["model", "tables", "measures", "fact_tables", "filter_measures",
              "user_facing", "helpers", "gaps", "flagged", "allowed",
              "expr_drift", "col_drift", "rel_drift"]

_GAP = ("table.missing", "measure.missing", "measure.wrong_host")
_FLAGGED = ("extra.measure", "extra.column", "relationship.extra")


def summarize(fleet, baseline: dict, findings: list, names: list,
              errors: list | None = None) -> list:
    """[{column: value}] in CSV_HEADER order, one per model.

    A model whose detection failed is still a row: it is marked as failed,
    so a reader of the report cannot mistake "not checked" for "in
    cascade".
    """
    roles = Roles(fleet.raw)
    b_tables = set(baseline.get("tables") or {})
    failed = {e.get("model") for e in (errors or [])}
    rows = []
    for name in names:
        if name in failed:
            rows.append(_failed_row(name))
            continue
        try:
            model = parse_model(fleet.model_definition(name))
        except Exception:          # parse errors are reported by detect
            rows.append(_failed_row(name))
            continue
        mine = [f for f in findings if f.model == name and not f.overridden]

        def count(*kinds):
            return sum(1 for f in mine if f.kind in kinds)

        classes = roles.measure_classes(model)
        rows.append({
            "model": name,
            "tables": len(model.tables),
            "measures": len(classes["all"]),
            "fact_tables": len(roles.fact_tables(model)),
            "filter_measures": len(classes["filter"]),
            "user_facing": len(classes["user_facing"]),
            "helpers": len(classes["helper"]),
            "gaps": count(*_GAP),
            "flagged": count(*_FLAGGED),
            "allowed": len(set(model.tables) - b_tables),
            "expr_drift": count("measure.expression_drift"),
            "col_drift": count("column.missing", "column.property_drift"),
            "rel_drift": count("relationship.missing", "relationship.modified"),
            "missing": sorted(f.ref.rsplit("[", 1)[-1].rstrip("]")
                              for f in mine if f.kind in _GAP),
            "failed": False,
        })
    return rows


def _failed_row(name: str) -> dict:
    """The row of a model the run could not check."""
    row = {k: 0 for k in CSV_HEADER if k != "model"}
    row.update({"model": name, "missing": [], "failed": True})
    return row


def in_cascade(row: dict) -> bool:
    if row.get("failed"):
        return False
    return not (row["gaps"] or row["expr_drift"] or row["rel_drift"])

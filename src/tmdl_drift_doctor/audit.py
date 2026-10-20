"""Ledger audit: did every applied fix stay applied?

Every ``remediated`` ledger event is a claim about a derived model. A later
hand edit, a merge whose stale side won a conflict, or a Desktop re-save can
silently undo it. ``drift-doctor audit`` re-verifies the claims that are
checkable from the files alone:

* a missing-object fix (``table.missing``, ``column.missing``,
  ``measure.missing``, ``mapping_row.missing``, ``relationship.missing``,
  ``measure.wrong_host``): the object is still present (on the template's
  host, for a move);
* a retirement (``retire.*``): the removed object has not come back.

A claim the template or the ledger has since superseded (the object was
retired after it was added, revived after it was removed, or an override now
declares the divergence) is reported as ``superseded``, not as a revert.
Only the latest event per (model, kind, object) is checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import DriftDoctorError
from .detect import split_ref
from .fleet import Fleet
from .ledger import Ledger
from .tmdl import Model, parse_mapping_rows, parse_model

ADDED = {
    "table.missing": "table",
    "column.missing": "column",
    "measure.missing": "measure",
    "measure.wrong_host": "measure",
    "mapping_row.missing": "mapping_row",
    "relationship.missing": "relationship",
}
REVERT_EXIT_CODE = 4


@dataclass
class AuditItem:
    event: dict
    status: str     # ok | reverted | superseded | error
    detail: str = ""


def _present(model: Model, family: str, ref: str) -> bool:
    if family == "table":
        return ref in model.tables
    if family in ("column", "measure"):
        t, n = split_ref(ref)
        return (model.column(t, n) if family == "column"
                else model.measure(t, n)) is not None
    if family == "mapping_row":
        t, key = ref.rsplit("/", 1)
        table = model.tables.get(t)
        if table is None:
            return False
        rows = {}
        for part in table.partitions.values():
            rows.update(parse_mapping_rows(part.source))
        return key in rows
    if family == "relationship":
        return ref in model.relationships
    return False


def _template_has(baseline: dict, family: str, ref: str) -> bool:
    from .baseline import kind_inventory
    return ref in kind_inventory(baseline).get(family, set())


def audit(fleet: Fleet, ledger: Ledger, baseline: dict,
          model_filter: Optional[str] = None) -> list:
    latest: dict = {}
    for e in ledger.remediations():
        key = (e.get("model"), e.get("kind"), e.get("ref"))
        latest[key] = e
    models: dict = {}
    out = []
    for (model, kind, ref), e in latest.items():
        if model not in fleet.models or (model_filter and model != model_filter):
            continue
        if kind not in ADDED and not str(kind).startswith("retire."):
            continue  # overwrites: drift on them is detect's job, not audit's
        if model not in models:
            try:
                models[model] = parse_model(fleet.model_definition(model))
            except DriftDoctorError as err:
                models[model] = err
        m = models[model]
        if isinstance(m, DriftDoctorError):
            out.append(AuditItem(e, "error", str(m)))
            continue
        declared = {(o.kind, o.object) for o in fleet.overrides.get(model, ())}
        if kind in ADDED:
            family = ADDED[kind]
            if not _template_has(baseline, family, ref) or \
                    ("absent", ref) in declared:
                out.append(AuditItem(e, "superseded",
                                     "no longer a template object for this "
                                     "model (retired or declared absent)"))
            elif _present(m, family, ref):
                out.append(AuditItem(e, "ok"))
            else:
                out.append(AuditItem(e, "reverted",
                                     f"{family} {ref} was added by this fix "
                                     f"and is missing again"))
        else:
            family = kind.split(".", 1)[1]
            if not ledger.is_retired(family, ref, model) or \
                    ("retain", ref) in declared:
                out.append(AuditItem(e, "superseded",
                                     "revived or retained since the removal"))
            elif _present(m, family, ref):
                out.append(AuditItem(e, "reverted",
                                     f"retired {family} {ref} was removed by "
                                     f"this fix and is back"))
            else:
                out.append(AuditItem(e, "ok"))
    return out

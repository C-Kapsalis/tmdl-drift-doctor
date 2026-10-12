"""Per-model overrides: declared, reviewed intentional divergence.

An override turns "this model deliberately differs from the template" into
data instead of tribal knowledge. Overrides live under each model entry in
``fleet.yml`` (schema-checked by ``fleet.load_fleet``) and are resolved here
against the committed baseline and the ledger, failing loudly on any entry
that matches nothing: a typo must never silently disable a gate.

An overridden finding is never remediated and never fails ``detect`` or the
ratchet, but it is never silent either: every run prints it as an ACK with
its reason, owner and date, and ``review`` lists it for periodic re-review.
"""

from __future__ import annotations

from typing import Optional

from . import DriftDoctorError
from .fleet import Fleet, Override
from .ledger import Ledger
from .tmdl import parse_signature

# Which finding kinds each override kind acknowledges.
SUPPRESSES = {
    "expression": ("measure.expression_drift", "measure.property_drift",
                   "column.property_drift", "expression.drift",
                   "relationship.modified"),
    "absent": ("table.missing", "column.missing", "measure.missing",
               "mapping_row.missing", "relationship.missing",
               "expression.drift"),
    "retain": ("retire.table", "retire.column", "retire.measure",
               "retire.mapping_row", "retire.relationship"),
}


def _template_refs(baseline: dict) -> dict:
    """{kind_family: set(refs)} of everything the template defines."""
    tables = baseline.get("tables") or {}
    return {
        "table": set(tables),
        "column": {f"{t}[{c}]" for t, b in tables.items()
                   for c in (b.get("columns") or {})},
        "measure": {f"{t}[{m}]" for t, b in tables.items()
                    for m in (b.get("measures") or {})},
        "mapping_row": {f"{t}/{k}" for t, rows in
                        (baseline.get("mapping_rows") or {}).items()
                        for k in rows},
        "relationship": set(baseline.get("relationships") or {}),
        "expression": set(baseline.get("expressions") or {}),
    }


def resolve_overrides(fleet: Fleet, baseline: dict, ledger: Ledger) -> None:
    """Raise DriftDoctorError listing every override whose ``object`` does
    not resolve: ``expression``/``absent`` against the template baseline,
    ``retain`` against the ledger's retirement history."""
    refs = _template_refs(baseline)
    template_any = set().union(*refs.values())
    shared = (refs["measure"] | refs["column"] | refs["relationship"]
              | refs["expression"])
    ever_retired = {e.get("ref") for e in ledger.entries()
                    if e.get("event") == "retired"}
    errors = []
    for model, entries in fleet.overrides.items():
        for o in entries:
            if "->" in o.object:
                try:
                    parse_signature(o.object)
                except DriftDoctorError as e:
                    errors.append(f"models.{model}: {e}")
                    continue
            if o.kind == "expression" and o.object not in shared:
                errors.append(
                    f"models.{model}: override `{o.object}` (kind expression) "
                    f"names no template measure, column, relationship or "
                    f"shared expression")
            elif o.kind == "absent" and o.object not in template_any:
                errors.append(
                    f"models.{model}: override `{o.object}` (kind absent) "
                    f"names nothing the template defines")
            elif o.kind == "retain" and o.object not in ever_retired:
                errors.append(
                    f"models.{model}: override `{o.object}` (kind retain) "
                    f"names nothing the ledger records as retired")
    if errors:
        raise DriftDoctorError(
            "fleet.yml overrides do not resolve (a typo must never silently "
            "disable a gate):\n  " + "\n  ".join(errors))


def match_override(fleet: Fleet, finding) -> Optional[Override]:
    """The override that acknowledges `finding`, or None."""
    for o in fleet.overrides.get(finding.model) or ():
        if finding.kind not in SUPPRESSES[o.kind]:
            continue
        if o.object == finding.ref:
            return o
        # a table declared absent also covers the relationships into it
        if (o.kind == "absent" and finding.kind == "relationship.missing"
                and "[" not in o.object and "->" not in o.object):
            ft, _fc, tt, _tc = parse_signature(finding.ref)
            if o.object in (ft, tt):
                return o
    return None


def format_ack(finding, o: Override) -> str:
    who = o.owner or "?"
    when = o.added or "?"
    return (f"  ACK {finding.kind:<26} {finding.ref}  [owner {who}, "
            f"added {when}]\n      reason: {o.reason}")

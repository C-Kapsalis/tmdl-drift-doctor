"""Per-model overrides: declared, reviewed intentional divergence.

An override turns "this model deliberately differs from the template" into
data instead of tribal knowledge. Overrides live under each model entry in
``fleet.yml`` (schema-checked by ``fleet.load_fleet``) and are resolved here
against the committed baseline and the ledger, failing loudly on any entry
that matches nothing: a typo must never silently disable a gate.

An overridden finding is never remediated and never fails ``detect`` or the
ratchet, but it is never silent either: every run prints it as an ACK with
its reason, owner and date, and ``review`` lists it for periodic re-review.
Declarations made on a fleet entry cover the entry's secondary models too.
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
                   "relationship.modified", "measure.wrong_host"),
    "absent": ("table.missing", "column.missing", "measure.missing",
               "mapping_row.missing", "relationship.missing",
               "expression.missing", "expression.drift"),
    "retain": ("retire.table", "retire.column", "retire.measure",
               "retire.mapping_row", "retire.relationship",
               "purge.table", "purge.relationship"),
    "extension": ("extra.column", "extra.measure", "purge.column",
                  "purge.measure"),
}


def _bare(ref: str) -> str:
    return ref[ref.rindex("[") + 1:-1] if ref.endswith("]") and "[" in ref \
        else ref


def _template_refs(baseline: dict) -> dict:
    """{family: set(refs)} of everything the template defines."""
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


def _problem(o: Override, refs: dict, retired: set) -> Optional[str]:
    obj = o.object
    if "->" in obj:
        try:
            parse_signature(obj)
        except DriftDoctorError as e:
            return str(e)
    bare_measures = {_bare(r) for r in refs["measure"]}
    if o.section == "columns":
        if o.kind == "absent" and obj not in refs["column"]:
            return (f"override `{obj}` (columns, kind absent) does not exist "
                    f"in the template (check the Table[column] spelling)")
        return None
    if o.section == "measures":
        if o.kind == "expression" and obj not in refs["measure"]:
            return (f"override `{obj}` (measures, kind expression) does not "
                    f"resolve in the template")
        if o.kind == "absent" and obj not in refs["measure"] \
                and obj not in bare_measures:
            return (f"override `{obj}` (measures, kind absent) does not "
                    f"resolve in the template")
        return None
    if o.section == "expressions":
        if obj not in refs["expression"]:
            return (f"override `{obj}` (expressions) does not resolve in the "
                    f"template")
        return None
    if o.section in ("tables", "relationships"):
        return None          # retention of derived or retired objects
    shared = (refs["measure"] | refs["column"] | refs["relationship"]
              | refs["expression"])
    if o.kind == "expression" and obj not in shared:
        return (f"override `{obj}` (kind expression) does not resolve in the "
                f"template: it names no template measure, column, "
                f"relationship or shared expression")
    if o.kind == "absent" and obj not in set().union(*refs.values()) \
            and obj not in bare_measures:
        return (f"override `{obj}` (kind absent) does not resolve in the "
                f"template: it names nothing the template defines")
    if o.kind == "retain" and obj not in retired:
        return (f"override `{obj}` (kind retain) names nothing the ledger "
                f"records as retired")
    return None


def resolve_overrides(fleet: Fleet, baseline: dict, ledger: Ledger) -> None:
    """Raise DriftDoctorError listing every override whose ``object`` does
    not resolve: ``expression``/``absent`` against the template baseline,
    ``retain`` against the ledger's retirement history."""
    refs = _template_refs(baseline)
    retired = {e.get("ref") for e in ledger.entries()
               if e.get("event") == "retired"}
    errors = []
    for model, entries in fleet.overrides.items():
        owner = fleet.entry_of.get(model, model)
        for o in entries:
            msg = _problem(o, refs, retired)
            if msg:
                line = f"models.{owner}: {msg}"
                if line not in errors:
                    errors.append(line)
    if errors:
        raise DriftDoctorError(
            "fleet.yml overrides do not resolve (a typo must never silently "
            "disable a gate):\n  " + "\n  ".join(errors))


def match_override(fleet: Fleet, finding) -> Optional[Override]:
    """The override that acknowledges `finding`, or None."""
    for o in fleet.overrides.get(finding.model) or ():
        if finding.kind not in SUPPRESSES.get(o.kind, ()):
            continue
        if o.object == finding.ref:
            return o
        # a bare measure name declared absent covers it on any table
        if (o.kind == "absent" and "[" not in o.object
                and finding.kind == "measure.missing"
                and _bare(finding.ref) == o.object):
            return o
        # a table declared absent also covers the relationships into it
        if (o.kind == "absent" and finding.kind == "relationship.missing"
                and "[" not in o.object and "->" not in o.object):
            ft, _fc, tt, _tc = parse_signature(finding.ref)
            if o.object in (ft, tt):
                return o
    return None


def resolve_for_model(fleet: Fleet, model: str) -> dict:
    """Every declaration in force for one model, by section. A model with
    no declarations gets empty sets and maps, never a KeyError."""
    out = {"expression": {}, "absent": {}, "retain": {}, "extension": {},
           "columns_absent": {}, "columns": {}, "source_views": {},
           "tables": set(), "relationships": set(),
           "fieldparam_entries": {}, "mapping_rows": {}, "files": []}
    for o in fleet.overrides.get(model) or ():
        if o.section == "columns":
            table, col = o.object[:o.object.rindex("[")], _bare(o.object)
            if o.kind == "absent":
                out["columns_absent"].setdefault(table, {})[col] = o.meta()
            else:
                out["columns"].setdefault(table, set()).add(col)
            continue
        if o.section == "tables":
            out["tables"].add(o.object)
            continue
        if o.section == "relationships":
            out["relationships"].add(o.object)
            continue
        out.setdefault(o.kind, {})[o.object] = o.meta()
    declared = fleet.sections.get(model) or {}
    views = out["source_views"]
    views.update((d["table"], d["view"])
                 for d in declared.get("source_views", ()))
    for name, (group, item) in _GROUPED.items():
        bucket = out[name]
        for d in declared.get(name, ()):
            bucket.setdefault(d[group], set()).add(d[item])
    out["files"] = [d.get("path") for d in declared.get("files", ())]
    return out


# grouped retention sections: section -> (group key, member key)
_GROUPED = {"fieldparam_entries": ("category", "entry"),
            "mapping_rows": ("category", "key")}


def override_measure_names(resolved: dict) -> set:
    """Bare measure names any override of `resolved` mentions (the set the
    rename and host-move logic must never "correct")."""
    names = set()
    for kind in ("expression", "absent"):
        for ref in resolved.get(kind) or {}:
            names.add(_bare(ref))
    return names


def format_ack(finding, o: Override) -> str:
    who = o.owner or "?"
    when = o.added or "?"
    return (f"  ACK {finding.kind:<26} {finding.ref}  [{o.kind}; owner {who}, "
            f"added {when}]\n      reason: {o.reason}")

"""Push named template measures to every model (``remediate --ref``).

After fixing one measure in the template, cascade exactly that measure:
overwrite it where each model hosts it (measure names are model-global, so
another host table still holds "the same" measure), keep the model's
lineage tag, never touch a model that declares the measure divergent, and
add it to models that lack it only with ``--add-missing``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import DriftDoctorError, TmdlEditError
from . import tmdl_edit as te
from .tmdl import parse_model


@dataclass
class NamedOutcome:
    model: str
    measure: str
    status: str            # applied | would-apply | skipped | failed
    reason: str = ""
    path: Optional[Path] = None
    old: Optional[str] = None
    new: Optional[str] = None


@dataclass
class NamedResult:
    outcomes: list = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)


def _split(ref: str) -> tuple:
    if ref.endswith("]") and "[" in ref:
        i = ref.rindex("[")
        return ref[:i], ref[i + 1:-1]
    return None, ref


def resolve_refs(fleet, refs) -> list:
    """[(template host, name)] for each ref, in order; raises when one does
    not exist in the template (nothing is touched then)."""
    template = parse_model(fleet.template_definition)
    hosts = template.measure_hosts()
    out = []
    for ref in refs:
        table, name = _split(ref)
        found = hosts.get(name) or []
        if table is not None:
            found = [t for t in found if t == table]
        if not found:
            raise DriftDoctorError(
                f"measure '{ref}' does not exist in the template; nothing was "
                f"changed")
        out.append((found[0], name))
    return out


def _declared(fleet, model: str, host: str, name: str) -> Optional[str]:
    for o in fleet.overrides.get(model) or []:
        if o.kind in ("expression", "absent") and o.object in (
                name, f"{host}[{name}]"):
            return o.reason
    return None


def cascade_named(fleet, refs, *, add_missing: bool = False,
                  model_filter: Optional[str] = None,
                  dry_run: bool = False) -> NamedResult:
    result = NamedResult()
    targets = resolve_refs(fleet, refs)
    tdef = fleet.template_definition
    for host, name in targets:
        t_text = (tdef / "tables" / f"{host}.tmdl").read_text(encoding="utf-8")
        block = te.extract_block(t_text, "measure", name)
        for model in fleet.models:
            if model_filter and model != model_filter:
                continue
            out = NamedOutcome(model, name, "skipped")
            result.outcomes.append(out)
            why = _declared(fleet, model, host, name)
            if why is not None:
                out.reason = f"declared divergence (override): {why}"
                continue
            mdef = fleet.model_definition(model)
            hosts = sorted(parse_model(mdef).measure_hosts().get(name) or [])
            try:
                if hosts:
                    table = hosts[0]
                    path = mdef / "tables" / f"{table}.tmdl"
                    old = path.read_text(encoding="utf-8")
                    existing = te.extract_block(old, "measure", name)
                    tag = te.first_lineage_tag(existing or "")
                    intended = te.set_first_lineage_tag(block, tag) if tag \
                        else te.regenerate_lineage_tag(block)
                    new = te.replace_block(old, "measure", name, intended)
                    note = ""
                    if table != host:
                        note = f" (hosted on '{table}' in this model)"
                    if len(hosts) > 1:
                        note += (f"; WARNING: also on {', '.join(hosts[1:])}, "
                                 f"edited the alphabetically first")
                    verb = "overwrite"
                else:
                    if not add_missing:
                        out.reason = ("absent from this model; pass "
                                      "--add-missing to insert it")
                        continue
                    path = mdef / "tables" / f"{host}.tmdl"
                    if not path.is_file():
                        out.reason = (f"absent, and the host table '{host}' "
                                      f"is missing too")
                        continue
                    old = path.read_text(encoding="utf-8")
                    intended = te.regenerate_lineage_tag(block)
                    new = te.insert_measure_block(old, intended)
                    note, verb = "", "insert"
                    table = host
                if new == old:
                    out.reason = "unchanged"
                    continue
                te.assert_round_trip(new, "measure", name, intended)
                te.assert_tmdl_valid(new)
            except (TmdlEditError, DriftDoctorError, OSError) as e:
                out.status, out.reason = "failed", str(e)
                continue
            out.path, out.old, out.new = path, old, new
            out.reason = f"{verb} '{name}' from the template{note}"
            if dry_run:
                out.status = "would-apply"
                continue
            path.write_text(new, encoding="utf-8")
            out.status = "applied"
    return result

"""The category gates: every fact a user-facing measure reads needs one, and
every analysis category needs a gate in the all-filters measure.

A report built on the selector pattern keeps one hidden ``f-<Category>``
measure per analysis category; it returns a positive number when the
category's fact tables hold rows. The combined ``f-All`` measure holds one
gate line per category, so the canvas only offers categories that have data.
Two things drift as a model grows: a fact is read by a user-facing measure
but by no ``f-`` measure, and a category registered in the mapping table has
no gate line in ``f-All``.

* ``ffilter.coverage_gap``: such a fact, and such an ungated category. The
  repair adds the missing measures and gate lines;
* ``ffilter.uncategorized`` (report-only): a fact the generator cannot
  categorise, so no gate can be written for it.

The generator lives in pbip-model-forge: this module calls it, it does not
re-implement it. Without the forge installed, neither the check nor the
repair runs.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
from pathlib import Path

from . import DriftDoctorError
from . import tmdl_edit as te

GATE_TABLE = "f-Filters"
COMBINED = "f-All"


def _forge():
    """The forge's f-filter module, or None when it is not installed."""
    try:
        from pbip_model_forge import f_filters
    except ImportError:
        return None
    return f_filters


def _quiet(fn, *args, **kw):
    """Run a forge generator without letting its warnings reach the
    console."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        return fn(*args, **kw)


def preview(fleet, name: str):
    """The forge's own dry run of the gap fill: what it would create,
    extend, gate and what it cannot categorise. None when the forge is not
    installed."""
    forge = _forge()
    if forge is None:
        return None
    model_root = Path(fleet.models[name])
    return _quiet(forge.fill_gaps, model_root, dry_run=True, backups=False,
                  verbose=False)


def findings(name: str, fleet, model, retired=()) -> list:
    """``ffilter.coverage_gap`` and ``ffilter.uncategorized`` for one model.

    `retired` holds the measure names the ledger retired: a gate for one of
    them is never written, so a retired measure does not come back."""
    from .detect import Finding
    forge = _forge()
    res = preview(fleet, name)
    if forge is None or res is None or not res.ok:
        return []
    retired = set(retired or ())
    model_root = Path(fleet.models[name])
    created = {n: facts for n, facts in (res.created or {}).items()
               if n not in retired}
    extended = {n: facts for n, facts in (res.extended or {}).items()
                if n not in retired}
    declared = (set(model.measure_hosts()) | set(res.created or {})
                | set(res.extended or {}))
    retired_categories = {c for c in forge.registered_categories(model_root)
                          if forge.find_gate_measure(c, declared) in retired}
    gates = [c for c in (res.combined_gates_added or [])
             if c not in retired_categories]
    gates += [c for c, _m in (res.coverage_gates or [])
              if c not in retired_categories]
    unresolved = [c for c in (res.unresolved or [])
                  if c not in retired_categories]
    skipped = list(res.skipped or [])
    out = []
    if created or extended or gates:
        lines = [f"      + create {n}" for n in sorted(created)[:30]]
        lines += [f"      ~ extend {n} (+{', '.join(facts)})"
                  for n, facts in sorted(extended.items())[:30]]
        lines += [f"      + gate {COMBINED}: {g}" for g in gates[:30]]
        lines += [f"      ! ungated, no f- measure resolves to it: {u}"
                  for u in unresolved[:30]]
        detail = (f"f-measure coverage gap in {name} ({len(created)} to "
                  f"create, {len(extended)} to extend, {len(gates)} "
                  f"{COMBINED} gate(s) to add, {len(unresolved)} category(ies) "
                  f"ungated with no resolvable f- measure) — every fact "
                  f"consumed by user-facing measures needs an f-<Category> "
                  f"gate, AND every mapping category must be GATED in "
                  f"{COMBINED}:\n" + "\n".join(lines) + "\n"
                  f"  AUTO-FIX runs the f-filter generator (additive: "
                  f"creates/extends f-<Category> measures + {COMBINED}; never "
                  f"overwrites existing)."
                  + ("\n  NOTE: 'extend' APPENDS `+ int(not ISEMPTY(...))` to "
                     "an existing f-measure — safe for a STANDARD additive "
                     "f-measure, but verify if the target is a "
                     "SWITCH/aggregator/display-string measure."
                     if extended else ""))
        out.append(Finding(
            kind="ffilter.coverage_gap", model=name, ref=GATE_TABLE,
            detail=detail,
            data={"table": GATE_TABLE, "created": sorted(created),
                  "extended": sorted(extended), "gates": gates,
                  "unresolved": unresolved, "retired": sorted(retired)}))
    if skipped:
        listing = "\n".join(f"      - {ft}: {forge.SKIP_REASON}"
                            for ft in skipped[:30])
        detail = (f"{len(skipped)} fact table(s) can't be auto-covered by the "
                  f"f-filter generator (no resolvable analysis category):\n"
                  f"{listing}\n"
                  f"  NO AUTO-FIX: assign the fact an analysis category (or "
                  f"add its f-measure by hand), then re-run.")
        out.append(Finding(
            kind="ffilter.uncategorized", model=name,
            ref=f"{GATE_TABLE}:uncategorized", detail=detail,
            data={"table": GATE_TABLE, "facts": skipped}))
    return out


def _drop_gate_lines(text: str, measure: str) -> str:
    """The f-Filters text without the gate lines that test `measure`."""
    span = te.block_span(text, "measure", COMBINED)
    if span is None:
        return text
    start, end = span
    lines = text.split("\n")
    kept = [ln for i, ln in enumerate(lines)
            if not (start <= i < end and f"[{measure}]" in ln)]
    return "\n".join(kept)


def apply_coverage(fleet, f) -> tuple:
    """Add the missing category gates through the forge's f-filter
    generator.

    The generator runs on a copy of the model, so the tool's own dry run and
    diff see the edit before anything is written. The file's own bytes are
    kept: its line endings and its byte-order mark survive."""
    from .remediate import FileChange
    forge = _forge()
    if forge is None:
        raise DriftDoctorError(
            "pbip-model-forge is not installed, so the category gates cannot "
            "be written: install it next to drift-doctor and re-run")
    model_root = Path(fleet.models[f.model])
    gate_file = model_root / "definition" / "tables" / f"{GATE_TABLE}.tmdl"
    if not gate_file.is_file():
        raise DriftDoctorError(
            f"the table file {gate_file} is gone; re-run `drift-doctor "
            f"detect`")
    retired = set(f.data.get("retired") or ())
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "model"
        shutil.copytree(model_root, work)
        res = _quiet(forge.fill_gaps, work, dry_run=False, backups=False,
                     verbose=False)
        if not res.ok:
            raise DriftDoctorError(
                f"the f-filter generator failed: {res.error or 'no message'}")
        blocked = []
        for name in sorted(res.created or {}):
            if name not in retired:
                continue
            path = work / "definition" / "tables" / f"{GATE_TABLE}.tmdl"
            text = te.read_text(path)
            without = te.remove_block(text, "measure", name)
            if without is None:
                continue
            text = _drop_gate_lines(without, name)
            te.write_text(path, text, te.line_ending(te.read_text(path)))
            blocked.append(name)
        new = te.read_text(work / "definition" / "tables"
                           / f"{GATE_TABLE}.tmdl")
    raw = gate_file.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    old = te.read_text(gate_file)
    eol = te.line_ending(old)
    if new == old:
        return ("every fact already has its category gate — no change", [])
    te.assert_tmdl_valid(new)
    created = len(res.created or {})
    extended = len(res.extended or {})
    label = (f"the f-filter generator created {created} measure(s) and "
             f"extended {extended}")
    if blocked:
        label += (f" (blocked {len(blocked)} ledger-retired re-creation(s): "
                  + ", ".join(blocked) + ")")
    return (label, [FileChange(gate_file, raw, te.encode(new, eol, bom))])

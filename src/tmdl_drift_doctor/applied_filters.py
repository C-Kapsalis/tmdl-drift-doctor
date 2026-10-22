"""The applied-filters measure: what the model tests, and what the measure
exposes.

``i-Applied Filters`` is a text measure that lists every dimension filter
currently applied, one line per filtered column. It is generated from the
visible y-Selector dimensions, so it goes stale as soon as a dimension is
offered that the measure does not mention — and it keeps a stale branch when
a dimension is taken away, which the coverage check never re-fires on.

* ``ifilter.coverage_gap``: a visible y-Selector dimension the measure does
  not expose. The repair regenerates the measure from the full visible
  y-Selector dimension set;
* ``retire.ifilter_regen`` / ``purge.ifilter_regen``: the measure references
  an object this run removes. The repair regenerates it from the
  post-retirement (post-purge) dimension set.

The generator lives in pbip-model-forge: this module calls it, it does not
re-implement it. Without the forge installed, neither the check nor the
repair runs.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import DriftDoctorError
from . import tmdl_edit as te
from .tmdl import strip_comments

MEASURE = "i-Applied Filters"

_ISFILTERED = re.compile(
    r"ISFILTERED\s*\(\s*(?:'((?:[^']|'')+)'|([A-Za-z_][A-Za-z0-9_]*))"
    r"\s*\[([^\]]+)\]\s*\)", re.IGNORECASE)


def _forge():
    """The forge's applied-filters module, or None when it is not
    installed."""
    try:
        from pbip_model_forge import i_filters
    except ImportError:
        return None
    return i_filters


def forge_installed() -> bool:
    """Is the generator that builds the applied-filters measure available?"""
    return _forge() is not None


def isfiltered_refs(expression: str) -> set:
    """The (table, column) pairs the expression tests with ISFILTERED.

    Comments are stripped first: a reference that only a comment mentions is
    not coverage."""
    out = set()
    for m in _ISFILTERED.finditer(strip_comments(expression or "")):
        table = (m.group(1) or m.group(2) or "").replace("''", "'")
        out.add((table.strip(), m.group(3).strip()))
    return out


def measure_expression(model) -> tuple:
    """(host table, expression) of the applied-filters measure, or
    (None, "")."""
    for host in model.measure_hosts().get(MEASURE) or []:
        m = model.measure(host, MEASURE)
        if m is not None:
            return host, m.expression
    return None, ""


def referenced_removed(expression: str, found: list, table_kind: str,
                       measure_kind: str, column_kind: str = "") -> list:
    """The names of the objects `found` removes that `expression` names.

    A table is named as ``'Table'``, a column or a measure as ``[Name]``."""
    gone = []
    for f in found:
        if f.kind not in (table_kind, measure_kind, column_kind) or f.blocked:
            continue
        name = f.ref if f.kind == table_kind else _member(f.ref)
        if name and (f"'{name}'" in expression or f"[{name}]" in expression):
            gone.append(name)
    return sorted(set(gone))


def _member(ref: str) -> str:
    """The object name of a ``Table[Name]`` reference."""
    i = ref.find("[")
    return ref[i + 1:-1] if i >= 0 else ref


def coverage(fleet, name: str, model) -> dict:
    """What the applied-filters measure exposes and what it does not.

    ``missing`` are the visible y-Selector dimensions the measure does not
    test."""
    forge = _forge()
    if forge is None:
        return {}
    dims, _unresolved = forge.discover(Path(fleet.models[name]))
    host, expression = measure_expression(model)
    covered = isfiltered_refs(expression)
    visible = {(d.table, d.column) for d in dims}
    return {"host": host, "missing": sorted(visible - covered)}


def findings(name: str, fleet, model) -> list:
    """``ifilter.coverage_gap``: a visible y-Selector dimension the
    applied-filters measure does not expose.

    A model that does not declare the measure at all has no coverage gap:
    there is nothing to bring up to date."""
    from .detect import Finding
    cov = coverage(fleet, name, model)
    if not cov or cov["host"] is None or not cov["missing"]:
        return []
    listing = "\n".join(f"      + {t}[{c}]" for t, c in cov["missing"][:40])
    more = ("" if len(cov["missing"]) <= 40
            else f"\n      … and {len(cov['missing']) - 40} more")
    detail = (f"{len(cov['missing'])} visible y-Selector dimension(s) are NOT "
              f"exposed via i-Applied Filters in {name}:\n{listing}{more}\n"
              f"  AUTO-FIX regenerates the 'i-Applied Filters' measure from "
              f"the full visible y-Selector dimension set (plain-text "
              f"ISFILTERED branches — review the diff; per-dimension wording "
              f"is the column name).")
    return [Finding(
        kind="ifilter.coverage_gap", model=name, ref=MEASURE, detail=detail,
        data={"table": cov["host"], "measure": MEASURE,
              "missing": [[t, c] for t, c in cov["missing"]]})]


def regen_finding(kind: str, name: str, objects, verb: str, when: str) -> list:
    """``retire.ifilter_regen`` / ``purge.ifilter_regen``: the measure
    references an object this run removes."""
    from .detect import Finding
    if not objects:
        return []
    listed = ", ".join(objects[:8])
    detail = (f"'i-Applied Filters' references object(s) this run {verb} "
              f"({listed}) — regenerate it from the {when} visible y-Selector "
              f"dimension set (the applied-filters regenerator; its coverage "
              f"detector is a superset check and never re-fires on stale "
              f"extra branches).")
    return [Finding(kind=kind, model=name, ref=MEASURE, detail=detail,
                    data={"measure": MEASURE, "objects": list(objects)})]


def apply_regeneration(fleet, f) -> tuple:
    """Regenerate the applied-filters measure from the visible y-Selector
    dimensions.

    The file's own bytes are kept: its line endings and its byte-order mark
    survive."""
    from .remediate import FileChange
    forge = _forge()
    if forge is None:
        raise DriftDoctorError(
            "pbip-model-forge is not installed, so the applied-filters "
            "measure cannot be regenerated: install it next to "
            "drift-doctor and re-run")
    model_root = Path(fleet.models[f.model])
    dims, _unresolved = forge.discover(model_root)
    from .tmdl import parse_model
    model = parse_model(model_root / "definition")
    host, expression = measure_expression(model)
    if not dims:
        if isfiltered_refs(expression):
            raise DriftDoctorError(
                "the model offers no visible y-Selector dimension, so the "
                f"'{MEASURE}' measure cannot be rebuilt: an empty caption is "
                f"not a measure. It still names objects the model no longer "
                f"declares — remove it or write the caption by hand.")
        return ("the model offers no visible y-Selector dimension — no "
                "change", [])
    if host is None:
        raise DriftDoctorError(
            f"the model does not declare the '{MEASURE}' measure; re-run "
            f"`drift-doctor detect`")
    path = model_root / "definition" / "tables" / f"{host}.tmdl"
    if not path.is_file():
        raise DriftDoctorError(
            f"the table file {path} is gone; re-run `drift-doctor detect`")
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    old = te.read_text(path)
    eol = te.line_ending(old)
    text = old.replace("\r\n", "\n")
    generated = forge.generate_caption(dims)
    verdict = forge.validate_caption(generated, list(dims))
    if not verdict.ok:
        raise DriftDoctorError(
            "the regenerated caption does not cover every dimension ("
            + "; ".join(verdict.errors) + "); nothing was written")
    replaced = forge.replace_caption(text, MEASURE, generated)
    if replaced is None:
        raise DriftDoctorError(
            f"the measure '{MEASURE}' could not be found in {path.name}; "
            f"re-run `drift-doctor detect`")
    new = replaced[0]
    te.assert_tmdl_valid(new)
    if new == text:
        return (f"'{MEASURE}' already exposes every visible y-Selector "
                f"dimension — no change", [])
    return (f"regenerate '{MEASURE}' from {len(dims)} visible y-Selector "
            f"dimension(s)",
            [FileChange(path, raw, te.encode(new, eol, bom))])

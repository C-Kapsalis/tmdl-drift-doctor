"""Remediation engine — cascade template truth into derived models.

Consumes ``detect``'s findings and applies a fix per kind, honoring three
gates in order:

1. **the allowlist** — a kind the fleet's allowlist does not name is never
   applied, however real the drift;
2. **the sync gate** — ``retire.*`` kinds (the only deletions) additionally
   require ``--sync``; a default run is purely additive/overwriting;
3. **the write guards** — every new file content passes
   ``tmdl_edit.assert_tmdl_valid`` (which includes the never-inject-into-DAX
   invariant) before it touches disk.

``--dry-run`` computes every edit and renders unified diffs without writing
anything — including the ledger. Applied actions are recorded in the ledger;
all appliers are idempotent (a second run finds nothing to do).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import DriftDoctorError, TmdlEditError
from . import tmdl_edit as te
from .detect import Finding, RETIREMENT_FINDING_KINDS
from .fleet import Fleet
from .ledger import Ledger


@dataclass
class FileChange:
    """One file-level effect of an applier. old=None => create,
    new=None => delete."""
    path: Path
    old: Optional[str]
    new: Optional[str]

    def diff(self) -> str:
        a = (self.old or "").splitlines(keepends=True)
        b = (self.new or "").splitlines(keepends=True)
        label = str(self.path)
        return "".join(difflib.unified_diff(
            a, b, fromfile=f"a/{label}", tofile=f"b/{label}"))


@dataclass
class Outcome:
    finding: Finding
    status: str            # applied | would-apply | skipped | failed
    reason: str = ""
    changes: list = field(default_factory=list)


@dataclass
class RunResult:
    outcomes: list = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)


# ───────────────────────── appliers ─────────────────────────
# Each applier returns (action_label, [FileChange]) or raises.

def _model_paths(fleet: Fleet, f: Finding) -> tuple:
    d = fleet.model_definition(f.model)
    t = fleet.template_definition
    return d, t


def _table_file(definition: Path, table: str) -> Path:
    p = definition / "tables" / f"{table}.tmdl"
    if not p.exists():
        raise DriftDoctorError(
            f"expected the table file {p}, but it does not exist — the model "
            f"changed after detection. Re-run `drift-doctor detect` for "
            f"current findings.")
    return p


def _split_ref(ref: str) -> tuple:
    return ref[:ref.rindex("[")], ref[ref.rindex("[") + 1:-1]


def _apply_table_missing(fleet: Fleet, f: Finding) -> tuple:
    derived, template = _model_paths(fleet, f)
    src = _table_file(template, f.ref)
    dst = derived / "tables" / src.name
    content = te.regenerate_all_lineage_tags(src.read_text(encoding="utf-8"))
    model_file = derived / "model.tmdl"
    model_old = model_file.read_text(encoding="utf-8")
    model_new = te.register_table(model_old, f.ref)
    changes = [FileChange(dst, None, content)]
    if model_new != model_old:
        changes.append(FileChange(model_file, model_old, model_new))
    return ("copy table from template + register in model.tmdl "
            "(review the partition source — it is the template's)", changes)


def _cascade_object(fleet: Fleet, f: Finding, keyword: str,
                    insert: Callable) -> tuple:
    """Shared replace-or-insert applier for measures and columns: splice the
    template's RAW block (faithful — lineage/annotation-preserving surgery,
    never re-serialized from parsed fields)."""
    derived, template = _model_paths(fleet, f)
    table, obj = _split_ref(f.ref)
    t_content = _table_file(template, table).read_text(encoding="utf-8")
    block = te.extract_block(t_content, keyword, obj)
    if block is None:
        raise DriftDoctorError(
            f"template no longer carries {keyword} {f.ref} — the baseline is "
            f"stale; run `drift-doctor capture`")
    d_file = _table_file(derived, table)
    d_content = d_file.read_text(encoding="utf-8")
    existing = te.extract_block(d_content, keyword, obj)
    if existing is None:
        new = insert(d_content, te.regenerate_lineage_tag(block))
        label = f"insert {keyword} block from template (fresh lineage tag)"
    else:
        tag = te.first_lineage_tag(existing)
        new_block = te.set_first_lineage_tag(block, tag) if tag \
            else te.regenerate_lineage_tag(block)
        new = te.replace_block(d_content, keyword, obj, new_block)
        label = f"overwrite {keyword} block from template (lineage tag preserved)"
    return (label, [FileChange(d_file, d_content, new)])


def _apply_measure(fleet: Fleet, f: Finding) -> tuple:
    return _cascade_object(fleet, f, "measure", te.insert_measure_block)


def _apply_column(fleet: Fleet, f: Finding) -> tuple:
    return _cascade_object(fleet, f, "column", te.insert_column_block)


def _apply_expression(fleet: Fleet, f: Finding) -> tuple:
    derived, template = _model_paths(fleet, f)
    t_file = template / "expressions.tmdl"
    block = te.extract_expression_block(
        t_file.read_text(encoding="utf-8"), f.ref)
    if block is None:
        raise DriftDoctorError(
            f"template expressions.tmdl no longer defines '{f.ref}' — stale "
            f"baseline; run `drift-doctor capture`")
    d_file = derived / "expressions.tmdl"
    d_content = d_file.read_text(encoding="utf-8") if d_file.exists() else ""
    existing = te.extract_expression_block(d_content, f.ref)
    if existing is None:
        block = te.regenerate_lineage_tag(block)
    else:
        tag = te.first_lineage_tag(existing)
        block = te.set_first_lineage_tag(block, tag) if tag \
            else te.regenerate_lineage_tag(block)
    new = te.replace_or_append_expression(d_content, f.ref, block)
    return (f"cascade shared expression '{f.ref}' from template "
            f"(allowlist-named; everything else untouched)",
            [FileChange(d_file, d_content, new)])


def _apply_mapping_row_missing(fleet: Fleet, f: Finding) -> tuple:
    derived, _ = _model_paths(fleet, f)
    table, row = f.data["table"], f.data["row_key"]
    d_file = _table_file(derived, table)
    d_content = d_file.read_text(encoding="utf-8")
    new = te.add_mapping_row(d_content, f.data["row"])
    return (f"add mapping row '{row}' from template",
            [FileChange(d_file, d_content, new)])


def _apply_retire_measure(fleet: Fleet, f: Finding) -> tuple:
    derived, _ = _model_paths(fleet, f)
    table, obj = _split_ref(f.ref)
    d_file = _table_file(derived, table)
    d_content = d_file.read_text(encoding="utf-8")
    new = te.remove_block(d_content, "measure", obj)
    if new is None:
        raise DriftDoctorError(
            f"measure {f.ref} is no longer present in {d_file} — the model "
            f"changed after detection. Re-run `drift-doctor detect`.")
    return ("remove retired measure block", [FileChange(d_file, d_content, new)])


def _apply_retire_column(fleet: Fleet, f: Finding) -> tuple:
    derived, _ = _model_paths(fleet, f)
    table, obj = _split_ref(f.ref)
    d_file = _table_file(derived, table)
    d_content = d_file.read_text(encoding="utf-8")
    new = te.remove_block(d_content, "column", obj)
    if new is None:
        raise DriftDoctorError(
            f"column {f.ref} is no longer present in {d_file} — the model "
            f"changed after detection. Re-run `drift-doctor detect`.")
    return ("remove retired column block", [FileChange(d_file, d_content, new)])


def _apply_retire_table(fleet: Fleet, f: Finding) -> tuple:
    derived, _ = _model_paths(fleet, f)
    d_file = _table_file(derived, f.ref)
    model_file = derived / "model.tmdl"
    model_old = model_file.read_text(encoding="utf-8")
    model_new = te.deregister_table(model_old, f.ref)
    changes = [FileChange(d_file, d_file.read_text(encoding="utf-8"), None)]
    if model_new != model_old:
        changes.append(FileChange(model_file, model_old, model_new))
    return ("remove retired table file + deregister from model.tmdl", changes)


def _apply_retire_mapping_row(fleet: Fleet, f: Finding) -> tuple:
    derived, _ = _model_paths(fleet, f)
    table, row = f.data["table"], f.data["row_key"]
    d_file = _table_file(derived, table)
    d_content = d_file.read_text(encoding="utf-8")
    new = te.remove_mapping_row(d_content, row)
    if new is None:
        raise DriftDoctorError(
            f"mapping row '{row}' is no longer present in {d_file} — the "
            f"model changed after detection. Re-run `drift-doctor detect`.")
    return (f"remove retired mapping row '{row}' (sibling rows kept)",
            [FileChange(d_file, d_content, new)])


APPLIERS: dict = {
    "table.missing": _apply_table_missing,
    "column.missing": _apply_column,
    "column.property_drift": _apply_column,
    "measure.missing": _apply_measure,
    "measure.expression_drift": _apply_measure,
    "measure.property_drift": _apply_measure,
    "expression.drift": _apply_expression,
    "mapping_row.missing": _apply_mapping_row_missing,
    "retire.table": _apply_retire_table,
    "retire.column": _apply_retire_column,
    "retire.measure": _apply_retire_measure,
    "retire.mapping_row": _apply_retire_mapping_row,
}


# ───────────────────────── engine ─────────────────────────

def remediate(fleet: Fleet, findings: list, *, kind: Optional[str] = None,
              dry_run: bool = False, sync: bool = False,
              ledger: Optional[Ledger] = None) -> RunResult:
    ledger = ledger or Ledger(fleet.ledger_path)
    result = RunResult()

    for f in findings:
        if kind and not f.kind.startswith(kind):
            continue
        if f.advisory:
            result.outcomes.append(Outcome(
                f, "skipped",
                "advisory — this object exists only in the derived model; "
                "extensions are reported for review, never auto-remediated. "
                "Edit the derived model directly if it is unwanted."))
            continue
        if f.kind not in APPLIERS:
            result.outcomes.append(Outcome(
                f, "skipped",
                f"kind '{f.kind}' has no automated fix — resolve it by "
                f"editing the model directly"))
            continue
        if f.kind not in fleet.allowlist_kinds:
            result.outcomes.append(Outcome(
                f, "skipped",
                f"kind '{f.kind}' is not in the fleet allowlist — nothing "
                f"cascades by default. Add it under allowlist.kinds in "
                f"fleet.yml to let remediate apply it."))
            continue
        if f.kind in RETIREMENT_FINDING_KINDS and not sync:
            result.outcomes.append(Outcome(
                f, "skipped",
                "retirement removals require --sync (deletions never run in "
                "a default pass) — re-run with --sync to apply this removal"))
            continue

        try:
            label, changes = APPLIERS[f.kind](fleet, f)
            for c in changes:
                if c.new is not None and c.path.suffix == ".tmdl":
                    te.assert_tmdl_valid(c.new)   # guard BEFORE any write
            if dry_run:
                result.outcomes.append(Outcome(f, "would-apply", label, changes))
                continue
            for c in changes:
                if c.new is None:
                    c.path.unlink()
                else:
                    c.path.parent.mkdir(parents=True, exist_ok=True)
                    c.path.write_text(c.new, encoding="utf-8")
            ledger.record_remediation(
                f.kind, f.model, f.ref, label,
                files=[str(c.path) for c in changes])
            result.outcomes.append(Outcome(f, "applied", label, changes))
        except (DriftDoctorError, TmdlEditError, OSError) as e:
            result.outcomes.append(Outcome(f, "failed", str(e)))

    return result

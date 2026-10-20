"""Remediation engine: cascade template truth into derived models.

Consumes ``detect``'s findings and applies a fix per kind, honoring these
gates in order:

1. **overrides**: a finding a fleet.yml override acknowledges is never
   applied (it prints as an ACK instead);
2. **the allowlist**: a kind the fleet's allowlist does not name is never
   applied, however real the drift;
3. **the sync gate**: ``retire.*`` kinds (the only deletions) additionally
   require ``--sync``; a default run is purely additive/overwriting;
4. **the coupling guard**: a retirement that derived-only work still depends
   on is ``blocked`` (decided at detect time, routed to a human);
5. **preconditions**: each fix declares what must already be true in the
   derived model when it runs (relationship endpoints exist, the columns and
   measures an inserted measure references exist or are being added in this
   run, ...). They are evaluated at apply time, after the fixes earlier in
   the same run; an unmet one is ``auto_skipped_precondition``, and
   ``--converge`` re-detects and re-applies until nothing changes;
6. **the write guards**: every new file content passes
   ``tmdl_edit.assert_tmdl_valid`` (which includes the never-inject-into-DAX
   invariant) before it touches disk.

Fixes run in dependency order (tables, columns, expressions, mapping rows,
measures, relationships, then retirements), so one pass usually suffices.

``--dry-run`` computes every edit and renders unified diffs without writing
anything, including the ledger. Applied actions are recorded in the ledger;
all appliers are idempotent (a second run finds nothing to do).
"""

from __future__ import annotations

import difflib
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import DriftDoctorError, TmdlEditError
from . import tmdl_edit as te
from .detect import (
    Finding,
    RETIREMENT_FINDING_KINDS,
    dependents,
    split_ref,
)
from .fleet import Fleet, kind_matches
from .ledger import Ledger
from .tmdl import Model, dax_references, parse_model, parse_signature

@dataclass
class FileChange:
    """One file-level effect of an applier. old=None => create,
    new=None => delete."""
    path: Path
    old: Optional[str]
    new: Optional[str]
    verify: Optional[Callable] = None    # read-back check after the write

    def diff(self) -> str:
        a = _as_text(self.old).splitlines(keepends=True)
        b = _as_text(self.new).splitlines(keepends=True)
        label = str(self.path)
        return "".join(difflib.unified_diff(
            a, b, fromfile=f"a/{label}", tofile=f"b/{label}"))


def _as_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8-sig", errors="replace")
    return value or ""


def write_change(change) -> None:
    """Write a change, keeping the file's own line endings.

    A file committed with Windows line endings must come back with Windows
    line endings: a fixer that converts the whole file makes the diff
    unreadable and hides the change it was asked for. The new text is
    written with the line ending the file already had (the old text when the
    file exists, the new text itself when the file is created)."""
    path = Path(change.path)
    new = change.new
    if isinstance(new, bytes):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(new)
        return
    eol = "\n"
    if path.is_file():
        raw = path.read_bytes()
        if b"\r\n" in raw:
            eol = "\r\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(te.with_line_ending(new, eol).encode("utf-8"))


class _Skip(Exception):
    """Raised by an applier that decides, on the live files, that the fix
    must not run; the finding is reported skipped with this reason."""


@dataclass
class Outcome:
    finding: Finding
    status: str            # applied | would-apply | skipped | failed |
                           # blocked | auto_skipped_precondition | overridden
    reason: str = ""
    changes: list = field(default_factory=list)


@dataclass
class RunResult:
    outcomes: list = field(default_factory=list)
    notices: list = field(default_factory=list)   # e.g. view-enrichment
    digest: list = field(default_factory=list)    # decisions for a human
    errors: list = field(default_factory=list)    # run-level failures
    backups_removed: int = 0
    backups_kept: list = field(default_factory=list)

    @property
    def applied(self) -> list:
        """(kind, ref) of every applied fix, in order (feeds receipts)."""
        return [(o.finding.kind, o.finding.ref) for o in self.outcomes
                if o.status == "applied"]

    def count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)


# ───────────────────────── appliers ─────────────────────────
# Each applier returns (action_label, [FileChange]) or raises.

def _model_paths(fleet: Fleet, f: Finding) -> tuple:
    d = fleet.model_definition(f.model)
    t = fleet.template_definition
    return d, t


def _table_file(definition: Path, table: str) -> Path:
    """The table's file, under the template's naming convention."""
    p = definition / "tables" / f"{table}.tmdl"
    if not p.exists():
        raise DriftDoctorError(
            f"expected the table file {p}, but it does not exist — the model "
            f"changed after detection. Re-run `drift-doctor detect` for "
            f"current findings.")
    return p


_split_ref = split_ref


def _apply_table_missing(fleet: Fleet, f: Finding) -> tuple:
    derived, template = _model_paths(fleet, f)
    src = _table_file(template, f.ref)
    dst = derived / "tables" / src.name
    if dst.exists():
        return ("table file already present", [])
    content = te.regenerate_all_lineage_tags(src.read_text(encoding="utf-8"))
    model_file = derived / "model.tmdl"
    model_old = model_file.read_text(encoding="utf-8")
    model_new = te.register_table(model_old, f.ref)
    if te.is_m_table(content):
        model_new = te.register_query_order(model_new, f.ref)
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
        new_block = te.regenerate_lineage_tag(block)
        new = insert(d_content, new_block)
        label = f"insert {keyword} block from template (fresh lineage tag)"
    else:
        tag = te.first_lineage_tag(existing)
        new_block = te.set_first_lineage_tag(block, tag) if tag \
            else te.regenerate_lineage_tag(block)
        new = te.replace_block(d_content, keyword, obj, new_block)
        label = f"overwrite {keyword} block from template (lineage tag preserved)"
    te.assert_round_trip(new, keyword, obj, new_block)
    return (label, [FileChange(d_file, d_content, new)])


def _apply_measure(fleet: Fleet, f: Finding) -> tuple:
    return _cascade_object(fleet, f, "measure", te.insert_measure_block)


def _apply_column(fleet: Fleet, f: Finding) -> tuple:
    return _cascade_object(fleet, f, "column", te.insert_column_block)


def _template_expression_block(fleet: Fleet, name: str) -> str:
    t_file = fleet.template_definition / "expressions.tmdl"
    text = t_file.read_text(encoding="utf-8") if t_file.exists() else ""
    block = te.extract_expression_block(text, name)
    if block is None:
        raise DriftDoctorError(
            f"template expressions.tmdl no longer defines '{name}' — stale "
            f"baseline; run `drift-doctor capture`")
    return block


def _apply_expression(fleet: Fleet, f: Finding) -> tuple:
    """Overwrite every declaration of an allowlisted shared expression that
    differs from the template's, each keeping its own lineage tag."""
    from .tmdl import expression_form
    derived, _ = _model_paths(fleet, f)
    block = _template_expression_block(fleet, f.ref)
    wanted = expression_form(block)
    d_file = derived / "expressions.tmdl"
    if not d_file.exists():
        raise DriftDoctorError(
            f"{d_file} does not exist: the model changed after detection. "
            f"Re-run `drift-doctor detect`.")
    d_content = d_file.read_text(encoding="utf-8")

    def rewrite(old: str) -> str:
        if expression_form(old) == wanted:
            return old
        tag = te.first_lineage_tag(old)
        return (te.set_first_lineage_tag(block, tag) if tag
                else te.regenerate_lineage_tag(block))

    new = te.replace_expression_blocks(d_content, f.ref, rewrite)
    return (f"cascade shared expression '{f.ref}' from template "
            f"(allowlist-named; lineage tag kept; everything else untouched)",
            [FileChange(d_file, d_content, new)])


CONNECTION_HINT = ("copy the declaration from the template and set this "
                   "model's own value")


def _apply_expression_missing(fleet: Fleet, f: Finding) -> tuple:
    if f.ref in fleet.connection_parameters:
        raise _Skip(f"'{f.ref}' is a connection parameter and is never "
                    f"written by remediation: {CONNECTION_HINT}")
    derived, _ = _model_paths(fleet, f)
    block = te.regenerate_lineage_tag(_template_expression_block(fleet, f.ref))
    d_file = derived / "expressions.tmdl"
    old = d_file.read_text(encoding="utf-8") if d_file.exists() else None
    if old is not None and te.expression_block_span(old, f.ref) is not None:
        return ("expression already present", [])
    new = te.append_expression_block(old or "", block)
    return (f"add template expression '{f.ref}' after the existing blocks "
            f"(fresh lineage tag)", [FileChange(d_file, old, new)])


def _apply_mapping_row_missing(fleet: Fleet, f: Finding) -> tuple:
    if f.data.get("encoded"):
        from .mappings import apply_row_missing
        return apply_row_missing(fleet, f)
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
    changes = [FileChange(d_file, d_content, new)]
    changes += _sweep_relationships(derived, f)
    label = "remove retired column block"
    if len(changes) > 1:
        label += " + its template-owned/retired relationships"
    return (label, changes)


def _sweep_relationships(derived: Path, f: Finding) -> list:
    """Remove the relationships detection marked as going WITH a retired
    table/column (template-owned or ledger-retired ones only)."""
    sigs = f.data.get("sweep_relationships") or []
    rel_file = derived / "relationships.tmdl"
    if not sigs or not rel_file.exists():
        return []
    old = rel_file.read_text(encoding="utf-8")
    new = old
    for sig in sigs:
        out = te.remove_relationship_block(new, sig)
        if out is not None:
            new = out
    return [FileChange(rel_file, old, new)] if new != old else []


def _apply_retire_table(fleet: Fleet, f: Finding) -> tuple:
    derived, _ = _model_paths(fleet, f)
    d_file = _table_file(derived, f.ref)
    model_file = derived / "model.tmdl"
    model_old = model_file.read_text(encoding="utf-8")
    model_new = te.deregister_table(model_old, f.ref)
    changes = [FileChange(d_file, d_file.read_text(encoding="utf-8"), None)]
    if model_new != model_old:
        changes.append(FileChange(model_file, model_old, model_new))
    swept = _sweep_relationships(derived, f)
    changes += swept
    label = "remove retired table file + deregister from model.tmdl"
    if swept:
        label += " + its template-owned/retired relationships"
    return (label, changes)


def _apply_retire_mapping_row(fleet: Fleet, f: Finding) -> tuple:
    if f.data.get("encoded"):
        from .mappings import apply_retire_rows
        return apply_retire_rows(fleet, f)
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


def _apply_measure_wrong_host(fleet: Fleet, f: Finding) -> tuple:
    """Move the derived model's OWN block verbatim to the template's host.
    The DAX and lineage tag are untouched (same object, same identity); if
    the body also differs, that surfaces as ordinary expression drift on the
    next detect, once the hosts match. The destination is written FIRST, so
    an interruption leaves a detectable duplicate, never a lost measure."""
    derived, _ = _model_paths(fleet, f)
    name = f.data["measure"]
    src_file = _table_file(derived, f.data["from_table"])
    dst_file = _table_file(derived, f.data["to_table"])
    src = src_file.read_text(encoding="utf-8")
    dst = dst_file.read_text(encoding="utf-8")
    block = te.extract_block(src, "measure", name)
    if block is None:
        raise DriftDoctorError(
            f"measure '{name}' is no longer on '{f.data['from_table']}': the "
            f"model changed after detection. Re-run `drift-doctor detect`.")
    new_src = te.remove_block(src, "measure", name)
    new_dst = te.insert_measure_block(dst, block)
    te.assert_round_trip(new_dst, "measure", name, block)
    return (f"move measure block verbatim from '{f.data['from_table']}' to "
            f"the template's host '{f.data['to_table']}' (DAX and lineage "
            f"tag untouched)",
            [FileChange(dst_file, dst, new_dst),
             FileChange(src_file, src, new_src)])


def _apply_measure_renamed(fleet: Fleet, f: Finding) -> tuple:
    """Rename the derived declaration (body untouched), then rewrite every
    DAX reference in the model and every report reference below it."""
    from .renames import (count_report_refs, rename_declaration,
                          report_changes, rewrite_measure_text)
    derived, _ = _model_paths(fleet, f)
    old, new, host = f.data["old"], f.data["new"], f.data["host"]
    host_file = _table_file(derived, host)
    changes, refs = [], 0
    for path in sorted((derived / "tables").glob("*.tmdl"),
                       key=lambda p: p.name):
        text = path.read_text(encoding="utf-8")
        updated = text
        if path == host_file:
            updated = rename_declaration(text, old, new)
            if updated is None:
                raise DriftDoctorError(
                    f"measure '{old}' is no longer declared on '{host}': the "
                    f"model changed after detection. Re-run `drift-doctor "
                    f"detect`.")
        updated, _n, hits = rewrite_measure_text(updated, {old: new})
        refs += hits
        if updated != text:
            changes.append(FileChange(path, text, updated))
    reports, n_reports = report_changes(fleet.models[f.model], old, new)
    for path, raw, data in reports:
        changes.append(FileChange(path, raw, data))
    label = (f"rename measure on '{host}': '{old}' -> '{new}' ({refs} DAX "
             f"reference(s) rewritten")
    if n_reports is None:
        left = count_report_refs(fleet.models[f.model], old)
        label += ("; report references NOT rewritten: install "
                  "pbi-plot-styler next to drift-doctor" if left is None
                  else "")
    else:
        label += f", {n_reports} report reference(s)"
    return label + ")", changes


def _apply_measure_duplicate_host(fleet: Fleet, f: Finding) -> tuple:
    """Remove the stale extra copy; the copy on the template's host stays."""
    derived, _ = _model_paths(fleet, f)
    table, name = _split_ref(f.ref)
    path = derived / "tables" / f"{table}.tmdl"
    if not path.exists():
        return ("table file already gone", [])
    old = path.read_text(encoding="utf-8")
    new = te.remove_block(old, "measure", name)
    if new is None:
        return ("stale copy already removed", [])
    return (f"remove the stale copy of '{name}' from '{table}' (the copy on "
            f"'{f.data.get('template_host')}' is kept)",
            [FileChange(path, old, new)])


def _template_relationship_block(fleet: Fleet, sig: str) -> str:
    t_file = fleet.template_definition / "relationships.tmdl"
    content = t_file.read_text(encoding="utf-8") if t_file.exists() else ""
    block = te.extract_relationship_block(content, sig)
    if block is None:
        raise DriftDoctorError(
            f"template relationships.tmdl no longer defines {sig}; the "
            f"baseline is stale. Run `drift-doctor capture`.")
    return block


def _apply_relationship_missing(fleet: Fleet, f: Finding) -> tuple:
    derived, _ = _model_paths(fleet, f)
    block = _template_relationship_block(fleet, f.ref)
    d_file = derived / "relationships.tmdl"
    old = d_file.read_text(encoding="utf-8") if d_file.exists() else None
    content = old or ""
    if te.relationship_span(content, f.ref) is not None:
        return ("relationship already present", [])
    name = block.split("\n", 1)[0].split(None, 1)[1].strip()
    label = "append relationship block from template"
    if name in te.relationship_names(content):
        block = te.set_relationship_name(block, str(uuid.uuid4()))
        label += " (fresh name: the template's collides)"
    return (label, [FileChange(d_file, old,
                               te.append_relationship_block(content, block))])


def _apply_relationship_modified(fleet: Fleet, f: Finding) -> tuple:
    derived, _ = _model_paths(fleet, f)
    block = _template_relationship_block(fleet, f.ref)
    d_file = derived / "relationships.tmdl"
    content = d_file.read_text(encoding="utf-8")
    existing = te.extract_relationship_block(content, f.ref)
    if existing is None:
        raise DriftDoctorError(
            f"relationship {f.ref} is no longer in {d_file}: the model "
            f"changed after detection. Re-run `drift-doctor detect`.")
    name = existing.split("\n", 1)[0].split(None, 1)[1].strip()
    new = te.replace_relationship_block(
        content, f.ref, te.set_relationship_name(block, name))
    return ("overwrite relationship properties from template (block name "
            "kept)", [FileChange(d_file, content, new)])


def _apply_retire_relationship(fleet: Fleet, f: Finding) -> tuple:
    derived, _ = _model_paths(fleet, f)
    d_file = derived / "relationships.tmdl"
    content = d_file.read_text(encoding="utf-8")
    new = te.remove_relationship_block(content, f.ref)
    if new is None:
        raise DriftDoctorError(
            f"relationship {f.ref} is no longer in {d_file}: the model "
            f"changed after detection. Re-run `drift-doctor detect`.")
    return ("remove retired relationship block",
            [FileChange(d_file, content, new)])


def _apply_casing(fleet: Fleet, f: Finding) -> tuple:
    """Rewrite both endpoints of a relationship to the template's spelling
    (a mechanical, meaning-preserving edit)."""
    derived, _ = _model_paths(fleet, f)
    d_file = derived / "relationships.tmdl"
    content = d_file.read_text(encoding="utf-8")
    sig = f.data["signature"]
    span = te.relationship_span(content, sig)
    if span is None:
        return ("relationship already spelled as in the template", [])
    baseline_tables = (_current_baseline(fleet).get("tables") or {})
    tables = {t.casefold(): t for t in baseline_tables}
    lines = content.split("\n")
    from .tmdl import _parse_column_ref
    for i in range(span[0], span[1]):
        m = re.match(r"^(\t(?:fromColumn|toColumn)\s*:\s*)(.+?)\s*$", lines[i])
        if not m:
            continue
        ref = _parse_column_ref(m.group(2))
        if not ref:
            continue
        table = tables.get(ref[0].casefold())
        if table is None:
            continue
        cols = {c.casefold(): c for c in
                (baseline_tables[table].get("columns") or {})}
        col = cols.get(ref[1].casefold(), ref[1])
        lines[i] = m.group(1) + te.smart_quote(table) + "." + te.smart_quote(col)
    new = "\n".join(lines)
    return ("rewrite relationship endpoints to the template's spelling",
            [FileChange(d_file, content, new)] if new != content else [])


def _current_baseline(fleet: Fleet) -> dict:
    from .baseline import load_baseline
    return load_baseline(fleet)


def _apply_category_missing(fleet: Fleet, f: Finding) -> tuple:
    from .mappings import apply_category_missing
    return apply_category_missing(fleet, f)


def _apply_retire_category(fleet: Fleet, f: Finding) -> tuple:
    from .mappings import apply_retire_rows
    return apply_retire_rows(fleet, f)


def _apply_annotation(fleet: Fleet, f: Finding) -> tuple:
    from .annotations import apply_undeclared
    return apply_undeclared(fleet, f)


def _apply_refresh(fleet: Fleet, f: Finding) -> tuple:
    from . import refresh
    fn = {"refresh_policy.missing": refresh.apply_missing,
          "refresh_policy.window_drift": refresh.apply_window,
          "refresh_policy.invalid": refresh.apply_realign}[f.kind]
    return fn(fleet, f)


def _apply_source_view(fleet: Fleet, f: Finding) -> tuple:
    from .sources import identity, rewrite_view
    derived, _ = _model_paths(fleet, f)
    from .tmdl import parse_model as _pm
    table = _pm(derived).tables.get(f.data["table"])
    if table is None:
        raise DriftDoctorError(f"'{f.data['table']}' is gone; re-run detect")
    path = table.file
    old = path.read_text(encoding="utf-8")
    new, n = rewrite_view(old, f.data["old_view"], f.data["new_view"],
                          identity(fleet.raw))
    if not n:
        raise _Skip(f"no read of view '{f.data['old_view']}' was found in "
                    f"{path.name}; nothing was changed")
    return (f"re-point '{f.data['table']}' from '{f.data['old_view']}' to "
            f"'{f.data['new_view']}' ({n} occurrence(s), spelling kept)",
            [FileChange(path, old, new)])


def _apply_purge(fleet: Fleet, f: Finding) -> tuple:
    from .purge import apply_purge
    return apply_purge(fleet, f)


def _apply_selector(fleet: Fleet, f: Finding) -> tuple:
    from .selectors import apply_dangling
    return apply_dangling(fleet, f)


def _apply_extra_delete(fleet: Fleet, f: Finding) -> tuple:
    """Delete a model-only object the person chose to drop (interactive)."""
    derived, _ = _model_paths(fleet, f)
    if f.kind == "relationship.extra":
        path = derived / "relationships.tmdl"
        old = path.read_text(encoding="utf-8")
        new = te.remove_relationship_block(old, f.ref)
        if new is None:
            raise DriftDoctorError(f"relationship {f.ref} is gone; re-run "
                                   f"detect")
        return ("delete the model-only relationship",
                [FileChange(path, old, new)])
    table, obj = split_ref(f.ref)
    keyword = "measure" if f.kind == "extra.measure" else "column"
    path = _table_file(derived, table)
    old = path.read_text(encoding="utf-8")
    new = te.remove_block(old, keyword, obj)
    if new is None:
        raise DriftDoctorError(f"{keyword} {f.ref} is gone; re-run detect")
    return (f"delete the model-only {keyword} from the model",
            [FileChange(path, old, new)])


APPLIERS: dict = {
    "fieldparam.dangling_selector": _apply_selector,
    "column.purge_listed": _apply_purge,
    "purge.fieldparam_entry": _apply_purge,
    "purge.measure": _apply_purge,
    "purge.column": _apply_purge,
    "purge.relationship": _apply_purge,
    "purge.zmap_row": _apply_purge,
    "purge.table": _apply_purge,
    "retire.source_view": _apply_source_view,
    "refresh_policy.missing": _apply_refresh,
    "refresh_policy.window_drift": _apply_refresh,
    "refresh_policy.invalid": _apply_refresh,
    "category_annotation.undeclared": _apply_annotation,
    "mapping_category.missing": _apply_category_missing,
    "retire.mapping_category": _apply_retire_category,
    "relationship.casing_mismatch": _apply_casing,
    "table.missing": _apply_table_missing,
    "column.missing": _apply_column,
    "column.property_drift": _apply_column,
    "measure.missing": _apply_measure,
    "measure.expression_drift": _apply_measure,
    "measure.property_drift": _apply_measure,
    "measure.wrong_host": _apply_measure_wrong_host,
    "measure.renamed": _apply_measure_renamed,
    "measure.duplicate_host": _apply_measure_duplicate_host,
    "relationship.missing": _apply_relationship_missing,
    "relationship.modified": _apply_relationship_modified,
    "retire.relationship": _apply_retire_relationship,
    "expression.drift": _apply_expression,
    "expression.missing": _apply_expression_missing,
    "mapping_row.missing": _apply_mapping_row_missing,
    "retire.table": _apply_retire_table,
    "retire.column": _apply_retire_column,
    "retire.measure": _apply_retire_measure,
    "retire.mapping_row": _apply_retire_mapping_row,
}


def applier_for(kind: str):
    if kind in APPLIERS:
        return APPLIERS[kind]
    from .fieldparams import applier_for as fp
    return fp(kind)


# ───────────────────────── run state ─────────────────────────

class RunState:
    """What one remediation run has done (or, in a dry run, would have done)
    so far. Preconditions consult it so a fix can rely on an earlier fix in
    the same run, whether or not anything was written to disk."""

    def __init__(self, fleet: Fleet, baseline: dict):
        self.fleet = fleet
        self.baseline = baseline
        self.added: set = set()        # ("table"|"column"|"measure"|"relationship", ref)
        self.measure_names: set = set()  # measure names added anywhere
        self.removed: set = set()      # ("table"|"column"|"measure"|..., ref)
        self.overwritten: set = set()  # refs whose block was overwritten
        self._models: dict = {}

    def model(self, name: str) -> Model:
        if name not in self._models:
            self._models[name] = parse_model(self.fleet.model_definition(name))
        return self._models[name]

    def invalidate(self, name: str) -> None:
        self._models.pop(name, None)

    def divergent_tables(self, name: str) -> dict:
        """{table: (template view, model view)} for the model's tables that
        read a different source view than the template's."""
        from .sources import divergent_tables
        return divergent_tables(self.fleet, self.baseline, name,
                                self.model(name))

    def record(self, f: Finding) -> None:
        kind = f.kind
        b_tables = self.baseline.get("tables") or {}
        if kind == "table.missing":
            self.added.add(("table", f.ref))
            body = b_tables.get(f.ref) or {}
            for c in body.get("columns") or {}:
                self.added.add(("column", f"{f.ref}[{c}]"))
            for m in body.get("measures") or {}:
                self.added.add(("measure", f"{f.ref}[{m}]"))
                self.measure_names.add(m)
        elif kind == "column.missing":
            self.added.add(("column", f.ref))
        elif kind in ("measure.missing", "measure.wrong_host"):
            self.added.add(("measure", f.ref))
            self.measure_names.add(split_ref(f.ref)[1])
        elif kind == "relationship.missing":
            self.added.add(("relationship", f.ref))
        elif kind == "measure.renamed":
            self.added.add(("measure", f"{f.data['host']}[{f.data['new']}]"))
            self.measure_names.add(f.data["new"])
        elif kind in ("measure.expression_drift", "measure.property_drift",
                      "column.property_drift"):
            self.overwritten.add(f.ref)
        elif kind.startswith("retire."):
            self.removed.add((kind.split(".", 1)[1], f.ref))
            for sig in f.data.get("sweep_relationships") or ():
                self.removed.add(("relationship", sig))

    # ── presence, counting what this run added ──
    def has_table(self, model: str, table: str) -> bool:
        return table in self.model(model).tables or ("table", table) in self.added

    def has_column(self, model: str, table: str, col: str) -> bool:
        return (self.model(model).column(table, col) is not None
                or ("column", f"{table}[{col}]") in self.added)

    def has_object(self, model: str, table: str, name: str) -> bool:
        m = self.model(model)
        return (m.column(table, name) is not None
                or m.measure(table, name) is not None
                or ("column", f"{table}[{name}]") in self.added
                or ("measure", f"{table}[{name}]") in self.added)

    def has_bare(self, model: str, name: str) -> bool:
        m = self.model(model)
        if name in m.measure_hosts() or name in self.measure_names:
            return True
        if any(name in t.columns for t in m.tables.values()):
            return True
        return any(k == "column" and split_ref(r)[1] == name
                   for k, r in self.added)


# ───────────────────────── preconditions ─────────────────────────
# Each returns None when the fix may run, else the reason it may not (yet).

def _pre_table_missing(state: RunState, f: Finding) -> Optional[str]:
    body = (state.baseline.get("tables") or {}).get(f.ref) or {}
    hosts = state.model(f.model).measure_hosts()
    clash = sorted(f"{h}[{m}]" for m in (body.get("measures") or {})
                   for h in hosts.get(m, []))
    if clash:
        return ("copying the table would duplicate measure name(s) already "
                "in the model: " + ", ".join(clash)
                + " (measure names are model-global; move or remove them "
                  "first)")
    return None


def _pre_measure_refs(state: RunState, f: Finding) -> Optional[str]:
    """Every column/measure/table the template DAX references must exist
    in the derived model or be added earlier in this run."""
    table, name = split_ref(f.ref)
    body = ((state.baseline.get("tables") or {}).get(table) or {})
    expr = ((body.get("measures") or {}).get(name) or {}).get("expression", "")
    refs = dax_references(expr)
    known_tables = set(state.baseline.get("tables") or {}) | set(
        state.model(f.model).tables)
    missing = []
    for t, n in sorted(refs["qualified"]):
        if not state.has_object(f.model, t, n):
            missing.append(f"{t}[{n}]")
    for n in sorted(refs["bare"]):
        if n != name and not state.has_bare(f.model, n):
            missing.append(f"[{n}]")
    for t in sorted(refs["tables"] & known_tables):
        if not state.has_table(f.model, t):
            missing.append(f"table '{t}'")
    if missing:
        return ("the template DAX references object(s) the model lacks and "
                "this run is not adding: " + ", ".join(dict.fromkeys(missing)))
    return None


def _pre_wrong_host(state: RunState, f: Finding) -> Optional[str]:
    src, dst, name = f.data["from_table"], f.data["to_table"], f.data["measure"]
    m = state.model(f.model)
    if dst not in m.tables:
        return (f"the template's host table '{dst}' is not in the model yet "
                f"(fix table.missing first)")
    if m.measure(dst, name) is not None:
        return (f"'{dst}' already carries '{name}': a duplicate, not a move "
                f"(see measure.duplicate_host)")
    needle = (src, name)
    for t in m.tables.values():
        for obj in list(t.measures.values()) + list(t.columns.values()):
            if needle in dax_references(getattr(obj, "expression", ""))["qualified"]:
                return (f"{t.name}[{obj.name}] references '{src}'[{name}] by "
                        f"its old host; moving would leave that reference "
                        f"dangling. Rewrite it as [{name}] first")
    return None


def _pre_relationship_endpoints(state: RunState, f: Finding) -> Optional[str]:
    ft, fc, tt, tc = parse_signature(f.ref)
    missing = [f"{t}[{c}]" for t, c in ((ft, fc), (tt, tc))
               if not state.has_column(f.model, t, c)]
    if missing:
        return ("relationship endpoint(s) not in the model and not being "
                "added in this run: " + ", ".join(missing))
    return None


def _pre_retirement(state: RunState, f: Finding) -> Optional[str]:
    """Re-check, on the model as it is NOW, that nothing still depends on the
    object: template-owned dependents must have been fixed (or be fixed
    earlier in this run), ledger-retired ones removed."""
    target_kind = f.kind.split(".", 1)[1]
    deps = dependents(state.model(f.model), target_kind, f.ref)
    sweep = set(f.data.get("sweep_relationships") or ())
    left = [ref for kind, ref in deps["definitions"]
            if (kind, ref) not in state.removed
            and ("table", split_ref(ref)[0]) not in state.removed
            and ref not in state.overwritten]
    left += [sig for sig in deps["relationships"]
             if sig not in sweep and ("relationship", sig) not in state.removed]
    left += deps["sort_by"] + deps["hierarchies"]
    if left:
        return ("still referenced by " + ", ".join(sorted(left))
                + "; their own fixes must land first (cascade them, then "
                  "re-run, or use --converge)")
    return None


PRECONDITIONS: dict = {
    "table.missing": _pre_table_missing,
    "measure.missing": _pre_measure_refs,
    "measure.expression_drift": _pre_measure_refs,
    "measure.wrong_host": _pre_wrong_host,
    "relationship.missing": _pre_relationship_endpoints,
    "retire.table": _pre_retirement,
    "retire.column": _pre_retirement,
    "retire.measure": _pre_retirement,
}

# Dependency order: what later fixes rely on comes first.
PHASES = (
    "table.missing", "column.missing", "column.property_drift",
    "expression.drift", "expression.missing", "mapping_row.missing",
    "measure.renamed", "measure.missing",
    "measure.wrong_host", "measure.duplicate_host",
    "measure.expression_drift", "measure.property_drift",
    "relationship.missing", "relationship.modified",
    "relationship.casing_mismatch", "retire.relationship", "retire.measure",
    "retire.mapping_row", "retire.mapping_category", "retire.source_view",
    "retire.column", "retire.table",
    "purge.fieldparam_entry", "purge.measure", "purge.column",
    "purge.relationship", "purge.zmap_row", "purge.table",
)


def _phase(f: Finding) -> tuple:
    if f.kind in PHASES:
        rank = PHASES.index(f.kind)
    else:
        derived = [i for i, k in enumerate(DERIVED_PHASE_KINDS)
                   if kind_matches(f.kind, k)]
        rank = len(PHASES) + (derived[0] if derived
                              else len(DERIVED_PHASE_KINDS))
    # template-only expressions are added in alphabetical order
    return (rank, f.ref if f.kind == "expression.missing" else "")


# ───────────────────────── engine ─────────────────────────

# Kinds a batch run (and `remediate --yes`) may apply without a person
# answering: deterministic, additive or overwriting, never deleting.
BATCH_SAFE_KINDS = (
    "table.missing", "column.missing", "column.property_drift",
    "measure.missing", "measure.expression_drift", "measure.property_drift",
    "measure.wrong_host", "measure.duplicate_host", "measure.renamed",
    "expression.drift",
    "expression.missing", "mapping_row.missing", "mapping_category.missing",
    "relationship.missing", "relationship.modified",
    "relationship.casing_mismatch", "category_annotation.undeclared",
    "fieldparam.blank_row", "fieldparam.cross_level",
    "fieldparam.plot_specific_extra", "fieldparam.schema_modernize",
    "fieldparam.row_missing", "retire.source_view",
    "fieldparam.dangling_selector",
)

# The field-parameter / mapping phase, in the order it is applied after the
# structural phase (tables, measures, columns, relationships).
DERIVED_PHASE_KINDS = (
    "fieldparam.schema_modernize", "fieldparam.row_missing",
    "fieldparam.row_drift", "fieldparam.cross_level",
    "fieldparam.plot_specific_extra", "fieldparam.blank_row",
    "fieldparam.dangling_row", "mapping_category.missing",
    "category_annotation.undeclared",
)

# Deletions: never applied in batch mode or under --yes; they need an
# interactive yes, or --sync together with --confirm-deletions.
DESTRUCTIVE_PREFIXES = ("retire.table", "retire.column", "retire.measure",
                        "retire.mapping_row", "retire.mapping_category",
                        "retire.relationship", "retire.stranded_ref",
                        "purge.", "column.purge_listed")


def is_destructive(kind: str) -> bool:
    return any(kind == p or kind.startswith(p if p.endswith(".") else p + ".")
               or kind == p.rstrip(".") for p in DESTRUCTIVE_PREFIXES) \
        or kind.startswith("purge.")


def is_batch_safe(kind: str) -> bool:
    return any(kind_matches(kind, k) for k in BATCH_SAFE_KINDS)


def _sync_only(kind: str) -> bool:
    from .detect import SYNC_ONLY_KINDS
    return kind in SYNC_ONLY_KINDS


def remediate(fleet: Fleet, findings: list, *, kind=None,
              dry_run: bool = False, sync: bool = False,
              ledger: Optional[Ledger] = None,
              baseline: Optional[dict] = None,
              confirm: Optional[Callable] = None,
              batch_only: bool = False,
              allow_destructive: bool = True,
              backup: bool = False,
              offer_extra_delete: bool = False) -> RunResult:
    """Apply every admitted finding. The Python API is the batch engine: it
    applies what the gates admit without asking. The CLI passes
    `confirm(finding, label, changes) -> (ok, reason)` for the interactive
    walk, `batch_only` for --yes (batch-safe kinds only) and
    `allow_destructive=False` unless deletions were confirmed."""
    ledger = ledger or Ledger(fleet.ledger_path)
    if baseline is None:
        from .baseline import load_baseline
        baseline = load_baseline(fleet)
    kinds = [kind] if isinstance(kind, str) else list(kind or [])
    state = RunState(fleet, baseline)
    result = RunResult()

    def out(f, status, reason="", changes=None):
        result.outcomes.append(Outcome(f, status, reason, changes or []))

    def wanted(f):
        k = f.data.get("for_kind", f.kind) if f.kind == "decision" else f.kind
        return not kinds or any(kind_matches(k, x) for x in kinds)

    for f in findings:
        if f.kind == "decision" and wanted(f):
            result.digest.append({"model": f.model, "kind": f.data["kind"],
                                  "name": f.data["name"],
                                  "report_refs": f.data.get("report_refs"),
                                  "note": f.data["note"]})
    admitted = [f for f in sorted(findings, key=_phase)
                if f.kind != "decision" and wanted(f)]
    overlay = OverlayWatch(fleet)
    for model in dict.fromkeys(f.model for f in admitted):
        overlay._scan(model)
    backups = Backups() if backup and not dry_run else None
    for position, f in enumerate(admitted, 1):
        if f.overridden:
            o = f.override
            out(f, "overridden",
                f"acknowledged by a fleet.yml override ({o.kind}): {o.reason}"
                f" [owner {o.owner or '?'}, added {o.added or '?'}]")
            continue
        if f.advisory and offer_extra_delete and \
                _extra_deletable(fleet, state, f):
            if batch_only or confirm is None:
                out(f, "skipped_destructive",
                    "deleting a model-only object is never done in batch "
                    "mode or under --yes; answer interactively")
                continue
            try:
                label, changes = _apply_extra_delete(fleet, f)
            except (DriftDoctorError, TmdlEditError) as e:
                out(f, "failed", str(e))
                continue
            if dry_run:
                out(f, "would-apply", label, changes)
                continue
            refs = _report_refs(fleet, f)
            label += (f" ({refs} report ref(s) will break)"
                      if refs else "")
            answer = confirm(f, label, changes, (position, len(admitted)))
            if not answer[0]:
                out(f, answer[2] if len(answer) > 2 else "skipped", answer[1])
                continue
            for c in changes:
                write_change(c)
            state.invalidate(f.model)
            ledger.record_remediation(f.kind, f.model, f.ref, label,
                                      files=[str(c.path) for c in changes])
            out(f, "applied", label, changes)
            continue
        if f.advisory:
            out(f, "skipped",
                "advisory — this object exists only in the derived model; "
                "extensions are reported for review, never auto-remediated. "
                "Edit the derived model directly if it is unwanted.")
            continue
        applier = applier_for(f.kind)
        if applier is None:
            out(f, "manual",
                f"kind '{f.kind}' has no automated fix — resolve it by "
                f"editing the model directly")
            continue
        if not fleet.allows(f.kind):
            out(f, "skipped",
                f"kind '{f.kind}' is not in the fleet allowlist — nothing "
                f"cascades by default. Add it under allowlist.kinds in "
                f"fleet.yml to let remediate apply it.")
            continue
        if (f.kind in RETIREMENT_FINDING_KINDS or _sync_only(f.kind)) \
                and not sync:
            out(f, "skipped",
                "retirement and purge items require --sync (they never run "
                "in a default pass) — re-run with --sync to apply this")
            continue
        if f.blocked:
            out(f, "blocked",
                "derived work depends on it: " + "; ".join(f.blocked)
                + ". Decide by hand: remove or rewrite the dependent, or "
                  "keep the object with a `retain` override or a ledger "
                  "revival.")
            continue
        if is_destructive(f.kind) and not allow_destructive:
            out(f, "skipped_destructive",
                "destructive: deletions are never applied under --yes or in "
                "batch mode; answer interactively, or pass --sync with "
                "--confirm-deletions")
            continue
        if batch_only and not is_batch_safe(f.kind):
            out(f, "skipped",
                f"kind '{f.kind}' is not batch-safe: it needs an interactive "
                f"answer")
            continue
        guard = _divergent_guard(fleet, state, f)
        if guard:
            out(f, "skipped_divergent", guard)
            continue
        late = _late_override(fleet, f)
        if late is not None:
            out(f, "skipped_override",
                f"acknowledged by a fleet.yml override ({late.kind}): "
                f"{late.reason} (re-checked at apply time)")
            continue

        try:
            pre = PRECONDITIONS.get(f.kind)
            reason = pre(state, f) if pre else None
            if reason:
                out(f, "auto_skipped_precondition", reason)
                continue
            try:
                label, changes = applier(fleet, f)
            except _Skip as skip:
                out(f, "skipped", str(skip))
                continue
            exempt = _exempt_change(fleet, f, changes)
            touched = overlay.refuses(f, changes)
            if touched:
                out(f, "failed",
                    f"OVERLAY: {touched} exists only in the derived model; "
                    f"kind '{f.kind}' never alters a derived-only file. "
                    f"Nothing was written.")
                continue
            if exempt is not None:
                out(f, "overridden",
                    f"{exempt.path} is exempt from the template cascade "
                    f"(overrides.files): {exempt.reason}")
                continue
            for c in changes:
                if isinstance(c.new, str) and c.path.suffix == ".tmdl":
                    te.assert_tmdl_valid(c.new)   # guard BEFORE any write
            if dry_run:
                state.record(f)
                out(f, "would-apply", label, changes)
                continue
            if confirm is not None:
                answer = confirm(f, label, changes, (position, len(admitted)))
                ok, why = answer[0], answer[1]
                if not ok:
                    out(f, answer[2] if len(answer) > 2 else "skipped", why)
                    continue
            for c in changes:   # in order: a move writes its destination first
                if backups is not None:
                    backups.save(c.path)
                overlay.allow(c.path)
                if c.new is None:
                    if c.path.is_dir():
                        import shutil
                        shutil.rmtree(c.path)
                    elif c.path.exists():
                        c.path.unlink()
                else:
                    write_change(c)
                if c.verify is not None:
                    try:
                        c.verify(c.path)
                    except DriftDoctorError:
                        _restore(c)
                        raise
            state.invalidate(f.model)
            state.record(f)
            if changes:
                ledger.record_remediation(
                    f.kind, f.model, f.ref, label,
                    files=[str(c.path) for c in changes])
            out(f, "applied", label, changes)
            if f.kind.startswith("refresh_policy.") and not (
                    f.kind == "refresh_policy.missing"
                    and f.data.get("windowed")):
                result.digest.append({
                    "model": f.model, "kind": f.kind, "name": f.ref,
                    "report_refs": None,
                    "note": "full refresh owed: the refresh policy of this "
                            "table changed; run a full refresh of its "
                            "archive"})
            for notice in _notices(fleet, f):
                result.notices.append(notice)
            if f.kind in ("column.missing", "measure.missing"):
                table = f.ref[:f.ref.rindex("[")]
                div = state.divergent_tables(f.model)
                if table in div:
                    from .enrichment import notice as enrich
                    result.notices.append(enrich(fleet, baseline, f,
                                                 *div[table]))
        except (DriftDoctorError, TmdlEditError, OSError) as e:
            out(f, "failed", str(e))

    if not dry_run and result.notices:
        from .enrichment import aggregate, merge_registry
        entries = aggregate(result.notices)
        if entries:
            merge_registry(fleet.state_dir, entries)
    if not dry_run:
        for path in overlay.changed():
            result.errors.append(
                f"OVERLAY: the derived-only file {path} changed during the "
                f"run without any fix writing it; the run is reported as "
                f"failed. Check the file and restore it from version "
                f"control if the change is unwanted.")
    if backups is not None:
        if result.count("failed") or result.errors:
            result.backups_kept = backups.kept()
        else:
            result.backups_removed = backups.remove_all()
    return result


def _restore(c: FileChange) -> None:
    """Put a file back as it was before a write whose read-back failed."""
    if c.old is None:
        Path(c.path).unlink(missing_ok=True)
    elif isinstance(c.old, bytes):
        Path(c.path).write_bytes(c.old)
    else:
        Path(c.path).write_text(c.old, encoding="utf-8")


class Backups:
    """``--backup``: one sibling copy (``<name>.bak_<timestamp>``) of each
    file before its first write in the run."""

    def __init__(self):
        from datetime import datetime
        self.stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.made: dict = {}

    def save(self, path: Path) -> None:
        import shutil
        key = Path(path).resolve()
        if key in self.made or not key.is_file():
            return
        copy = key.with_name(f"{key.name}.bak_{self.stamp}")
        shutil.copy2(key, copy)
        self.made[key] = copy

    def kept(self) -> list:
        return [str(p) for p in self.made.values() if p.exists()]

    def remove_all(self) -> int:
        n = 0
        for copy in self.made.values():
            try:
                copy.unlink()
                n += 1
            except FileNotFoundError:
                pass
        return n


# Kinds whose fix may legitimately write a file the template does not have
# (a reference rewrite, a removal, a move between hosts).
OVERLAY_WRITERS = ("measure.renamed", "measure.wrong_host", "retire",
                   "purge", "category_group", "fieldparam.dangling_row",
                   "selector.dangling_branch")


class OverlayWatch:
    """The run-level invariant: derived-only files (no template
    counterpart at the same relative path) are never altered, except by a
    kind that exists to do so. Hashes are taken lazily, per model, before
    the run's first write to that model."""

    def __init__(self, fleet: Fleet):
        self.fleet = fleet
        self.hashes: dict = {}       # path -> sha1 (or None when absent)
        self.models: set = set()
        self.allowed: set = set()

    def _scan(self, model: str) -> None:
        import hashlib
        if model in self.models or model not in self.fleet.models:
            return
        self.models.add(model)
        mdef = self.fleet.model_definition(model)
        tdef = self.fleet.template_definition
        for p in mdef.rglob("*"):
            if p.is_file() and not (tdef / p.relative_to(mdef)).exists():
                self.hashes[p.resolve()] = hashlib.sha1(
                    p.read_bytes()).hexdigest()

    def refuses(self, f: Finding, changes: list) -> str:
        self._scan(f.model)
        if any(kind_matches(f.kind, k) for k in OVERLAY_WRITERS):
            return ""
        for c in changes:
            if Path(c.path).resolve() in self.hashes:
                return str(c.path)
        return ""

    def allow(self, path) -> None:
        self.allowed.add(Path(path).resolve())

    def changed(self) -> list:
        import hashlib
        out = []
        for p, h in self.hashes.items():
            if p in self.allowed:
                continue
            now = hashlib.sha1(p.read_bytes()).hexdigest() if p.exists() \
                else None
            if now != h:
                out.append(str(p))
        return sorted(out)


def _divergent_guard(fleet: Fleet, state, f: Finding) -> str:
    """A modified relationship whose endpoint table reads a different
    source view than the template's is left alone in batch mode."""
    if f.kind == "refresh_policy.missing":
        table = f.data.get("table")
        if table in state.divergent_tables(f.model):
            return (f"'{table}' reads a different source view than the "
                    f"template's; adding its refresh policy is decided by "
                    f"hand")
        return ""
    if f.kind != "relationship.modified":
        return ""
    divergent = state.divergent_tables(f.model)
    ft, _fc, tt, _tc = parse_signature(f.ref)
    hit = [t for t in (ft, tt) if t in divergent]
    if hit:
        return (f"endpoint table(s) {', '.join(hit)} read a different source "
                f"view than the template's; a relationship on a "
                f"source-divergent table is decided by hand")
    return ""


def _extra_deletable(fleet: Fleet, state, f: Finding) -> bool:
    """Which advisory extras the interactive walk may offer to delete."""
    if f.kind not in ("extra.measure", "extra.column", "relationship.extra"):
        return False
    if f.kind == "relationship.extra":
        return True
    table, obj = split_ref(f.ref)
    if f.kind == "extra.measure":
        if obj.startswith("i-"):
            return False
        m = state.model(f.model).measure(table, obj)
        refs = dax_references(m.expression if m else "")
        b_tables = set(state.baseline.get("tables") or {})
        divergent = set(state.divergent_tables(f.model))
        tables = {t for t, _n in refs["qualified"]} | refs["tables"]
        tables &= set(state.model(f.model).tables)
        return not any(t not in b_tables or t in divergent for t in tables)
    from .fieldparams import fieldparam_files
    return table not in fieldparam_files(fleet, fleet.model_definition(f.model))


def _report_refs(fleet: Fleet, f: Finding) -> int:
    if f.kind != "extra.measure":
        return 0
    from .renames import count_report_refs
    return count_report_refs(fleet.models[f.model], split_ref(f.ref)[1]) or 0


def _late_override(fleet: Fleet, f: Finding):
    """Re-check overrides at apply time: a finding carried over from an
    earlier detect must still never write a declared divergence."""
    from .overrides import match_override
    return match_override(fleet, f) if f.model in fleet.models else None


def _notices(fleet: Fleet, f: Finding) -> list:
    return list((f.data or {}).get("notices") or [])


@dataclass
class _Exempt:
    path: str
    reason: str


def _exempt_change(fleet: Fleet, f: Finding, changes: list):
    """The first change touching a file the model declared exempt."""
    if f.model not in fleet.models or not fleet.exempt_patterns(f.model):
        return None
    root = fleet.model_root(f.model)
    for c in changes:
        try:
            rel = Path(c.path).resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        hit = fleet.file_override(f.model, rel)
        if hit is not None:
            return _Exempt(rel, hit.reason)
    return None


MAX_PASSES = 4


def remediate_converge(fleet: Fleet, detect: Callable[[], list], *,
                       max_passes: int = MAX_PASSES, **kw) -> list:
    """Re-detect and re-apply until a pass applies nothing (a fixed point)
    or `max_passes` passes ran. Returns one RunResult per pass. A dry run
    cannot converge (nothing changes on disk), so it is always one pass."""
    results = []
    for _ in range(max_passes):
        res = remediate(fleet, detect(), **kw)
        results.append(res)
        if kw.get("dry_run") or res.count("applied") == 0 or res.count("failed"):
            break
    return results

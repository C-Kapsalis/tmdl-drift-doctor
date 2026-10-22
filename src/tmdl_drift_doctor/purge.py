"""Pristine-category purge (``purge.*``, sync mode only).

A model that never customised an analysis category should carry exactly the
template's content for it. A category counts as CUSTOMISED (KEEP) when the
model both reads one of the category's tables through its own source view
(signal A) and wires that table to a table of its own with a relationship
the template lacks (signal B). Otherwise the category is PURGE and the
model-only content that serves only it goes:

* ``purge.fieldparam_entry``: model-only field-parameter rows of the
  category (one finding per category and table);
* ``purge.measure`` / ``purge.column``: model-only measures and columns that
  only those rows surfaced;
* ``purge.table``: a model-only table every row of which is purged;
* ``purge.zmap_row``: model-only mapping keys of the category;
* ``purge.relationship``: model-only relationships between two template
  tables whose categories are all PURGE.

Protected names (``protected_name_pattern``), override retentions and data
extensions (rows reading tables or columns the template does not have, or a
source-divergent table) are always kept; retentions and calls that need a
person are written to the run digest as ``decision`` items. Every purge is
a deletion: ``--sync`` plus an interactive yes (never ``--yes``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from . import mapping_codec as mc
from . import tmdl_edit as te
from .detect import split_ref
from .tmdl import dax_references, normalize_expression, parse_model

PURGE_KINDS = ("purge.fieldparam_entry", "purge.measure", "purge.column",
               "purge.relationship", "purge.zmap_row", "purge.table")


# ───────────────────────── protected names ─────────────────────────

def protected_pattern(fleet) -> Optional[re.Pattern]:
    pat = fleet.setting("protected_name_pattern")
    return re.compile(pat, re.IGNORECASE) if pat else None


def is_protected(fleet, *names) -> bool:
    pat = protected_pattern(fleet)
    return bool(pat) and any(pat.search(n or "") for n in names)


# ───────────────────────── helpers ─────────────────────────

def _mapping_rows(definition: Path, table: str) -> Optional[list]:
    path = Path(definition) / "tables" / f"{table}.tmdl"
    if not path.is_file():
        return None
    text = te.read_text(path)
    if not mc.is_encoded(text):
        return None
    try:
        rows = mc.extract_rows(text)
    except mc.CodecError:
        return None
    return [r for r in rows if isinstance(r, list) and len(r) >= 3
            and str(r[0]).strip() and str(r[-1]).strip()]


def _encoded_table(fleet) -> Optional[str]:
    for t in fleet.mapping_tables:
        path = fleet.template_definition / "tables" / f"{t}.tmdl"
        if path.is_file() and mc.is_encoded(te.read_text(path)):
            return t
    return None


def _axis_rows(fleet, definition) -> list:
    """[(role, file stem, Row)] of every field-parameter family table."""
    from .fieldparams import family_members, fieldparam_files, read_rows
    files = fieldparam_files(fleet, definition)
    out = []
    for role, levels, variants in family_members(fleet, files):
        for path in list(levels.values()) + list(variants.values()):
            for row in read_rows(path)[0]:
                out.append((role, path.stem, row))
    return out


def _measure_index(model) -> dict:
    return {m.name: m for t in model.tables.values()
            for m in t.measures.values()}


def fact_trace(model, measure: str, seen=None) -> set:
    """Tables a measure reads, following measure references."""
    seen = set() if seen is None else seen
    if measure in seen:
        return set()
    seen.add(measure)
    m = _measure_index(model).get(measure)
    if m is None:
        return set()
    refs = dax_references(m.expression)
    tables = {t for t, _n in refs["qualified"]} | (refs["tables"]
                                                    & set(model.tables))
    measures = set(_measure_index(model))
    for t, n in refs["qualified"]:
        if n in measures and t not in model.tables:
            tables.discard(t)
    for name in refs["bare"]:
        if name in measures:
            tables |= fact_trace(model, name, seen)
    for t, n in refs["qualified"]:
        if n in measures:
            tables |= fact_trace(model, n, seen)
    return {t for t in tables if t in model.tables}


def _row_tables(model, role: str, row, roles=None) -> set:
    """y rows: their NAMEOF table; x rows: the fact tables their measure
    reads (dimension filters inside the DAX do not count)."""
    tgt = row.target()
    if tgt is None:
        return set()
    table, member = tgt
    if role == "dimension":
        return {table} if table else set()
    if roles is None:
        from .roles import Roles
        roles = Roles()
    return {t for t in fact_trace(model, member)
            if roles.is_fact(model.tables[t])}


def _decision(model, kind, name, note, report_refs=None):
    from .detect import decision
    return decision(model, kind, name, note, for_kind=f"purge.{kind}",
                    report_refs=report_refs)


def _f(kind, model, ref, detail, **data):
    from .detect import Finding
    return Finding(kind=kind, model=model, ref=ref, detail=detail, data=data)


# ───────────────────────── detection ─────────────────────────

def purge_findings(name: str, fleet, baseline: dict, model, ledger) -> list:
    conf = fleet.setting("purge") or {}
    if not isinstance(conf, dict) or not conf.get("enabled"):
        return []
    table = _encoded_table(fleet)
    if table is None:
        return []
    t_rows = _mapping_rows(fleet.template_definition, table)
    d_rows = _mapping_rows(model.definition, table)
    if not t_rows or d_rows is None:
        return []
    template = parse_model(fleet.template_definition)
    from .roles import Roles
    roles = Roles(fleet.raw)
    fraction = float(conf.get("infrastructure_fraction") or 0.5)

    # 1. category -> template tables
    t_axis = _axis_rows(fleet, fleet.template_definition)
    key_tables: dict = {}
    for role, _stem, row in t_axis:
        key_tables.setdefault(row.group, set()).update(
            _row_tables(template, role, row, roles))
    t_keys: dict = {}
    for r in t_rows:
        t_keys.setdefault(str(r[0]).strip(), set()).add(str(r[-1]).strip())
    cat_tables = {c: set().union(*(key_tables.get(k, set()) for k in keys))
                  for c, keys in t_keys.items()}
    counts: dict = {}
    for tables in cat_tables.values():
        for t in tables:
            counts[t] = counts.get(t, 0) + 1
    infra = {t for t, n in counts.items() if n >= fraction * len(cat_tables)}
    cat_tables = {c: ts - infra for c, ts in cat_tables.items()}

    # 2-3. shared categories and their verdict
    d_keys: dict = {}
    key_owners: dict = {}
    for r in d_rows:
        cat, key = str(r[0]).strip(), str(r[-1]).strip()
        d_keys.setdefault(cat, set()).add(key)
        key_owners.setdefault(key, set()).add(cat)
    shared = {c for c in d_keys if cat_tables.get(c)}
    from .sources import divergent_tables
    divergent = set(divergent_tables(fleet, baseline, name, model))
    b_tables = set(baseline.get("tables") or {})
    b_rels = set(baseline.get("relationships") or {})
    own_rels = [rel for sig, rel in model.relationships.items()
                if sig not in b_rels]
    coupled: set = set()          # template tables wired to model tables
    for rel in own_rels:
        if is_protected(fleet, rel.from_table, rel.to_table):
            continue
        for a, b in ((rel.from_table, rel.to_table),
                     (rel.to_table, rel.from_table)):
            if a in b_tables and b not in b_tables:
                coupled.add(a)
    verdict = {}
    for c in shared:
        a = bool(cat_tables[c] & divergent)
        b = bool(cat_tables[c] & coupled)
        verdict[c] = "KEEP" if (a and b) else "PURGE"

    # 4. model-only axis entries and their disposition
    t_set: dict = {}
    from .fieldparams import membership_key
    for _role, _stem, row in t_axis:
        t_set.setdefault(row.group, set()).add((row.key, membership_key(row)))
    declared = {(d["category"], d["entry"]) for d in
                (fleet.sections.get(name) or {}).get("fieldparam_entries", ())}
    t_measures = _measure_index(template)
    d_measures = _measure_index(model)
    out, purged_rows = [], []
    all_rows = _axis_rows(fleet, model.definition)
    for role, stem, row in all_rows:
        if (row.key, membership_key(row)) in t_set.get(row.group, set()):
            continue
        tgt = row.target() or ("", "")
        tables = _row_tables(model, role, row, roles)
        if is_protected(fleet, row.display, tgt[0], tgt[1], row.group):
            continue
        if (row.group, row.display) in declared:
            out.append(_decision(name, "fieldparam_entry",
                                 f"{stem}: {row.display}",
                                 "retained by client override "
                                 "(fieldparam_entries)"))
            continue
        if _data_extension(role, tgt, tables, model, b_tables, baseline,
                           divergent, coupled, d_measures, t_measures):
            continue
        if role == "metric" and tgt[1] in d_measures \
                and tgt[1] not in t_measures and any(
                    normalize_expression(m.expression) == normalize_expression(
                        d_measures[tgt[1]].expression)
                    for m in t_measures.values()):
            continue                     # an alias: owned by the alias rule
        owners = key_owners.get(row.group, set())
        if not owners or any(o not in shared for o in owners):
            continue
        if all(verdict[o] == "KEEP" for o in owners):
            out.append(_decision(
                name, "fieldparam_entry", f"{stem}: {row.display}",
                "needs-decision: a model-only row in a customised category"))
            continue
        cat = sorted(o for o in owners if verdict[o] == "PURGE")[0]
        purged_rows.append((cat, role, stem, row, tables))

    by_item: dict = {}
    for cat, role, stem, row, _t in purged_rows:
        by_item.setdefault((cat, stem), []).append(row)
    for (cat, stem), rows in sorted(by_item.items()):
        out.append(_f("purge.fieldparam_entry", name, f"{cat} / {stem}",
                      f"category '{cat}' is not customised in '{name}': its "
                      f"model-only row(s) "
                      + ", ".join(r.display for r in rows)
                      + f" in '{stem}' go", table=stem, category=cat,
                      rows=[[r.display, r.ref] for r in rows]))

    out += _measure_and_column_purges(name, fleet, model, template,
                                       purged_rows, ledger)
    out += _zmap_purges(name, fleet, table, d_keys, t_keys, verdict)
    out += _relationship_purges(name, fleet, model, b_tables, b_rels,
                                cat_tables, verdict, ledger)
    out += _table_purges(name, fleet, model, b_tables, b_rels, ledger, out,
                         purged_rows, all_rows)
    out += _ifilter_regen(name, model, out)
    return out


def _ifilter_regen(name: str, model, findings: list) -> list:
    """``purge.ifilter_regen``: the applied-filters measure references an
    object this run purges, so the measure must be rebuilt afterwards."""
    from .applied_filters import measure_expression, referenced_removed
    from .applied_filters import regen_finding
    _host, expression = measure_expression(model)
    if not expression:
        return []
    return regen_finding(
        "purge.ifilter_regen", name,
        referenced_removed(expression, findings, "purge.table",
                           "purge.measure", "purge.column"),
        "purges", "post-purge")


def _table_purges(name, fleet, model, b_tables, b_rels, ledger, findings,
                  purged_rows, all_rows) -> list:
    """``purge.table``: a client-only table that only the purge removes.

    A table the template does not have survives only because of the content
    that reads it. When every measure that reads it goes, and no
    relationship that survives wires it, the table goes with them: it is
    left with nothing to serve. A table that still carries a surviving
    measure, a surviving field-parameter row, or a surviving relationship
    is kept.
    """
    out = []
    retired = {ref for (k, ref) in ledger.active_retirements(name)
               if k == "table"}
    kept_tables = {d.get("object") for d in
                   (fleet.sections.get(name) or {}).get("tables", ())
                   if d.get("kind") in ("retain", "absent")}
    purged_rels = {f.ref for f in findings if f.kind == "purge.relationship"}
    purged_measures = {f.data.get("measure") for f in findings
                       if f.kind == "purge.measure"}
    from .tmdl import dax_references
    # the tables a surviving measure reads
    surviving_tables: set = set()
    for t, table in model.tables.items():
        for measure in table.measures.values():
            if measure.name in purged_measures:
                continue
            refs = dax_references(measure.expression)
            for tn, _n in refs["qualified"]:
                if tn in model.tables:
                    surviving_tables.add(tn)
    # the tables a surviving field-parameter row still surfaces
    purged_keys = {(stem, r.display, r.ref) for _c, _r, stem, r, _t in
                   purged_rows}
    for role, stem, row in all_rows:
        if (stem, row.display, row.ref) in purged_keys:
            continue
        tgt = row.target() or ("", "")
        if tgt[0]:
            surviving_tables.add(tgt[0])
    for tname, table in sorted(model.tables.items()):
        if tname in b_tables or tname in retired or tname in kept_tables:
            continue
        if is_protected(fleet, tname):
            continue
        # a measure of the table's own that serves something else keeps it
        # alive; a measure that reads only the table goes with it
        own_serving = False
        for m in table.measures.values():
            if m.name in purged_measures:
                continue
            refs = dax_references(m.expression)
            reads = {t for t, _n in refs["qualified"]} | \
                (refs["tables"] & set(model.tables))
            if reads - {tname}:
                own_serving = True
                break
        if own_serving:
            continue
        if tname in surviving_tables:
            # a surviving measure or row still serves it
            continue
        # a relationship that survives keeps the table alive
        own = [sig for sig, rel in model.relationships.items()
               if sig not in b_rels and sig not in purged_rels
               and tname in (rel.from_table, rel.to_table)]
        if own:
            continue
        # the table's own model-only relationships go with it
        going = [sig for sig, rel in model.relationships.items()
                 if sig not in b_rels and tname in (rel.from_table,
                                                   rel.to_table)]
        out.append(_f("purge.table", name, tname,
                      f"model-only table '{tname}' serves only uncustomised "
                      f"categories and goes",
                      relationships=sorted(going)))
    return out


def _data_extension(role, tgt, tables, model, b_tables, baseline, divergent,
                    coupled, d_measures, t_measures) -> bool:
    if role == "dimension":
        table, col = tgt
        if table and table not in b_tables:
            return True
        if table in divergent:
            cols = ((baseline.get("tables") or {}).get(table) or {}).get(
                "columns") or {}
            if col not in cols:
                return True
        return bool(tables & coupled and tables & divergent) or \
            bool(table in coupled and divergent)
    member = tgt[1]
    if member in d_measures and member not in t_measures:
        if tables & (divergent | coupled):
            return True
    if any(t not in b_tables for t in tables):
        return True
    return False


def _measure_and_column_purges(name, fleet, model, template, purged_rows,
                               ledger) -> list:
    from .fieldparams import read_rows
    out = []
    d_measures = _measure_index(model)
    t_measures = _measure_index(template)
    purged_keys = {(stem, r.display, r.ref) for _c, _r, stem, r, _t in
                   purged_rows}
    all_rows = _axis_rows(fleet, model.definition)
    surviving = [(role, stem, r) for role, stem, r in all_rows
                 if (stem, r.display, r.ref) not in purged_keys]
    retired = {ref.rsplit("[", 1)[-1].rstrip("]") for (k, ref) in
               ledger.active_retirements(name) if k == "measure"}
    kept_measures = {o.object.rsplit("[", 1)[-1].rstrip("]")
                     for o in fleet.overrides.get(name) or ()
                     if o.kind in ("extension", "retain", "expression")}

    # measures surfaced only by purged metric rows
    cands = set()
    for _c, role, _s, r, _t in purged_rows:
        tgt = r.target()
        if role == "metric" and tgt and tgt[1] in d_measures \
                and tgt[1] not in t_measures:
            cands.add(tgt[1])
    for role, _s, r in surviving:
        tgt = r.target()
        if tgt:
            cands.discard(tgt[1])
    cands -= retired | kept_measures
    cands = {c for c in cands if not is_protected(fleet, c)}
    # never purge a measure a surviving measure references (fixed point)
    while True:
        users = set()
        for m in d_measures.values():
            if m.name in cands:
                continue
            refs = dax_references(m.expression)
            users |= cands & (refs["bare"] | {n for _t, n in
                                              refs["qualified"]})
        if not users:
            break
        for u in sorted(users):
            out.append(_decision(name, "measure", u,
                                 "needs-decision: a surviving measure still "
                                 "references it, so it is kept"))
        cands -= users
    from .renames import count_report_refs
    for mname in sorted(cands):
        m = d_measures[mname]
        refs = count_report_refs(model.definition.parent, mname)
        out.append(_f("purge.measure", name, f"{m.table}[{mname}]",
                      f"model-only measure '{mname}' served only purged "
                      f"field-parameter rows", table=m.table,
                      measure=mname, report_refs=refs))
        if refs:
            out.append(_decision(name, "measure", mname,
                                 f"{refs} report reference(s) bind it; report "
                                 f"bindings are never rewritten",
                                 report_refs=refs))

    # columns surfaced only by purged dimension rows
    t_cols = {(t.name, c) for t in template.tables.values() for c in t.columns}
    col_cands = set()
    for _c, role, _s, r, _t in purged_rows:
        tgt = r.target()
        if role == "dimension" and tgt and tgt[0] in template.tables \
                and (tgt[0], tgt[1]) not in t_cols \
                and model.column(tgt[0], tgt[1]) is not None:
            col_cands.add(tgt)
    for role, _s, r in surviving:
        if role == "dimension" and r.target():
            col_cands.discard(r.target())
    for table, col in sorted(col_cands):
        ref = f"{table}[{col}]"
        if is_protected(fleet, table, col):
            continue
        users = [f"{m.table}[{m.name}]" for m in d_measures.values()
                 if m.name not in cands and not m.name.startswith("i-")
                 and (table, col) in dax_references(m.expression)["qualified"]]
        f = _f("purge.column", name, ref,
               f"model-only column {ref} served only purged field-parameter "
               f"rows", table=table, column=col)
        if users:
            f.data["blocked"] = [f"measure(s) still read it: "
                                 f"{', '.join(sorted(users))}"]
        out.append(f)
    return out


def _zmap_purges(name, fleet, table, d_keys, t_keys, verdict) -> list:
    out = []
    retained = {(d["category"], d["key"]) for d in
                (fleet.sections.get(name) or {}).get("mapping_rows", ())}
    for cat, v in sorted(verdict.items()):
        if v != "PURGE":
            continue
        extra = sorted(k for k in d_keys.get(cat, set()) - t_keys.get(cat, set())
                       if not is_protected(fleet, k, cat))
        keys = []
        for k in extra:
            if (cat, k) in retained:
                out.append(_decision(name, "zmap_row", f"{cat}/{k}",
                                     "retained by client override "
                                     "(mapping_rows)"))
            else:
                keys.append(k)
        if keys:
            out.append(_f("purge.zmap_row", name, f"{table}/{cat}",
                          f"model-only mapping key(s) {', '.join(keys)} of "
                          f"the uncustomised category '{cat}' go",
                          table=table, category=cat, keys=keys, encoded=True))
    return out


def _relationship_purges(name, fleet, model, b_tables, b_rels, cat_tables,
                         verdict, ledger) -> list:
    out = []
    owners_of: dict = {}
    for c, ts in cat_tables.items():
        for t in ts:
            owners_of.setdefault(t, set()).add(c)
    retired = {ref for (k, ref) in ledger.active_retirements(name)
               if k == "relationship"}
    kept = {d["signature"] for d in
            (fleet.sections.get(name) or {}).get("relationships", ())}
    for sig, rel in sorted(model.relationships.items()):
        if sig in b_rels or sig in retired or sig in kept:
            continue
        ends = (rel.from_table, rel.to_table)
        if not all(t in b_tables for t in ends) or is_protected(fleet, *ends):
            continue
        owners = [owners_of.get(t, set()) for t in ends]
        if all(o and all(verdict.get(c) == "PURGE" for c in o)
               for o in owners):
            out.append(_f("purge.relationship", name, sig,
                          f"model-only relationship {sig} between template "
                          f"tables of uncustomised categories goes"))
    return out


# ───────────────────────── fixes ─────────────────────────

def apply_purge(fleet, f) -> tuple:
    from . import tmdl_edit as te
    from .remediate import FileChange
    defn = fleet.model_definition(f.model)
    if f.kind == "purge.fieldparam_entry":
        from .fieldparams import read_rows, rewrite_rows_text
        path = defn / "tables" / f"{f.data['table']}.tmdl"
        rows, arity = read_rows(path)
        gone = {(d, r) for d, r in f.data["rows"]}
        keep = [r for r in rows if (r.display, r.ref) not in gone]
        if len(keep) == len(rows):
            return ("rows already removed", [])
        old = te.read_text(path)
        return (f"remove {len(rows) - len(keep)} model-only row(s) from "
                f"'{f.data['table']}'",
                [FileChange(path, old, rewrite_rows_text(old, keep, arity,
                                                         path=path))])
    if f.kind in ("purge.measure", "purge.column", "column.purge_listed"):
        keyword = "measure" if f.kind == "purge.measure" else "column"
        obj = f.data["measure" if keyword == "measure" else "column"]
        path = defn / "tables" / f"{f.data['table']}.tmdl"
        if not path.is_file():
            return ("table file already gone", [])
        old = te.read_text(path)
        new = te.remove_block(old, keyword, obj)
        if new is None:
            if f.kind == "column.purge_listed":
                from . import DriftDoctorError
                raise DriftDoctorError(f"column {f.ref} is no longer in the "
                                       f"model; re-run detect")
            return (f"{keyword} already removed", [])
        return (f"remove model-only {keyword} {f.ref}",
                [FileChange(path, old, new)])
    if f.kind == "purge.zmap_row":
        from .mappings import apply_retire_rows
        return apply_retire_rows(fleet, f)
    if f.kind == "purge.relationship":
        path = defn / "relationships.tmdl"
        old = te.read_text(path)
        new = te.remove_relationship_block(old, f.ref)
        if new is None:
            return ("relationship already removed", [])
        return (f"remove model-only relationship {f.ref}",
                [FileChange(path, old, new)])
    if f.kind == "purge.table":
        path = defn / "tables" / f"{f.ref}.tmdl"
        changes = []
        if path.is_file():
            changes.append(FileChange(path, te.read_text(path),
                                      None))
        mf = defn / "model.tmdl"
        old = te.read_text(mf)
        new = te.deregister_table(old, f.ref)
        if new != old:
            changes.append(FileChange(mf, old, new))
        # the table's own relationships go with it
        rel_path = defn / "relationships.tmdl"
        if rel_path.is_file():
            rel_old = te.read_text(rel_path)
            rel_new = rel_old
            for sig in f.data.get("relationships") or ():
                rel_new = te.remove_relationship_block(rel_new, sig) or rel_new
            if rel_new != rel_old:
                changes.append(FileChange(rel_path, rel_old, rel_new))
        return (f"remove model-only table '{f.ref}'", changes)
    raise ValueError(f.kind)


# ───────────────────────── configured column purge ─────────────────────────

def column_purge_findings(name: str, fleet, findings: list) -> list:
    """``column.purge_listed`` (sync mode): a model-only column on a shared
    table whose name is on the configured purge list."""
    conf = fleet.setting("column_purge") or {}
    names = set(conf.get("names") or []) if isinstance(conf, dict) else set()
    if not names:
        return []
    exceptions = set(conf.get("exceptions") or [])
    from .fieldparams import fieldparam_files
    fp = set(fieldparam_files(fleet, fleet.model_definition(name)))
    out = []
    for f in findings:
        if f.model != name or f.kind != "extra.column":
            continue
        table, col = split_ref(f.ref)
        if col not in names or f.ref in exceptions or table in fp:
            continue
        out.append(_f("column.purge_listed", name, f.ref,
                      f"column {f.ref} is on the configured purge list; "
                      f"remove it, then clean the source view too",
                      table=table, column=col,
                      notices=[{"kind": "source_cleanup", "model": name,
                                "table": table, "column": col,
                                "message": f"{name}: drop column {col} from "
                                           f"the source of '{table}'"}]))
    return out

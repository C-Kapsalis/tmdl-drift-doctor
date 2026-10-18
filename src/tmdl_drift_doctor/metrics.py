"""Template metrics and the count floors derived from them.

``capture`` stores a handful of counts about the template next to its
object snapshot (baseline.json ``metrics``). ``detect`` recomputes the same
counts on every derived model and reports ``model.count_below_template``
when a model holds fewer of something than the template does: fewer
measures, DAX expressions, NAMEOF references, relationships, many-to-one
relationships, axis-family tables, family-table columns or family-table
references. There are no upper bounds; a model may always carry more.

Count floors are a backstop for the per-object kinds: they fail whatever
``allowlist.kinds`` says, because a model that silently lost a whole axis
table must never pass a gate.
"""

from __future__ import annotations

import re

from .roles import Roles
from .tmdl import Model, dax_references, strip_comments

COUNT_KIND = "model.count_below_template"
DEFAULT_FAMILY_PREFIXES = ("x-Selector", "x-Plot Specific", "y-Selector",
                           "y-Plot Specific")
METRIC_NAMES = ("measures", "dax_expressions", "nameof_references",
                "relationships", "many_to_one", "family_tables",
                "table_columns", "family_table_refs")

_NAMEOF = re.compile(
    r"NAMEOF\s*\(\s*(?:'((?:[^']|'')+)'|([A-Za-z_][\w ]*?))?\s*\[([^\]]+)\]\s*\)",
    re.IGNORECASE)


def nameof_targets(expr: str) -> list:
    """``Table[col]`` (or ``[name]``) of every NAMEOF call in `expr`,
    comments excluded."""
    out = []
    for m in _NAMEOF.finditer(strip_comments(expr or "")):
        table = (m.group(1) or "").replace("''", "'") or (m.group(2) or "")
        out.append(f"{table.strip()}[{m.group(3)}]")
    return out


def family_prefixes(config: dict) -> tuple:
    cf = (config or {}).get("count_floor") or {}
    return tuple(cf.get("family_prefixes") or DEFAULT_FAMILY_PREFIXES)


def family_of(table: str, prefixes) -> str | None:
    for p in prefixes:
        if table == p or table.startswith(p + " "):
            return p
    return None


def compute_metrics(model: Model, config: dict | None = None) -> dict:
    roles = Roles(config)
    prefixes = family_prefixes(config)
    classes = roles.measure_classes(model)
    measures = [m for t in model.tables.values() for m in t.measures.values()]
    calc_cols = [c for t in model.tables.values() for c in t.columns.values()
                 if c.expression.strip()]
    targets = [ref for m in measures for ref in nameof_targets(m.expression)]
    families = {p: [] for p in prefixes}
    for tname in model.tables:
        fam = family_of(tname, prefixes)
        if fam:
            families[fam].append(tname)
    fam_tables = {t for ts in families.values() for t in ts}
    refs = {t: 0 for t in fam_tables}
    for m in measures:
        for table in {t for t, _c in dax_references(m.expression)["qualified"]}:
            if table in refs:
                refs[table] += 1
    rels = list(model.relationships.values())
    return {
        "measures": len(measures),
        "f_measures": len(classes["filter"]),
        "user_facing_measures": len(classes["user_facing"]),
        "helper_measures": len(classes["helper"]),
        "fact_tables": roles.fact_tables(model),
        "dax_expressions": sum(1 for m in measures if m.expression.strip())
        + len(calc_cols),
        "nameof_references": len(targets),
        "nameof_targets": sorted(targets),
        "relationships": len(rels),
        "many_to_one": sum(1 for r in rels
                           if r.properties.get("fromCardinality") == "many"
                           and r.properties.get("toCardinality") == "one"),
        "family_tables": {p: sorted(ts) for p, ts in families.items()},
        "table_column_counts": {t: len(model.tables[t].columns)
                                for t in sorted(model.tables)},
        "table_columns": {t: sorted(model.tables[t].columns)
                          for t in sorted(model.tables)},
        "family_table_refs": dict(sorted(refs.items())),
        "family_refs_total": {p: sum(refs[t] for t in ts)
                              for p, ts in families.items()},
    }


def _absent_columns(overrides) -> set:
    out = set()
    for o in overrides or ():
        if o.kind == "absent" and o.object.endswith("]") and "[" in o.object:
            out.add(o.object)
    return out


def count_floor_findings(name: str, model: Model, metrics: dict,
                         config: dict, overrides) -> list:
    """[(ref, data, detail)] for every count below its template floor."""
    if not metrics:
        return []
    disabled = {k for k, v in ((config or {}).get("count_floor") or {})
                .get("metrics", {}).items() if v is False}
    mine = compute_metrics(model, config)
    out = []

    def floor(metric, client, template, adjustment=0, **extra):
        target = template - adjustment
        if metric in disabled or client >= target:
            return
        data = {"metric": metric, "client": client, "template": template,
                "adjustment": adjustment, "floor": target, **extra}
        where = f" on '{extra['table']}'" if extra.get("table") else ""
        out.append((data, f"'{name}' has {client} {metric.replace('_', ' ')}"
                          f"{where}, below the template floor of {target}"))

    for metric in ("measures", "dax_expressions", "relationships",
                   "many_to_one"):
        if metric in metrics:
            floor(metric, mine[metric], metrics[metric])
    if "nameof_references" in metrics:
        absent = _absent_columns(overrides)
        adj = sum(1 for t in metrics.get("nameof_targets") or () if t in absent)
        floor("nameof_references", mine["nameof_references"],
              metrics["nameof_references"], adj)

    t_fams = metrics.get("family_tables") or {}
    t_tables = {t for ts in t_fams.values() for t in ts}
    present = set(model.tables)
    if "family_tables" not in disabled and t_tables:
        missing = sorted(t_tables - present)
        if missing:
            data = {"metric": "family_tables",
                    "client": len(t_tables) - len(missing),
                    "template": len(t_tables), "adjustment": 0,
                    "floor": len(t_tables), "missing": missing}
            out.append((data, f"'{name}' lacks template axis-family "
                              f"table(s): {', '.join(missing)}"))
    t_cols = metrics.get("table_column_counts") or {}
    for t in sorted(t_tables & present):
        if t in t_cols:
            floor("table_columns", len(model.tables[t].columns), t_cols[t],
                  table=t)

    if "family_table_refs" not in disabled:
        t_refs = metrics.get("family_table_refs") or {}
        for prefix, total in (metrics.get("family_refs_total") or {}).items():
            client_total = mine["family_refs_total"].get(prefix, 0)
            if client_total < total:
                data = {"metric": "family_table_refs", "client": client_total,
                        "template": total, "adjustment": 0, "floor": total,
                        "prefix": prefix}
                out.append((data, f"'{name}' references its {prefix} tables "
                                  f"{client_total} time(s), below the "
                                  f"template's {total}"))
        for t, n in sorted(t_refs.items()):
            # an absent family table holds no references at all
            floor("family_table_refs", mine["family_table_refs"].get(t, 0), n,
                  table=t)
        for t, n in sorted(mine["family_table_refs"].items()):
            if t not in t_tables and n == 0:
                data = {"metric": "family_table_refs", "client": 0,
                        "template": None, "adjustment": 0, "floor": None,
                        "table": t}
                out.append((data, f"'{name}' carries the axis-family table "
                                  f"'{t}' but no measure references it: it "
                                  f"appears unused"))
    return out

"""Source identity: which warehouse view a table's partition reads.

A table's source view is recognised in three spellings of a partition
source, with the helper function and the database parameter taken from
fleet.yml ``source_identity`` (defaults ``source_filter_dates`` and
``v-db``):

* bare call    ``source_filter_dates("ads_n", ...)``
* quoted call  ``#"source_filter_dates"("ads_n", ...)``
* inlined SQL  ``... & #"v-db" & "." & "ads_n" & ...`` (incremental-refresh
  partitions fold the view into the query text)

A partition in none of these shapes has no recognisable view, and a table
without one is never judged source-divergent.
"""

from __future__ import annotations

import re

DEFAULT_IDENTITY = {"function": "source_filter_dates", "db_parameter": "v-db"}


def identity(config: dict | None) -> dict:
    raw = (config or {}).get("source_identity") or {}
    return {"function": raw.get("function") or DEFAULT_IDENTITY["function"],
            "db_parameter": raw.get("db_parameter")
            or DEFAULT_IDENTITY["db_parameter"]}


def _patterns(ident: dict) -> list:
    fn = re.escape(ident["function"])
    db = re.escape(ident["db_parameter"])
    return [
        re.compile(r'(?:#"' + fn + r'"|(?<![\w"])' + fn + r')\s*\(\s*"([^"]+)"'),
        re.compile(r'#"' + db + r'"\s*&\s*"\."\s*&\s*"([^"]+)"'),
    ]


def views_in(text: str, ident: dict) -> list:
    """Every view name the text reads, in order of appearance."""
    hits = []
    for pat in _patterns(ident):
        hits.extend((m.start(1), m.group(1)) for m in pat.finditer(text or ""))
    return [v for _pos, v in sorted(hits)]


def table_view(table, ident: dict):
    """The view a table's Power Query partition reads, or None."""
    for part in table.partitions.values():
        found = views_in(part.source, ident)
        if found:
            return found[0]
    return None


def table_source_views(model, ident: dict) -> dict:
    out = {}
    for name in sorted(model.tables):
        view = table_view(model.tables[name], ident)
        if view:
            out[name] = view
    return out


def rewrite_view(text: str, old: str, new: str, ident: dict) -> tuple:
    """(new text, count): every recognised read of view `old` repointed to
    `new`, in every spelling, the surrounding text untouched."""
    count = 0
    for pat in _patterns(ident):
        def swap(m):
            nonlocal count
            if m.group(1) != old:
                return m.group(0)
            count += 1
            a, b = m.span(1)
            base = m.start(0)
            g = m.group(0)
            return g[:a - base] + new + g[b - base:]
        text = pat.sub(swap, text)
    return text, count


def divergence_on(config: dict | None) -> bool:
    """Divergence is detected only when fleet.yml has a `source_identity`
    block (declared `source_divergent` tables always count)."""
    return bool((config or {}).get("source_identity"))


def divergent_tables(fleet, baseline: dict, name: str, model) -> dict:
    """{table: (template view, model view)} for the tables of `model` that
    read a different source view than the template's, plus the tables the
    fleet entry declares divergent."""
    out = {}
    if divergence_on(fleet.raw):
        t_views = baseline.get("table_source_views") or {}
        mine = table_source_views(model, identity(fleet.raw))
        out = {t: (t_views[t], v) for t, v in mine.items()
               if t in t_views and t_views[t] != v}
    else:
        mine = {}
    t_views = baseline.get("table_source_views") or {}
    for t in fleet.source_divergent.get(name, ()):
        out.setdefault(t, (t_views.get(t), mine.get(t)))
    return dict(sorted(out.items()))


def source_view_changes(prev: dict, fresh: dict) -> list:
    """[(table, old view, new view)] for tables both snapshots read with
    different views."""
    a = prev.get("table_source_views") or {}
    b = fresh.get("table_source_views") or {}
    return [(t, a[t], b[t]) for t in sorted(set(a) & set(b)) if a[t] != b[t]]


def view_chain(ledger) -> dict:
    """{table: [(old, new), ...]} of the ledger's source-view changes, in
    the order they were recorded."""
    out: dict = {}
    for e in ledger.entries():
        if e.get("kind") == "source_view_change" and e.get("table"):
            out.setdefault(e["table"], []).append((e.get("old_view"),
                                                   e.get("new_view")))
    return out


def source_view_findings(name: str, fleet, model, ledger) -> list:
    """``retire.source_view``: a table the template re-pointed that this
    model still reads through the old view. A model on a view it declares
    (``source_views`` override) is retained; one on some other view is left
    for a person. Both are ``decision`` items (the run digest), never a
    fix."""
    from .detect import Finding, decision
    chain = view_chain(ledger)
    if not chain:
        return []
    ident = identity(fleet.raw)
    declared = {d["table"]: d["view"] for d in
                (fleet.sections.get(name) or {}).get("source_views", ())}
    out = []
    for table, steps in sorted(chain.items()):
        t = model.tables.get(table)
        if t is None:
            continue
        view = table_view(t, ident)
        final = steps[-1][1]
        if view is None or view == final:
            continue
        step = next(((o, n) for o, n in steps if o == view), None)
        if step is not None and table not in declared:
            out.append(Finding(
                kind="retire.source_view", model=name,
                ref=f"{table}: {step[0]} -> {step[1]}",
                detail=(f"the template re-pointed '{table}' from view "
                        f"'{step[0]}' to '{step[1]}' (ledger) and '{name}' "
                        f"still reads the old view"),
                data={"table": table, "old_view": step[0],
                      "new_view": step[1]}))
            continue
        if any(n == view for _o, n in steps):
            continue                     # an intermediate view: a later step
        if table in declared:
            note = (f"retained by the model's source_views override "
                    f"(declared view '{declared[table]}'); the template moved "
                    f"to '{final}'")
        else:
            note = (f"needs-decision: '{name}' reads '{view}', neither the "
                    f"template's old nor its new view ('{final}'); left "
                    f"untouched")
        out.append(decision(name, "source_view", table, note,
                            for_kind="retire.source_view"))
    return out

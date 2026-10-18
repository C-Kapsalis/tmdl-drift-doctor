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
        re.compile(r'(?:#")?(?<![\w"])' + fn + r'"?\s*\(\s*"([^"]+)"'),
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

"""Analysis groups: the group graph, the keep/drop plan and the layered drop.

A *group* (a subject area such as an analysis category) owns *field groups*
(the ids that tie field-parameter rows to it, read from the mapping table),
which surface measures, which read fact tables. Dropping a group by deleting
rows by name breaks surviving groups whenever an object is shared, so every
drop starts from a read-only graph of the whole model and a plan that splits
each candidate into *exclusive* (safe to remove) and *shared* (kept, with
the survivors that keep it).

The drop runs in layers, each usable alone (``groups drop --layer``):
selector branches, measures, selector rows, annotation, gates, bookmarks,
tables (dead fact tables, re-derived after the measure purge) and mapping
rows LAST. Every layer is a dry run unless ``--apply`` is given, and every
TMDL write passes the write guard.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import DriftDoctorError, TmdlEditError
from . import mapping_codec as mc
from . import tmdl_edit as te

DEFAULTS = {
    "source": "mapping_table",
    "mapping_table": "z-Map Dimension-Measure Groupings",
    "group_column": 0,
    "field_group_column": 2,
    "annotation_key": "Category",
    "annotation_prefix": "Category_",
    "selector_tables": ["x-Selector *"],
    "dimension_selector_tables": ["y-Selector *"],
    "row_drop_tables": ["x-Selector *", "x-Plot Specific *", "y-Selector *",
                        "y-Plot Specific *"],
    "non_attributing_field_groups": ["Blank Measure_x", "Blank Dimension_y",
                                     "Date_y", "Date_day_y", "Date_week_y"],
    "sensor_functions": ["ISFILTERED", "ISINSCOPE", "ISEMPTY",
                         "ISCROSSFILTERED", "HASONEVALUE", "HASONEFILTER"],
}


def conf(fleet) -> dict:
    out = dict(DEFAULTS)
    raw = fleet.setting("grouping") or {} if fleet is not None else {}
    if isinstance(raw, dict):
        out.update({k: v for k, v in raw.items() if v is not None})
    if fleet is not None and not (raw or {}).get("mapping_table") \
            and fleet.mapping_tables:
        out["mapping_table"] = fleet.mapping_tables[0]
    return out


def _sub(fleet, key, defaults) -> dict:
    raw = (fleet.setting(key) or {}) if fleet is not None else {}
    out = dict(defaults)
    if isinstance(raw, dict):
        out.update(raw)
    return out


def _tables_dir(definition) -> Path:
    return Path(definition) / "tables"


def _files(definition, globs) -> list:
    out, seen = [], set()
    for g in globs:
        for f in sorted(_tables_dir(definition).glob("*.tmdl")):
            if fnmatch.fnmatchcase(f.stem, g) and f not in seen:
                seen.add(f)
                out.append(f)
    return out


# ───────────────────────── mapping rows ─────────────────────────

def mapping_rows(definition, c) -> Optional[list]:
    """The mapping table's rows (encoded or DATATABLE), or None when the
    table is missing."""
    path = _tables_dir(definition) / f"{c['mapping_table']}.tmdl"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    if mc.is_encoded(text):
        return mc.extract_rows(text)
    rows = []
    for m in re.finditer(r"\{\s*(\"(?:[^\"]|\"\")*\"(?:\s*,\s*\"(?:[^\"]|\"\")*\")*)"
                         r"\s*\}", text):
        rows.append([s[1:-1].replace('""', '"') for s in
                     re.findall(r'"(?:[^"]|"")*"', m.group(1))])
    return rows


def group_relation(rows, c) -> dict:
    """{group: set(field group ids)} from mapping rows (trimmed, exact)."""
    gi, fi = int(c["group_column"]), int(c["field_group_column"])
    out: dict = {}
    for r in rows or []:
        if not isinstance(r, list) or len(r) < 3:
            continue
        try:
            g, fg = str(r[gi]).strip(), str(r[fi]).strip()
        except IndexError:
            continue
        if g and fg:
            out.setdefault(g, set()).add(fg)
    return out


# ───────────────────────── graph ─────────────────────────

def _strip_comments(text: str) -> str:
    from .tmdl import strip_comments
    return strip_comments(text)


def _mentions(expr: str, table: str) -> bool:
    if f"'{table}'" in expr or f"[{table}]" in expr:
        return True
    return " " not in table and f"{table}[" in expr


def _sensor_spans(text: str, functions) -> list:
    spans = []
    pat = re.compile(r"\b(?:" + "|".join(map(re.escape, functions))
                     + r")\s*\(", re.IGNORECASE)
    for m in pat.finditer(text):
        end = _close_paren(text, m.end() - 1)
        if end is not None:
            spans.append((m.start(), end))
    return spans


def _close_paren(text: str, open_i: int) -> Optional[int]:
    depth, i = 0, open_i
    while i < len(text):
        ch = text[i]
        if ch in "'\"":
            j = i + 1
            while j < len(text):
                if text[j] == ch:
                    if text[j + 1:j + 2] == ch:
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def table_refs(expr: str, table: str, functions) -> tuple:
    """(data references, sensor references) of `table` in a measure
    expression, comments removed."""
    text = _strip_comments(expr or "")
    pats = [re.escape(f"'{table.replace(chr(39), chr(39) * 2)}'")]
    if re.fullmatch(r"[A-Za-z_]\w*", table):
        pats.append(r"(?<![\w'])" + re.escape(table) + r"(?![\w'])")
    spans = _sensor_spans(text, functions)
    data = sensor = 0
    for m in re.finditer("|".join(pats), text):
        if any(a <= m.start() < b for a, b in spans):
            sensor += 1
        else:
            data += 1
    return data, sensor


@dataclass
class Graph:
    groups: dict = field(default_factory=dict)      # group -> {field groups}
    fact_groups: dict = field(default_factory=dict)  # fact -> {groups}
    measure_groups: dict = field(default_factory=dict)  # measure -> {groups}
    dead: dict = field(default_factory=dict)         # fact -> sensor count
    facts: list = field(default_factory=list)

    def fg_groups(self) -> dict:
        out: dict = {}
        for g, fgs in self.groups.items():
            for fg in fgs:
                out.setdefault(fg, set()).add(g)
        return out


def fact_tables(model, fleet) -> list:
    from .roles import Roles
    roles = Roles(fleet.raw if fleet is not None else None)
    return sorted(t.name for t in model.tables.values()
                  if roles.is_fact(t) and any(not c.expression
                                              for c in t.columns.values()))


def build_graph(fleet, definition, ignore_measures=()) -> Optional[Graph]:
    from .fieldparams import read_rows
    from .tmdl import parse_model
    c = conf(fleet)
    rows = mapping_rows(definition, c)
    if rows is None:
        return None
    model = parse_model(Path(definition))
    g = Graph(groups=group_relation(rows, c))
    fgs = g.fg_groups()
    skip = set(c["non_attributing_field_groups"])
    measures = {m.name: m.expression for t in model.tables.values()
                for m in t.measures.values()}
    facts = fact_tables(model, fleet)
    g.facts = facts
    direct = {n: {t for t in facts if _mentions(e or "", t)}
              for n, e in measures.items()}
    deps = {n: {o for o in measures if o != n and f"[{o}]" in (e or "")}
            for n, e in measures.items()}

    def trace(name, seen=None):
        seen = set() if seen is None else seen
        if name in seen or name not in measures:
            return set()
        seen.add(name)
        out = set(direct[name])
        for o in deps[name]:
            out |= trace(o, seen)
        return out

    for f in _files(definition, c["selector_tables"]):
        for row in read_rows(f)[0]:
            if row.bracket or row.group in skip:
                continue
            owners = fgs.get(row.group, set())
            if not owners:
                continue
            tgt = row.target()
            if tgt is None:
                continue
            g.measure_groups.setdefault(tgt[1], set()).update(owners)
            if tgt[1] in measures:
                for t in trace(tgt[1]):
                    g.fact_groups.setdefault(t, set()).update(owners)
    funcs = c["sensor_functions"]
    for t in facts:
        data = sensor = 0
        for n, e in measures.items():
            if n in ignore_measures:
                continue
            d, s = table_refs(e, t, funcs)
            data += d
            sensor += s
        if data == 0:
            g.dead[t] = sensor
    return g


def make_plan(g: Graph, drop) -> dict:
    drop = list(dict.fromkeys(drop))
    known = set(g.groups)
    present = sorted(d for d in drop if d in known)
    absent = sorted(d for d in drop if d not in known)
    survivors = sorted(known - set(drop))
    fgs = g.fg_groups()
    ex_fg, sh_fg = [], {}
    for fg in sorted({x for d in present for x in g.groups[d]}):
        keep = sorted(s for s in fgs.get(fg, ()) if s in survivors)
        if keep:
            sh_fg[fg] = keep
        else:
            ex_fg.append(fg)
    ex_ft, sh_ft = [], {}
    for t in sorted(t for t, gs in g.fact_groups.items() if gs & set(present)):
        keep = sorted(s for s in g.fact_groups[t] if s in survivors)
        if keep and t not in g.dead:
            sh_ft[t] = keep
        else:
            ex_ft.append(t)
    return {"drop": drop, "present": present, "absent": absent,
            "survivors": survivors,
            "exclusive": {"field_groups": ex_fg, "fact_tables": ex_ft},
            "shared": {"field_groups": sh_fg, "fact_tables": sh_ft},
            "dead_fact_tables": dict(sorted(g.dead.items()))}


# ───────────────────────── changes ─────────────────────────

@dataclass
class Change:
    path: Path
    old: object           # str | bytes | None
    new: object           # str | bytes | None (delete)


def write_changes(changes: list) -> None:
    """Validate every TMDL result first, then write (LF), then delete."""
    for c in changes:
        if isinstance(c.new, str) and str(c.path).endswith(".tmdl"):
            te.assert_tmdl_valid(c.new)
    for c in changes:
        if c.new is None:
            Path(c.path).unlink(missing_ok=True)
        elif isinstance(c.new, bytes):
            Path(c.path).write_bytes(c.new)
        else:
            Path(c.path).write_bytes(c.new.encode("utf-8"))


def _staged(changes: list, path: Path) -> str:
    for c in reversed(changes):
        if Path(c.path) == Path(path) and isinstance(c.new, str):
            return c.new
    return Path(path).read_text(encoding="utf-8")


def _stage(changes: list, path: Path, new) -> None:
    for c in changes:
        if Path(c.path) == Path(path):
            c.new = new
            return
    old = Path(path).read_text(encoding="utf-8") if Path(path).is_file() \
        else None
    if new != old:
        changes.append(Change(Path(path), old, new))


# ───────────────────────── layers ─────────────────────────

def layer_mapping(definition, c, drop, changes) -> dict:
    path = _tables_dir(definition) / f"{c['mapping_table']}.tmdl"
    if not path.is_file():
        return {"skipped": "no mapping table"}
    text = _staged(changes, path)
    drop = set(drop)
    if mc.is_encoded(text):
        rows = mc.extract_rows(text)
        if not rows:
            raise DriftDoctorError(f"{path.name}: the mapping table decodes "
                                   f"to no rows")
        kept, removed = mc.drop_groups(rows, drop)
        if removed:
            new = mc.replace_rows(text, kept)
            mc.verify_rows(new, kept)
            _stage(changes, path, new)
        per = {g: sum(1 for r in removed if str(r[0]).strip() == g)
               for g in sorted(drop)}
        return {"before": len(rows), "after": len(kept),
                "removed": len(removed), "per_group": per,
                "field_groups": sorted({str(r[-1]) for r in removed})}
    gi = int(c["group_column"])
    removed = 0
    new = text
    for m in reversed(list(re.finditer(
            r"\{\s*\"(?:[^\"]|\"\")*\"(?:\s*,\s*\"(?:[^\"]|\"\")*\")*\s*\}",
            text))):
        cells = [s[1:-1] for s in re.findall(r'"(?:[^"]|"")*"', m.group(0))]
        if len(cells) > gi and cells[gi].strip() in drop:
            a, b = m.start(), m.end()
            after = re.match(r"\s*,", new[b:])
            if after:
                b += after.end()
            else:
                before = re.search(r",\s*$", new[:a])
                if before:
                    a = before.start()
            new = new[:a] + new[b:]
            removed += 1
    if removed:
        new = re.sub(r"\n[ \t]*\n(?=[ \t]*\})", "\n", new)
        _stage(changes, path, new)
    return {"removed": removed}


def annotation_token(prefix: str, group: str) -> str:
    return prefix + group.strip().replace(" ", "_")


def layer_annotation(definition, c, drop, changes) -> dict:
    path = Path(definition) / "model.tmdl"
    if not path.is_file():
        return {"skipped": "no model.tmdl"}
    prefix = c["annotation_prefix"]
    tokens = {annotation_token(prefix, g) for g in drop}
    lines = _staged(changes, path).split("\n")
    pat = re.compile(r"^\s*annotation\s+(" + re.escape(prefix)
                     + r"\w*)\s*=")
    removed, out, skip_blank = [], [], False
    for ln in lines:
        if skip_blank:
            skip_blank = False
            if not ln.strip():
                continue
        m = pat.match(ln)
        if m and m.group(1) in tokens:
            removed.append(m.group(1))
            skip_blank = True
            continue
        out.append(ln)
    if removed:
        _stage(changes, path, "\n".join(out))
    return {"keys": removed}


def layer_selector_rows(definition, c, exclusive, changes) -> dict:
    from .fieldparams import read_rows, rewrite_rows_text
    ex = set(exclusive)
    files = []
    total = 0
    if not ex:
        return {"exclusive": [], "rows_removed": 0, "files": []}
    for f in _files(definition, c["row_drop_tables"]):
        text = _staged(changes, f)
        from .fieldparams import parse_rows
        rows, arity = parse_rows(text)
        gone = [r for r in rows if r.group in ex]
        if not gone:
            continue
        keep = [r for r in rows if r.group not in ex]
        _stage(changes, f, rewrite_rows_text(text, keep, arity, path=f))
        total += len(gone)
        files.append({"file": f.name, "rows": len(gone),
                      "field_groups": sorted({r.group for r in gone}),
                      "labels": [r.display for r in gone][:20]})
    return {"exclusive": sorted(ex), "rows_removed": total, "files": files}


def dropped_axis_measures(definition, c, exclusive) -> list:
    from .fieldparams import read_rows
    out = set()
    for f in _files(definition, c["selector_tables"]):
        for r in read_rows(f)[0]:
            if r.group in set(exclusive):
                tgt = r.target()
                if tgt and tgt[1]:
                    out.add(tgt[1])
    return sorted(out)


def layer_selector_branches(definition, c, measures, changes) -> dict:
    if not measures:
        return {"measures": [], "lines_removed": 0, "files": {}}
    lhs = _sub(None, "selector_branch", {"lhs_variables": ["SelectedField"]})
    lhs_vars = lhs["lhs_variables"]
    pat = re.compile(r"(?:\b(?:" + "|".join(map(re.escape, lhs_vars))
                     + r")\b|SELECTEDVALUE\s*\([^)]*\))\s*=\s*\"((?:[^\"]|\"\")*)\"")
    wanted = {f"[{m}]" for m in measures}
    files, total = {}, 0
    for f in sorted(_tables_dir(definition).glob("*.tmdl")):
        lines = _staged(changes, f).split("\n")
        keep, gone = [], 0
        for ln in lines:
            m = pat.search(ln)
            if m and any(w in m.group(1) for w in wanted):
                gone += 1
                continue
            keep.append(ln)
        if gone:
            files[f.name] = gone
            total += gone
            _stage(changes, f, "\n".join(keep))
    return {"measures": sorted(measures), "lines_removed": total,
            "files": files}


def _decl_line(text: str, name: str) -> bool:
    return bool(re.search(r"^\s*measure\s+(?:'" + re.escape(
        name.replace("'", "''")) + r"'|" + re.escape(name) + r")\s*=",
        text, re.MULTILINE))


def layer_gates(definition, fleet, drop, changes) -> dict:
    g = _sub(fleet, "gates", {"prefix": "f-", "aggregate": "f-All"})
    files = sorted(_tables_dir(definition).glob("*.tmdl"))
    agg = next((f for f in files
                if _decl_line(_staged(changes, f), g["aggregate"])), None)
    removed, branches = {}, {}
    for group in drop:
        gate = g["prefix"] + group
        if agg is not None:
            text = _staged(changes, agg)
            span = te.block_span(text, "measure", g["aggregate"])
            if span:
                lines = text.split("\n")
                body = lines[span[0]:span[1]]
                kept = [ln for ln in body if f"[{gate}]" not in ln]
                if len(kept) != len(body):
                    branches[gate] = len(body) - len(kept)
                    _stage(changes, agg, "\n".join(lines[:span[0]] + kept
                                                   + lines[span[1]:]))
        host = next((f for f in files
                     if _decl_line(_staged(changes, f), gate)), None)
        if host is not None:
            new = te.remove_block(_staged(changes, host), "measure", gate)
            if new is not None:
                _stage(changes, host, new)
                removed[gate] = host.name
    return {"gates_removed": removed, "aggregate_lines": branches}


# ── bookmarks ──

def _walk(node, depth=0, limit=64):
    if depth > limit:
        return
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk(v, depth + 1, limit)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, depth + 1, limit)


def bookmark_groups(state, entity: str, column: str) -> set:
    out = set()
    for node in _walk(state):
        if not isinstance(node, dict):
            continue
        filt = node.get("filter") if isinstance(node.get("filter"), dict) \
            else (node if "From" in node and "Where" in node else None)
        if not filt:
            continue
        sources = {s.get("Name") for s in filt.get("From") or []
                   if isinstance(s, dict) and s.get("Entity") == entity}
        if not sources:
            continue
        for w in filt.get("Where") or []:
            cond = ((w or {}).get("Condition") or {}).get("In") or {}
            exprs = cond.get("Expressions") or []
            if not any((e.get("Column") or {}).get("Property") == column
                       for e in exprs if isinstance(e, dict)):
                continue
            for row in cond.get("Values") or []:
                for v in row or []:
                    lit = ((v or {}).get("Literal") or {}).get("Value")
                    if isinstance(lit, str) and lit.strip("'"):
                        out.add(lit.strip("'"))
    return out


def report_roots(model_dir) -> list:
    base = Path(model_dir).parent
    return sorted(p for p in base.rglob("*.Report") if p.is_dir())


def layer_bookmarks(model_dir, fleet, c, drop, changes) -> dict:
    b = _sub(fleet, "bookmarks", {"group_column": "Analysis Category",
                                  "exempt_display_names": ["++new report"]})
    exempt = {s.strip().lower() for s in b["exempt_display_names"]}
    out = []
    for root in report_roots(model_dir):
        d = root / "definition"
        if not (d / "report.json").is_file() or not (d / "bookmarks").is_dir():
            continue
        for f in sorted((d / "bookmarks").glob("*.bookmark.json")):
            try:
                doc = json.loads(f.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            name = str(doc.get("displayName") or "")
            if name.strip().lower() in exempt:
                continue
            pinned = bookmark_groups(doc.get("explorationState") or doc,
                                     c["mapping_table"], b["group_column"])
            if pinned and pinned <= set(drop):
                out.append({"file": str(f), "display_name": name,
                            "groups": sorted(pinned)})
                changes.append(Change(f, f.read_bytes(), None))
    return {"bookmarks": out}


# ── measures (purge for dropped groups) ──

def _norm_words(name: str) -> set:
    words = set()
    for w in name.lower().replace("#", " ").split():
        w = re.sub(r"^[^0-9a-z]+", "", w)
        if len(w) >= 2 and w.endswith("s"):
            w = w[:-1]
        if w:
            words.add(w)
    return words


def measure_purge(fleet, definition, graph: Graph, drop) -> dict:
    from .tmdl import parse_model
    model = parse_model(Path(definition))
    measures = {m.name: m for t in model.tables.values()
                for m in t.measures.values()}
    live_conf = _sub(fleet, "measure_liveness", {
        "helper_prefixes": ["-", "_", "!"], "diagnostic_prefixes": ["i-"],
        "always_live": []})
    drop = set(drop)
    present = drop & set(graph.groups)
    seed = set()
    if present:
        seed |= {n for n, gs in graph.measure_groups.items()
                 if n in measures and gs and gs <= drop}
    for g in drop:
        gw = _norm_words(g)
        if len(g.split()) < 2 or not gw:
            continue
        seed |= {n for n in measures if gw <= _norm_words(n)}
    refs = {n: {o for o in measures if o != n and f"[{o}]" in
                re.sub(r"(?:'[^']*'|[A-Za-z_][\w ]*?)\[[^\]]*\]", " ",
                       m.expression or "") or f"[{o}]" in
                _unqualified(m.expression or "")}
            for n, m in measures.items()}
    dependants: dict = {}
    for n, rs in refs.items():
        for r in rs:
            dependants.setdefault(r, set()).add(n)
    comp = set(seed)
    stack = list(seed)
    while stack:
        n = stack.pop()
        for d in dependants.get(n, ()):
            if d not in comp:
                comp.add(d)
                stack.append(d)

    def live(n):
        if not n.startswith(tuple(live_conf["helper_prefixes"])):
            return True
        if n.startswith(tuple(live_conf["diagnostic_prefixes"])):
            return True
        if n in live_conf["always_live"]:
            return True
        if graph.measure_groups.get(n, set()) - drop:
            return True
        return _report_bound(definition, n)

    purge = {n for n in comp if not live(n)}
    changed = True
    while changed:
        changed = False
        for n in sorted(purge):
            if dependants.get(n, set()) - purge:
                purge.discard(n)
                changed = True
    return {"seed": sorted(seed), "component": sorted(comp),
            "purge": sorted(purge), "kept": sorted(comp - purge),
            "hosts": {n: measures[n].table for n in sorted(purge)}}


def _unqualified(expr: str) -> str:
    """The expression with table-qualified references blanked out, so only
    unqualified ``[Name]`` references remain."""
    from .tmdl import blank_strings
    text = blank_strings(expr)
    return re.sub(r"(?:'(?:[^']|'')*'|\b[A-Za-z_]\w*)\s*\[[^\]]*\]", " ", text)


def _report_bound(definition, name: str) -> bool:
    from .renames import count_report_refs
    n = count_report_refs(Path(definition).parent, name)
    if n is not None:
        return n > 0
    needle = re.compile(r'"Property"\s*:\s*"' + re.escape(name) + r'"|\.'
                        + re.escape(name) + r'"')
    for root in report_roots(Path(definition).parent):
        for f in root.rglob("*.json"):
            try:
                if needle.search(f.read_text(encoding="utf-8-sig")):
                    return True
            except (OSError, UnicodeDecodeError):
                continue
    return False


def layer_measures(fleet, definition, graph, drop, changes) -> dict:
    res = measure_purge(fleet, definition, graph, drop)
    skipped = []
    for n in res["purge"]:
        f = _tables_dir(definition) / f"{res['hosts'][n]}.tmdl"
        if not f.is_file():
            skipped.append(n)
            continue
        new = te.remove_block(_staged(changes, f), "measure", n)
        if new is None:
            skipped.append(n)
            continue
        _stage(changes, f, new)
    res["skipped"] = skipped
    return res


# ── tables ──

def remove_sensor_operands(text: str, table: str, functions) -> tuple:
    """(new text, operands removed): boolean sensor calls on `table`
    (``ISFILTERED('T')``, optionally under ``NOT``) dropped from their
    ``||`` / ``&&`` chains. Once the table is gone such a test is constant,
    so the chain keeps its meaning."""
    quoted = "'" + table.replace("'", "''") + "'"
    bare = re.fullmatch(r"[A-Za-z_]\w*", table) is not None
    mention = re.compile(re.escape(quoted) + (
        r"|(?<![\w'])" + re.escape(table) + r"(?![\w'])" if bare else ""))
    call = re.compile(r"(?:\bNOT\s+)?\b(?:" + "|".join(map(re.escape,
                                                            functions))
                      + r")\s*\(", re.IGNORECASE)
    count = 0
    for _ in range(500):
        hit = None
        for m in call.finditer(text):
            end = _close_paren(text, m.end() - 1)
            if end is not None and mention.search(text, m.end(), end - 1):
                hit = (m.start(), end)
                break
        if hit is None:
            return text, count
        a, b = hit
        after = re.match(r"(?:[ \t]|\n|--[^\n]*|//[^\n]*)*?(\|\||&&)",
                         text[b:])
        before = re.search(r"(\|\||&&)\s*$", text[:a])
        if after:
            b += after.end()
            text = _cut(text, a, b, eat_after=True)
        elif before:
            text = _cut(text, before.start(), b, eat_after=False)
        else:
            text = _cut(text, a, b, eat_after=False)
        count += 1
    raise DriftDoctorError(f"sensor removal for '{table}' did not converge")


def _cut(text: str, a: int, b: int, eat_after: bool) -> str:
    """Remove text[a:b] and tidy the layout around the cut."""
    head, tail = text[:a], text[b:]
    if eat_after:
        tail = re.sub(r"^[ \t]+", "", tail)
    if tail[:1] in (",", ")"):
        head = head.rstrip(" \t")
    line_start = head.rfind("\n") + 1
    line_end = tail.find("\n")
    rest_of_line = tail if line_end < 0 else tail[:line_end]
    if not head[line_start:].strip() and not rest_of_line.strip():
        # the cut emptied its line: drop the line
        head = head[:line_start]
        tail = "" if line_end < 0 else tail[line_end + 1:]
        if not head and tail.startswith("\n"):
            tail = tail[1:]
        return head + tail
    if not rest_of_line.strip():
        head = head.rstrip(" \t")
    return head + tail


def _balanced(text: str) -> bool:
    from .tmdl import blank_strings
    t = re.sub(r"'(?:[^']|'')*'", "''", blank_strings(text))
    return t.count("(") == t.count(")") and t.count("[") == t.count("]")


def _culture_prune(text: str, table: str) -> tuple:
    lines = text.split("\n")
    start = next((i for i, ln in enumerate(lines) if ln.strip() == "{"), None)
    if start is None:
        return text, 0, 0
    indent = lines[start][:len(lines[start]) - len(lines[start].lstrip())]
    for end in range(len(lines) - 1, start, -1):
        chunk = "\n".join(ln[len(indent):] if ln.startswith(indent) else ln
                          for ln in lines[start:end + 1])
        try:
            doc = json.loads(chunk)
        except ValueError:
            continue
        break
    else:
        return text, 0, 0
    entities = doc.get("Entities") or {}
    gone = [k for k, v in entities.items()
            if ((v or {}).get("Definition") or {}).get("Binding", {}).get(
                "ConceptualEntity") == table]
    if not gone:
        return text, 0, 0
    for k in gone:
        entities.pop(k)
    rels = doc.get("Relationships") or {}
    def names_gone(v):
        for tok in re.findall(r"[\w.]+", json.dumps(v)):
            if any(tok == k or tok.startswith(k + ".") for k in gone):
                return True
        return False

    dead = [k for k, v in rels.items() if names_gone(v)]
    for k in dead:
        rels.pop(k)
    body = json.dumps(doc, indent=2, ensure_ascii=False).split("\n")
    body = [indent + ln if ln else ln for ln in body]
    return "\n".join(lines[:start] + body + lines[end + 1:]), len(gone), len(dead)


_REPORT_NOT_FIELD = {"config", "filterConfig", "pages", "position", "sections",
                     "singleVisual", "visual", "visualContainers",
                     "visualGroup"}
_REPORT_FIELD = {"Column", "expression", "field", "queryRef", "queryRefs",
                 "selector"}


def _refers(node, table: str) -> bool:
    needles = (f"'{table}'[", f"''{table}''[")
    for n in _walk(node):
        if isinstance(n, dict):
            if n.get("Entity") == table:
                return True
            md = (n.get("selector") or {}).get("metadata") \
                if isinstance(n.get("selector"), dict) else None
            if isinstance(md, str) and md.startswith(table + "."):
                return True
        elif isinstance(n, str):
            if n.startswith(table + ".") or any(x in n for x in needles):
                return True
    return False


def _prune(node, table: str) -> int:
    removed = 0
    if isinstance(node, dict):
        for key in list(node):
            v = node[key]
            if key == "filter" and isinstance(v, dict) and any(
                    (s or {}).get("Entity") == table for s in v.get("From") or []):
                del node[key]
                removed += 1
                continue
            if key == "byName" and isinstance(v, dict):
                for k in [k for k, x in v.items() if _refers(x, table)]:
                    del v[k]
                    removed += 1
            elif key in ("byExpr", "filters", "Values",
                         "cachedFilterDisplayItems") and isinstance(v, list):
                keep = [x for x in v if not _refers(x, table)]
                removed += len(v) - len(keep)
                node[key] = keep
            elif key in ("projections", "activeProjections") and \
                    isinstance(v, dict):
                for bucket, items in v.items():
                    if isinstance(items, list):
                        keep = [x for x in items if not _refers(x, table)]
                        removed += len(items) - len(keep)
                        v[bucket] = keep
            removed += _prune(node.get(key), table) if key in node else 0
    elif isinstance(node, list):
        keep = []
        for x in node:
            if isinstance(x, dict) and _refers(x, table) and \
                    set(x) & _REPORT_FIELD and not set(x) & _REPORT_NOT_FIELD:
                removed += 1
                continue
            keep.append(x)
        node[:] = keep
        for x in node:
            removed += _prune(x, table)
    return removed


def _cleanup(node) -> bool:
    changed = False
    if isinstance(node, dict):
        if isinstance(node.get("Where"), list):
            keep = [w for w in node["Where"] if not (
                isinstance(w, dict) and isinstance(
                    ((w.get("Condition") or {}).get("In") or {}).get("Values"),
                    list) and not w["Condition"]["In"]["Values"])]
            if len(keep) != len(node["Where"]):
                node["Where"] = keep
                changed = True
        if isinstance(node.get("filter"), dict):
            f = node["filter"]
            if not f or ("Version" in f and not f.get("Where")):
                del node["filter"]
                changed = True
        for v in list(node.values()):
            changed |= _cleanup(v)
    elif isinstance(node, list):
        for v in node:
            changed |= _cleanup(v)
    return changed


_FLOAT_TAG = "\u0000F"


def _load_exact(raw: str):
    """(document, dump function) when the JSON re-serializes to exactly
    its bytes (2-space indent, float literals kept), else (None, None)."""
    floats: list = []

    def keep_float(s):
        floats.append(s)
        return f"{_FLOAT_TAG}{len(floats) - 1}\u0000"

    try:
        doc = json.loads(raw, parse_float=keep_float)
    except ValueError:
        return None, None

    def dump(d) -> str:
        text = json.dumps(d, indent=2, ensure_ascii=False)
        text = re.sub(r'"\\u0000F(\d+)\\u0000"',
                      lambda m: floats[int(m.group(1))], text)
        return text + ("\n" if raw.endswith("\n") else "")

    return (doc, dump) if dump(doc) == raw else (None, None)


def drop_table(fleet, model_dir: Path, table: str, changes: list,
               scratch=("_backup", "_b2", "legacy/")) -> dict:
    """Every surface of `table` in one model, staged into `changes`."""
    defn = Path(model_dir) / "definition"
    funcs = conf(fleet)["sensor_functions"]
    report = {"table": table, "file": None, "sensors": {},
              "relationships": [], "registration": False,
              "qa_entities": 0, "qa_relationships": 0, "reports": {},
              "manual_review": []}
    quoted = "'" + table.replace("'", "''") + "'"
    bare = re.fullmatch(r"[A-Za-z_]\w*", table) is not None
    mention = re.compile(re.escape(quoted) + (
        r"|(?<![\w'])" + re.escape(table) + r"(?![\w'])" if bare else ""))
    # 1. sensors
    for f in sorted(_tables_dir(defn).glob("*.tmdl")):
        if f.stem == table:
            continue
        text = _staged(changes, f)
        if not mention.search(text):
            continue
        new, n = remove_sensor_operands(text, table, funcs)
        if n:
            if not _balanced(new):
                raise TmdlEditError(f"{f.name}: removing the sensor calls on "
                                    f"'{table}' leaves unbalanced delimiters")
            te.assert_tmdl_valid(new)
            _stage(changes, f, new)
            report["sensors"][f.name] = n
        for i, ln in enumerate(new.split("\n"), 1):
            if mention.search(ln):
                report["manual_review"].append(
                    f"{f.name}:{i}: {ln.strip()[:100]}")
    # 2. relationships
    rel = defn / "relationships.tmdl"
    if rel.is_file():
        from .tmdl import parse_relationships_text
        text = _staged(changes, rel)
        for sig, r in parse_relationships_text(text).items():
            if r.touches_table(table):
                out = te.remove_relationship_block(text, sig)
                if out is not None:
                    text = out
                    report["relationships"].append(r.name)
        _stage(changes, rel, text)
    # 3. registration
    mf = defn / "model.tmdl"
    if mf.is_file():
        old = _staged(changes, mf)
        new = te.deregister_table(old, table)
        if new != old:
            _stage(changes, mf, new)
            report["registration"] = True
    # 4. Q&A schema
    for cf in sorted((defn / "cultures").glob("*.tmdl")) \
            if (defn / "cultures").is_dir() else []:
        new, ents, rels = _culture_prune(_staged(changes, cf), table)
        if ents:
            _stage(changes, cf, new)
            report["qa_entities"] += ents
            report["qa_relationships"] += rels
    # 5. reports
    base = Path(model_dir).parent
    for root in report_roots(model_dir):
        if any(s in str(root.relative_to(base)) + "/" for s in scratch):
            continue
        targets = []
        layout = root / "semanticModelDiagramLayout.json"
        if layout.is_file():
            targets.append(layout)
        d = root / "definition"
        targets += sorted(d.glob("bookmarks/*.json"))
        targets += sorted(d.glob("pages/*/visuals/*/visual.json"))
        targets += sorted(d.rglob("filterConfig.json"))
        for f in targets:
            raw = f.read_bytes().decode("utf-8-sig")
            if table not in raw:
                continue
            doc, dump = _load_exact(raw)
            rel_name = str(f.relative_to(base))
            if doc is None:
                report["manual_review"].append(f"{rel_name}: not reproducible "
                                               f"as JSON; left unchanged")
                continue
            if f.name == "semanticModelDiagramLayout.json":
                n = 0
                for node in _walk(doc):
                    if isinstance(node, dict):
                        for k, v in list(node.items()):
                            if isinstance(v, list):
                                keep = [x for x in v if not (
                                    isinstance(x, dict)
                                    and x.get("nodeIndex") == table)]
                                n += len(v) - len(keep)
                                node[k] = keep
            else:
                n = _prune(doc, table)
                if n:
                    while _cleanup(doc):
                        pass
            text = dump(doc)
            if _refers(doc, table) or quoted + "[" in text:
                report["manual_review"].append(
                    f"{rel_name}: still mentions '{table}'"
                    + (" after pruning" if n else ""))
                continue
            if n:
                changes.append(Change(f, raw, text))
                report["reports"][rel_name] = n
    # 6. file
    tf = _tables_dir(defn) / f"{table}.tmdl"
    if tf.is_file():
        report["file"] = str(tf.relative_to(Path(model_dir).parent))
        changes.append(Change(tf, tf.read_bytes(), None))
    return report

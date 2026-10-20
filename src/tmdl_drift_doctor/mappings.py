"""Encoded mapping tables in the drift pipeline: snapshot, ledger
inventory, detection and fixes.

A ``mapping_tables`` entry whose partition calls ``Binary.FromText`` is an
encoded entered-data table (``mapping_codec``). Its rows are captured in the
baseline under ``encoded_mapping`` and compared by identity (category, key):

* ``mapping_category.missing``: template categories the model lacks (one
  finding per table); the fix appends the template rows of each category
  after every model row;
* ``mapping_row.missing``: a template row missing from a category the model
  has; the fix appends it at the model table's own width;
* ``extra.mapping_row``: a model-only row (advisory, never removed);
* ``retire.mapping_row`` / ``retire.mapping_category``: rows or whole
  categories the ledger proves left the template; only the listed keys are
  removed, siblings stay.

Every write goes through the codec's verified replace and is read back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import mapping_codec as mc
from .mapping_codec import CodecError


def table_names(entries) -> list:
    out = []
    for e in entries or []:
        if isinstance(e, dict):
            if e.get("name"):
                out.append(str(e["name"]))
        else:
            out.append(str(e))
    return out


def source_text(table) -> str:
    return "\n".join(p.source for p in table.partitions.values())


def snapshot_encoded(model, mapping_tables) -> dict:
    """{table: rows} for every encoded mapping table of `model`."""
    out = {}
    for name in table_names(mapping_tables):
        t = model.tables.get(name)
        if t is None:
            continue
        text = t.file.read_text(encoding="utf-8")
        if mc.is_encoded(text):
            out[name] = mc.extract_rows(text)
    return out


def row_ref(table: str, category: str, key: str) -> str:
    return f"{table}/{category}/{key}"


def inventory(snapshot: dict) -> tuple:
    """({row ref: (table, category, key)}, {category ref: (table, category,
    [keys])}) of a snapshot's encoded tables."""
    rows, cats = {}, {}
    for table, data in (snapshot.get("encoded_mapping") or {}).items():
        for r in data:
            if not r:
                continue
            cat, key = mc.identity(r)
            rows[row_ref(table, cat, key)] = (table, cat, key)
            cats.setdefault(f"{table}/{cat}", (table, cat, []))[2].append(key)
    return rows, cats


def live_rows(definition: Path, table: str) -> Optional[list]:
    """Decoded rows of the model's table, None when the table is absent or
    not encoded; raises CodecError when it does not decode."""
    path = Path(definition) / "tables" / f"{table}.tmdl"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    if not mc.is_encoded(text):
        return None
    return mc.extract_rows(text)


# ───────────────────────── detection ─────────────────────────

def _finding(kind, model, ref, detail, **data):
    from .detect import Finding
    return Finding(kind=kind, model=model, ref=ref, detail=detail, data=data)


def _absent_categories(fleet, model: str) -> dict:
    out = {}
    for d in (fleet.sections.get(model) or {}).get("mapping_categories", ()):
        out[d["category"]] = d
    return out


def _retained_rows(fleet, model: str) -> set:
    return {(d["category"], d["key"]) for d in
            (fleet.sections.get(model) or {}).get("mapping_rows", ())}


def merge_findings(name: str, fleet, baseline: dict, definition) -> list:
    out = []
    absent = _absent_categories(fleet, name)
    for table, t_rows in (baseline.get("encoded_mapping") or {}).items():
        path = Path(definition) / "tables" / f"{table}.tmdl"
        if not path.is_file():
            continue                    # table.missing owns it
        try:
            d_rows = mc.extract_rows(path.read_text(encoding="utf-8"))
        except CodecError as e:
            out.append(_finding(
                "mapping_category.missing", name, table,
                f"the encoded rows of '{table}' cannot be read: {e}",
                table=table, categories=[], error=str(e)))
            continue
        d_cats = set(mc.categories(d_rows))
        d_ids = {mc.identity(r) for r in d_rows}
        t_ids = {mc.identity(r) for r in t_rows}
        missing = [c for c in mc.categories(t_rows)
                   if c not in d_cats and c not in absent]
        if missing:
            counts = {c: sum(1 for r in t_rows if r and r[0] == c)
                      for c in missing}
            out.append(_finding(
                "mapping_category.missing", name, table,
                f"'{name}' lacks template categor"
                f"{'y' if len(missing) == 1 else 'ies'} of '{table}': "
                + ", ".join(f"{c} ({counts[c]} key(s))" for c in missing),
                table=table, categories=missing))
        seen = set()
        for r in t_rows:
            ident = mc.identity(r)
            if not r or ident in d_ids or ident in seen \
                    or r[0] not in d_cats:
                continue
            seen.add(ident)
            out.append(_finding(
                "mapping_row.missing", name, f"{table}/{ident[0]}|{ident[1]}",
                f"template row ({ident[0]}, {ident[1]}) is absent from "
                f"'{name}' table '{table}'", table=table, row=list(r),
                encoded=True))
        for r in d_rows:
            ident = mc.identity(r)
            if r and ident not in t_ids:
                out.append(_finding(
                    "extra.mapping_row", name,
                    f"{table}/{ident[0]}|{ident[1]}",
                    f"'{name}' carries row ({ident[0]}, {ident[1]}) of "
                    f"'{table}' with no template counterpart; advisory only",
                    table=table))
    return out


def _live_template_ids(fleet, table: str, cache: dict) -> set:
    if table not in cache:
        try:
            rows = live_rows(fleet.template_definition, table) or []
        except CodecError:
            rows = []
        cache[table] = {mc.identity(r) for r in rows}
    return cache[table]


def retirement_findings(name: str, fleet, definition, retire: dict) -> list:
    """``retire.mapping_row`` (one per category, listing keys) and
    ``retire.mapping_category`` for encoded tables."""
    out, cache, decoded = [], {}, {}
    retained = _retained_rows(fleet, name)

    def rows_of(table):
        if table not in decoded:
            try:
                decoded[table] = live_rows(definition, table)
            except CodecError:
                decoded[table] = None
        return decoded[table]

    by_cat: dict = {}
    for (kind, ref), e in sorted(retire.items(), key=lambda kv: kv[0]):
        if kind != "mapping_row" or not e.get("category"):
            continue
        table, cat, key = e.get("table"), e["category"], e["row_key"]
        rows = rows_of(table)
        if rows is None:
            continue
        if (cat, key) in _live_template_ids(fleet, table, cache):
            continue                     # the row is back in the template
        if (cat, key) in retained:
            continue
        if any(mc.identity(r) == (cat, key) for r in rows):
            by_cat.setdefault((table, cat), []).append(key)
    for (table, cat), keys in sorted(by_cat.items()):
        out.append(_finding(
            "retire.mapping_row", name, f"{table}/{cat}",
            f"row(s) {', '.join(keys)} of category '{cat}' were retired from "
            f"the template (ledger) but '{name}' still carries them; the "
            f"category's other rows stay", table=table, category=cat,
            keys=keys, encoded=True))

    for (kind, ref), e in sorted(retire.items(), key=lambda kv: kv[0]):
        if kind != "mapping_category":
            continue
        table, cat = e.get("table"), e.get("category")
        rows = rows_of(table)
        if rows is None or cat not in mc.categories(rows):
            continue
        if any(c == cat for c, _k in _live_template_ids(fleet, table, cache)):
            continue
        ledger_keys = set(e.get("keys") or [])
        mine = [mc.identity(r)[1] for r in rows if r and r[0] == cat]
        go = [k for k in mine if (cat, k) not in retained]
        own = sorted(k for k in go if k not in ledger_keys)
        f = _finding(
            "retire.mapping_category", name, f"{table}/{cat}",
            f"category '{cat}' was retired from the template (ledger) but "
            f"'{name}' still carries {len(go)} row(s) of it",
            table=table, category=cat, keys=[k for k in go
                                             if k in ledger_keys])
        if own:
            f.data["blocked"] = [f"the model added its own key(s) to the "
                                 f"category: {', '.join(own)}; decide by "
                                 f"hand"]
            f.detail += "; BLOCKED: the model added its own keys"
        if go:
            out.append(f)
    return out


# ───────────────────────── fixes ─────────────────────────

def verified_change(path: Path, rows: list):
    """A FileChange replacing the encoded string of `path` with `rows`,
    verified before the write and read back after it."""
    from .remediate import FileChange
    old = path.read_text(encoding="utf-8")
    new = mc.replace_rows(old, rows)
    mc.verify_rows(new, rows)

    def readback(p, rows=rows):
        mc.verify_rows(Path(p).read_text(encoding="utf-8"), rows)

    change = FileChange(path, old, new)
    change.verify = readback
    return [change] if new != old else []


def _table_path(fleet, f) -> Path:
    from . import DriftDoctorError
    path = fleet.model_definition(f.model) / "tables" / f"{f.data['table']}.tmdl"
    if not path.is_file():
        raise DriftDoctorError(f"{path} no longer exists; re-run detect")
    return path


def apply_category_missing(fleet, f) -> tuple:
    if f.data.get("error"):
        raise CodecError(f.data["error"])
    path = _table_path(fleet, f)
    rows = mc.extract_rows(path.read_text(encoding="utf-8"))
    from .baseline import load_baseline
    t_rows = (load_baseline(fleet).get("encoded_mapping") or {}).get(
        f.data["table"]) or []
    have = set(mc.categories(rows))
    wanted = [c for c in f.data.get("categories") or [] if c not in have]
    if not wanted:
        return ("categories already present", [])
    width = max((len(r) for r in rows), default=3)
    merged = rows + [mc.shaped(r, width) for r in t_rows
                     if r and r[0] in set(wanted)]
    return (f"add template categor{'y' if len(wanted) == 1 else 'ies'} "
            f"{', '.join(wanted)} to '{f.data['table']}' (model rows kept "
            f"first)", verified_change(path, merged))


def apply_row_missing(fleet, f) -> tuple:
    path = _table_path(fleet, f)
    rows = mc.extract_rows(path.read_text(encoding="utf-8"))
    row = f.data["row"]
    if mc.identity(row) in {mc.identity(r) for r in rows}:
        return ("row already present", [])
    width = max((len(r) for r in rows), default=len(row))
    return (f"add template row ({row[0]}, {row[-1]}) to '{f.data['table']}'",
            verified_change(path, rows + [mc.shaped(row, width)]))


def apply_retire_rows(fleet, f) -> tuple:
    path = _table_path(fleet, f)
    rows = mc.extract_rows(path.read_text(encoding="utf-8"))
    kept, removed, left = mc.remove_category_rows(rows, f.data["category"],
                                                  f.data["keys"])
    if not removed:
        return ("rows already removed", [])
    what = ("category" if f.kind == "retire.mapping_category" else "row(s)")
    return (f"remove retired {what} {f.data['category']}: "
            f"{', '.join(r[-1] for r in removed)} ({len(left)} sibling row(s) "
            f"kept)", verified_change(path, kept))

"""Measure and column renames: pairing, reference rewrites, the rename map.

Three layers, each usable on its own:

* **reference rewrite** (``rewrite_measure_references`` and friends): given
  a map of old -> new names, rewrite every DAX reference inside the
  expression regions of a table file (measure bodies, calculated columns,
  partition sources with or without fences, calculation items, format
  string definitions). Text outside those regions is never touched. A
  measure is never made to reference itself.
* **declaration and label edits** (``rename_declaration``,
  ``rename_labels``): the measure line itself, and the display label of a
  field-parameter row whose NAMEOF points at the renamed measure.
* **the rename map** (``RenameMap``): a reviewed, durable list of renames
  kept under ``renames:`` in fleet.yml, with a validity check.

Detection of template-driven renames (``measure.renamed``) pairs a missing
template measure with an extra derived one that shares its *core* name
(``core_name``); see ``detect``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import DriftDoctorError, TmdlEditError
from . import tmdl_edit as te

DEFAULT_SIGILS = ("-", "_", "!", "#", "^")
DEFAULT_EXCLUDE = ("i-",)


# ───────────────────────── names ─────────────────────────

def core_name(name: str, sigils=DEFAULT_SIGILS) -> str:
    """Trim, strip ONE leading prefix (a single sigil, or one ASCII letter
    followed by a dash), trim again, lower-case."""
    s = (name or "").strip()
    if s[:1] and s[0] in sigils:
        s = s[1:]
    elif len(s) >= 2 and s[0].isascii() and s[0].isalpha() and s[1] == "-":
        s = s[2:]
    return s.strip().lower()


def quote_name(name: str) -> str:
    """Quote a declaration name unless it is letters, digits and '_' only."""
    if re.fullmatch(r"[A-Za-z0-9_]+", name):
        return name
    return "'" + name.replace("'", "''") + "'"


def _name_forms(name: str) -> str:
    """A pattern matching `name` bare, single-quoted or double-quoted."""
    esc = re.escape(name)
    sq = re.escape(name.replace("'", "''"))
    return rf"(?:'{sq}'|\"{esc}\"|{esc})"


# ───────────────────────── expression regions ─────────────────────────

_REGION_DECL = re.compile(
    r"^(\t+)(measure|column|calculationItem|source|formatStringDefinition)\b"
    r"(.*?)=[ \t]*(.*)$")


@dataclass
class Region:
    start: int        # character offset of the region in the text
    end: int
    kind: str         # measure | column | source | calculationItem | ...
    name: str = ""    # the measure's name (for the self-reference guard)


def _decl_name(kind: str, between: str) -> str:
    if kind != "measure":
        return ""
    raw = between.strip()
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1].replace("''", "'")
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return raw[1:-1]
    return raw


def expression_regions(text: str) -> list:
    """Every DAX/M expression region of a table file, in order."""
    lines = text.split("\n")
    offsets, pos = [], 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    out = []
    i = 0
    while i < len(lines):
        m = _REGION_DECL.match(lines[i])
        if not m or (m.group(2) == "column" and "=" not in lines[i]):
            i += 1
            continue
        depth = len(m.group(1))
        kind, rhs = m.group(2), m.group(4)
        rhs_start = offsets[i] + m.start(4)
        name = _decl_name(kind, m.group(3))
        if rhs.strip().startswith("```"):
            j = i + 1
            while j < len(lines) and "```" not in lines[j]:
                j += 1
            end = offsets[j] + len(lines[j]) if j < len(lines) else len(text)
            out.append(Region(rhs_start, end, kind, name))
            i = j + 1
            continue
        j = i + 1
        if rhs.strip():
            # a one-line body may continue on lines indented two levels deeper
            while j < len(lines) and lines[j].strip() and \
                    _tabs(lines[j]) >= depth + 2:
                j += 1
        else:
            while j < len(lines) and (not lines[j].strip()
                                      or _tabs(lines[j]) > depth):
                if kind in ("measure", "column") and lines[j].strip() \
                        and _tabs(lines[j]) == depth + 1:
                    break           # the object's own properties
                j += 1
        last = j - 1
        while last > i and not lines[last].strip():
            last -= 1
        end = offsets[last] + len(lines[last])
        out.append(Region(rhs_start, end, kind, name))
        i = j
    return out


def _tabs(line: str) -> int:
    return len(line) - len(line.lstrip("\t"))


# ───────────────────────── reference rewrite ─────────────────────────

def _measure_pattern(mapping: dict):
    olds = sorted((o for o, n in mapping.items() if o and n), key=len,
                  reverse=True)
    if not olds:
        return None, {}
    lookup = {o.casefold(): mapping[o] for o in olds}
    pat = re.compile(r"\[(" + "|".join(re.escape(o) for o in olds) + r")\]",
                     re.IGNORECASE)
    return pat, lookup


def rewrite_measure_text(text: str, mapping: dict) -> tuple:
    """(new text, measures modified, replacements) for one table file."""
    pat, lookup = _measure_pattern(mapping)
    if pat is None:
        return text, 0, 0
    pieces, last, total, measures = [], 0, 0, 0
    for region in expression_regions(text):
        own = region.name.casefold() if region.kind == "measure" else None
        hits = 0

        def repl(m):
            nonlocal hits
            new = lookup[m.group(1).casefold()]
            if own is not None and new.casefold() == own:
                return m.group(0)       # never make a measure cite itself
            hits += 1
            return "[" + new + "]"

        body = pat.sub(repl, text[region.start:region.end])
        pieces.append(text[last:region.start])
        pieces.append(body)
        last = region.end
        total += hits
        if hits and region.kind == "measure":
            measures += 1
    pieces.append(text[last:])
    return "".join(pieces), measures, total


def rewrite_measure_references(path, mapping: dict,
                               dry_run: bool = False) -> tuple:
    """Rewrite measure references in one table file. Returns
    (measures modified, replacements); writes only when something changed
    and `dry_run` is off."""
    path = Path(path)
    text = te.read_text(path)
    new, measures, total = rewrite_measure_text(text, mapping)
    if new != text and not dry_run:
        path.write_text(new, encoding="utf-8")
    return measures, total


def split_column_path(path: str) -> tuple:
    """``Table.Column`` split at the LAST dot; (None, None) without one."""
    if not isinstance(path, str) or "." not in path:
        return None, None
    table, col = path.rsplit(".", 1)
    return (table, col) if table and col else (None, None)


def _column_renames(mapping: dict) -> list:
    out = []
    for old, new in (mapping or {}).items():
        ot, oc = split_column_path(old)
        nt, nc = split_column_path(new)
        if ot and nt and ot == nt and oc != nc:
            out.append((ot, oc, nc))
    return out


def rewrite_column_text(text: str, mapping: dict) -> tuple:
    """(new text, replacements): qualified ``'T'[old]`` / ``T[old]`` inside
    expression regions become ``'T'[new]``. Bare ``[old]`` is left alone
    (it could be a measure). Case-sensitive."""
    renames = _column_renames(mapping)
    if not renames:
        return text, 0
    total = 0
    pieces, last = [], 0
    for region in expression_regions(text):
        body = text[region.start:region.end]
        for table, old, new in renames:
            pat = re.compile(r"(?:'" + re.escape(table.replace("'", "''"))
                             + r"'|(?<![\w'])" + re.escape(table) + r")\["
                             + re.escape(old) + r"\]")
            body, n = pat.subn("'" + table.replace("'", "''") + "'[" + new
                               + "]", body)
            total += n
        pieces.append(text[last:region.start])
        pieces.append(body)
        last = region.end
    pieces.append(text[last:])
    return "".join(pieces), total


def rewrite_relationship_endpoints(text: str, mapping: dict) -> tuple:
    """(new text, endpoints updated) in relationships.tmdl."""
    total = 0
    for table, old, new in _column_renames(mapping):
        t = r"(?:'" + re.escape(table.replace("'", "''")) + r"'|" \
            + re.escape(table) + r")"
        c = r"(?:'" + re.escape(old.replace("'", "''")) + r"'|" \
            + re.escape(old) + r")"
        pat = re.compile(r"^(\t(?:fromColumn|toColumn)\s*:\s*)" + t + r"\."
                         + c + r"\s*$", re.MULTILINE)
        text, n = pat.subn(lambda m: m.group(1) + "'" + table.replace(
            "'", "''") + "'.'" + new.replace("'", "''") + "'", text)
        total += n
    return text, total


@dataclass
class RewriteResult:
    files_modified: list = field(default_factory=list)
    measures_modified: int = 0
    replacements: int = 0
    relationships: int = 0
    errors: list = field(default_factory=list)


def rewrite_model(definition, measures: Optional[dict] = None,
                  columns: Optional[dict] = None,
                  dry_run: bool = False) -> RewriteResult:
    """Apply a measure and/or column rename map to every table file of one
    model definition (and, for columns, relationships.tmdl)."""
    definition = Path(definition)
    res = RewriteResult()
    tables = definition / "tables"
    if not tables.is_dir():
        res.errors.append(f"{definition}: no tables/ folder")
        return res
    for f in sorted(tables.glob("*.tmdl"), key=lambda p: p.name):
        text = te.read_text(f)
        new, n_meas, n_rep = rewrite_measure_text(text, measures or {})
        new, n_col = rewrite_column_text(new, columns or {})
        if new != text:
            res.files_modified.append(str(f))
            if not dry_run:
                f.write_text(new, encoding="utf-8")
        res.measures_modified += n_meas
        res.replacements += n_rep + n_col
    rel = definition / "relationships.tmdl"
    if columns and rel.is_file():
        text = te.read_text(rel)
        new, n = rewrite_relationship_endpoints(text, columns)
        if new != text:
            res.files_modified.append(str(rel))
            if not dry_run:
                rel.write_text(new, encoding="utf-8")
        res.relationships += n
    return res


# ───────────────────────── declaration / labels ─────────────────────────

def declaration_pattern(name: str):
    return re.compile(r"^(\tmeasure\s+)" + _name_forms(name) + r"(\s*=)",
                      re.IGNORECASE | re.MULTILINE)


def declares(text: str, name: str) -> bool:
    return bool(declaration_pattern(name).search(text))


def rename_declaration(text: str, old: str, new: str) -> Optional[str]:
    """Rename the declaration line of measure `old` (any quoting, any case)
    to `new`; the body is untouched. None when `old` is not declared.
    Refuses a result whose renamed block holds more than one fence pair."""
    pat = declaration_pattern(old)
    if not pat.search(text):
        return None
    out = pat.sub(lambda m: m.group(1) + quote_name(new) + m.group(2), text,
                  count=1)
    from .tmdl_edit import extract_block
    block = extract_block(out, "measure", new)
    if block is None or block.count("```") % 2 or block.count("```") > 2:
        raise TmdlEditError(
            f"renaming measure {old!r} to {new!r} leaves a block that cannot "
            f"be read back with balanced fences. Nothing was written.")
    return out


def rename_labels(text: str, measure: str, old_label: str,
                  new_label: str) -> tuple:
    """(new text, count): the first element of each field-parameter row
    whose label is `old_label` (any case) AND whose NAMEOF points at
    `measure`. Rows naming another measure are untouched."""
    pat = re.compile(
        r'(\(\s*)"' + re.escape(old_label) + r'"(\s*,\s*NAMEOF\s*\(\s*'
        r"(?:'[^']*'|[A-Za-z_][\w]*)?\s*\[" + re.escape(measure)
        + r"\]\s*\))", re.IGNORECASE)
    label = new_label.replace('"', '""')
    return pat.subn(lambda m: m.group(1) + '"' + label + '"' + m.group(2),
                    text)


def declared_measures(definition) -> set:
    """Names of measures declared at one-tab depth in `tables/*.tmdl`."""
    out = set()
    decl = re.compile(r"^\tmeasure\s+('(?:[^']|'')*'|\"[^\"]*\"|[^=]+?)\s*=")
    tables = Path(definition) / "tables"
    if not tables.is_dir():
        return out
    for f in tables.glob("*.tmdl"):
        for ln in te.read_text(f).split("\n"):
            m = decl.match(ln)
            if m:
                raw = m.group(1).strip()
                if raw.startswith("'"):
                    raw = raw[1:-1].replace("''", "'")
                elif raw.startswith('"'):
                    raw = raw[1:-1]
                out.add(raw)
    return out


# ───────────────────────── the rename map ─────────────────────────

@dataclass
class RenameMap:
    measures: dict = field(default_factory=dict)
    columns: dict = field(default_factory=dict)
    profiles: dict = field(default_factory=dict)   # name -> RenameMap

    @classmethod
    def from_config(cls, raw) -> "RenameMap":
        raw = raw or {}
        if not isinstance(raw, dict):
            raise DriftDoctorError("`renames` must be a mapping with "
                                   "`measures`, `columns` and `profiles`")
        unknown = sorted(set(raw) - {"measures", "columns", "profiles",
                                      "pairing"})
        if unknown:
            raise DriftDoctorError(f"renames: unknown key(s) "
                                   f"{', '.join(unknown)}")
        out = cls(measures={str(k): "" if v is None else str(v) for k, v in
                            (raw.get("measures") or {}).items()},
                  columns={str(k): "" if v is None else str(v) for k, v in
                           (raw.get("columns") or {}).items()})
        for name, prof in (raw.get("profiles") or {}).items():
            out.profiles[str(name)] = cls.from_config(
                {k: v for k, v in (prof or {}).items() if k != "profiles"})
        return out

    def for_profile(self, profile: Optional[str]) -> "RenameMap":
        """A selected profile REPLACES the default maps (no merge)."""
        if profile and profile in self.profiles:
            return self.profiles[profile]
        return RenameMap(dict(self.measures), dict(self.columns))

    def active_measures(self) -> dict:
        return {o: n for o, n in self.measures.items() if o and n}

    def measure(self, old: str) -> Optional[str]:
        return self.measures.get(old) or None

    def column(self, table: str, old: str) -> Optional[tuple]:
        new = self.columns.get(f"{table}.{old}")
        t, c = split_column_path(new or "")
        return (t, c) if t else None

    def dump(self, path) -> None:
        doc = {"measures": self.measures, "columns": self.columns}
        Path(path).write_text(json.dumps(doc, indent=2, sort_keys=True,
                                         ensure_ascii=False) + "\n",
                              encoding="utf-8")

    def load(self, path) -> "RenameMap":
        """MERGE a dumped map into this one; a missing file is a no-op."""
        path = Path(path)
        if not path.is_file():
            return self
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.measures.update(doc.get("measures") or {})
        self.columns.update(doc.get("columns") or {})
        return self

    def check(self, definition) -> tuple:
        """(valid count, [(old, new)] whose target the model lacks)."""
        if not Path(definition).is_dir():
            return 0, []
        names = declared_measures(definition)
        valid, invalid = 0, []
        for old, new in self.active_measures().items():
            if new in names:
                valid += 1
            else:
                invalid.append((old, new))
        return valid, invalid


def fleet_rename_map(fleet) -> RenameMap:
    return RenameMap.from_config(fleet.setting("renames"))


def model_profile(fleet, name: str) -> Optional[str]:
    entry = (fleet.raw.get("models") or {}).get(fleet.entry_of.get(name, name))
    if isinstance(entry, dict):
        return entry.get("rename_profile") or None
    return None


# ───────────────────────── report layer ─────────────────────────

def report_roots(model_dir) -> list:
    """Every ``*.Report`` folder at any depth under the model's parent."""
    base = Path(model_dir).parent
    return sorted(p for p in base.rglob("*.Report") if p.is_dir())


def report_changes(model_dir, old: str, new: str, old_label=None,
                   new_label=None, visited: Optional[set] = None) -> tuple:
    """([(path, old text, new text)], references) for renaming measure
    `old` in every report below the model's folder. The rewrite itself is
    pbi-plot-styler's; without it installed, nothing is planned and the
    second element is None."""
    try:
        from pbi_plot_styler import rename as styler
    except ImportError:
        return [], None
    out, total = [], 0
    for root in report_roots(model_dir):
        key = root.resolve()
        if visited is not None:
            if key in visited:
                continue
            visited.add(key)
        for path in styler.report_files(root):
            try:
                raw = path.read_bytes()
                text = raw.decode("utf-8-sig")
                doc = json.loads(text)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            hits = styler.rewrite_tree(doc, old, new, old_label, new_label)
            if not hits:
                continue
            total += hits
            out.append((path, raw, _dump_like(styler, raw, doc)))
    return out, total


def _dump_like(styler, raw: bytes, doc) -> bytes:
    layout = getattr(styler, "Layout", None)
    dump = getattr(styler, "dump_json", None)
    if layout is not None and dump is not None:
        return layout.of(raw).encode(dump(doc))
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode()


def count_report_refs(model_dir, name: str, label=None) -> Optional[int]:
    try:
        from pbi_plot_styler import rename as styler
    except ImportError:
        return None
    return styler.count_measure_refs(report_roots(model_dir), name, label)


# ───────────────────────── format precision ─────────────────────────

def format_decimals(fmt: Optional[str]) -> Optional[int]:
    """Decimal places a format string shows: ``P<n>`` -> n; otherwise the
    run of 0/# after the first '.' that follows a digit placeholder; 0 for
    a numeric format with no fraction; None when not numeric."""
    if fmt is None:
        return None
    s = fmt.strip()
    m = re.fullmatch(r"[Pp](\d+)", s)
    if m:
        return int(m.group(1))
    m = re.search(r"[0#][0#,]*\.([0#]*)", s)
    if m:
        return len(m.group(1))
    return 0 if re.search(r"[0#]", s) else None


def _split_args(body: str) -> list:
    """Top-level comma split of a call's argument text (strings and nested
    brackets respected)."""
    from .tmdl import _split_top_level
    return [a.strip() for a in _split_top_level(body)]


def switch_calls(text: str) -> list:
    """[(start, end, args)] for every ``SWITCH(`` call in `text`, where
    start/end delimit the argument text."""
    from .tmdl import _string_end
    out = []
    for m in re.finditer(r"\bSWITCH\s*\(", text, re.IGNORECASE):
        depth, j = 1, m.end()
        while j < len(text) and depth:
            c = text[j]
            if c == '"':
                j = _string_end(text, j)
                continue
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            j += 1
        if depth == 0:
            out.append((m.end(), j - 1, _split_args(text[m.end():j - 1])))
    return out


_CONTAINS = re.compile(r'^CONTAINSSTRING\s*\(\s*(.+?)\s*,\s*"((?:[^"]|"")*)"\s*\)$',
                       re.IGNORECASE | re.DOTALL)


def resolve_formats(text: str, measure: str) -> list:
    """[(path label, format or None)] the format-selector `text` gives a
    measure named `measure`, one per SWITCH call."""
    out = []
    low = measure.lower()
    for n, (_s, _e, args) in enumerate(switch_calls(text), 1):
        if len(args) < 3:
            continue
        pairs = args[1:]
        default = pairs[-1] if len(pairs) % 2 else None
        chosen = default
        for i in range(0, len(pairs) - 1, 2):
            m = _CONTAINS.match(pairs[i])
            if m and m.group(2).replace('""', '"').lower() in low:
                chosen = pairs[i + 1]
                break
        fmt = None
        if chosen is not None:
            lit = re.fullmatch(r'"((?:[^"]|"")*)"', chosen.strip())
            fmt = lit.group(1).replace('""', '"') if lit else None
        out.append(("plain measure" if n == 1 else f"switch {n}", fmt))
    return out


def precision_risks(text: str, old: str, new: str) -> list:
    """[(path, before, after)] where `new` would show fewer decimals."""
    risks = []
    for (path, before), (_p, after) in zip(resolve_formats(text, old),
                                           resolve_formats(text, new)):
        b, a = format_decimals(before), format_decimals(after)
        if b is not None and a is not None and a < b:
            risks.append((path, before, after))
    return risks


def insert_format_arm(text: str, new: str, fmt: str,
                      family: str = "cost") -> tuple:
    """(new text, arms inserted): in every SWITCH with >= 3 arguments, put
    ``CONTAINSSTRING(<selector>, "<new>"), "<fmt>",`` before the first
    condition testing for the `family` literal. Idempotent: nothing is
    inserted when the lower-cased name is already a quoted literal."""
    low = new.lower()
    if re.search(r'"' + re.escape(low) + r'"', text, re.IGNORECASE):
        return text, 0
    edits = []
    for start, end, args in switch_calls(text):
        if len(args) < 3:
            continue
        for i in range(1, len(args) - 1, 2):
            m = _CONTAINS.match(args[i])
            if m and m.group(2).lower().startswith(family.lower()):
                # locate the argument in the original text
                pos = text.find(args[i], start, end)
                if pos < 0:
                    break
                line_start = text.rfind("\n", 0, pos) + 1
                indent = text[line_start:pos]
                if indent.strip():
                    indent = ""
                arm = (f'CONTAINSSTRING({m.group(1)}, "{low}"), "{fmt}",'
                       + ("\n" + indent if indent else " "))
                edits.append((pos, arm))
                break
    for pos, arm in sorted(edits, reverse=True):
        text = text[:pos] + arm + text[pos:]
    return text, len(edits)


# ───────────────────────── fleet-wide measure rename ─────────────────────────

@dataclass
class ModelPlan:
    name: str
    definition: Path
    changes: dict = field(default_factory=dict)     # Path -> (old, new)
    labels: int = 0
    decl: int = 0
    dax: int = 0
    reports: Optional[int] = 0
    format_arm: int = 0
    risks: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def stage(self, path: Path, old, new) -> None:
        first = self.changes.get(path, (old, None))[0]
        self.changes[path] = (first, new)

    def current(self, path: Path) -> str:
        if path in self.changes:
            return self.changes[path][1]
        return te.read_text(path)


def _targets(fleet, model_filters) -> list:
    """(name, model dir) for the template and every fleet model, deduped."""
    seen, out = set(), []
    for name, path in [("template", fleet.template)] + list(
            fleet.models.items()):
        key = Path(path).resolve()
        if key in seen:
            continue
        rel = str(Path(path))
        if model_filters and not any(s in rel or s == name
                                     for s in model_filters):
            continue
        seen.add(key)
        out.append((name, Path(path)))
    return out


def plan_measure_rename(fleet, old: str, new: str, *, old_label=None,
                        new_label=None, format_arms: Optional[dict] = None,
                        model_filters=()) -> list:
    """Plan `old` -> `new` across every model that declares `old`. Nothing
    is written; the plan holds every file change and the counters."""
    conf = fleet.setting("renames") or {}
    conf = conf if isinstance(conf, dict) else {}
    fmt_table = conf.get("format_table")
    family = conf.get("format_family") or "cost"
    old_label = old_label or old
    new_label = new_label or new
    plans, visited = [], set()
    for name, model_dir in _targets(fleet, model_filters):
        tables = model_dir / "definition" / "tables"
        if not tables.is_dir():
            continue
        files = sorted(tables.glob("*.tmdl"), key=lambda p: p.name)
        declaring = [f for f in files
                     if declares(te.read_text(f), old)]
        if not declaring:
            continue
        plan = ModelPlan(name, model_dir / "definition")
        # a. format check (inspects the old name, so it runs first)
        if fmt_table:
            fpath = tables / f"{fmt_table}.tmdl"
            if fpath.is_file():
                text = plan.current(fpath)
                risks = precision_risks(text, old, new)
                arm = _arm_for(format_arms or {}, model_dir, name)
                if risks and arm:
                    updated, n = insert_format_arm(text, new, arm, family)
                    if n:
                        plan.stage(fpath, text, updated)
                        plan.format_arm += n
                    risks = precision_risks(updated, old, new)
                plan.risks = risks
        # b. picker labels
        for f in files:
            text = plan.current(f)
            updated, n = rename_labels(text, old, old_label, new_label)
            if n:
                plan.stage(f, text, updated)
                plan.labels += n
        # c. declaration
        for f in declaring:
            text = plan.current(f)
            try:
                updated = rename_declaration(text, old, new)
            except TmdlEditError as e:
                plan.errors.append(f"{f}: {e}")
                continue
            if updated is not None:
                plan.stage(f, text, updated)
                plan.decl += 1
        # d. DAX references and text comparisons
        for f in files:
            text = plan.current(f)
            updated, _m, n = rewrite_measure_text(text, {old: new})
            if n:
                plan.stage(f, text, updated)
                plan.dax += n
        # e. reports (a shared visited set keeps totals honest)
        reports, n = report_changes(model_dir, old, new,
                                    old_label if old_label != old else None,
                                    new_label if new_label != new else None,
                                    visited)
        plan.reports = n
        for path, raw, data in reports:
            plan.changes[path] = (raw, data)
        plans.append(plan)
    return plans


def _arm_for(arms: dict, model_dir: Path, name: str) -> Optional[str]:
    rel = str(model_dir).replace(".SemanticModel", "")
    for key, fmt in arms.items():
        if key == name or key in rel:
            return fmt
    return None


def write_plan(plan: ModelPlan) -> None:
    """Validate every staged TMDL file first, then write them all."""
    from .tmdl_edit import assert_tmdl_valid
    for path, (_old, new) in plan.changes.items():
        if isinstance(new, str) and str(path).endswith(".tmdl"):
            assert_tmdl_valid(new)
    for path, (_old, new) in plan.changes.items():
        if isinstance(new, bytes):
            Path(path).write_bytes(new)
        else:
            Path(path).write_text(new, encoding="utf-8")

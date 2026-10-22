"""Field-parameter tables: a lossless row reader/writer and the drift kinds
built on it.

A field parameter is a calculated table whose partition source is a DAX
table constructor of tuples::

    { ( "Display", NAMEOF ( [Measure] ), 3, "Group", "extra", ... ), ... }

Every command that adds, replaces or removes rows goes through
``write_rows``. Its contract: rows may be re-sorted by ordinal, but every
live row, every commented-out ("parked") row and every comment payload
(group headers are regenerated; annotations stay above their row; inline
trailers stay on their row) survives, the last tuple never keeps a comma,
and writing the same rows twice changes nothing.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import DriftDoctorError, TmdlEditError
from . import tmdl_edit as te

PAD_VALUE = "Other"


class FieldParamSchemaError(DriftDoctorError):
    """A field-parameter table whose schema cannot be modernized safely."""


# ───────────────────────── comment masking ─────────────────────────

def mask_line_comments(text: str) -> str:
    """Drop ``--`` and ``//`` line comments. A marker inside a double-quoted
    string or a single-quoted name is content, never a comment."""
    out = []
    for line in text.split("\n"):
        i, n, cut = 0, len(line), None
        while i < n:
            c = line[i]
            if c in "\"'":
                j = i + 1
                while j < n:
                    if line[j] == c:
                        if j + 1 < n and line[j + 1] == c:
                            j += 2
                            continue
                        break
                    j += 1
                i = j + 1
                continue
            if line.startswith("--", i) or line.startswith("//", i):
                cut = i
                break
            i += 1
        out.append(line if cut is None else line[:cut].rstrip())
    return "\n".join(out)


# ───────────────────────── rows ─────────────────────────

_STR = r'"(?:[^"]|"")*"'
TUPLE_RE = re.compile(
    r"\(\s*(?P<display>\"(?:[^\"]|\"\")+\"|\[[^\]]*\])\s*,\s*"
    r"(?P<ref>NAMEOF\s*\((?:[^()]|\([^()]*\))*\))\s*,\s*"
    r"(?P<order>-?\d+)\s*,\s*"
    r"(?P<group>\"(?:[^\"]|\"\")+\")"
    r"(?P<extras>(?:\s*,\s*" + _STR + r")*)\s*\)"
    r"(?P<comma>[ \t]*,)?")


def _unq(s: str) -> str:
    return s[1:-1].replace('""', '"')


@dataclass
class Row:
    display: str
    ref: str
    order: int
    group: str
    extras: list = field(default_factory=list)
    raw: str = ""
    bracket: bool = False        # display written as [ ... ]

    @property
    def width(self) -> int:
        return 4 + len(self.extras)

    @property
    def key(self) -> str:
        """Display key: trimmed, outer quotes stripped, lower-cased."""
        return self.display.strip().strip("\"'").lower()

    def target(self) -> Optional[tuple]:
        """(table or "", member) of the NAMEOF reference, or None."""
        return nameof_target(self.ref)


def _row_from_match(m) -> Row:
    disp = m.group("display")
    bracket = disp.startswith("[")
    extras = re.findall(_STR, m.group("extras") or "")
    return Row(display=disp if bracket else _unq(disp), ref=m.group("ref"),
               order=int(m.group("order")), group=_unq(m.group("group")),
               extras=[_unq(e) for e in extras],
               raw=m.group(0).rstrip(), bracket=bracket)


def parse_rows(text: str) -> tuple:
    """(live rows in file order, arity) of a table file's text. A tuple with
    a comment marker before it on its line is a disabled row and skipped."""
    rows = []
    for m in TUPLE_RE.finditer(text or ""):
        line_start = text.rfind("\n", 0, m.start()) + 1
        prefix = text[line_start:m.start()]
        if mask_line_comments(prefix) != prefix:
            continue
        rows.append(_row_from_match(m))
    arity = max([4] + [r.width for r in rows])
    return rows, arity


def read_rows(path) -> tuple:
    """(rows, arity) of a table file; a missing file has none (arity 4)."""
    path = Path(path)
    if not path.is_file():
        return [], 4
    return parse_rows(te.read_text(path))


_NAMEOF_ARG = re.compile(r"^NAMEOF\s*\((.*)\)$", re.DOTALL)


def nameof_target(ref: str) -> Optional[tuple]:
    """``NAMEOF('T'[m])`` -> (T, m); ``NAMEOF([m])`` -> ("", m). A quoted
    member ``'T'['m']`` has its quotes stripped. None when unparseable."""
    m = _NAMEOF_ARG.match((ref or "").strip())
    if not m:
        return None
    inner = m.group(1).strip()
    t = re.fullmatch(r"(?:'((?:[^']|'')*)'|\"([^\"]*)\"|([^\[\]'\"]*?))\s*"
                     r"\[(.+)\]", inner, re.DOTALL)
    if not t:
        return None
    table = (t.group(1) or "").replace("''", "'") if t.group(1) is not None \
        else (t.group(2) if t.group(2) is not None else (t.group(3) or ""))
    member = t.group(4).strip()
    if len(member) >= 2 and member[0] == member[-1] == "'":
        member = member[1:-1].replace("''", "'")
    return table.strip(), member


def raw_field_count(raw: str) -> int:
    """Commas at parenthesis depth 1, outside strings, plus one."""
    depth, count, i = 0, 0, 0
    while i < len(raw):
        c = raw[i]
        if c == '"':
            j = raw.find('"', i + 1)
            while j != -1 and raw[j + 1:j + 2] == '"':
                j = raw.find('"', j + 2)
            i = len(raw) if j == -1 else j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        elif c == "," and depth == 1:
            count += 1
        i += 1
    return count + 1


def _q(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def render_row(row: Row, arity: int, pad: str = PAD_VALUE) -> str:
    """The tuple text at `arity` fields. A row read from a file that is
    already wide enough is written exactly as it was read (never
    truncated)."""
    if row.raw and raw_field_count(row.raw) >= arity:
        return row.raw
    extras = list(row.extras)
    while 4 + len(extras) < arity:
        extras.append(pad)
    disp = row.display if row.bracket or row.display.startswith("[") \
        else _q(row.display)
    fields = [disp, row.ref, str(row.order), _q(row.group)] + \
        [_q(e) for e in extras]
    return "( " + ", ".join(fields) + " )"


def _norm(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    return t[:-1].rstrip() if t.endswith(",") else t


# ───────────────────────── writer ─────────────────────────

@dataclass
class _Block:
    lines: list
    open_i: int          # index of the "{" line
    close_i: int         # index of the "}" line
    fenced: bool
    source_indent: str


def _locate(lines: list, path) -> _Block:
    """The constructor of the table's calculated partition (fenced first)."""
    found = []
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)source\s*=\s*(```)?\s*$", ln)
        if not m:
            continue
        # must sit inside a calculated partition
        head = next((lines[k] for k in range(i - 1, -1, -1)
                     if re.match(r"^\tpartition\b", lines[k])), "")
        if not re.search(r"=\s*calculated\s*$", head):
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines) or lines[j].strip() != "{":
            continue
        k = j + 1
        while k < len(lines) and lines[k].strip() != "}":
            k += 1
        if k >= len(lines):
            continue
        found.append(_Block(lines, j, k, bool(m.group(2)), m.group(1)))
    if not found:
        raise TmdlEditError(
            f"{path}: no calculated partition with a `source =` table "
            f"constructor was found; the file was left untouched")
    fenced = [b for b in found if b.fenced]
    return (fenced or found)[0]


_END_RUN = re.compile(r'((?:"(?:[^"]|"")*"\s*,\s*)*"(?:[^"]|"")*")\s*\)\s*,?\s*$')


def _line_group(text: str) -> Optional[str]:
    m = TUPLE_RE.search(text)
    if m:
        return _unq(m.group("group"))
    m = _END_RUN.search(text)
    if not m:
        return None
    first = re.match(_STR, m.group(1))
    return _unq(first.group(0)) if first else None


def write_rows(path, rows: list, arity: int, pad: str = PAD_VALUE) -> str:
    """Rewrite the row block of `path` with `rows` at `arity` fields (see
    the module docstring for what survives). Returns the new text; raises
    when the constructor cannot be found (the file is untouched then). The
    file keeps its own line endings."""
    path = Path(path)
    text = te.read_text(path)
    new = rewrite_rows_text(text, rows, arity, pad, path)
    if new != text:
        te.write_text(path, new, te.line_ending(text))
    return new


def rewrite_rows_text(text: str, rows: list, arity: int,
                      pad: str = PAD_VALUE, path="table") -> str:
    eol = te.line_ending(text)
    lines = text.replace("\r\n", "\n").split("\n")
    b = _locate(lines, path)
    old = lines[b.open_i + 1:b.close_i]
    indent = next((ln[:len(ln) - len(ln.lstrip())] for ln in old
                   if ln.strip().startswith(("(", "--", "//"))), "")

    # classify the old block
    live_keys = []                 # normalized text of each live row, in order
    groups_used = set()
    block_text = "\n".join(old)
    for m in TUPLE_RE.finditer(block_text):
        groups_used.add(_unq(m.group("group")))
    annotations: dict = {}         # norm key -> [annotation lines]
    trailers: dict = {}            # norm key -> [trailer text]
    disabled: list = []            # (group or None, text)
    unparsed: list = []            # (group or None, text)
    pending: list = []
    i = 0
    while i < len(old):
        s = old[i].strip()
        if not s:
            i += 1
            continue
        if s.startswith(("--", "//")):
            body = s.lstrip("-/ \t")
            tm = TUPLE_RE.search(s)
            if tm:
                disabled.append((_unq(tm.group("group")), s))
            elif not body or (body in groups_used and not re.search(
                    r"\(.*NAMEOF", s)):
                pass                               # header: regenerated
            else:
                pending.append(s)
            i += 1
            continue
        if s.startswith("("):
            # a live tuple may span several lines
            offset = sum(len(x) + 1 for x in old[:i]) + (len(old[i])
                                                         - len(old[i].lstrip()))
            m = TUPLE_RE.match(block_text, offset)
            if m:
                end_line = block_text.count("\n", 0, m.end())
                key = _norm(m.group(0))
                after = old[end_line][m.end() - (sum(
                    len(x) + 1 for x in old[:end_line])):]
                trail = after.rstrip()
                if trail.strip():
                    trailers.setdefault(key, []).append(trail)
                if pending:
                    annotations.setdefault(key, []).extend(pending)
                    pending = []
                live_keys.append(key)
                i = end_line + 1
                continue
            unparsed.append((_line_group(s), s))
            i += 1
            continue
        unparsed.append((_line_group(s), s))
        i += 1

    # order and group the new rows
    ordered = sorted(enumerate(rows), key=lambda p: (p[1].order, p[0]))
    groups: dict = {}
    for _n, row in ordered:
        groups.setdefault(row.group, []).append(row)

    out: list = []                 # (text, trailer)
    used_ann: set = set()

    def emit_rows(group):
        for row in groups.get(group, []):
            rendered = render_row(row, arity, pad)
            key = _norm(row.raw) if row.raw else _norm(rendered)
            for ann in annotations.pop(key, []):
                if ann not in used_ann:
                    out.append((indent + ann, ""))
                    used_ann.add(ann)
            trail = ""
            queue = trailers.get(key) or []
            if queue:
                trail = queue.pop(0)
            line = rendered if rendered.rstrip().endswith(",") \
                else rendered + ","
            out.append((indent + line, trail))

    seen_groups = list(groups)
    for g in seen_groups:
        out.append((indent + "-- " + g, ""))
        emit_rows(g)
        for grp, s in unparsed:
            if grp == g:
                out.append((indent + (s if s.endswith(",") else s + ","), ""))
        for grp, s in disabled:
            if grp == g:
                out.append((indent + s, ""))
        out.append(("", ""))
    extra_groups = []
    for grp, _s in disabled + unparsed:
        if grp is not None and grp not in groups and grp not in extra_groups:
            extra_groups.append(grp)
    for g in extra_groups:
        out.append((indent + "-- " + g, ""))
        for grp, s in unparsed:
            if grp == g:
                out.append((indent + (s if s.endswith(",") else s + ","), ""))
        for grp, s in disabled:
            if grp == g:
                out.append((indent + s, ""))
        out.append(("", ""))
    for grp, s in unparsed:
        if grp is None:
            out.append((indent + (s if s.endswith(",") else s + ","), ""))
    for grp, s in disabled:
        if grp is None:
            out.append((indent + s, ""))

    while out and not out[-1][0].strip():
        out.pop()
    for k in range(len(out) - 1, -1, -1):
        t = out[k][0].strip()
        if t and not t.startswith(("--", "//")):
            out[k] = (out[k][0].rstrip().rstrip(","), out[k][1])
            break
    body = [t + tr for t, tr in out]

    block_indent = (b.source_indent + "\t\t") if b.source_indent else "\t\t\t\t"
    head = lines[:b.open_i + 1]
    tail_start = b.close_i + 1
    closing = [block_indent + "}"]
    if b.fenced:
        # the old closing fence follows the "}" line
        if tail_start < len(lines) and lines[tail_start].strip() == "```":
            tail_start += 1
        closing.append(block_indent + "```")
    else:
        while tail_start < len(lines) and lines[tail_start].strip() and \
                set(lines[tail_start].strip()) == {"`"}:
            tail_start += 1
    return eol.join(head + body + closing + lines[tail_start:])


# ───────────────────────── which tables ─────────────────────────

DEFAULT_FAMILIES = (
    {"prefixes": ["x-Selector", "x-Plot Specific"], "role": "metric"},
    {"prefixes": ["y-Selector", "y-Plot Specific"], "role": "dimension"},
)


def _settings(fleet) -> dict:
    conf = fleet.setting("fieldparam") or {}
    return conf if isinstance(conf, dict) else {}


def configured_globs(fleet) -> list:
    globs = fleet.setting("fieldparam_tables")
    if isinstance(globs, str):
        globs = [globs]
    return list(globs or [])


def is_constructor_table(text: str) -> bool:
    """A calculated table whose source is a constructor of NAMEOF tuples."""
    return bool(re.search(r"=\s*calculated\s*$", text, re.MULTILINE)) and \
        bool(TUPLE_RE.search(text))


def fieldparam_files(fleet, definition: Path) -> dict:
    """{table file stem: path} of the model's field-parameter tables:
    `fieldparam_tables` globs when configured, else every table whose
    partition is a calculated constructor of NAMEOF tuples."""
    tables = Path(definition) / "tables"
    if not tables.is_dir():
        return {}
    globs = configured_globs(fleet)
    out = {}
    for f in sorted(tables.glob("*.tmdl")):
        if globs:
            if any(fnmatch.fnmatchcase(f.stem, g) for g in globs):
                out[f.stem] = f
        elif is_constructor_table(f.read_text(encoding="utf-8")):
            out[f.stem] = f
    return out


def families(fleet) -> list:
    conf = _settings(fleet).get("families")
    return list(conf or DEFAULT_FAMILIES)


def family_members(fleet, files: dict) -> list:
    """[(role, {level n: path}, {variant name: path})] per family."""
    out = []
    for fam in families(fleet):
        prefixes = list(fam.get("prefixes") or [])
        if not prefixes:
            continue
        levels, variants = {}, {}
        level_re = re.compile(r"^" + re.escape(prefixes[0]) + r" (\d+)$")
        for stem, path in files.items():
            m = level_re.match(stem)
            if m:
                levels[int(m.group(1))] = path
                continue
            if any(stem == p or stem.startswith(p + " ") for p in prefixes[1:]):
                variants[stem] = path
        out.append((fam.get("role") or "metric", dict(sorted(levels.items())),
                    variants))
    return out


# ───────────────────────── schema modernization ─────────────────────────

@dataclass
class SchemaPlan:
    derived: Path
    template: Path
    add: list = field(default_factory=list)
    rewrite: list = field(default_factory=list)
    reorder: bool = False
    misfit_rows: int = 0
    target_arity: int = 4
    t_positions: dict = field(default_factory=dict)   # n -> column name
    d_positions: dict = field(default_factory=dict)

    @property
    def noop(self) -> bool:
        return not (self.add or self.rewrite or self.reorder
                    or self.misfit_rows)

    def describe(self) -> str:
        parts = []
        if self.add:
            parts.append("add column(s) " + ", ".join(self.add))
        if self.rewrite:
            parts.append("rewrite column block(s) " + ", ".join(self.rewrite))
        if self.reorder:
            parts.append("restore the template's column order")
        if self.misfit_rows:
            parts.append(f"remap {self.misfit_rows} row(s) to "
                         f"{self.target_arity} fields")
        return "; ".join(parts)


def _layout(path: Path) -> tuple:
    """(Table, {position: column name}) with the layout guard applied."""
    from .tmdl import parse_table_file
    text = path.read_text(encoding="utf-8")
    table = parse_table_file(path)
    problems = []
    if table.measures:
        problems.append("it hosts measures")
    if table.hierarchies:
        problems.append("it has hierarchies")
    if re.search(r"^\tcalculationGroup\b", text, re.MULTILINE):
        problems.append("it is a calculation group")
    if len(table.partitions) != 1:
        problems.append(f"it has {len(table.partitions)} partitions")
    if not table.columns:
        problems.append("it has no columns")
    if problems:
        raise FieldParamSchemaError(f"{path.name}: {'; '.join(problems)}")
    positions = {}
    for col in table.columns.values():
        if col.expression:
            continue
        m = re.fullmatch(r"\[Value(\d+)\]", col.properties.get("sourceColumn",
                                                               "").strip())
        if not m:
            continue
        n = int(m.group(1))
        if n in positions:
            raise FieldParamSchemaError(
                f"{path.name}: columns {positions[n]} and {col.name} both "
                f"feed tuple position {n}")
        positions[n] = col.name
    return table, positions


def _column_block(text: str, name: str) -> str:
    from . import tmdl_edit as te
    return te.extract_block(text, "column", name) or ""


def _untagged(block: str) -> str:
    return re.sub(r"(lineageTag:\s*)\S+", r"\1", block).rstrip()


def build_schema_plan(derived, template) -> SchemaPlan:
    """The modernization plan for one derived/template table pair; raises
    FieldParamSchemaError when the pair must be left to a person."""
    derived, template = Path(derived), Path(template)
    d_table, d_pos = _layout(derived)
    t_table, t_pos = _layout(template)
    for n in (1, 2, 3, 4):
        if d_pos.get(n) != t_pos.get(n):
            raise FieldParamSchemaError(
                f"{derived.name}: position {n} is fed by {d_pos.get(n)!r} "
                f"here and {t_pos.get(n)!r} in the template; cannot remap "
                f"safely")
    extras = sorted(n for n in t_pos if n > 4)
    if extras != list(range(5, 5 + len(extras))):
        raise FieldParamSchemaError(
            f"{template.name}: the template's extra positions are not "
            f"contiguous from 5 ({extras})")
    target = 4 + len(extras)
    t_rows, t_arity = read_rows(template)
    if t_rows and t_arity != target:
        raise FieldParamSchemaError(
            f"{template.name}: the template's rows have {t_arity} fields but "
            f"its columns declare {target}; fix the template first")
    own = sorted(set(d_table.columns) - set(t_table.columns))
    if own:
        raise FieldParamSchemaError(
            f"{derived.name}: derived-only field-parameter column(s) "
            f"{', '.join(own)} are the model's own customization (a "
            f"client-only axis column is never stripped); left untouched")
    d_text = derived.read_text(encoding="utf-8")
    t_text = template.read_text(encoding="utf-8")
    plan = SchemaPlan(derived, template, target_arity=target,
                      t_positions=t_pos, d_positions=d_pos)
    for name in t_table.columns:
        if name not in d_table.columns:
            plan.add.append(name)
        elif _untagged(_column_block(d_text, name)) != \
                _untagged(_column_block(t_text, name)):
            plan.rewrite.append(name)
    shared = [c for c in d_table.columns if c in t_table.columns]
    plan.reorder = shared != [c for c in t_table.columns if c in shared]
    rows, _a = parse_rows(d_text)
    plan.misfit_rows = sum(1 for r in rows if r.width != target)
    return plan


def _column_section(text: str, names) -> Optional[tuple]:
    """(start line, end line) spanning every column block of `names`."""
    from . import tmdl_edit as te
    spans = [te.block_span(text, "column", n) for n in names]
    spans = [s for s in spans if s]
    if not spans:
        return None
    return min(s[0] for s in spans), max(s[1] for s in spans)


def modernize_schema(derived, template, write: bool = True) -> str:
    """Bring a derived field-parameter table to the template's schema:
    column blocks as in the template (each existing column keeps its
    lineage tag, new ones get a fresh one) and every live row remapped to
    the template's positions. Returns the new text; raises on a blocked
    pair or a failed verification (nothing written then)."""
    from . import tmdl_edit as te
    plan = build_schema_plan(derived, template)
    d_text = Path(derived).read_text(encoding="utf-8")
    if plan.noop:
        return d_text
    t_text = Path(template).read_text(encoding="utf-8")
    from .tmdl import parse_table_file
    d_cols = list(parse_table_file(Path(derived)).columns)
    t_cols = list(parse_table_file(Path(template)).columns)

    blocks = []
    for name in t_cols:
        block = _column_block(t_text, name).rstrip("\n")
        if name in d_cols:
            tag = te.first_lineage_tag(_column_block(d_text, name))
            block = te.set_first_lineage_tag(block, tag) if tag \
                else te.regenerate_lineage_tag(block)
        else:
            block = te.regenerate_lineage_tag(block)
        blocks.append(block)
    lines = d_text.split("\n")
    span = _column_section(d_text, d_cols)
    if span is None:
        raise FieldParamSchemaError(f"{Path(derived).name}: no column blocks")
    new_lines = lines[:span[0]] + "\n\n".join(blocks).split("\n") + [""] \
        + lines[span[1]:]
    text = "\n".join(new_lines)

    # remap live rows to the template's positions
    t_rows, _ = read_rows(template)
    by_name = {v: k for k, v in plan.d_positions.items()}
    fill = {}
    for n in range(5, plan.target_arity + 1):
        values = [r.extras[n - 5] for r in t_rows if len(r.extras) > n - 5]
        fill[n] = max(set(values), key=values.count) if values else PAD_VALUE
    before = [(r.display, r.ref, r.order, r.group) for r in parse_rows(text)[0]]

    def remap(m):
        prefix = text[text.rfind("\n", 0, m.start()) + 1:m.start()]
        if mask_line_comments(prefix) != prefix:
            return m.group(0)
        extras = [_unq(e) for e in re.findall(_STR, m.group("extras") or "")]
        new_extras = []
        for n in range(5, plan.target_arity + 1):
            src = by_name.get(plan.t_positions[n])
            if src is not None and src > 4 and len(extras) > src - 5:
                new_extras.append(extras[src - 5])
            else:
                new_extras.append(fill[n])
        head = m.group(0)[:m.start("extras") - m.start()]
        tail = m.group(0)[m.end("extras") - m.start():]
        return head + "".join(", " + _q(e) for e in new_extras) + tail

    text = TUPLE_RE.sub(remap, text)
    after_rows, arity = parse_rows(text)
    after = [(r.display, r.ref, r.order, r.group) for r in after_rows]
    if after != before or any(r.width != plan.target_arity
                              for r in after_rows):
        raise FieldParamSchemaError(
            f"{Path(derived).name}: the remapped rows do not read back as "
            f"the original rows; nothing was written")
    te.assert_tmdl_valid(text)
    if write:
        Path(derived).write_text(text, encoding="utf-8")
    return text


# ───────────────────────── blank option row ─────────────────────────

BLANK_CATEGORY = "Blank Dimension_y"


class TemplateUnavailable(DriftDoctorError):
    """The template carries no blank option row to copy."""


def _blank_conf(fleet) -> tuple:
    conf = _settings(fleet).get("blank_row") or {}
    tables = conf.get("tables") or ["y-*"]
    if isinstance(tables, str):
        tables = [tables]
    return conf.get("category") or BLANK_CATEGORY, list(tables)


def _elements(row_text: str) -> list:
    """Top-level elements of a one-line tuple (quotes and brackets kept)."""
    from .tmdl import _split_top_level
    inner = row_text.strip().rstrip(",").strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    return [e.strip() for e in _split_top_level(inner)]


def blank_row_line(text: str, category: str) -> Optional[tuple]:
    """(line index, row text) of the first live single-line tuple naming
    the blank category, or None."""
    for i, line in enumerate(text.split("\n")):
        masked = mask_line_comments(line)
        if f'"{category}"' in masked and re.match(r"^\s*\(.*\)\s*,?\s*$",
                                                  masked):
            return i, masked.strip()
    return None


def _column_exists(definition: Path, table: str, column: str) -> bool:
    f = Path(definition) / "tables" / f"{table}.tmdl"
    if not f.is_file():
        return False
    pat = re.compile(r"^\tcolumn\s+(?:'" + re.escape(column.replace("'", "''"))
                     + r"'|" + re.escape(column) + r")\s*(?:=|$)", re.MULTILINE)
    return bool(pat.search(f.read_text(encoding="utf-8")))


def canonical_blank_rows(fleet) -> dict:
    """{table stem: row text} from the template; raises TemplateUnavailable
    when no template axis table carries a blank option row."""
    category, globs = _blank_conf(fleet)
    tables = fleet.template_definition / "tables"
    out = {}
    for f in sorted(tables.glob("*.tmdl")) if tables.is_dir() else []:
        if not any(fnmatch.fnmatchcase(f.stem, g) for g in globs):
            continue
        hit = blank_row_line(f.read_text(encoding="utf-8"), category)
        if hit:
            out[f.stem] = hit[1]
    if not out:
        raise TemplateUnavailable(
            f"the template has no '{category}' blank option row in any "
            f"table matching {', '.join(globs)}; there is no canonical row "
            f"to compare against")
    return out


def blank_row_findings(name: str, fleet, definition: Path,
                       strict: bool = False) -> list:
    from .detect import Finding
    try:
        canon = canonical_blank_rows(fleet)
    except TemplateUnavailable:
        if strict:
            raise
        return []
    category, _g = _blank_conf(fleet)
    out = []
    for stem, row in canon.items():
        f = Path(definition) / "tables" / f"{stem}.tmdl"
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8")
        hit = blank_row_line(text, category)
        want = _elements(row)
        target = nameof_target(want[1]) if len(want) > 1 else None
        if hit is None:
            out.append(Finding(
                kind="fieldparam.blank_row", model=name, ref=stem,
                template_value=row,
                detail=(f"field parameter '{stem}' lacks its '{category}' "
                        f"row (the 'no legend' option)"),
                data={"table": stem, "state": "missing", "row": row}))
            continue
        have = _elements(hit[1])
        if len(have) < 3 or len(want) < 3:
            continue
        if (have[0], have[1], have[2]) == (want[0], want[1], want[2]):
            continue
        if target is None or not _column_exists(definition, target[0] or "",
                                                target[1]):
            continue
        out.append(Finding(
            kind="fieldparam.blank_row", model=name, ref=stem,
            template_value=row, derived_value=hit[1],
            detail=(f"field parameter '{stem}': its '{category}' row differs "
                    f"from the template's (label, target or order)"),
            data={"table": stem, "state": "drifted", "row": row}))
    return out


def fix_blank_row(text: str, row: str, category: str) -> str:
    """Add the canonical blank row at the end of the constructor, or bring
    a drifted one back to the canonical label, target and order."""
    lines = text.split("\n")
    nl = "\n"
    hit = blank_row_line(text, category)
    want = _elements(row)
    if hit is not None:
        i, _r = hit
        line = lines[i]
        masked = mask_line_comments(line)
        have = _elements(masked)
        if len(have) != len(want):
            raise TmdlEditError("the blank row has a different number of "
                                "elements than the template's; fix it by hand")
        new = have[:]
        new[0], new[1], new[2] = want[0], want[1], want[2]
        indent = line[:len(line) - len(line.lstrip())]
        comma = "," if masked.rstrip().endswith(",") else ""
        lines[i] = (indent + "( " + ", ".join(new) + " )" + comma
                    + line[len(masked):])
        out = nl.join(lines)
        if re.search(r",\s*,", out.split(nl)[i]):
            raise TmdlEditError("the edit would leave a comma run")
        return out
    src = next((k for k, ln in enumerate(lines)
                if re.match(r"^\s*source\s*=", ln)), None)
    if src is None:
        raise TmdlEditError("no `source =` line")
    close = None
    for k in range(len(lines) - 1, src, -1):
        if lines[k].strip() == "}":
            close = k
            break
    if close is None:
        raise TmdlEditError("the table constructor's closing `}` is missing")
    last = None
    for k in range(close - 1, src, -1):
        masked = mask_line_comments(lines[k]).strip()
        if masked.startswith("(") and re.match(r"^\(.*\)\s*,?$", masked):
            last = k
            break
    if last is None:
        raise TmdlEditError("no tuple row before the closing `}`")
    masked = mask_line_comments(lines[last])
    if len(_elements(masked)) != len(want):
        raise TmdlEditError(
            f"the table's rows have {len(_elements(masked))} elements and the "
            f"template's blank row has {len(want)}; an arity mismatch needs a "
            f"hand-built row")
    if not masked.rstrip().endswith(","):
        lines[last] = masked.rstrip() + "," + lines[last][len(masked):]
    indent = lines[last][:len(lines[last]) - len(lines[last].lstrip())]
    lines.insert(last + 1, indent + row.strip().rstrip(","))
    out = nl.join(lines)
    if re.search(r",\s*,", lines[last]):
        raise TmdlEditError("the edit would leave a comma run")
    return out


# ───────────────────────── reference resolution ─────────────────────────

def model_inventory(model) -> tuple:
    """({table (lower): {objects (lower)}}, {measure names (lower)})."""
    tables = {}
    for tname, t in model.tables.items():
        tables[tname.lower()] = {n.lower() for n in
                                 list(t.columns) + list(t.measures)}
    measures = {m.lower() for m in model.measure_hosts()}
    return tables, measures


def resolves(row: Row, inventory: tuple) -> bool:
    tables, measures = inventory
    tgt = row.target()
    if tgt is None:
        return False
    table, member = tgt
    if table:
        return member.lower() in tables.get(table.lower(), set())
    return member.lower() in measures


def membership_key(row: Row) -> str:
    m = _NAMEOF_ARG.match(row.ref.strip())
    inner = m.group(1) if m else row.ref
    return re.sub(r"\s+", " ", inner.replace("'", "")).strip().lower()


# ───────────────────────── detection ─────────────────────────

def category_missing_findings(name: str, fleet, definition: Path) -> list:
    """``fieldparam.category_missing``: a category the template's field
    parameters use and the derived model's mapping table does not carry.

    A field-parameter row names the mapping key it serves (``Account_y``);
    the mapping table maps that key to an analysis category (``Account``).
    When the template's field parameters use a key the derived model's
    mapping table does not map, that category cannot be sliced in the
    derived model. It is report-only: the mapping table is written by
    hand, so the tool says what is missing and stops there.
    """
    from .mappings import live_rows
    tables = fleet.mapping_tables
    if not tables:
        return []
    out = []
    for table in tables:
        try:
            d_rows = live_rows(definition, table)
            t_rows = live_rows(fleet.template_definition, table)
        except Exception:
            continue
        if not d_rows or not t_rows:
            continue
        d_cats = {str(r[0]).strip(): {str(r[-1]).strip() for r in d_rows
                                     if r} for r in d_rows if r}
        t_cats = {str(r[0]).strip(): {str(r[-1]).strip() for r in t_rows
                                     if r} for r in t_rows if r}
        # the keys the derived model's mapping table knows
        d_keys = set().union(*d_cats.values()) if d_cats else set()
        # the keys the template's mapping table maps: a key nobody maps is
        # not an analysis category, it is a plain dimension
        t_keys = set().union(*t_cats.values()) if t_cats else set()
        for stem, path in fieldparam_files(fleet,
                                           fleet.template_definition).items():
            for r in read_rows(path)[0]:
                key = (r.group or "").strip()
                if not key or key in d_keys or key not in t_keys:
                    continue
                cats = sorted(c for c, keys in t_cats.items() if key in keys)
                label = ", ".join(cats) if cats else key
                out.append(_f("fieldparam.category_missing", name, key,
                              f"analysis category '{label}' is used by the "
                              f"template's field parameters but is absent "
                              f"from '{name}'s mapping table '{table}' — "
                              f"its mapping is likely missing. Add the row "
                              f"by hand",
                              table=table, category=label, key=key))
    return out


def _f(kind, model, ref, detail, **data):
    from .detect import Finding
    return Finding(kind=kind, model=model, ref=ref, detail=detail, data=data)


def fieldparam_findings(name: str, fleet, model) -> list:
    """Every field-parameter finding for one derived model."""
    definition = model.definition
    files = fieldparam_files(fleet, definition)
    t_files = fieldparam_files(fleet, fleet.template_definition)
    out = []
    # schema (only when the tables are configured explicitly)
    if configured_globs(fleet):
        for stem, path in files.items():
            if stem not in t_files:
                continue
            try:
                plan = build_schema_plan(path, t_files[stem])
            except FieldParamSchemaError as e:
                out.append(_f("fieldparam.schema_blocked", name, stem,
                              f"field parameter '{stem}' is left for a "
                              f"person: {e}", table=stem))
                continue
            except DriftDoctorError:
                continue
            if not plan.noop:
                out.append(_f("fieldparam.schema_modernize", name, stem,
                              f"field parameter '{stem}' differs from the "
                              f"template's schema: {plan.describe()}",
                              table=stem))
    out.extend(blank_row_findings(name, fleet, definition))
    out.extend(category_missing_findings(name, fleet, definition))
    inventory = model_inventory(model)
    out.extend(_row_findings(name, fleet, files, t_files, inventory))
    out.extend(_cross_level_findings(name, fleet, files))
    dangling = _dangling(files, inventory)
    if dangling:
        listed = ", ".join(f"{s}: {r.display}" for s, r in dangling[:20])
        out.append(_f("fieldparam.dangling_row", name, "field parameters",
                      f"{len(dangling)} live field-parameter row(s) point at "
                      f"nothing in '{name}': {listed}",
                      rows=[[s, r.display, r.ref] for s, r in dangling]))
    return out


def _skip_tables(fleet) -> set:
    return set(_settings(fleet).get("skip_tables") or [])


def _row_findings(name, fleet, files, t_files, inventory) -> list:
    """Template rows absent from (or different in) the same-named derived
    field-parameter table (H-fieldparam-row-merge, report form)."""
    out = []
    skip = _skip_tables(fleet)
    for stem, path in files.items():
        if stem not in t_files or stem in skip:
            continue
        d_rows, d_arity = read_rows(path)
        t_rows, _ = read_rows(t_files[stem])
        have = {}
        for r in d_rows:
            have.setdefault(r.key, r)
        seen = set()
        for t in t_rows:
            if t.key in seen:
                continue
            seen.add(t.key)
            if not resolves(t, inventory):
                continue
            d = have.get(t.key)
            ref = f"{stem}/{t.display}"
            if d is None:
                if any(membership_key(x) == membership_key(t) for x in d_rows):
                    continue        # already offered under another label
                out.append(_f("fieldparam.row_missing", name, ref,
                              f"template row '{t.display}' is absent from "
                              f"field parameter '{stem}'", table=stem,
                              display=t.display))
            elif (d.ref.replace(" ", ""), d.order, d.group) != (
                    t.ref.replace(" ", ""), t.order, t.group):
                out.append(_f("fieldparam.row_drift", name, ref,
                              f"row '{t.display}' of field parameter "
                              f"'{stem}' differs from the template's",
                              table=stem, display=t.display))
    return out


def _cross_level_findings(name, fleet, files) -> list:
    out = []
    placeholder = (_settings(fleet).get("placeholder_table")
                   or "#report measures").lower() + "["
    for role, levels, variants in family_members(fleet, files):
        if role != "dimension" or len(levels) < 2:
            continue
        rows_of = {n: read_rows(p)[0] for n, p in levels.items()}
        keys = {}
        for n, rows in rows_of.items():
            for r in rows:
                k = membership_key(r)
                if not k.startswith(placeholder):
                    keys.setdefault(k, (n, r))
        by_cat: dict = {}
        for k, (n0, r) in keys.items():
            lacking = [n for n, rows in rows_of.items()
                       if all(membership_key(x) != k for x in rows)]
            if lacking:
                by_cat.setdefault(r.group, []).append((k, n0, lacking))
        for cat, items in sorted(by_cat.items()):
            out.append(_f(f"fieldparam.cross_level.{cat}", name,
                          f"{_family_name(levels)}/{cat}",
                          f"{len(items)} row(s) of category '{cat}' are "
                          f"missing from some numbered levels",
                          keys=[k for k, _n, _l in items]))
        level_keys = {membership_key(r) for rows in rows_of.values()
                      for r in rows}
        for vname, vpath in sorted(variants.items()):
            extra = [r for r in read_rows(vpath)[0]
                     if membership_key(r) not in level_keys
                     and not membership_key(r).startswith(placeholder)]
            for cat in sorted({r.group for r in extra}):
                out.append(_f(f"fieldparam.plot_specific_extra.{cat}", name,
                              f"{vname}/{cat}",
                              f"'{vname}' offers row(s) of category '{cat}' "
                              f"that no numbered level registers",
                              variant=vname,
                              keys=[membership_key(r) for r in extra
                                    if r.group == cat]))
    return out


def _family_name(levels: dict) -> str:
    first = next(iter(levels.values()))
    return re.sub(r" \d+$", "", first.stem)


def _dangling(files, inventory) -> list:
    out = []
    for stem, path in files.items():
        for r in read_rows(path)[0]:
            if not resolves(r, inventory):
                out.append((stem, r))
    return out


# ───────────────────────── retired targets ─────────────────────────

def retired_targets(retire: dict) -> dict:
    """{target: kind} of every retired object a field-parameter row can
    point at.

    A dimension row points at a column (``table``, ``member``); a metric row
    points at a measure, whose reference is the measure's own name. The
    ledger's measure refs are ``Table[Measure]``."""
    out = {}
    for (kind, ref) in retire or {}:
        if kind == "measure":
            table, _sep, member = ref.partition("[")
            out[(table.rstrip(), member.rstrip("]"))] = kind
        elif kind == "column":
            table, _sep, member = ref.partition("[")
            out[(table.rstrip(), member.rstrip("]"))] = kind
        elif kind == "table":
            out[(ref, "")] = kind
    return out


def retired_tables(retire: dict) -> set:
    """The names of every retired table.

    A row that names a column of a retired table is a row that points at a
    retired object too: the table it names is gone, so the row goes with
    it. The table's own name is the target, with no member beside it."""
    out = set()
    for (kind, ref) in retire or {}:
        if kind == "table":
            out.add(ref.rstrip())
    return out


def _row_target_key(row: Row) -> Optional[tuple]:
    """The (table, member) a row points at, as the ledger spells it.

    A dimension row names its table; a metric row names a measure, whose
    host table the row does not carry — the measure name alone identifies
    it, and the ledger's measure ref does too."""
    tgt = row.target()
    if tgt is None:
        return None
    table, member = tgt
    if table:
        return (table, member)
    return ("", member)


def retired_rows(files: dict, retire: dict) -> dict:
    """{table stem: [(row, category, target ref)]} of the live rows that
    point at a retired object."""
    targets = retired_targets(retire)
    tables = retired_tables(retire)
    if not targets and not tables:
        return {}
    measures = {member for (table, member), kind in targets.items()
                if not table and kind == "measure"}
    out: dict = {}
    for stem, path in files.items():
        hits = []
        for r in read_rows(path)[0]:
            key = _row_target_key(r)
            if key is None:
                continue
            table, member = key
            if table:
                if (table, member) not in targets and table not in tables:
                    continue
                ref = f"{table}[{member}]"
            else:
                if member not in measures:
                    continue
                ref = member
            hits.append((r, r.group, ref))
        if hits:
            out[stem] = hits
    return out


def retired_entry_findings(name: str, fleet, definition: Path,
                            retire: dict) -> list:
    """``retire.fieldparam_entry``: a field-parameter row that points at an
    object the ledger records as retired. One finding per table and
    category, naming both."""
    files = fieldparam_files(fleet, definition)
    out = []
    for stem, hits in sorted(retired_rows(files, retire).items()):
        by_cat: dict = {}
        for row, cat, ref in hits:
            by_cat.setdefault(cat, []).append((row, ref))
        for cat, items in sorted(by_cat.items()):
            listed = ", ".join(sorted(ref for _r, ref in items))
            out.append(_f("retire.fieldparam_entry", name, f"{stem}/{cat}",
                          f"field parameter '{stem}' still offers row(s) "
                          f"pointing at retired object(s) {listed} "
                          f"(category '{cat}')",
                          table=stem, category=cat,
                          rows=[[r.display, r.ref] for r, _ref in items]))
    return out


# ───────────────────────── appliers ─────────────────────────

def _derived_file(fleet, f, stem) -> Path:
    path = fleet.model_definition(f.model) / "tables" / f"{stem}.tmdl"
    if not path.is_file():
        raise DriftDoctorError(f"{path} no longer exists; re-run detect")
    return path


def _change(path: Path, new: str):
    from .remediate import FileChange
    old = te.read_text(path)
    return [FileChange(path, old, new)] if new != old else []


def apply_schema(fleet, f) -> tuple:
    stem = f.data["table"]
    path = _derived_file(fleet, f, stem)
    tpath = fleet.template_definition / "tables" / f"{stem}.tmdl"
    new = modernize_schema(path, tpath, write=False)
    return (f"bring field parameter '{stem}' to the template's schema",
            _change(path, new))


def apply_blank_row(fleet, f) -> tuple:
    stem = f.data["table"]
    path = _derived_file(fleet, f, stem)
    category, _g = _blank_conf(fleet)
    new = fix_blank_row(te.read_text(path), f.data["row"],
                        category)
    verb = "add" if f.data.get("state") == "missing" else "restore"
    return (f"{verb} the '{category}' row of '{stem}' from the template",
            _change(path, new))


def apply_row(fleet, f) -> tuple:
    """Add (or, for drift, replace) one template row in the derived table."""
    stem, display = f.data["table"], f.data["display"]
    path = _derived_file(fleet, f, stem)
    t_rows, _ = read_rows(fleet.template_definition / "tables"
                          / f"{stem}.tmdl")
    t_row = next((r for r in t_rows if r.display == display), None)
    if t_row is None:
        raise DriftDoctorError(f"the template no longer has row '{display}' "
                               f"in '{stem}'; re-run capture and detect")
    rows, arity = read_rows(path)
    fresh = Row(t_row.display, t_row.ref, t_row.order, t_row.group,
                list(t_row.extras), "", t_row.bracket)
    idx = next((i for i, r in enumerate(rows) if r.key == t_row.key), None)
    if idx is None:
        rows.append(fresh)
        verb = "add"
    else:
        rows[idx] = fresh
        verb = "replace"
    new = rewrite_rows_text(te.read_text(path), rows, arity,
                            path=path)
    return (f"{verb} row '{display}' of '{stem}' from the template",
            _change(path, new))


def apply_cross_level(fleet, f) -> tuple:
    """Copy each listed row to every numbered level lacking it (never into
    a level whose rows are narrower)."""
    from .remediate import FileChange
    files = fieldparam_files(fleet, fleet.model_definition(f.model))
    keys = set(f.data.get("keys") or [])
    changes, skipped = [], []
    for role, levels, variants in family_members(fleet, files):
        if not levels or _family_name(levels) != f.ref.split("/", 1)[0] \
                and not f.data.get("variant"):
            continue
        sources = list(levels.values())
        if f.data.get("variant"):
            if f.data["variant"] not in variants:
                continue
            sources = [variants[f.data["variant"]]] + sources
        donors = {}
        for p in sources:
            for r in read_rows(p)[0]:
                donors.setdefault(membership_key(r), r)
        for n, path in levels.items():
            rows, arity = read_rows(path)
            have = {membership_key(r) for r in rows}
            add = [donors[k] for k in sorted(keys) if k in donors
                   and k not in have]
            fits = [r for r in add if r.width <= arity]
            skipped += [f"{path.stem}: {r.display}" for r in add
                        if r.width > arity]
            if not fits:
                continue
            new_rows = rows + [Row(r.display, r.ref, r.order, r.group,
                                   list(r.extras), "", r.bracket)
                               for r in fits]
            old = te.read_text(path)
            new = rewrite_rows_text(old, new_rows, arity, path=path)
            if new != old:
                changes.append(FileChange(path, old, new))
    label = f"add the missing row(s) to the numbered levels of {f.ref}"
    if skipped:
        label += f" (skipped, wider than the level: {', '.join(skipped)})"
    return label, changes


def apply_dangling(fleet, f) -> tuple:
    from .remediate import FileChange
    mdef = fleet.model_definition(f.model)
    by_file: dict = {}
    for stem, display, ref in f.data.get("rows") or []:
        by_file.setdefault(stem, set()).add((display, ref))
    changes = []
    for stem, gone in sorted(by_file.items()):
        path = mdef / "tables" / f"{stem}.tmdl"
        if not path.is_file():
            continue
        rows, arity = read_rows(path)
        keep = [r for r in rows if (r.display, r.ref) not in gone]
        if len(keep) == len(rows):
            continue
        old = te.read_text(path)
        changes.append(FileChange(path, old, rewrite_rows_text(
            old, keep, arity, path=path)))
    return ("remove field-parameter rows whose NAMEOF points at nothing",
            changes)


def apply_retired_entry(fleet, f) -> tuple:
    """Remove the field-parameter rows that point at a retired object.

    Only the listed rows go: every other row, the row order and the
    table's own formatting stay as they were."""
    from .remediate import FileChange
    mdef = fleet.model_definition(f.model)
    stem = f.data["table"]
    path = mdef / "tables" / f"{stem}.tmdl"
    if not path.is_file():
        raise DriftDoctorError(f"{path} no longer exists; re-run detect")
    gone = {(d, r) for d, r in f.data.get("rows") or []}
    rows, arity = read_rows(path)
    keep = [r for r in rows if (r.display, r.ref) not in gone]
    if len(keep) == len(rows):
        return ("rows already removed", [])
    old = te.read_text(path)
    return (f"remove {len(rows) - len(keep)} row(s) of category "
            f"'{f.data.get('category')}' pointing at a retired object from "
            f"'{stem}'",
            [FileChange(path, old, rewrite_rows_text(old, keep, arity,
                                                     path=path))])


APPLIERS = {
    "fieldparam.schema_modernize": apply_schema,
    "fieldparam.blank_row": apply_blank_row,
    "fieldparam.row_missing": apply_row,
    "fieldparam.row_drift": apply_row,
    "fieldparam.cross_level": apply_cross_level,
    "fieldparam.plot_specific_extra": apply_cross_level,
    "fieldparam.dangling_row": apply_dangling,
    "retire.fieldparam_entry": apply_retired_entry,
}


def applier_for(kind: str):
    for k, fn in APPLIERS.items():
        if kind == k or kind.startswith(k + "."):
            return fn
    return None


# ───────────────────────── retag ─────────────────────────

def retag(text: str, source: str, dest: str, displays) -> tuple:
    """(new text, displays retagged): rewrite only the category literal of
    the first live row per requested display whose category is `source`.
    Raises when any requested display is not found under `source`."""
    wanted = list(dict.fromkeys(displays))
    done, pieces, last = [], [], 0
    for m in TUPLE_RE.finditer(text):
        prefix = text[text.rfind("\n", 0, m.start()) + 1:m.start()]
        if mask_line_comments(prefix) != prefix:
            continue
        row = _row_from_match(m)
        if row.group != source or row.display not in wanted \
                or row.display in done:
            continue
        done.append(row.display)
        pieces.append(text[last:m.start("group")])
        pieces.append(_q(dest))
        last = m.end("group")
    pieces.append(text[last:])
    missing = [d for d in wanted if d not in done]
    if missing:
        raise DriftDoctorError(
            f"retag incomplete: {', '.join(missing)} not found as a live row "
            f"of category '{source}'; nothing was written")
    return "".join(pieces), done

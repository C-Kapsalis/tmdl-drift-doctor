"""Minimal TMDL reader — parses just enough of a semantic model for diffing.

TMDL's indentation contract (tabs, never spaces):

* ``table X`` / ``expression X = ...`` sit at column 0,
* object declarations inside a table file (``column`` / ``measure`` /
  ``partition`` / table-level ``annotation``) sit at ONE tab,
* their properties and annotations sit at TWO tabs,
* bare multi-line expression bodies sit at THREE-or-more tabs (or blank),
* fenced expression bodies are wrapped in ``` markers,
* ``relationships.tmdl`` holds column-0 ``relationship <name>`` blocks whose
  properties sit at ONE tab.

This reader deliberately does NOT round-trip: lineage tags, annotations and
formatting are preserved by editing the *raw text* (see ``tmdl_edit``), never
by re-serializing parsed objects. Parsing exists only to answer "what objects
does this model contain and do their expressions/properties match?".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from . import DriftDoctorError

# Single-value properties that participate in drift comparison. lineageTag and
# annotations are identity/metadata noise and are always excluded.
COMPARED_PROPERTIES = (
    "dataType",
    "formatString",
    "displayFolder",
    "summarizeBy",
    "sourceColumn",
    "sortByColumn",
    "isHidden",
)

_TABLE_RE = re.compile(r"^table\s+(.+?)\s*$")
_EXPRESSION_RE = re.compile(r"^expression\s+(.+?)\s*=\s*(.*)$")
_OBJECT_RE = re.compile(
    r"^\t(column|measure|partition|hierarchy|annotation)\s+(.*)$"
)
_PROP_RE = re.compile(r"^\t\t([A-Za-z][A-Za-z0-9]*)\s*:\s*(.*?)\s*$")
_FLAG_RE = re.compile(r"^\t\t([A-Za-z][A-Za-z0-9]*)\s*$")
_REF_TABLE_RE = re.compile(r"^ref table\s+(.+?)\s*$")
_RELATIONSHIP_RE = re.compile(r"^relationship\s+(.+?)\s*$")
_REL_PROP_RE = re.compile(r"^\t([A-Za-z][A-Za-z0-9]*)\s*:\s*(.*?)\s*$")
_REL_FLAG_RE = re.compile(r"^\t([A-Za-z][A-Za-z0-9]*)\s*$")

# Relationship properties that participate in drift comparison, with the
# engine default each one takes when the line is omitted. Normalizing to the
# default means an explicit `isActive: true` never registers as drift against
# a block that simply leaves it out.
RELATIONSHIP_DEFAULTS = {
    "fromCardinality": "many",
    "toCardinality": "one",
    "isActive": "true",
    "crossFilteringBehavior": "oneDirection",
    "securityFilteringBehavior": "oneDirection",
    "relyOnReferentialIntegrity": "false",
}

# One row tuple of a DAX DATATABLE literal: `{ "key", ... }` where the first
# element is the quoted row key. The row-list's OUTER brace never matches
# (its first non-space child is another brace, not a quote).
MAPPING_ROW_RE = re.compile(r"\{\s*\"((?:[^\"\\]|\\.)*)\"[^{}]*\}")


def unquote(name: str) -> str:
    """Strip TMDL identifier quoting: 'My Name' -> My Name (with '' -> ')."""
    name = name.strip()
    if len(name) >= 2 and name[0] == "'" and name[-1] == "'":
        return name[1:-1].replace("''", "'")
    if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
        return name[1:-1]
    return name


def _split_decl(rest: str) -> tuple[str, str]:
    """Split an object declaration's remainder into (name, after-=-part).

    ``'Active Members' = DISTINCTCOUNT(...)`` -> (Active Members, DISTINCT...)
    ``MemberId``                              -> (MemberId, "")
    """
    rest = rest.strip()
    if rest.startswith("'"):
        m = re.match(r"^'((?:[^']|'')*)'\s*(.*)$", rest)
        if m:
            name = m.group(1).replace("''", "'")
            tail = m.group(2)
        else:  # unterminated quote — treat whole thing as the name
            return rest, ""
    else:
        m = re.match(r"^([^=]+?)\s*(=.*|$)", rest)
        name, tail = m.group(1).strip(), m.group(2)
    tail = tail.strip()
    if tail.startswith("="):
        return name, tail[1:].strip()
    return name, ""


@dataclass
class Measure:
    name: str
    table: str
    expression: str          # raw expression text (may span lines)
    properties: dict = field(default_factory=dict)
    lineage_tag: str = ""

    @property
    def ref(self) -> str:
        return f"{self.table}[{self.name}]"


@dataclass
class Column:
    name: str
    table: str
    properties: dict = field(default_factory=dict)
    lineage_tag: str = ""
    expression: str = ""     # DAX of a calculated column, else ""

    @property
    def ref(self) -> str:
        return f"{self.table}[{self.name}]"


@dataclass
class Partition:
    name: str
    table: str
    source: str = ""         # raw source expression text


@dataclass
class Table:
    name: str
    file: Path
    columns: dict = field(default_factory=dict)     # name -> Column
    measures: dict = field(default_factory=dict)    # name -> Measure
    partitions: dict = field(default_factory=dict)  # name -> Partition
    hierarchies: dict = field(default_factory=dict) # name -> [level column]


@dataclass
class Relationship:
    """One relationship. Identity is the FIELD signature
    ``FromTable[col] -> ToTable[col]``; the block name (usually a GUID) is
    per-model noise and never participates in comparison."""
    name: str
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    properties: dict = field(default_factory=dict)  # normalized, see defaults

    @property
    def signature(self) -> str:
        return relationship_signature(self.from_table, self.from_column,
                                      self.to_table, self.to_column)

    def touches_table(self, table: str) -> bool:
        return table in (self.from_table, self.to_table)

    def touches_column(self, table: str, column: str) -> bool:
        return ((self.from_table, self.from_column) == (table, column)
                or (self.to_table, self.to_column) == (table, column))


@dataclass
class Model:
    definition: Path                                # the definition/ dir
    tables: dict = field(default_factory=dict)      # name -> Table
    expressions: dict = field(default_factory=dict) # name -> comparison form
    expression_forms: dict = field(default_factory=dict)  # name -> [forms]
    has_expressions_file: bool = False
    ref_tables: list = field(default_factory=list)  # model.tmdl `ref table`s
    relationships: dict = field(default_factory=dict)  # signature -> Relationship
    _hosts: Optional[dict] = field(default=None, repr=False, compare=False)

    def measure(self, table: str, name: str) -> Optional[Measure]:
        t = self.tables.get(table)
        return t.measures.get(name) if t else None

    def column(self, table: str, name: str) -> Optional[Column]:
        t = self.tables.get(table)
        return t.columns.get(name) if t else None

    def measure_hosts(self) -> dict:
        """{measure name: [host tables]} across the whole model. Measure
        names are model-global in the engine, so this is the index every
        name-keyed decision must consult. Computed once per parse (a parsed
        Model is never mutated; edits re-parse)."""
        if self._hosts is None:
            out: dict = {}
            for tname, table in self.tables.items():
                for mname in table.measures:
                    out.setdefault(mname, []).append(tname)
            self._hosts = out
        return self._hosts


def _is_expr_continuation(line: str) -> bool:
    """A line that belongs to a bare multi-line expression body: blank, or
    indented three-or-more tabs."""
    return line.strip() == "" or line.startswith("\t\t\t")


def _collect_properties(lines: list[str], start: int) -> tuple[dict, str, int]:
    """Read two-tab property/flag lines from `start` until the block ends.

    Returns (compared_properties, lineage_tag, next_index). Annotation lines
    and unknown properties are skipped but consumed.
    """
    props: dict = {}
    lineage = ""
    i = start
    while i < len(lines):
        line = lines[i]
        if line.strip() == "":
            i += 1
            continue
        if not line.startswith("\t\t"):
            break  # one-tab boundary: next object / table annotation
        m = _PROP_RE.match(line)
        if m:
            key, value = m.group(1), m.group(2)
            if key == "lineageTag":
                lineage = value
            elif key in COMPARED_PROPERTIES:
                props[key] = value
            i += 1
            continue
        f = _FLAG_RE.match(line)
        if f:
            key = f.group(1)
            if key in COMPARED_PROPERTIES:
                props[key] = "true"
            i += 1
            continue
        i += 1  # annotation payload / extendedProperty / anything else
    return props, lineage, i


def _read_expression(lines: list[str], decl_index: int, inline: str) -> tuple[str, int]:
    """Extract an object's expression starting at its declaration line.

    Handles the three TMDL shapes:
      * single-line   — ``measure X = COUNTROWS(T)``
      * fenced        — ``measure X = ``` ... ``` `` (backtick fence)
      * bare multiline — ``measure X =`` then blank/three-tab body lines

    Returns (expression_text, index_of_first_line_after_the_expression).
    """
    i = decl_index + 1
    if inline.startswith("```"):
        body: list[str] = []
        while i < len(lines):
            if "```" in lines[i]:
                i += 1
                break
            body.append(lines[i].lstrip("\t"))
            i += 1
        return "\n".join(body), i
    if inline:
        return inline, i
    # bare multi-line: blank lines + three-tab lines until a two-tab property
    # or a one-tab boundary
    body = []
    while i < len(lines) and _is_expr_continuation(lines[i]):
        body.append(lines[i].lstrip("\t"))
        i += 1
    while body and body[-1].strip() == "":
        body.pop()
    while body and body[0].strip() == "":
        body.pop(0)
    return "\n".join(body), i


def parse_table_file(path: Path) -> Table:
    """Parse one ``tables/*.tmdl`` file into a Table."""
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    table_name = None
    for line in lines:
        m = _TABLE_RE.match(line)
        if m:
            table_name = unquote(m.group(1))
            break
    if table_name is None:
        raise DriftDoctorError(f"{path}: no `table` declaration found")

    table = Table(name=table_name, file=path)
    i = 0
    while i < len(lines):
        m = _OBJECT_RE.match(lines[i])
        if not m:
            i += 1
            continue
        keyword, rest = m.group(1), m.group(2)
        if keyword == "annotation":  # table-level annotation tail
            i += 1
            continue
        name, inline = _split_decl(rest)
        if keyword == "column":
            expr = ""
            if inline:  # calculated column: `column X = <dax>`
                expr, i = _read_expression(lines, i, inline)
            else:
                i += 1
            props, lineage, i = _collect_properties(lines, i)
            table.columns[name] = Column(name=name, table=table_name,
                                         properties=props, lineage_tag=lineage,
                                         expression=expr)
        elif keyword == "measure":
            expr, i = _read_expression(lines, i, inline)
            props, lineage, i = _collect_properties(lines, i)
            table.measures[name] = Measure(name=name, table=table_name,
                                           expression=expr, properties=props,
                                           lineage_tag=lineage)
        elif keyword == "partition":
            # `partition Name = <mode-word>`; its `source =` is a two-tab
            # property whose body sits at three-plus tabs.
            src = ""
            j = i + 1
            while j < len(lines):
                line = lines[j]
                if line.strip() == "":
                    j += 1
                    continue
                if not line.startswith("\t\t"):
                    break
                sm = re.match(r"^\t\tsource\s*=\s*(.*)$", line)
                if sm:
                    src, j = _read_expression(lines, j, sm.group(1).strip())
                    continue
                j += 1
            table.partitions[name] = Partition(name=name, table=table_name,
                                               source=src)
            i = j
        else:  # hierarchy: record which columns its levels use
            levels: list = []
            j = i + 1
            while j < len(lines):
                line = lines[j]
                if line.strip() and not line.startswith("\t\t"):
                    break
                lm = re.match(r"^\t\t\tcolumn\s*:\s*(.+?)\s*$", line)
                if lm:
                    levels.append(unquote(lm.group(1)))
                j += 1
            table.hierarchies[name] = levels
            i = j
    return table


_DOC_LINE = re.compile(r"^///")


def expression_spans(content: str) -> list:
    """[(name, start, end)] line spans of every column-0 ``expression`` block.

    Ownership rules: a run of column-0 ``///`` lines documents the
    declaration BELOW it, so a span opens at its own leading doc run, takes
    the declaration and every following line up to the next declaration, and
    then gives back its trailing blank lines and any trailing doc run (those
    belong to the next block). Names may repeat; every occurrence is listed.
    """
    lines = content.split("\n")
    decls = [i for i, ln in enumerate(lines) if _EXPRESSION_RE.match(ln)]
    spans = []
    for n, i in enumerate(decls):
        start = i
        while start - 1 >= 0 and _DOC_LINE.match(lines[start - 1]):
            start -= 1
        stop = decls[n + 1] if n + 1 < len(decls) else len(lines)
        end = stop
        while end - 1 > i and (lines[end - 1].strip() == ""
                               or _DOC_LINE.match(lines[end - 1])):
            end -= 1
        name = unquote(_EXPRESSION_RE.match(lines[i]).group(1))
        spans.append((name, start, end))
    return spans


def expression_form(block: str) -> str:
    """The comparison form of one expression block: lineage-tag lines
    dropped, the ``meta [...]`` bag put in canonical order, trailing
    whitespace and blank edges removed. Everything else, doc comments and
    ``//`` comments included, is compared as written: a comment-only change
    is drift."""
    kept = []
    for line in block.replace("\r\n", "\n").split("\n"):
        if line.strip().startswith("lineageTag:"):
            continue
        kept.append(line.rstrip())
    while kept and not kept[-1]:
        kept.pop()
    while kept and not kept[0]:
        kept.pop(0)
    return canonicalize_meta("\n".join(kept))


def parse_expression_forms(path: Path) -> dict:
    """{name: [comparison form, ...]} with one entry per declaration (a file
    may declare the same name more than once)."""
    path = Path(path)
    if not path.exists():
        return {}
    content = path.read_text(encoding="utf-8")
    lines = content.split("\n")
    out: dict = {}
    for name, start, end in expression_spans(content):
        out.setdefault(name, []).append(
            expression_form("\n".join(lines[start:end])))
    return out


def parse_expressions_file(path: Path) -> dict:
    """{name: comparison form} of the FIRST declaration of each name."""
    return {name: forms[0]
            for name, forms in parse_expression_forms(path).items()}


def parse_model(definition: Path) -> Model:
    """Parse a model's ``definition/`` directory."""
    definition = Path(definition)
    if not definition.is_dir():
        raise DriftDoctorError(f"model definition dir not found: {definition}")
    model = Model(definition=definition)
    tables_dir = definition / "tables"
    if tables_dir.is_dir():
        for f in sorted(tables_dir.glob("*.tmdl")):
            t = parse_table_file(f)
            model.tables[t.name] = t
    model.expression_forms = parse_expression_forms(
        definition / "expressions.tmdl")
    model.expressions = {n: f[0] for n, f in model.expression_forms.items()}
    model.has_expressions_file = (definition / "expressions.tmdl").exists()
    model.relationships = parse_relationships_file(
        definition / "relationships.tmdl")
    model_file = definition / "model.tmdl"
    if model_file.exists():
        for line in model_file.read_text(encoding="utf-8").split("\n"):
            m = _REF_TABLE_RE.match(line)
            if m:
                model.ref_tables.append(unquote(m.group(1)))
    return model


def relationship_signature(from_table: str, from_column: str,
                           to_table: str, to_column: str) -> str:
    return f"{from_table}[{from_column}] -> {to_table}[{to_column}]"


def parse_signature(sig: str) -> tuple:
    """``A[x] -> B[y]`` -> (A, x, B, y). Raises DriftDoctorError on a
    malformed signature."""
    m = re.match(r"^(.+)\[([^\]]+)\]\s*->\s*(.+)\[([^\]]+)\]$", sig.strip())
    if not m:
        raise DriftDoctorError(
            f"malformed relationship signature {sig!r}; expected "
            f"'FromTable[column] -> ToTable[column]'")
    return m.group(1).strip(), m.group(2), m.group(3).strip(), m.group(4)


def _parse_column_ref(value: str) -> Optional[tuple]:
    """``'Plan Map'.PlanKey`` / ``Visits.'Member Id'`` -> (table, column)."""
    m = re.match(r"^\s*(?:'((?:[^']|'')+)'|([^.']+))\.(?:'((?:[^']|'')+)'|(.+?))\s*$",
                 value)
    if not m:
        return None
    table = (m.group(1) or m.group(2)).replace("''", "'").strip()
    column = (m.group(3) or m.group(4)).replace("''", "'").strip()
    return table, column


def relationship_block_spans(content: str) -> list:
    """[(name, start_line, end_line)] for every column-0 relationship block:
    the declaration through its one-tab property/annotation lines, trailing
    blanks excluded."""
    lines = content.split("\n")
    spans = []
    i = 0
    while i < len(lines):
        m = _RELATIONSHIP_RE.match(lines[i])
        if not m:
            i += 1
            continue
        start = i
        j = i + 1
        while j < len(lines) and (lines[j].strip() == ""
                                  or lines[j].startswith("\t")):
            j += 1
        end = j
        while end - 1 > start and lines[end - 1].strip() == "":
            end -= 1
        spans.append((unquote(m.group(1)), start, end))
        i = j
    return spans


def parse_relationships_text(content: str) -> dict:
    """{signature: Relationship}. A block without both endpoints is skipped
    (it cannot be identified); a repeated signature keeps the first block."""
    out: dict = {}
    lines = content.split("\n")
    for name, start, end in relationship_block_spans(content):
        props: dict = {}
        ends = {}
        for line in lines[start + 1:end]:
            pm = _REL_PROP_RE.match(line)
            if pm:
                key, value = pm.group(1), pm.group(2)
                if key in ("fromColumn", "toColumn"):
                    ref = _parse_column_ref(value)
                    if ref:
                        ends[key] = ref
                elif key in RELATIONSHIP_DEFAULTS:
                    props[key] = value
                continue
            fm = _REL_FLAG_RE.match(line)
            if fm and fm.group(1) in RELATIONSHIP_DEFAULTS:
                props[fm.group(1)] = "true"
        if "fromColumn" not in ends or "toColumn" not in ends:
            continue
        normalized = {k: props.get(k, d) for k, d in RELATIONSHIP_DEFAULTS.items()}
        rel = Relationship(name=name, from_table=ends["fromColumn"][0],
                           from_column=ends["fromColumn"][1],
                           to_table=ends["toColumn"][0],
                           to_column=ends["toColumn"][1],
                           properties=normalized)
        out.setdefault(rel.signature, rel)
    return out


def parse_relationships_file(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    return parse_relationships_text(path.read_text(encoding="utf-8"))


# ───────────────────── string-aware expression scanning ─────────────────────

def _string_end(text: str, i: int) -> int:
    """Index just past the double-quoted literal opening at ``text[i]``
    (``""`` is an escaped quote in both DAX and M)."""
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == '"':
            if j + 1 < n and text[j + 1] == '"':
                j += 2
                continue
            return j + 1
        j += 1
    return n


def _opaque_end(text: str, i: int) -> Optional[int]:
    """End of a single-line DAX identifier span opening at ``text[i]``:
    ``'Quoted Table'`` or a ``[Bracket Name]`` that contains no string or
    nesting (so ``[Member's Count]`` is never misread as opening a quote).
    None when the character does not open such a span."""
    eol = text.find("\n", i)
    eol = len(text) if eol < 0 else eol
    c = text[i]
    if c == "'":
        j = i + 1
        while j < eol:
            if text[j] == "'":
                if j + 1 < eol and text[j + 1] == "'":
                    j += 2
                    continue
                return j + 1
            j += 1
        return None
    if c == "[":
        k = text.find("]", i + 1)
        if 0 <= k < eol and not any(ch in text[i + 1:k] for ch in '"[{'):
            return k + 1
    return None


def strip_comments(text: str) -> str:
    """Remove ``//`` and ``--`` line comments and ``/* */`` block comments
    that sit OUTSIDE string literals and quoted identifiers. A URL inside a
    string (``"https://host/path"``) is content, never a comment."""
    out: list = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = _string_end(text, i)
            out.append(text[i:j])
            i = j
            continue
        if c in "'[":
            j = _opaque_end(text, i)
            if j is not None:
                out.append(text[i:j])
                i = j
                continue
        if text.startswith("//", i) or text.startswith("--", i):
            eol = text.find("\n", i)
            i = n if eol < 0 else eol
            out.append(" ")
            continue
        if text.startswith("/*", i):
            k = text.find("*/", i + 2)
            i = n if k < 0 else k + 2
            out.append(" ")
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _split_top_level(body: str) -> list:
    """Split on commas outside strings and nested brackets."""
    parts, depth, cur, i = [], 0, [], 0
    while i < len(body):
        c = body[i]
        if c == '"':
            j = _string_end(body, i)
            cur.append(body[i:j])
            i = j
            continue
        if c in "[({":
            depth += 1
        elif c in "])}":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur))
    return parts


def _canonical_meta_item(item: str) -> str:
    item = re.sub(r"\s+", " ", item).strip()
    m = re.match(r"^([^=\"]+?)\s*=\s*(.*)$", item)
    return f"{m.group(1)} = {m.group(2)}" if m else item


def canonicalize_meta(text: str) -> str:
    """Rewrite every ``meta [k = v, ...]`` record outside string literals
    into one canonical form: items split, trimmed and sorted. Power BI
    Desktop reorders these bags on save; the order carries no meaning."""
    out: list = []
    i, n = 0, len(text)
    meta = re.compile(r"\bmeta\s*\[")
    while i < n:
        c = text[i]
        if c == '"':
            j = _string_end(text, i)
            out.append(text[i:j])
            i = j
            continue
        m = meta.match(text, i)
        if m and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            depth, j = 1, m.end()
            while j < n and depth:
                if text[j] == '"':
                    j = _string_end(text, j)
                    continue
                if text[j] == "[":
                    depth += 1
                elif text[j] == "]":
                    depth -= 1
                j += 1
            if depth == 0:
                items = [_canonical_meta_item(p)
                         for p in _split_top_level(text[m.end():j - 1])]
                items = sorted(p for p in items if p)
                out.append("meta [" + ", ".join(items) + "]")
                i = j
                continue
        out.append(c)
        i += 1
    return "".join(out)


def normalize_expression(expr: Optional[str]) -> str:
    """Reduce an expression to its comparable form: fence markers removed,
    DAX/M comments stripped (string-aware), ``meta [...]`` bags put in
    canonical order, whitespace collapsed. Reformatting a measure or a
    Desktop re-save must never register as drift; a changed string literal
    (a URL, a label) always does."""
    if not expr:
        return ""
    t = expr.replace("```", " ")
    t = strip_comments(t)
    t = canonicalize_meta(t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def blank_strings(text: str) -> str:
    """Comments removed and every string literal emptied to ``""``, so a
    reference scan never matches inside text."""
    t = strip_comments(text or "")
    out, i = [], 0
    while i < len(t):
        if t[i] == '"':
            j = _string_end(t, i)
            out.append('""')
            i = j
            continue
        out.append(t[i])
        i += 1
    return "".join(out)


def dax_references(expr: Optional[str]) -> dict:
    """See ``_dax_references``; results are cached per expression text,
    because dependency checks scan every definition once per finding."""
    q, b, t = _dax_references(expr or "")
    return {"qualified": set(q), "bare": set(b), "tables": set(t)}


@lru_cache(maxsize=8192)
def _dax_references(expr: str) -> tuple:
    """What a DAX expression refers to, from a string-aware token scan:

    * ``qualified``: {(table, name)} for ``Table[Name]`` / ``'Table'[Name]``
    * ``bare``: {name} for unqualified ``[Name]`` (a measure, or a column
      through row context)
    * ``tables``: table names referenced as ``'Quoted'`` or bare words, plus
      every qualified ref's table (callers intersect bare words with the
      table names they know, since a bare word may be a function or a VAR)
    """
    text = blank_strings(expr or "").replace("```", " ")
    qualified, bare, tables = set(), set(), set()
    i, n = 0, len(text)

    def bracket_after(k: int):
        if k < n and text[k] == "[":
            close = text.find("]", k + 1)
            eol = text.find("\n", k)
            if close > k and (eol < 0 or close < eol):
                return text[k + 1:close], close + 1
        return None, k

    while i < n:
        c = text[i]
        if c == '"':
            i = _string_end(text, i)
            continue
        if c == "'":
            j = _opaque_end(text, i)
            if j is None:
                i += 1
                continue
            table = text[i + 1:j - 1].replace("''", "'")
            name, k = bracket_after(j)
            if name is not None:
                qualified.add((table, name))
            tables.add(table)
            i = k
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            name, k = bracket_after(j)
            if name is not None:
                qualified.add((word, name))
                tables.add(word)
                i = k
                continue
            if j >= n or text[j] != "(":
                tables.add(word)
            i = j
            continue
        if c == "[":
            name, k = bracket_after(i)
            if name is not None:
                bare.add(name)
                i = k
                continue
        if c.isdigit():
            while i < n and (text[i].isalnum() or text[i] in "._"):
                i += 1
            continue
        i += 1
    return frozenset(qualified), frozenset(bare), frozenset(tables)


def parse_mapping_rows(source: str) -> dict:
    """Parse a mapping table's DATATABLE partition source into
    {row_key: raw_row_tuple_text}. The row key is the first quoted element."""
    rows: dict = {}
    for m in MAPPING_ROW_RE.finditer(source or ""):
        rows[m.group(1)] = m.group(0)
    return rows

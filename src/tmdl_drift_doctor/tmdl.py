"""Minimal TMDL reader — parses just enough of a semantic model for diffing.

TMDL's indentation contract (tabs, never spaces):

* ``table X`` / ``expression X = ...`` sit at column 0,
* object declarations inside a table file (``column`` / ``measure`` /
  ``partition`` / table-level ``annotation``) sit at ONE tab,
* their properties and annotations sit at TWO tabs,
* bare multi-line expression bodies sit at THREE-or-more tabs (or blank),
* fenced expression bodies are wrapped in ``` markers.

This reader deliberately does NOT round-trip: lineage tags, annotations and
formatting are preserved by editing the *raw text* (see ``tmdl_edit``), never
by re-serializing parsed objects. Parsing exists only to answer "what objects
does this model contain and do their expressions/properties match?".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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


@dataclass
class Model:
    definition: Path                                # the definition/ dir
    tables: dict = field(default_factory=dict)      # name -> Table
    expressions: dict = field(default_factory=dict) # name -> raw block text
    ref_tables: list = field(default_factory=list)  # model.tmdl `ref table`s

    def measure(self, table: str, name: str) -> Optional[Measure]:
        t = self.tables.get(table)
        return t.measures.get(name) if t else None

    def column(self, table: str, name: str) -> Optional[Column]:
        t = self.tables.get(table)
        return t.columns.get(name) if t else None


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
            if inline:  # calculated column: `column X = <dax>`
                expr, i = _read_expression(lines, i, inline)
            else:
                i += 1
            props, lineage, i = _collect_properties(lines, i)
            table.columns[name] = Column(name=name, table=table_name,
                                         properties=props, lineage_tag=lineage)
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
        else:  # hierarchy — consume and ignore
            i += 1
    return table


def parse_expressions_file(path: Path) -> dict:
    """Parse ``expressions.tmdl`` into {name: comparable_block_text}.

    The comparable text is the raw block minus lineageTag/annotation lines —
    shared-parameter drift must not fire on identity metadata.
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    out: dict = {}
    current: Optional[str] = None
    body: list[str] = []
    for line in lines:
        m = _EXPRESSION_RE.match(line)
        if m:
            if current is not None:
                out[current] = "\n".join(body).strip()
            current = unquote(m.group(1))
            body = [line]
            continue
        if current is not None:
            stripped = line.strip()
            if stripped.startswith("lineageTag:") or stripped.startswith("annotation "):
                continue
            body.append(line)
    if current is not None:
        out[current] = "\n".join(body).strip()
    return out


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
    model.expressions = parse_expressions_file(definition / "expressions.tmdl")
    model_file = definition / "model.tmdl"
    if model_file.exists():
        for line in model_file.read_text(encoding="utf-8").split("\n"):
            m = _REF_TABLE_RE.match(line)
            if m:
                model.ref_tables.append(unquote(m.group(1)))
    return model


def normalize_expression(expr: Optional[str]) -> str:
    """Reduce an expression to its comparable form: DAX/M comments stripped,
    fence markers removed, whitespace collapsed. Reformatting a measure must
    never register as drift."""
    if not expr:
        return ""
    t = expr.replace("```", " ")
    t = re.sub(r"/\*.*?\*/", " ", t, flags=re.DOTALL)
    t = re.sub(r"(?m)//[^\n]*", " ", t)
    t = re.sub(r"(?m)^\s*--[^\n]*", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def parse_mapping_rows(source: str) -> dict:
    """Parse a mapping table's DATATABLE partition source into
    {row_key: raw_row_tuple_text}. The row key is the first quoted element."""
    rows: dict = {}
    for m in MAPPING_ROW_RE.finditer(source or ""):
        rows[m.group(1)] = m.group(0)
    return rows

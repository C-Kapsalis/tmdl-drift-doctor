"""Safe raw-block TMDL surgery.

A faithful cascade copies the *raw text block* of an object from the template
and splices it into a derived model — it never regenerates TMDL from parsed
dataclasses (parsers drop lineage tags, annotations and formatting). These
helpers do that text surgery, guarded so malformed TMDL can never be written.

THE INVARIANT (learned the hard way, encoded in ``insert_property_line`` and
enforced by ``assert_no_property_injection``):

    A property line must NEVER be inserted INTO a DAX expression body.

TMDL measures come in three shapes. Single-line (``measure X = COUNTROWS(T)``)
and fenced (backtick-delimited) bodies have an obvious "after the expression"
insertion point. The trap is the *bare multi-line* shape::

    measure 'Churn Risk' =

            VAR _window = ...
            RETURN ...
        formatString: 0.0%

A naive "insert after the declaration line" places the property between the
``=`` and the DAX — the engine then reads ``isHidden`` as the start of the
expression and every affected measure fails to compile, fleet-wide. Properties
for this shape belong AFTER the whole expression body.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Optional, Tuple

from . import TmdlEditError

# A line that begins a new one-tab object inside a table file (or the
# table-level annotation tail). `///` description lines belong to the NEXT
# object and open its block.
_OBJECT_BOUNDARY = re.compile(
    r"^\t(?:///|column\b|measure\b|partition\b|hierarchy\b|annotation\b)"
)

# TMDL property keywords that must never appear inside an expression body.
_PROPERTY_KEYWORDS = (
    "isHidden", "formatString", "displayFolder", "lineageTag", "dataType",
    "summarizeBy", "sourceColumn", "sortByColumn", "isPrivate", "mode",
)
_PROPERTY_LINE = re.compile(
    r"^\t\t(?:" + "|".join(_PROPERTY_KEYWORDS) + r")\b\s*:?"
)


# ───────────────────── block locate / extract / remove ─────────────────────

def _decl_line_index(lines: list, keyword: str, name: str) -> Optional[int]:
    """Index of the ``\\t<keyword> <name>`` declaration line, or None."""
    quoted = re.escape("'" + name.replace("'", "''") + "'")
    decl = re.compile(
        rf"^\t{keyword}\s+(?:{quoted}|\"{re.escape(name)}\"|{re.escape(name)})"
        rf"(?:\s*=|\s*$)"
    )
    for i, line in enumerate(lines):
        if decl.match(line):
            return i
    return None


def block_span(content: str, keyword: str, name: str) -> Optional[Tuple[int, int]]:
    """(start_line, end_line) of an object's full block: leading ``///``
    description lines through trailing properties/annotations, excluding the
    next object and trailing blank lines. None if the object is absent."""
    lines = content.split("\n")
    idx = _decl_line_index(lines, keyword, name)
    if idx is None:
        return None
    start = idx
    while start - 1 >= 0 and lines[start - 1].startswith("\t///"):
        start -= 1
    end = len(lines)
    in_fence = "```" in lines[idx]
    for j in range(idx + 1, len(lines)):
        if in_fence:
            if "```" in lines[j]:
                in_fence = False
            continue
        if _OBJECT_BOUNDARY.match(lines[j]):
            end = j
            break
    while end - 1 > idx and lines[end - 1].strip() == "":
        end -= 1
    return (start, end)


def extract_block(content: str, keyword: str, name: str) -> Optional[str]:
    """The raw text of an object's block (no trailing blank lines)."""
    span = block_span(content, keyword, name)
    if span is None:
        return None
    return "\n".join(content.split("\n")[span[0]:span[1]])


def remove_block(content: str, keyword: str, name: str) -> Optional[str]:
    """Content with the object's block removed (one blank separator collapsed).
    None if the object is absent."""
    span = block_span(content, keyword, name)
    if span is None:
        return None
    lines = content.split("\n")
    start, end = span
    if end < len(lines) and lines[end].strip() == "":
        end += 1
    del lines[start:end]
    return "\n".join(lines)


def replace_block(content: str, keyword: str, name: str, new_block: str) -> Optional[str]:
    """Replace an object's block in place. None if the object is absent."""
    span = block_span(content, keyword, name)
    if span is None:
        return None
    lines = content.split("\n")
    lines[span[0]:span[1]] = new_block.split("\n")
    return "\n".join(lines)


# ───────────────────────── block insertion ─────────────────────────

def insert_measure_block(content: str, block: str) -> str:
    """Insert a measure block before the first partition or table-level
    annotation (idiomatic TMDL keeps measures above partitions)."""
    lines = content.split("\n")
    insert_at = len(lines)
    boundary = re.compile(r"^\t(?:partition\b|annotation\b)")
    for i, line in enumerate(lines):
        if boundary.match(line):
            insert_at = i
            break
    lines[insert_at:insert_at] = block.split("\n") + [""]
    return "\n".join(lines)


def insert_column_block(content: str, block: str) -> str:
    """Insert a column block before the first measure/partition (or the
    table-level annotation tail)."""
    lines = content.split("\n")
    boundary = re.compile(r"^\t(?:measure\b|partition\b|annotation\b)")
    insert_at = len(lines)
    for i, line in enumerate(lines):
        if boundary.match(line):
            insert_at = i
            break
    lines[insert_at:insert_at] = block.split("\n") + [""]
    return "\n".join(lines)


# ───────────────── the never-inject-into-DAX invariant ─────────────────

def _expression_end(lines: list, decl_i: int, block_end: int) -> int:
    """First line index AFTER an object's expression body.

    * inline body (text after ``=``)      -> decl_i + 1
    * fenced body                          -> line after the closing fence
    * bare multi-line body                 -> after the last blank/3-tab line
    * no expression at all (plain column)  -> decl_i + 1
    """
    decl = lines[decl_i]
    m = re.match(r"^\t\S+\s+.*?=\s*(.*)$", decl)
    inline = m.group(1).strip() if m else ""
    if inline.startswith("```"):
        for j in range(decl_i + 1, block_end):
            if "```" in lines[j]:
                return j + 1
        return block_end
    if inline or "=" not in decl:
        return decl_i + 1
    # bare multi-line: consume blank + three-tab lines
    j = decl_i + 1
    last_body = decl_i
    while j < block_end and (lines[j].strip() == "" or lines[j].startswith("\t\t\t")):
        if lines[j].strip() != "":
            last_body = j
        j += 1
    return last_body + 1 if last_body > decl_i else decl_i + 1


def insert_property_line(content: str, keyword: str, name: str,
                         prop_line: str) -> Optional[str]:
    """Insert a two-tab property line (e.g. ``\\t\\tisHidden``) into an
    object's block — ALWAYS after the expression body, never inside it.

    Idempotent: if the object already carries the property, the content is
    returned unchanged. Returns None if the object is absent.
    """
    span = block_span(content, keyword, name)
    if span is None:
        return None
    lines = content.split("\n")
    start, end = span
    prop_key = prop_line.strip().split(":", 1)[0].split(" ", 1)[0]
    for j in range(start, end):
        existing = lines[j].strip()
        if existing == prop_key or existing.split(":", 1)[0].split(" ", 1)[0] == prop_key:
            return content  # already present — no-op
    decl = re.compile(rf"^\t{keyword}\b")
    decl_i = next((i for i in range(start, end) if decl.match(lines[i])), None)
    if decl_i is None:
        return None
    insert_at = _expression_end(lines, decl_i, end)
    lines.insert(insert_at, prop_line)
    return "\n".join(lines)


def remove_property_line(content: str, keyword: str, name: str,
                         prop_key: str) -> Optional[str]:
    """Drop a two-tab property line (by keyword) from an object's block."""
    span = block_span(content, keyword, name)
    if span is None:
        return None
    lines = content.split("\n")
    start, end = span
    pat = re.compile(rf"^\t\t{re.escape(prop_key)}\b")
    kept = [l for i, l in enumerate(lines)
            if not (start <= i < end and pat.match(l))]
    return "\n".join(kept)


# ───────────────────────── write guards ─────────────────────────

def assert_no_property_injection(content: str) -> None:
    """Refuse content where a property line sits INSIDE an expression body.

    For every measure/column declaration whose ``=`` has an empty right-hand
    side (the bare multi-line shape), a two-tab property line that appears
    BEFORE the expression body — while body lines still follow — means a
    property was injected into the expression. The engine would read the
    property keyword as DAX and the object would fail to compile.
    """
    lines = content.split("\n")
    decl = re.compile(r"^\t(?:measure|column)\s+.*=\s*$")
    for i, line in enumerate(lines):
        if not decl.match(line):
            continue
        j = i + 1
        seen_property_at = None
        while j < len(lines) and not _OBJECT_BOUNDARY.match(lines[j]):
            if _PROPERTY_LINE.match(lines[j]):
                if seen_property_at is None:
                    seen_property_at = j
            elif lines[j].startswith("\t\t\t") and lines[j].strip():
                if seen_property_at is not None:
                    raise TmdlEditError(
                        f"line {seen_property_at + 1} places a property inside "
                        f"the expression body of the object declared on line "
                        f"{i + 1}, so the engine would read the property as "
                        f"DAX and the object would fail to compile. Nothing "
                        f"was written — move the property after the whole "
                        f"expression body.")
            j += 1


def assert_tmdl_valid(content: str) -> None:
    """Post-edit guard: refuse to persist malformed TMDL.

    Checks: no space-indentation (TMDL is tab-indented), balanced fence
    markers, no duplicated single-value property within one object, and the
    never-inject-into-DAX invariant."""
    space = re.search(r"^ +\S", content, re.MULTILINE)
    if space:
        line_no = content.count("\n", 0, space.start()) + 1
        raise TmdlEditError(
            f"line {line_no} is indented with spaces — TMDL requires tabs. "
            f"Convert the indentation to tabs and retry.")
    if content.count("```") % 2 != 0:
        raise TmdlEditError(
            "the content has an odd number of ``` expression fence markers — "
            "a fenced expression was opened or closed without its pair. "
            "Balance the fences and retry.")
    obj = re.compile(r"^\t(?:measure|column|partition|hierarchy)\b")
    seen: set = set()
    for i, ln in enumerate(content.split("\n")):
        if obj.match(ln):
            seen = set()
        m = re.match(r"^\t\t([A-Za-z][A-Za-z0-9]*)\s*(?::|$)", ln)
        if m and m.group(1) in _PROPERTY_KEYWORDS:
            if m.group(1) in seen:
                raise TmdlEditError(
                    f"line {i + 1} repeats the property '{m.group(1)}' — a "
                    f"duplicate property within one object. Remove one of "
                    f"the two occurrences and retry.")
            seen.add(m.group(1))
    assert_no_property_injection(content)


def guarded_write(path: Path, content: str) -> None:
    """Validate then write. Malformed TMDL is never persisted."""
    assert_tmdl_valid(content)
    Path(path).write_text(content, encoding="utf-8")


# ───────────────────────── lineage tags ─────────────────────────

_LINEAGE_RE = re.compile(r"(lineageTag:\s*)[0-9A-Fa-f-]{8,}")


def regenerate_lineage_tag(block: str) -> str:
    """Fresh uuid for the FIRST lineageTag in a spliced block. A block copied
    from the template carries the template's tag; pasting it into a derived
    model can collide with an existing tag and the model refuses to load."""
    return _LINEAGE_RE.sub(lambda m: m.group(1) + str(uuid.uuid4()), block, count=1)


def regenerate_all_lineage_tags(content: str) -> str:
    """Fresh uuid for EVERY lineageTag — for a whole table file copied from
    the template."""
    return _LINEAGE_RE.sub(lambda m: m.group(1) + str(uuid.uuid4()), content)


def first_lineage_tag(block: str) -> Optional[str]:
    m = _LINEAGE_RE.search(block)
    return m.group(0).split(":", 1)[1].strip() if m else None


def set_first_lineage_tag(block: str, tag: str) -> str:
    """Overwrite the first lineageTag with `tag` (used to preserve a derived
    object's stable identity when its block is overwritten from the template)."""
    return _LINEAGE_RE.sub(lambda m: m.group(1) + tag, block, count=1)


# ───────────────────────── mapping-table rows ─────────────────────────

def add_mapping_row(content: str, row_tuple: str, after_key_re: Optional[str] = None) -> str:
    """Insert a DATATABLE row tuple after the last existing row.

    `row_tuple` is the raw ``{ "key", ... }`` text as extracted from the
    template. Idempotent on the row key."""
    from .tmdl import MAPPING_ROW_RE, parse_mapping_rows
    key_m = MAPPING_ROW_RE.match(row_tuple.strip())
    if key_m and key_m.group(1) in parse_mapping_rows(content):
        return content  # already present
    matches = list(MAPPING_ROW_RE.finditer(content))
    if not matches:
        raise TmdlEditError(
            "the table's partition source contains no DATATABLE rows to "
            "anchor the insert after — add the first row to the derived "
            "model manually, then re-run remediation for the rest.")
    last = matches[-1]
    line_start = content.rfind("\n", 0, last.start()) + 1
    indent = content[line_start:last.start()]
    if indent.strip():
        indent = "\t\t\t\t"
    return (content[:last.end()] + ",\n" + indent + row_tuple.strip()
            + content[last.end():])


def remove_mapping_row(content: str, row_key: str) -> Optional[str]:
    """Remove the row tuple whose first element equals `row_key`, together
    with its separating comma. None if the row is absent."""
    from .tmdl import MAPPING_ROW_RE
    for m in MAPPING_ROW_RE.finditer(content):
        if m.group(1) != row_key:
            continue
        start, end = m.start(), m.end()
        # prefer swallowing the PRECEDING comma (row is not the first)
        before = content[:start]
        trailing_ws = len(before) - len(before.rstrip())
        cut_from = start - trailing_ws
        if before.rstrip().endswith(","):
            cut_from = len(before.rstrip()) - 1
            return content[:cut_from] + content[end:]
        # first row: swallow the FOLLOWING comma instead
        after = content[end:]
        m2 = re.match(r"\s*,", after)
        if m2:
            return content[:start] + after[m2.end():]
        return content[:cut_from] + content[end:]
    return None


# ───────────────────────── model.tmdl registration ─────────────────────────

def smart_quote(name: str) -> str:
    """Quote a TMDL identifier only when it needs it."""
    if any(c in name for c in " .'"):
        return "'" + name.replace("'", "''") + "'"
    return name


def register_table(model_content: str, table_name: str) -> str:
    """Add a ``ref table <name>`` line to model.tmdl. Idempotent."""
    ref_stmt = f"ref table {smart_quote(table_name)}"
    if re.search(rf"^{re.escape(ref_stmt)}\s*$", model_content, re.MULTILINE):
        return model_content
    refs = list(re.finditer(r"^ref table .*$", model_content, re.MULTILINE))
    if refs:
        last = refs[-1]
        return (model_content[:last.end()] + "\n" + ref_stmt
                + model_content[last.end():])
    return model_content.rstrip("\n") + "\n\n" + ref_stmt + "\n"


def deregister_table(model_content: str, table_name: str) -> str:
    """Remove the ``ref table <name>`` line from model.tmdl."""
    ref_stmt = f"ref table {smart_quote(table_name)}"
    pattern = re.compile(rf"^{re.escape(ref_stmt)}\s*\n?", re.MULTILINE)
    return pattern.sub("", model_content)


# ───────────────────────── expressions.tmdl blocks ─────────────────────────

def expression_block_span(content: str, name: str) -> Optional[Tuple[int, int]]:
    """(start, end) lines of a top-level ``expression NAME = ...`` block."""
    esc = re.escape(name)
    quoted = re.escape("'" + name.replace("'", "''") + "'")
    decl = re.compile(rf"^expression\s+(?:{quoted}|{esc})\s*=")
    lines = content.split("\n")
    start = next((i for i, l in enumerate(lines) if decl.match(l)), None)
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("expression "):
            end = j
            break
    while end - 1 > start and lines[end - 1].strip() == "":
        end -= 1
    return (start, end)


def extract_expression_block(content: str, name: str) -> Optional[str]:
    span = expression_block_span(content, name)
    if span is None:
        return None
    return "\n".join(content.split("\n")[span[0]:span[1]])


def replace_or_append_expression(content: str, name: str, new_block: str) -> str:
    """Replace a named top-level expression block, or append it."""
    span = expression_block_span(content, name)
    lines = content.split("\n")
    if span is None:
        while lines and lines[-1].strip() == "":
            lines.pop()
        lines.extend(["", *new_block.split("\n")])
        return "\n".join(lines) + "\n"
    lines[span[0]:span[1]] = new_block.split("\n")
    return "\n".join(lines)

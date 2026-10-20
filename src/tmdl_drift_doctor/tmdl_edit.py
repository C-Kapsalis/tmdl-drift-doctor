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
# object and open its block. Table-level blocks such as refreshPolicy end the
# previous object too: without them, a table's last column would carry the
# refresh policy along when it is copied or removed.
_OBJECT_BOUNDARY = re.compile(
    r"^(?:\t(?:///|column\b|measure\b|partition\b|hierarchy\b|annotation\b"
    r"|refreshPolicy\b|calculationGroup\b|changedProperty\b|extendedProperty\b"
    r"|table\b)|table\b)"
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
    annotation (idiomatic TMDL keeps measures above partitions). A block
    line that does not start with a tab gets one, so a block written at
    column 0 still lands at object depth."""
    block = "\n".join(ln if (not ln or ln.startswith("\t")) else "\t" + ln
                      for ln in block.split("\n"))
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
    assert_properties_before_annotations(content)
    assert_unique_vars(content)


_TAIL_LINE = re.compile(r"^\t\t(?:annotation|changedProperty|extendedProperty)\b")


def assert_properties_before_annotations(content: str) -> None:
    """Refuse a simple property line that sits after the object's first
    two-tab annotation / changedProperty line: TMDL lists an object's
    properties first and its annotations last."""
    tail_at = None
    for i, ln in enumerate(content.split("\n")):
        if _OBJECT_BOUNDARY.match(ln):
            tail_at = None
            continue
        if _TAIL_LINE.match(ln):
            if tail_at is None:
                tail_at = i
            continue
        if tail_at is not None and _PROPERTY_LINE.match(ln):
            raise TmdlEditError(
                f"line {i + 1} sets a property after the annotation on line "
                f"{tail_at + 1}; properties come before an object's "
                f"annotations. Nothing was written.")


_VAR_TOKEN = re.compile(r"\(|\)|\bVAR\s+([A-Za-z_][A-Za-z0-9_.]*)|\bRETURN\b",
                        re.IGNORECASE)


def duplicate_var(dax: str) -> Optional[str]:
    """The first VAR name declared twice in one scope (same nesting level,
    before that level's RETURN), or None."""
    from .tmdl import blank_strings
    text = re.sub(r"'[^']*'|\[[^\]]*\]", " ", blank_strings(dax or ""))
    depth = 0
    scopes: dict = {0: set()}
    for m in _VAR_TOKEN.finditer(text):
        tok = m.group(0)
        if tok == "(":
            depth += 1
            scopes[depth] = set()
        elif tok == ")":
            scopes.pop(depth, None)
            depth = max(depth - 1, 0)
            scopes.setdefault(depth, set())
        elif m.group(1):
            name = m.group(1).casefold()
            if name in scopes.setdefault(depth, set()):
                return m.group(1)
            scopes[depth].add(name)
        else:   # RETURN closes the scope opened by this level's VARs
            scopes[depth] = set()
    return None


def _measure_bodies(content: str):
    """Yield (line number, name, DAX) for every measure in `content`."""
    lines = content.split("\n")
    decl = re.compile(r"^\tmeasure\s+(.+?)\s*=\s?(.*)$")
    for i, ln in enumerate(lines):
        m = decl.match(ln)
        if not m:
            continue
        body, j = [m.group(2)], i + 1
        fenced = m.group(2).strip() == "```"
        while j < len(lines):
            nxt = lines[j]
            if fenced:
                body.append(nxt)
                if nxt.strip() == "```":
                    break
            elif nxt.startswith("\t\t\t") or not nxt.strip():
                body.append(nxt)
            else:
                break
            j += 1
        yield i + 1, m.group(1), "\n".join(body)


def assert_unique_vars(content: str) -> None:
    """Refuse a measure whose DAX declares one VAR name twice in a scope
    (the engine rejects the whole measure)."""
    for line_no, name, dax in _measure_bodies(content):
        dup = duplicate_var(dax)
        if dup:
            raise TmdlEditError(
                f"measure {name} (line {line_no}) declares VAR {dup} twice "
                f"in one scope. Nothing was written.")


def assert_round_trip(content: str, keyword: str, name: str,
                      intended: str) -> None:
    """After splicing or moving a block, re-extract it: it must exist,
    hold at most one fence pair, and equal the intended block exactly."""
    got = extract_block(content, keyword, name)
    if got is None:
        raise TmdlEditError(f"{keyword} {name} cannot be found again after "
                            f"the edit. Nothing was written.")
    if got.count("```") > 2:
        raise TmdlEditError(f"{keyword} {name} holds nested expression "
                            f"fences after the edit. Nothing was written.")
    if got.strip("\n") != intended.strip("\n"):
        raise TmdlEditError(f"{keyword} {name} does not read back as the "
                            f"block that was written. Nothing was written.")


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
# Spans come from ``tmdl.expression_spans``: a block owns the ``///`` run
# directly above its declaration and never the run above the NEXT one.

def expression_block_spans(content: str, name: str) -> list:
    """Every (start, end) line span declaring `name`, in file order."""
    from .tmdl import expression_spans
    return [(s, e) for n, s, e in expression_spans(content) if n == name]


def expression_block_span(content: str, name: str) -> Optional[Tuple[int, int]]:
    """(start, end) lines of the first ``expression NAME = ...`` block,
    its own leading doc comment included."""
    spans = expression_block_spans(content, name)
    return spans[0] if spans else None


def extract_expression_block(content: str, name: str) -> Optional[str]:
    span = expression_block_span(content, name)
    if span is None:
        return None
    return "\n".join(content.split("\n")[span[0]:span[1]])


def replace_expression_blocks(content: str, name: str, rewrite) -> str:
    """Replace each declaration of `name` with ``rewrite(old_block)``
    (return the old block to keep it). Neighbouring blocks, and the doc
    comment of the next declaration in particular, are never touched."""
    lines = content.split("\n")
    for start, end in reversed(expression_block_spans(content, name)):
        old = "\n".join(lines[start:end])
        new = rewrite(old)
        if new != old:
            lines[start:end] = new.split("\n")
    return "\n".join(lines)


def append_expression_block(text, block):
    """Append a block after the existing ones: exactly one blank line
    between blocks, the rest of the file byte-identical."""
    lines = text.split("\n")
    while lines and lines[-1].strip() == "":
        lines.pop()
    if lines:
        lines.append("")
    lines.extend(block.rstrip("\n").split("\n"))
    return "\n".join(lines) + "\n"


def replace_or_append_expression(content: str, name: str, new_block: str) -> str:
    """Replace every declaration of `name` with `new_block`, or append it."""
    if expression_block_span(content, name) is None:
        return append_expression_block(content, new_block)
    return replace_expression_blocks(content, name, lambda _old: new_block)


# ───────────────────── measure DAX body replacement ─────────────────────
# Replaces the DAX body of one measure and nothing else. The span finder and
# formatter live in ``measure_expression_replacer`` (a module attribute, so a
# caller or a test can swap it); ``replace_measure_expression`` wraps it in a
# preservation guard that locates the non-DAX regions on its own.

def _measure_decl(lines: list, name: str) -> Optional[Tuple[int, int]]:
    """(line index, column just past '=') of the measure's declaration."""
    q = re.escape(name.replace("'", "''"))
    pat = re.compile(rf"^\s*measure\s+(?:'{q}'|\"{re.escape(name)}\"|"
                     rf"{re.escape(name)})\s*=")
    for i, line in enumerate(lines):
        m = pat.match(line)
        if m:
            return i, m.end()
    return None


def _shallow(line: str) -> bool:
    """A line that ends a bare/inline body: at most two tabs, then text."""
    return re.match(r"^\t{0,2}\S", line) is not None


def _fenced_dax_lines(dax: str) -> list:
    out = []
    raw = dax.split("\n")
    if raw and raw[0].strip().startswith("measure"):
        raw = raw[1:]
    for line in raw:
        if not line.strip():
            out.append("")
            continue
        lead = re.match(r"^[ \t]*", line).group(0)
        rest = line[len(lead):]
        # each leading group of four spaces becomes a tab, then the tabs go
        lead = lead.replace("    ", "\t").lstrip("\t")
        text = lead + rest
        stripped = text.strip()
        depth = 3 if (stripped.startswith(") &") or stripped == ")") else 5
        out.append("\t" * depth + text)
    return out


def _default_measure_replacer(text, measure, dax_text):
    content, name, new_dax = text, measure, dax_text
    lines = content.split("\n")
    found = _measure_decl(lines, name)
    if found is None:
        return False, content
    i, eq = found
    rest = lines[i][eq:].strip()
    if rest.startswith("```"):
        if rest != "```":
            return False, content
        close = next((j for j in range(i + 1, len(lines))
                      if lines[j].strip() == "```"), None)
        if close is None:
            return False, content
        first, last = i + 1, close
        while first < last and not lines[first].strip():
            first += 1
        while last > first and not lines[last - 1].strip():
            last -= 1
        lines[first:last] = _fenced_dax_lines(new_dax)
        return True, "\n".join(lines)
    j = i + 1
    while j < len(lines) and not _shallow(lines[j]):
        j += 1
    end = j
    while end > i + 1 and not lines[end - 1].strip():
        end -= 1
    if not rest and end == i + 1:
        return False, content          # bare declaration with no body
    dax = new_dax.split("\n")
    while dax and not dax[0].strip():
        dax.pop(0)
    while dax and not dax[-1].strip():
        dax.pop()
    if dax and dax[0].strip().startswith("measure "):
        dax.pop(0)
        while dax and not dax[0].strip():
            dax.pop(0)
    if not dax:
        return False, content
    head = lines[i][:eq]
    if rest and len(dax) == 1:
        lines[i:end] = [head + " " + dax[0].strip()]
    else:
        lines[i:end] = [head] + [("\t\t\t" + d) if d.strip() else ""
                                 for d in dax]
    return True, "\n".join(lines)


measure_expression_replacer = _default_measure_replacer


def _guard_regions(content: str, name: str) -> Optional[tuple]:
    """The four regions of a file around one measure that are not DAX:
    above the declaration, the declaration through '=', the measure's own
    property/annotation lines, and everything from the next object on.
    Located independently of the body finder it guards."""
    lines = content.split("\n")
    found = _measure_decl(lines, name)
    if found is None:
        return None
    i, eq = found
    k = i + 1
    if lines[i][eq:].strip().startswith("```"):
        while k < len(lines) and lines[k].strip() != "```":
            k += 1
        k += 1
    props_at = None
    while k < len(lines):
        line = lines[k]
        if re.match(r"^\t\t[^\t\s]", line):
            props_at = k
            break
        if re.match(r"^\t?[^\t\s]", line):
            break
        k += 1
    if props_at is None:
        props, tail_at = [], k
    else:
        tail_at = props_at
        while tail_at < len(lines) and not re.match(r"^\t?[^\t\s]",
                                                     lines[tail_at]):
            tail_at += 1
        props = lines[props_at:tail_at]
        while props and not props[-1].strip():
            props.pop()
    return ("\n".join(lines[:i]), lines[i][:eq], "\n".join(props),
            "\n".join(lines[tail_at:]))


_REGION_NAMES = ("the text above the declaration (its description)",
                 "the declaration line", "its properties and annotations",
                 "everything after the measure")


def replace_measure_expression(text, measure, dax_text):
    """(ok, new content): the measure's DAX body replaced, nothing else.

    Raises TmdlEditError, and returns nothing to write, when the result
    changed any non-DAX region (a property, the description, the next
    object); ``(False, content)`` when the measure or its body cannot be
    located."""
    ok, out = measure_expression_replacer(text, measure, dax_text)
    if not ok:
        return False, text
    name = measure
    before, after = _guard_regions(text, name), _guard_regions(out, name)
    if after is None:
        raise TmdlEditError(
            f"replacing the DAX of measure '{name}' lost its declaration; "
            f"nothing was written.")
    for label, a, b in zip(_REGION_NAMES, before, after):
        if a != b:
            raise TmdlEditError(
                f"replacing the DAX of measure '{name}' would change {label}, "
                f"not just the expression body; nothing was written.")
    return True, out


# ───────────────────────── relationships.tmdl blocks ─────────────────────────
# Relationships are matched on their FIELD signature (``A[x] -> B[y]``), never
# on the block name: the name is usually a GUID that differs per model and
# changes when Power BI Desktop re-creates a relationship.

def relationship_span(content: str, signature: str) -> Optional[Tuple[int, int]]:
    """(start, end) lines of the block whose endpoints match `signature`."""
    from .tmdl import parse_relationships_text, relationship_block_spans
    lines = content.split("\n")
    for _name, start, end in relationship_block_spans(content):
        rels = parse_relationships_text("\n".join(lines[start:end]))
        if signature in rels:
            return (start, end)
    return None


def extract_relationship_block(content: str, signature: str) -> Optional[str]:
    span = relationship_span(content, signature)
    if span is None:
        return None
    return "\n".join(content.split("\n")[span[0]:span[1]])


def relationship_names(content: str) -> set:
    from .tmdl import relationship_block_spans
    return {name for name, _s, _e in relationship_block_spans(content)}


def set_relationship_name(block: str, name: str) -> str:
    """Rewrite the ``relationship <name>`` declaration line of a block."""
    lines = block.split("\n")
    lines[0] = f"relationship {smart_quote(name)}"
    return "\n".join(lines)


def append_relationship_block(content: str, block: str) -> str:
    """Append a raw relationship block, one blank line as separator."""
    body = content.rstrip("\n")
    if not body:
        return block.rstrip("\n") + "\n"
    return body + "\n\n" + block.rstrip("\n") + "\n"


def replace_relationship_block(content: str, signature: str,
                               new_block: str) -> Optional[str]:
    span = relationship_span(content, signature)
    if span is None:
        return None
    lines = content.split("\n")
    lines[span[0]:span[1]] = new_block.rstrip("\n").split("\n")
    return "\n".join(lines)


def remove_relationship_block(content: str, signature: str) -> Optional[str]:
    """Content without the matching block (one blank separator collapsed).
    None if no block matches."""
    span = relationship_span(content, signature)
    if span is None:
        return None
    lines = content.split("\n")
    start, end = span
    if end < len(lines) and lines[end].strip() == "":
        end += 1
    elif start > 0 and lines[start - 1].strip() == "":
        start -= 1
    del lines[start:end]
    out = "\n".join(lines)
    return out if out.strip() else ""

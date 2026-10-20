"""Removing a dead table sensor from a DAX expression.

A table the ledger retired no longer exists in the model. A measure that
still names it does so in one of two ways:

* inside a **sensor** operand — ``ISFILTERED('Old Campaigns')``,
  ``ISCROSSFILTERED(...)``, ``HASONEVALUE(...)`` and friends. The model
  reads the table through a sensor, not for its rows, so the operand can
  go and the rest of the chain stays.
* as **data** — the measure reads the table for its rows. The reference
  itself has to go.

The sensor case is the one that can be repaired without deciding anything:
the operand is dead, and the chain it belonged to keeps working. The data
case needs a person, and the tool says so.
"""

from __future__ import annotations

import re
from typing import Optional

# The DAX functions that test a table rather than read it.
SENSOR_FUNCS = ("ISFILTERED", "ISCROSSFILTERED", "HASONEVALUE", "HASONEFILTER",
                "ISEMPTY", "ISINSCOPE")

_OPERATORS = ("&&", "||")

# A table reference: bare `Table[` or quoted `'Table'[`, and the quoted
# bare form a sensor takes (`ISEMPTY('Old Campaigns')`).
def _table_ref_pattern(table: str) -> re.Pattern:
    quoted = re.escape("'" + table.replace("'", "''") + "'")
    bare = re.escape(table)
    return re.compile(rf"(?:{quoted}\s*(?:\[|\)|$)|{bare}\s*\[)")


def _match_paren(text: str, open_idx: int) -> Optional[int]:
    """The index of the ``)`` that closes the ``(`` at ``open_idx``."""
    depth = 0
    i = open_idx
    in_str: Optional[str] = None
    while i < len(text):
        c = text[i]
        if in_str:
            if c == in_str:
                in_str = None
        elif c in "'\"":
            in_str = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _find_sensor_operands(text: str, table: str) -> list:
    """Spans of whole boolean operands that are a table sensor over
    ``table``.

    An operand is a boolean term whose test is a table sensor:
    ``ISFILTERED('Old Campaigns')``, ``int(not ISEMPTY('Old Campaigns'))``,
    ``NOT ISFILTERED(...)``. Returned in document order; the caller
    re-scans after each removal because the indices shift.
    """
    ref = _table_ref_pattern(table)
    spans = []
    for m in re.finditer(
            rf"\b({'|'.join(SENSOR_FUNCS)})\s*\(", text, re.IGNORECASE):
        open_idx = text.index("(", m.end() - 1)
        close = _match_paren(text, open_idx)
        if close is None:
            continue
        if not ref.search(text[open_idx:close]):
            continue
        # widen to the whole boolean operand: the wrapping call and the
        # `not` in front of it
        start = m.start()
        prefix = _skip_ws_back(text, start)
        if prefix >= 3 and text[prefix - 3:prefix].lower() == "not":
            start = prefix - 3
        end = close + 1
        # a wrapping call: `int(<operand>)`, `boolean(<operand>)`
        for wrapper in ("int", "boolean", "is", "coalesce"):
            call_open = text.rfind(wrapper + "(", max(0, start - 40), start)
            if call_open < 0:
                continue
            call_close = _match_paren(text, call_open + len(wrapper))
            if call_close is None or call_close < end:
                continue
            between = text[call_open + len(wrapper) + 1:start]
            if re.fullmatch(r"[\s\w]*", between):
                start = call_open
                end = call_close + 1
                break
        spans.append((start, end))
    return spans


def _skip_ws_fwd(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def _skip_ws_back(text: str, i: int) -> int:
    while i > 0 and text[i - 1] in " \t\r\n":
        i -= 1
    return i


def _remove_operand(text: str, start: int, end: int) -> str:
    """Remove the operand ``[start, end)`` plus the operator joining it to
    its chain.

    The following operator is taken when the chain continues, else the
    preceding one. The surrounding layout is otherwise untouched.
    """
    after = _skip_ws_fwd(text, end)
    has_following = any(text.startswith(op, after) for op in _OPERATORS)

    if has_following:
        cut_start, cut_end = start, after + 2
    else:
        before = _skip_ws_back(text, start)
        if before >= 2 and text[before - 2:before] in _OPERATORS:
            cut_start, cut_end = before - 2, end
        else:
            # the sole operand of its chain: no operator to consume
            cut_start, cut_end = start, end

    # tidy only the whitespace this cut orphaned
    if cut_end < len(text) and text[cut_end] in ",)":
        while cut_start > 0 and text[cut_start - 1] in " \t":
            cut_start -= 1
    # a cut that leaves a dangling comma behind absorbs it
    if cut_end < len(text) and text[cut_end] == ",":
        cut_end += 1
        while cut_end < len(text) and text[cut_end] in " \t":
            cut_end += 1

    out = text[:cut_start] + text[cut_end:]

    # the cut may have left trailing whitespace at the end of a line
    lines = out.split("\n")
    lo = out[:cut_start].count("\n")
    if lo < len(lines):
        lines[lo] = lines[lo].rstrip()

    # drop a line the cut left entirely blank
    hi = min(len(lines), lo + 2)
    keep = [ln for idx, ln in enumerate(lines)
            if not (lo <= idx < hi and ln.strip() == "")]
    return "\n".join(keep)


def strip_dax_sensors(text: str, table: str) -> tuple:
    """Remove every droppable table-sensor operand for ``table``.

    Returns ``(new text, number of operands removed)``.
    """
    removed = 0
    while True:
        spans = _find_sensor_operands(text, table)
        if not spans:
            break
        start, end = spans[0]
        text = _remove_operand(text, start, end)
        removed += 1
        if removed > 500:      # runaway guard
            raise RuntimeError(
                f"sensor removal did not converge for {table!r}")
    return text, removed


def data_references(text: str, table: str) -> list:
    """The spans where ``text`` reads ``table`` for its rows.

    A reference that is not inside a sensor operand is a data reference.
    """
    ref = _table_ref_pattern(table)
    spans = _find_sensor_operands(text, table)
    out = []
    for m in ref.finditer(text):
        inside = any(start <= m.start() < end for start, end in spans)
        if not inside:
            out.append((m.start(), m.end()))
    return out

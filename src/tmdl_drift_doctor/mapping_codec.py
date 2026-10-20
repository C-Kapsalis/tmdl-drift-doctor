"""Encoded entered-data tables (Power BI "Enter data").

Desktop stores such a table's rows as one string inside the M partition: the
quoted first argument of ``Binary.FromText(``, holding base64 of a raw
DEFLATE stream of JSON (a list of rows, each a list of cells). Rows are
HEADERLESS: the first row is data. The usual shape is three cells
(category, subcategory, key); some models keep two (category, key). Row
identity everywhere is (first cell, last cell).

Desktop re-encodes the string on every edit, so the contract is content
fidelity (the decoded rows), never byte identity of the string. Every write
re-encodes, verifies the new text decodes to exactly the intended rows, and
changes nothing outside the string.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import zlib

from . import DriftDoctorError


class CodecError(DriftDoctorError):
    """An encoded table that cannot be read or written safely."""


_BLOB = re.compile(r'Binary\.FromText\(\s*"([A-Za-z0-9+/=]*)"')


def decode_rows(b64: str) -> list:
    try:
        packed = base64.b64decode(b64, validate=True)
        plain = zlib.decompressobj(-zlib.MAX_WBITS).decompress(packed)
        rows = json.loads(plain.decode("utf-8"))
    except (binascii.Error, ValueError, zlib.error, UnicodeDecodeError) as e:
        raise CodecError(f"the encoded string does not decode ({e})") from e
    if not isinstance(rows, list) or not all(isinstance(r, list) for r in rows):
        raise CodecError("the encoded value does not decode to a list of rows")
    return rows


def encode_rows(rows: list) -> str:
    raw = json.dumps(rows, separators=(",", ":"), ensure_ascii=True)
    c = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    return base64.b64encode(c.compress(raw.encode("utf-8"))
                            + c.flush()).decode("ascii")


def _blob(text: str):
    found = list(_BLOB.finditer(text or ""))
    if len(found) != 1:
        raise CodecError(f"expected exactly 1 encoded string "
                         f"(Binary.FromText), found {len(found)}")
    return found[0]


def is_encoded(text: str) -> bool:
    return bool(_BLOB.search(text or ""))


def extract_rows(text: str) -> list:
    return decode_rows(_blob(text).group(1))


def replace_rows(text: str, rows: list) -> str:
    """`text` with only the encoded string replaced by the encoding of
    `rows`; every other byte is unchanged."""
    m = _blob(text)
    return text[:m.start(1)] + encode_rows(rows) + text[m.end(1):]


def verify_rows(text: str, rows: list) -> None:
    """The write guard: the string in `text` must decode to `rows`."""
    widths = {len(r) for r in rows}
    if len(widths) > 1:
        raise CodecError(f"the intended rows mix widths {sorted(widths)}")
    got = extract_rows(text)
    if got != rows:
        raise CodecError("the encoded string does not decode to the intended "
                         "rows; nothing was written")


def categories(rows: list) -> list:
    """Distinct first cells, in first-seen order."""
    return list(dict.fromkeys(r[0] for r in rows if r))


def identity(row: list) -> tuple:
    return (row[0], row[-1]) if row else ("", "")


def merge_categories(client: list, template: list, wanted) -> list:
    """Client rows unchanged and first, then the template rows of every
    requested category, in template order. Never edits a client row."""
    wanted = set(wanted)
    return [list(r) for r in client] + [list(r) for r in template
                                        if r and r[0] in wanted]


def add_category_rows(rows: list, category: str, keys,
                      subcategory: str = "") -> tuple:
    """(rows, added, skipped): append ``[category, subcategory, key]`` for
    each key the category does not yet hold. Idempotent."""
    if not rows:
        raise CodecError("refusing to add rows to an empty z-Map (no row to "
                         "learn the table's shape from)")
    widths = {len(r) for r in rows}
    if widths != {3}:
        raise CodecError(f"add_category_rows needs 3-cell rows; this table's "
                         f"row width is {sorted(widths)}")
    have = {r[-1] for r in rows if r[0] == category}
    out, added, skipped = [list(r) for r in rows], [], []
    for key in keys:
        if key in have:
            skipped.append(key)
            continue
        row = [category, subcategory, key]
        out.append(row)
        added.append(row)
        have.add(key)
    return out, added, skipped


def remove_category_rows(rows: list, category: str, keys) -> tuple:
    """(rows kept, rows removed, siblings left in the category): remove
    only the rows of `category` whose key is listed."""
    keys = set(keys)
    kept, removed = [], []
    for r in rows:
        if r and r[0] == category and r[-1] in keys:
            removed.append(r)
        else:
            kept.append(r)
    left = [r for r in kept if r and r[0] == category]
    return kept, removed, left


def drop_groups(rows: list, groups) -> tuple:
    """(rows kept, rows removed): every row whose category cell, trimmed,
    is in `groups` (case-sensitive) goes."""
    groups = set(groups)
    kept, removed = [], []
    for r in rows:
        (removed if r and str(r[0]).strip() in groups else kept).append(r)
    return kept, removed


def union_rows(template: list, derived: list) -> tuple:
    """(rows, derived-only added): every template row in order (duplicates
    kept), then each derived row whose identity is new, as
    ``[category, "", key]``, at most once."""
    out = [list(r) for r in template]
    seen = {identity(r) for r in template}
    added = 0
    for r in derived:
        if not r:
            continue
        key = identity(r)
        if key in seen:
            continue
        seen.add(key)
        out.append([r[0], "", r[-1]])
        added += 1
    return out, added


def shaped(row: list, width: int) -> list:
    """A row at a table's arity: ``[category, "", key]`` for 3 cells,
    ``[category, key]`` for 2."""
    cat, key = row[0], row[-1]
    sub = row[1] if len(row) >= 3 else ""
    return [cat, sub, key] if width >= 3 else [cat, key]

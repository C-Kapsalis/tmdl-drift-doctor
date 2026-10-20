"""A fixer must not change a file's bytes beyond the change it was asked for.

A fleet is committed from Windows. A model file that comes back with Unix
line endings shows up as a whole-file diff: the reviewer cannot see the
change that was asked for, and the next fixer run sees a file that differs
from the one it was written from. The same holds for the trailing newline
and for a byte-order mark.

These tests pin the four cases the adversarial review found:

* DD-1  the cascade, the field-parameter retag and the field-parameter row
        writer must keep the file's own line endings;
* DD-2  ``retire`` must keep each file's own line endings;
* DD-3  ``purge`` must keep the file's trailing newline;
* DD-4  a byte-order mark must be ignored, not reported as an unparseable
        model.
"""

from __future__ import annotations

import textwrap

import pytest

CRLF = "\r\n"


def crlf(text: str) -> str:
    return text.replace("\n", CRLF)


def bytes_of(path) -> bytes:
    return path.read_bytes()


def unchanged_lines_are_identical(before: bytes, after: bytes) -> bool:
    """Every line that is still in the file must be byte-identical."""
    b = before.replace(CRLF.encode(), b"\n").split(b"\n")
    a = after.replace(CRLF.encode(), b"\n").split(b"\n")
    return [ln for ln in a if ln in b] == [ln for ln in b if ln in a]


# ── DD-1  the cascade keeps the file's line endings ────────────────────────

TEMPLATE_TABLE = textwrap.dedent("""\
    table Sales
    \tculture: en-US
    \tcolumn Amount
    \t\tdataType: int64
    \t\tlineageTag: 11111111-1111-1111-1111-111111111111
    \tmeasure 'Total Amount' = SUM(Sales[Amount])
    """)


def _fleet_with_crlf(tmp_path, model_body: str = TEMPLATE_TABLE):
    """A template and one model, both with Windows line endings."""
    root = tmp_path / "fleet"
    tdef = root / "tpl" / "definition"
    (tdef / "tables").mkdir(parents=True)
    (tdef / "model.tmdl").write_text("model Model\n\tculture: en-US\n",
                                     encoding="utf-8")
    (tdef / "tables" / "Sales.tmdl").write_bytes(crlf(model_body).encode())

    mdef = root / "north" / "definition"
    (mdef / "tables").mkdir(parents=True)
    (mdef / "model.tmdl").write_text("model Model\n\tculture: en-US\n",
                                     encoding="utf-8")
    (mdef / "tables" / "Sales.tmdl").write_bytes(crlf(model_body).encode())

    from conftest import write_fleet
    fy = write_fleet(root, tdef.parent, {"north": mdef.parent},
                     state_dir=str(root / "state"))
    return root, fy


def test_cascade_keeps_windows_line_endings(tmp_path):
    """DD-1: inserting a measure must not convert the whole file."""
    from conftest import drift
    root, fy = _fleet_with_crlf(tmp_path)
    table = root / "north" / "definition" / "tables" / "Sales.tmdl"

    # The template carries an extra measure the model does not have.
    tpl_table = root / "tpl" / "definition" / "tables" / "Sales.tmdl"
    tpl_table.write_bytes(crlf(TEMPLATE_TABLE +
                               "\tmeasure 'Extra' = COUNTROWS(Sales)\n")
                          .encode())

    drift("capture", fleet=fy)
    before = bytes_of(table)
    assert before.count(CRLF.encode()) > 0

    r = drift("remediate", "--kind", "measure.missing", "--yes", fleet=fy)
    assert r.exit_code == 0, r.output

    after = bytes_of(table)
    assert after.count(CRLF.encode()) == len(after.splitlines()), (
        "the cascade rewrote the file's line endings")
    assert b"\n" not in after.replace(CRLF.encode(), b""), (
        "the cascade left a bare LF in a CRLF file")
    assert unchanged_lines_are_identical(before, after)


# ── DD-1  the field-parameter writers keep the file's line endings ─────────

AXIS = (
    "table 'x-Plot Specific 1'\n"
    "\tcolumn 'x-Plot Specific 1'\n"
    "\t\tdataType: string\n"
    "\t\tsourceColumn: [Value1]\n"
    "\tpartition 'x-Plot Specific 1' = calculated\n"
    "\t\tmode: import\n"
    "\t\tsource =\n"
    "\t\t\t\t{\n"
    "\t\t\t\t-- Perf\n"
    "\t\t\t\t(\"Value\", NAMEOF('Sales'[amount]), 0, \"Perf\", \"Other\")\n"
    "\t\t\t\t}\n"
)


def test_the_field_parameter_retag_keeps_windows_line_endings(tmp_path):
    """DD-1: the retag command writes the file back in its own endings."""
    from conftest import drift
    axis = tmp_path / "x-Plot Specific 1.tmdl"
    axis.write_bytes(crlf(AXIS).encode())
    before = bytes_of(axis)

    r = drift("fieldparam", "retag", "--from", "Perf", "--to", "Growth",
              "--display", "Value", str(axis))
    assert r.exit_code == 0, r.output

    after = bytes_of(axis)
    assert b"\n" not in after.replace(CRLF.encode(), b""), (
        "the retag rewrote the file's line endings")
    assert after.count(CRLF.encode()) == len(after.splitlines())
    assert unchanged_lines_are_identical(before, after)


def test_the_field_parameter_row_writer_keeps_windows_line_endings(tmp_path):
    """DD-1: the row writer keeps the file's own endings."""
    from tmdl_drift_doctor import tmdl_edit as te
    from tmdl_drift_doctor.fieldparams import read_rows, rewrite_rows_text
    axis = tmp_path / "x-Plot Specific 1.tmdl"
    axis.write_bytes(crlf(AXIS).encode())
    rows, arity = read_rows(axis)
    assert rows, "the fixture axis has no rows"

    new = rewrite_rows_text(te.read_text(axis), rows, arity, path=axis)
    assert "\n" not in new.replace(CRLF, ""), (
        "the row writer rewrote the text's line endings")
    te.write_text(axis, new, te.line_ending(te.read_text(axis)))
    after = bytes_of(axis)
    assert b"\n" not in after.replace(CRLF.encode(), b"")
    assert after.count(CRLF.encode()) == len(after.splitlines())


# ── DD-2  retire keeps each file's own line endings ───────────────────────

def test_retire_keeps_windows_line_endings(tmp_path):
    """DD-2: retiring one measure must not convert a whole fleet."""
    from conftest import drift
    body = TEMPLATE_TABLE + "\tmeasure 'New' = SUM(Sales[Amount])\n"
    root, fy = _fleet_with_crlf(tmp_path, body)
    table = root / "north" / "definition" / "tables" / "Sales.tmdl"
    before = bytes_of(table)

    r = drift("retire", "Total Amount", "--new", "New", "--yes", fleet=fy)
    assert r.exit_code == 0, r.output

    after = bytes_of(table)
    assert b"\n" not in after.replace(CRLF.encode(), b""), (
        "retire rewrote the file's line endings")
    assert after.count(CRLF.encode()) == len(after.splitlines()), (
        "retire left a bare LF in a CRLF file")
    assert unchanged_lines_are_identical(before, after)


# ── DD-3  purge keeps the trailing newline ─────────────────────────────────

def test_purge_keeps_the_trailing_newline(tmp_path):
    """DD-3: removing the last object must not drop the final newline."""
    from conftest import drift
    root, fy = _fleet_with_crlf(tmp_path)
    table = root / "north" / "definition" / "tables" / "Sales.tmdl"
    before = bytes_of(table)
    assert before.endswith(b"\n")

    r = drift("purge", "--measure", "Total Amount", "--model", "north",
              "--apply", "--yes", fleet=fy)
    assert r.exit_code == 0, r.output

    after = bytes_of(table)
    assert after.endswith(b"\n"), "purge dropped the file's trailing newline"


def test_purge_keeps_a_file_that_had_no_trailing_newline_without_one(tmp_path):
    """The other side of the same rule: a file that had none keeps none."""
    from conftest import drift
    root, fy = _fleet_with_crlf(tmp_path)
    table = root / "north" / "definition" / "tables" / "Sales.tmdl"
    table.write_bytes(table.read_bytes().rstrip(b"\n"))

    r = drift("purge", "--measure", "Total Amount", "--model", "north",
              "--apply", "--yes", fleet=fy)
    assert r.exit_code == 0, r.output
    assert not table.read_bytes().endswith(b"\n"), (
        "purge added a trailing newline the file did not have")


# ── DD-4  a byte-order mark is ignored, not a parse error ─────────────────

def test_byte_order_mark_does_not_break_parsing(tmp_path):
    """DD-4: a BOM must not make the model look like it has no table."""
    from conftest import drift
    root, fy = _fleet_with_crlf(tmp_path)
    for base in (root / "tpl" / "definition", root / "north" / "definition"):
        for f in base.rglob("*.tmdl"):
            f.write_bytes(b"\xef\xbb\xbf" + f.read_bytes())

    r = drift("detect", fleet=fy)
    assert r.exit_code != 3, r.output
    assert "no `table` declaration found" not in r.output, (
        "a byte-order mark was reported as an unparseable model")

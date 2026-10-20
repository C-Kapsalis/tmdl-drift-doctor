"""An object's name may contain the characters the reference itself uses.

A reference is a table name and an object name joined with brackets:
``Sales[Total Amount]``. A name may legitimately hold a bracket of its own
(``Joe's Ünï [Proj]``), and then the reference reads
``Sales[Joe's Ünï [Proj]]``.

The reference must still be read as ONE table name and ONE object name, and
the fixer must find the object. Splitting the reference at its LAST bracket
turns the table into ``Sales[Joe's Ünï`` and the object into ``Proj]``; the
fix then looks for a table file whose name was built from the middle of the
reference, and the drift can never be remediated.
"""

from __future__ import annotations

import textwrap

BRACKET_NAME = "Joe's Ünï [Proj]"

TEMPLATE_TABLE = textwrap.dedent(f"""\
    table Sales
    \tculture: en-US
    \tcolumn Amount
    \t\tdataType: int64
    \t\tlineageTag: 11111111-1111-1111-1111-111111111111
    \tmeasure 'Total Amount' = SUM(Sales[Amount])
    \tmeasure '{BRACKET_NAME.replace("'", "''")}' = SUM(Sales[Amount])
    """)

DERIVED_TABLE = textwrap.dedent("""\
    table Sales
    \tculture: en-US
    \tcolumn Amount
    \t\tdataType: int64
    \t\tlineageTag: 22222222-2222-2222-2222-222222222222
    """)

ALLOWLIST = {"kinds": ["measure.missing", "column.missing"]}


def _fleet(tmp_path, template_body: str, derived_body: str):
    root = tmp_path / "fleet"
    tdef = root / "tpl" / "definition"
    (tdef / "tables").mkdir(parents=True)
    (tdef / "model.tmdl").write_text("model Model\n\tculture: en-US\n",
                                     encoding="utf-8")
    (tdef / "tables" / "Sales.tmdl").write_text(template_body, encoding="utf-8")

    mdef = root / "north" / "definition"
    (mdef / "tables").mkdir(parents=True)
    (mdef / "model.tmdl").write_text("model Model\n\tculture: en-US\n",
                                     encoding="utf-8")
    (mdef / "tables" / "Sales.tmdl").write_text(derived_body, encoding="utf-8")

    from conftest import write_fleet
    fy = write_fleet(root, tdef.parent, {"north": mdef.parent},
                     state_dir=str(root / "state"), allowlist=ALLOWLIST)
    return root, fy


def test_split_ref_reads_a_name_with_a_bracket():
    """The reference is one table and one object, however the name looks."""
    from tmdl_drift_doctor.detect import split_ref
    table, obj = split_ref(f"Sales[{BRACKET_NAME}]")
    assert table == "Sales"
    assert obj == BRACKET_NAME


def test_split_ref_still_reads_a_plain_reference():
    from tmdl_drift_doctor.detect import split_ref
    assert split_ref("Sales[Total Amount]") == ("Sales", "Total Amount")
    assert split_ref("Visits[Peak Hour Visits]") == ("Visits",
                                                      "Peak Hour Visits")


def test_cascade_inserts_a_measure_whose_name_has_a_bracket(tmp_path):
    """DD-5: the fix must find the object and insert it."""
    from conftest import drift
    root, fy = _fleet(tmp_path, TEMPLATE_TABLE, DERIVED_TABLE)
    table = root / "north" / "definition" / "tables" / "Sales.tmdl"

    drift("capture", fleet=fy)
    r = drift("remediate", "--kind", "measure.missing", "--yes", fleet=fy)
    assert r.exit_code == 0, r.output

    text = table.read_text(encoding="utf-8")
    assert BRACKET_NAME.replace("'", "''") in text, (
        "the measure with a bracket in its name was not inserted")
    assert f"Sales[{BRACKET_NAME}]" in r.output, (
        "the reference was split at the wrong bracket")


def test_the_measure_with_a_bracket_is_reported_as_missing(tmp_path):
    """Detection names the object; the report is not confused by it."""
    from conftest import drift
    _root, fy = _fleet(tmp_path, TEMPLATE_TABLE, DERIVED_TABLE)

    drift("capture", fleet=fy)
    r = drift("detect", fleet=fy)
    assert "measure.missing" in r.output
    assert BRACKET_NAME in r.output


def test_cascade_inserts_a_column_whose_name_has_a_bracket(tmp_path):
    """The same rule for a column."""
    from conftest import drift
    quoted = BRACKET_NAME.replace("'", "''")
    template = TEMPLATE_TABLE + (
        f"\tcolumn '{quoted}'\n"
        "\t\tdataType: int64\n"
        "\t\tlineageTag: 11111111-1111-1111-1111-111111111113\n")
    root, fy = _fleet(tmp_path, template, DERIVED_TABLE)
    table = root / "north" / "definition" / "tables" / "Sales.tmdl"

    drift("capture", fleet=fy)
    r = drift("remediate", "--kind", "column.missing", "--yes", fleet=fy)
    assert r.exit_code == 0, r.output

    text = table.read_text(encoding="utf-8")
    assert BRACKET_NAME.replace("'", "''") in text, (
        "the column with a bracket in its name was not inserted")

"""DC-3: an axis row that points at a retired object is removed.

A measure retired from the template leaves its field-parameter rows behind in
the derived models: the row's ``NAMEOF(...)`` still names the measure the
template deliberately dropped. The tool removed a row that points at nothing
at all (``fieldparam.dangling_row``), but not a row that points at an object
the ledger records as retired.

The finding is ``retire.fieldparam_entry``: one per field-parameter table
that carries such a row, naming the table and the category. The repair
removes exactly those rows — every other row, the row order and the table's
own formatting stay as they were.
"""

from __future__ import annotations

from pathlib import Path

from conftest import drift, write_fleet

from tmdl_drift_doctor.baseline import capture
from tmdl_drift_doctor.fleet import load_fleet
from tmdl_drift_doctor.ledger import Ledger

MODEL = "model Model\n\tculture: en-US\n\n"

MEASURES = """table 'Book Measures'
\tlineageTag: 40000000-0000-4000-8000-000000000001

\tmeasure 'Pages Read' = SUM(Loans[pages])
\t\tlineageTag: 40000000-0000-4000-8000-000000000002

\tmeasure 'Loans Issued' = COUNTROWS(Loans)
\t\tlineageTag: 40000000-0000-4000-8000-000000000003

\tpartition 'Book Measures' = m
\t\tmode: import
\t\tsource = x
"""

FACT = """table Loans
\tlineageTag: 40000000-0000-4000-8000-000000000010

\tcolumn pages
\t\tdataType: int64
\t\tlineageTag: 40000000-0000-4000-8000-000000000011
\t\tsummarizeBy: sum
\t\tsourceColumn: pages

\tpartition Loans = m
\t\tmode: import
\t\tsource = x
"""


def _axis(name: str, rows: list) -> str:
    body = ",\n".join("\t\t\t\t" + r for r in rows)
    return (f"table '{name}'\n\tlineageTag: 40000000-0000-4000-8000-"
            f"000000000020\n\n"
            f"\tcolumn '{name}'\n\t\tlineageTag: 40000000-0000-4000-8000-"
            f"000000000021\n\t\tsummarizeBy: none\n\t\tsourceColumn: "
            f"[Value1]\n\n"
            f"\tcolumn '{name} Fields'\n\t\tlineageTag: 40000000-0000-4000-"
            f"8000-000000000022\n\t\tsummarizeBy: none\n\t\tsourceColumn: "
            f"[Value2]\n\n"
            f"\tcolumn '{name} Order'\n\t\tlineageTag: 40000000-0000-4000-"
            f"8000-000000000023\n\t\tsummarizeBy: sum\n\t\tsourceColumn: "
            f"[Value3]\n\n"
            f"\tcolumn '{name} Field Category'\n\t\tlineageTag: 40000000-0000-"
            f"4000-8000-000000000024\n\t\tsummarizeBy: none\n\t\tsourceColumn: "
            f"[Value4]\n\n"
            f"\tpartition '{name}' = calculated\n\t\tmode: import\n"
            f"\t\tsource =\n\t\t\t\t{{\n{body}\n\t\t\t\t}}\n")


def _row(display: str, ref: str, order: int, group: str) -> str:
    return (f'("{display}", NAMEOF({ref}), {order}, "{group}", "Other")')


GONE = _row("Pages Read", "'Book Measures'[Pages Read]", 1, "Reading_x")
LIVE = _row("Loans Issued", "'Book Measures'[Loans Issued]", 2, "Circulation_x")


def _model(root: Path, tables: dict) -> Path:
    definition = root / "definition"
    (definition / "tables").mkdir(parents=True)
    refs = "".join(f"ref table '{n}'\n" for n in tables)
    (definition / "model.tmdl").write_text(MODEL + refs, encoding="utf-8")
    for name, text in tables.items():
        (definition / "tables" / f"{name}.tmdl").write_text(text,
                                                            encoding="utf-8")
    return root


def _fleet(tmp_path: Path):
    template = _model(tmp_path / "t" / "Library.SemanticModel",
                      {"Loans": FACT, "Book Measures": MEASURES,
                       "x-Selector 1": _axis("x-Selector 1", [GONE, LIVE])})
    derived = _model(tmp_path / "m" / "Library.SemanticModel",
                     {"Loans": FACT, "Book Measures": MEASURES,
                      "x-Selector 1": _axis("x-Selector 1", [GONE, LIVE])})
    fy = write_fleet(tmp_path, template, {"branch": derived},
                     state_dir=str(tmp_path / "state"),
                     allowlist={"kinds": ["retire.fieldparam_entry"]})
    capture(load_fleet(fy))
    return fy, derived


def _table(derived: Path) -> Path:
    return derived / "definition" / "tables" / "x-Selector 1.tmdl"


def _retire(fy: Path, ref: str) -> None:
    fleet = load_fleet(fy)
    Ledger(fleet.ledger_path).record_retirement(
        "measure", ref, reason="dropped from the template")


def test_the_finding_names_the_table_and_the_category(tmp_path):
    """DC-3: a row pointing at a retired measure is reported."""
    fy, derived = _fleet(tmp_path)
    _retire(fy, "Book Measures[Pages Read]")
    r = drift("detect", "--kind", "retire.fieldparam_entry", fleet=fy)
    assert r.exit_code == 1, r.output
    assert "retire.fieldparam_entry" in r.output
    assert "x-Selector 1" in r.output
    assert "Reading_x" in r.output


def test_the_repair_removes_only_the_retired_row(tmp_path):
    """DC-3: the retired row goes; the live row stays byte-identical."""
    fy, derived = _fleet(tmp_path)
    _retire(fy, "Book Measures[Pages Read]")
    before = _table(derived).read_text(encoding="utf-8")
    assert LIVE in before
    r = drift("remediate", "--kind", "retire.fieldparam_entry", "--sync",
              "--confirm-deletions", fleet=fy)
    assert r.exit_code == 0, r.output
    after = _table(derived).read_text(encoding="utf-8")
    assert "Pages Read" not in after
    assert LIVE in after, "the live row changed"
    assert GONE not in after


def test_a_second_detect_finds_nothing(tmp_path):
    """DC-3: the repair converges."""
    fy, derived = _fleet(tmp_path)
    _retire(fy, "Book Measures[Pages Read]")
    drift("remediate", "--kind", "retire.fieldparam_entry", "--sync",
          "--confirm-deletions", fleet=fy)
    r = drift("detect", "--kind", "retire.fieldparam_entry", fleet=fy)
    assert r.exit_code == 0, r.output


def test_a_live_row_is_never_removed(tmp_path):
    """DC-3: nothing is removed while the measure is live."""
    fy, derived = _fleet(tmp_path)
    before = _table(derived).read_bytes()
    r = drift("remediate", "--kind", "retire.fieldparam_entry", "--sync",
              "--confirm-deletions", fleet=fy)
    assert r.exit_code == 0, r.output
    assert _table(derived).read_bytes() == before

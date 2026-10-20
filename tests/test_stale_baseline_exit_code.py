"""A stale baseline is a configuration problem, not a finding.

The committed baseline is the template's truth. When the live template no
longer matches it, every command that reads the baseline must refuse: the
findings computed against a stale baseline are misclassified (a retired
object looks missing and gets resurrected).

Exit 1 is reserved for actionable findings. A configuration problem must
exit 2 and say so on standard error, and it must not print a findings
summary — a reader must never read "2 finding(s)" as a verdict about a model
whose baseline is out of date.
"""

from __future__ import annotations

import textwrap

TEMPLATE_TABLE = textwrap.dedent("""\
    table Sales
    \tculture: en-US
    \tcolumn Amount
    \t\tdataType: int64
    \t\tlineageTag: 11111111-1111-1111-1111-111111111111
    \tmeasure 'Total Amount' = SUM(Sales[Amount])
    """)

DERIVED_TABLE = textwrap.dedent("""\
    table Sales
    \tculture: en-US
    \tcolumn Amount
    \t\tdataType: int64
    \t\tlineageTag: 22222222-2222-2222-2222-222222222222
    \tmeasure 'Total Amount' = SUM(Sales[Amount])
    """)


def _fleet(tmp_path):
    root = tmp_path / "fleet"
    tdef = root / "tpl" / "definition"
    (tdef / "tables").mkdir(parents=True)
    (tdef / "model.tmdl").write_text("model Model\n\tculture: en-US\n",
                                     encoding="utf-8")
    (tdef / "tables" / "Sales.tmdl").write_text(TEMPLATE_TABLE, encoding="utf-8")

    mdef = root / "north" / "definition"
    (mdef / "tables").mkdir(parents=True)
    (mdef / "model.tmdl").write_text("model Model\n\tculture: en-US\n",
                                     encoding="utf-8")
    (mdef / "tables" / "Sales.tmdl").write_text(DERIVED_TABLE, encoding="utf-8")

    from conftest import write_fleet
    fy = write_fleet(root, tdef.parent, {"north": mdef.parent},
                     state_dir=str(root / "state"),
                     allowlist={"kinds": ["measure.missing"]})
    return root, fy


def _make_stale(root):
    """Edit the template after the capture: the baseline no longer matches."""
    tpl = root / "tpl" / "definition" / "tables" / "Sales.tmdl"
    tpl.write_text(TEMPLATE_TABLE.replace(
        "SUM(Sales[Amount])", "SUM(VALUES(Sales[Amount]))"),
        encoding="utf-8")


def test_detect_exits_2_on_a_stale_baseline(tmp_path):
    """DD-6: detection refuses with the configuration exit code."""
    from conftest import drift
    root, fy = _fleet(tmp_path)
    assert drift("capture", fleet=fy).exit_code == 0
    _make_stale(root)

    r = drift("detect", fleet=fy)
    assert r.exit_code == 2, r.output
    assert "stale" in r.output.lower()


def test_detect_prints_no_findings_summary_on_a_stale_baseline(tmp_path):
    """DD-6: the refusal must not look like a verdict."""
    from conftest import drift
    root, fy = _fleet(tmp_path)
    assert drift("capture", fleet=fy).exit_code == 0
    _make_stale(root)

    r = drift("detect", fleet=fy)
    assert "finding(s)" not in r.output, r.output
    assert "Summary:" not in r.output, r.output


def test_remediate_exits_2_on_a_stale_baseline(tmp_path):
    """DD-6: remediation refuses too, and writes nothing."""
    from conftest import drift
    root, fy = _fleet(tmp_path)
    assert drift("capture", fleet=fy).exit_code == 0
    _make_stale(root)

    r = drift("remediate", "--kind", "measure.missing", "--yes", fleet=fy)
    assert r.exit_code == 2, r.output
    assert "stale" in r.output.lower()


def test_the_ratchet_check_exits_2_on_a_stale_baseline(tmp_path):
    """DD-6: the ratchet check refuses."""
    from conftest import drift
    from click.testing import CliRunner
    from tmdl_drift_doctor.cli import main
    root, fy = _fleet(tmp_path)
    assert drift("capture", fleet=fy).exit_code == 0
    _make_stale(root)

    r = CliRunner().invoke(main, ["ratchet", "check", "--fleet", str(fy)])
    assert r.exit_code == 2, r.output
    assert "stale" in r.output.lower()


def test_the_ratchet_tighten_exits_2_on_a_stale_baseline(tmp_path):
    """DD-6: the ratchet tighten refuses."""
    from conftest import drift
    from click.testing import CliRunner
    from tmdl_drift_doctor.cli import main
    root, fy = _fleet(tmp_path)
    assert drift("capture", fleet=fy).exit_code == 0
    _make_stale(root)

    r = CliRunner().invoke(main, ["ratchet", "tighten", "--fleet", str(fy)])
    assert r.exit_code == 2, r.output
    assert "stale" in r.output.lower()


def test_the_review_exits_2_on_a_stale_baseline(tmp_path):
    """DD-6: the review command refuses."""
    from conftest import drift
    root, fy = _fleet(tmp_path)
    assert drift("capture", fleet=fy).exit_code == 0
    _make_stale(root)

    r = drift("review", fleet=fy)
    assert r.exit_code == 2, r.output
    assert "stale" in r.output.lower()


def test_allow_stale_still_proceeds(tmp_path):
    """The escape hatch keeps working: it detects and exits 1 on findings."""
    from conftest import drift
    root, fy = _fleet(tmp_path)
    # the derived model misses a measure the template carries
    (root / "north" / "definition" / "tables" / "Sales.tmdl").write_text(
        DERIVED_TABLE.replace(
            "\tmeasure 'Total Amount' = SUM(Sales[Amount])\n", ""),
        encoding="utf-8")
    assert drift("capture", fleet=fy).exit_code == 0
    _make_stale(root)

    r = drift("detect", "--allow-stale", fleet=fy)
    assert r.exit_code == 1, r.output
    assert "measure.missing" in r.output

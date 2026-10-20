"""Write guards, the derived-only file invariant and per-run backups."""

import pytest

from conftest import detect_all

from tmdl_drift_doctor import TmdlEditError
from tmdl_drift_doctor import tmdl_edit as te
from tmdl_drift_doctor.detect import Finding
from tmdl_drift_doctor.remediate import remediate

TABLE = ("table Sales\n"
         "\tlineageTag: 0b0b0b0b-0000-0000-0000-000000000001\n\n"
         "\tmeasure Revenue = SUM(Sales[amount])\n"
         "{props}\n\n"
         "\tcolumn amount\n"
         "\t\tdataType: double\n"
         "\t\tsourceColumn: amount\n")


def _table(props: str) -> str:
    return TABLE.format(props=props)


def test_property_after_annotation_is_refused():
    bad = _table("\t\tlineageTag: 1\n"
                 "\t\tannotation PBI_FormatHint = {}\n"
                 "\t\tformatString: 0")
    with pytest.raises(TmdlEditError, match="after the annotation"):
        te.assert_tmdl_valid(bad)
    te.assert_tmdl_valid(_table("\t\tformatString: 0\n"
                                "\t\tannotation PBI_FormatHint = {}"))


def test_duplicate_var_in_one_scope_is_refused():
    bad = ("table T\n\n\tmeasure M =\n\t\t\tVAR a = 1\n\t\t\tVAR a = 2\n"
           "\t\t\tRETURN a\n\t\tformatString: 0\n")
    with pytest.raises(TmdlEditError, match="VAR a twice"):
        te.assert_tmdl_valid(bad)
    nested = ("table T\n\n\tmeasure M =\n\t\t\tVAR a = 1\n"
              "\t\t\tVAR b = CALCULATE(VAR a = 2 RETURN a)\n"
              "\t\t\tRETURN a + b\n")
    te.assert_tmdl_valid(nested)
    assert te.duplicate_var("VAR x = 1 RETURN x") is None
    assert te.duplicate_var("VAR x = 1 VAR X = 2 RETURN x") == "X"


def test_nested_fences_and_duplicate_flag_are_refused():
    with pytest.raises(TmdlEditError, match="fence"):
        te.assert_tmdl_valid("table T\n\n\tmeasure M = ```\n\t\t\t1\n")
    with pytest.raises(TmdlEditError, match="isHidden"):
        te.assert_tmdl_valid(_table("\t\tisHidden\n\t\tisHidden"))


def test_round_trip_detects_a_block_that_reads_back_differently():
    content = _table("\t\tformatString: 0")
    block = te.extract_block(content, "measure", "Revenue")
    te.assert_round_trip(content, "measure", "Revenue", block)
    with pytest.raises(TmdlEditError, match="read back"):
        te.assert_round_trip(content, "measure", "Revenue",
                             block.replace("SUM", "AVERAGE"))
    with pytest.raises(TmdlEditError, match="cannot be found"):
        te.assert_round_trip(content, "measure", "Cost", block)


def test_derived_only_file_is_never_written_by_an_ordinary_fix(captured):
    from tmdl_drift_doctor.remediate import FileChange, OverlayWatch
    own = captured.model_definition("alpha") / "tables" / "Alpha Notes.tmdl"
    own.write_text("table 'Alpha Notes'\n\tlineageTag: 1\n\n"
                   "\tmeasure 'Note Count' = 1\n", encoding="utf-8")
    watch = OverlayWatch(captured)
    change = [FileChange(own, own.read_text(encoding="utf-8"), "x")]
    ordinary = Finding(kind="measure.missing", model="alpha", ref="X[Y]")
    assert "Alpha Notes.tmdl" in watch.refuses(ordinary, change)
    removal = Finding(kind="retire.table", model="alpha", ref="Alpha Notes")
    assert watch.refuses(removal, change) == ""
    # a write nobody declared is caught at the end of the run
    assert watch.changed() == []
    own.write_text("tampered", encoding="utf-8")
    assert [p.endswith("Alpha Notes.tmdl") for p in watch.changed()] == [True]


def test_a_run_reports_overlay_tampering_as_failed(captured, monkeypatch):
    import tmdl_drift_doctor.remediate as rem
    own = captured.model_definition("alpha") / "tables" / "Alpha Notes.tmdl"
    own.write_text("table 'Alpha Notes'\n\tlineageTag: 1\n", encoding="utf-8")
    findings = [f for f in detect_all(captured) if f.model == "alpha"
                and f.kind == "measure.expression_drift"]
    real = rem.APPLIERS["measure.expression_drift"]

    def sneaky(fleet, f):
        own.write_text("changed behind the run's back", encoding="utf-8")
        return real(fleet, f)

    monkeypatch.setitem(rem.APPLIERS, "measure.expression_drift", sneaky)
    res = remediate(captured, findings)
    assert res.errors and "OVERLAY" in res.errors[0]
    assert "Alpha Notes.tmdl" in res.errors[0]


def test_backup_keeps_one_copy_per_file_and_removes_it_after_success(captured):
    visits = captured.model_definition("alpha") / "tables" / "Visits.tmdl"
    seen = []
    orig = visits.read_text(encoding="utf-8")

    findings = [f for f in detect_all(captured) if f.model == "alpha"
                and f.kind in ("measure.expression_drift",
                               "measure.property_drift")
                and f.ref.startswith("Visits[")]
    assert len(findings) == 2

    import tmdl_drift_doctor.remediate as rem
    real_save = rem.Backups.save

    def spy(self, path):
        real_save(self, path)
        seen.append(sorted(p.name for p in visits.parent.glob(
            "Visits.tmdl.bak_*")))

    rem.Backups.save = spy
    try:
        res = remediate(captured, findings, backup=True)
    finally:
        rem.Backups.save = real_save
    assert res.count("applied") == 2
    assert all(len(s) == 1 for s in seen)      # one copy, however many writes
    assert res.backups_removed == 1
    assert not list(visits.parent.glob("*.bak_*"))
    assert visits.read_text(encoding="utf-8") != orig


def test_backup_is_never_made_in_a_dry_run(captured):
    findings = [f for f in detect_all(captured) if f.model == "alpha"]
    remediate(captured, findings, dry_run=True, backup=True)
    assert not list(captured.model_definition("alpha").rglob("*.bak_*"))

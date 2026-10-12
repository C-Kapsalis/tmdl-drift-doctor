"""Measure names are model-global: hosts are compared by name across the
whole model, a wrong host is moved verbatim, a name is never inserted twice."""

from conftest import detect_all, edit_file

from tmdl_drift_doctor import tmdl_edit as te
from tmdl_drift_doctor.remediate import remediate
from tmdl_drift_doctor.tmdl import parse_model

VPM = "Visits per Member"


def test_wrong_host_replaces_missing_and_extra(captured):
    alpha = {(f.kind, f.ref) for f in detect_all(captured, model_filter="alpha")}
    assert ("measure.wrong_host", f"Visits[{VPM}]") in alpha
    assert ("measure.missing", f"Visits[{VPM}]") not in alpha
    assert ("extra.measure", f"Members[{VPM}]") not in alpha


def test_duplicate_host_is_advisory(captured):
    f = next(f for f in detect_all(captured, model_filter="bravo")
             if f.kind == "measure.duplicate_host")
    assert f.ref == f"Members[{VPM}]" and f.advisory
    result = remediate(captured, [f])
    assert result.outcomes[0].status == "skipped"
    bravo = parse_model(captured.model_definition("bravo"))
    assert bravo.measure("Members", VPM) is not None


def test_move_is_verbatim_and_writes_destination_first(captured):
    d = captured.model_definition("alpha") / "tables"
    block = te.extract_block((d / "Members.tmdl").read_text(encoding="utf-8"),
                             "measure", VPM)
    findings = [f for f in detect_all(captured) if f.kind == "measure.wrong_host"]
    dry = remediate(captured, findings, dry_run=True).outcomes[0]
    assert [c.path.name for c in dry.changes] == ["Visits.tmdl", "Members.tmdl"]

    result = remediate(captured, findings)
    assert result.count("applied") == 1
    alpha = parse_model(captured.model_definition("alpha"))
    assert alpha.measure("Members", VPM) is None
    moved = te.extract_block((d / "Visits.tmdl").read_text(encoding="utf-8"),
                             "measure", VPM)
    assert moved == block            # DAX, properties and lineage tag intact
    assert not [f for f in detect_all(captured, model_filter="alpha")
                if f.ref.endswith(f"[{VPM}]")]


def test_move_waits_when_old_host_is_referenced_qualified(captured):
    mf = captured.model_definition("alpha") / "tables" / "Members.tmdl"
    edit_file(mf, "\tpartition Members = m",
              f"\tmeasure 'Alpha VPM x2' = Members[{VPM}] * 2\n"
              "\t\tlineageTag: aaaa1111-1111-4111-8111-000000000098\n\n"
              "\tpartition Members = m")
    findings = [f for f in detect_all(captured) if f.kind == "measure.wrong_host"]
    o = remediate(captured, findings).outcomes[0]
    assert o.status == "auto_skipped_precondition"
    assert "dangling" in o.reason


def test_missing_never_inserts_a_name_present_elsewhere(captured):
    """A template measure absent from its host but present on two other
    tables is reported per host (advisory), never inserted a third time."""
    vf = captured.model_definition("alpha") / "tables" / "Classes.tmdl"
    edit_file(vf, "\tpartition Classes = m",
              f"\tmeasure '{VPM}' = 1\n"
              "\t\tlineageTag: aaaa3333-3333-4333-8333-000000000097\n\n"
              "\tpartition Classes = m")
    alpha = [f for f in detect_all(captured, model_filter="alpha")
             if f.ref.endswith(f"[{VPM}]")]
    assert {(f.kind, f.ref) for f in alpha} == {
        ("measure.duplicate_host", f"Members[{VPM}]"),
        ("measure.duplicate_host", f"Classes[{VPM}]")}


def test_table_copy_waits_when_it_would_duplicate_a_measure(captured):
    # bravo lacks Classes; give it the template's Classes measure elsewhere
    mf = captured.model_definition("bravo") / "tables" / "Members.tmdl"
    edit_file(mf, "\tpartition Members = m",
              "\tmeasure 'Class Fill Rate %' = 0\n"
              "\t\tlineageTag: bbbb1111-1111-4111-8111-000000000098\n\n"
              "\tpartition Members = m")
    findings = detect_all(captured, model_filter="bravo")
    assert ("measure.wrong_host", "Classes[Class Fill Rate %]") in {
        (f.kind, f.ref) for f in findings}
    result = remediate(captured, findings)
    o = next(o for o in result.outcomes if o.finding.kind == "table.missing")
    assert o.status == "auto_skipped_precondition"
    assert "Members[Class Fill Rate %]" in o.reason

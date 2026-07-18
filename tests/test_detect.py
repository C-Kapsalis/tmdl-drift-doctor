"""Every seeded drift kind is detected — and only genuine drift is."""

from tmdl_drift_doctor.baseline import load_baseline
from tmdl_drift_doctor.detect import detect_fleet
from tmdl_drift_doctor.ledger import Ledger


def _findings(fleet, **kw):
    return detect_fleet(fleet, load_baseline(fleet), Ledger(fleet.ledger_path), **kw)


def _kinds(findings, model):
    return {(f.kind, f.ref) for f in findings if f.model == model}


def test_all_seeded_kinds_detected(captured):
    findings = _findings(captured)

    assert _kinds(findings, "alpha") == {
        ("measure.missing", "Members[New Members #]"),
        ("measure.expression_drift", "Visits[Total Visits]"),
        ("measure.property_drift", "Visits[Avg Visit Duration]"),
        ("expression.drift", "Reporting Start Date"),
        ("extra.measure", "Members[Alpha Loyalty Score]"),
    }
    assert _kinds(findings, "bravo") == {
        ("table.missing", "Classes"),
        ("column.missing", "Members[MembershipTier]"),
        ("column.property_drift", "Members[JoinDate]"),
        ("mapping_row.missing", "Plan Map/elite"),
    }


def test_non_allowlisted_expression_never_compared(captured):
    # 'Data Source' differs in BOTH franchises but is not in the allowlist —
    # cascading it would repoint every tenant's warehouse.
    findings = _findings(captured)
    assert not any(f.ref == "Data Source" for f in findings)


def test_extras_are_advisory(captured):
    findings = _findings(captured)
    extra = next(f for f in findings if f.kind == "extra.measure")
    assert extra.advisory
    # a derived-only measure on a shared table is reported, never a delete
    assert "never auto-deleted" in extra.detail


def test_expression_drift_carries_both_values(captured):
    f = next(x for x in _findings(captured)
             if x.kind == "measure.expression_drift")
    assert "COUNTROWS(Visits)" in f.template_value
    assert "FILTER" in f.derived_value


def test_model_and_kind_filters(captured):
    only_alpha = _findings(captured, model_filter="alpha")
    assert {f.model for f in only_alpha} == {"alpha"}
    only_measures = _findings(captured, kind_filter="measure.")
    assert all(f.kind.startswith("measure.") for f in only_measures)

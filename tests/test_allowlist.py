"""Nothing cascades by default — the allowlist is the only gate that opens."""

from tmdl_drift_doctor.baseline import load_baseline
from tmdl_drift_doctor.detect import detect_fleet
from tmdl_drift_doctor.ledger import Ledger
from tmdl_drift_doctor.remediate import remediate
from tmdl_drift_doctor.tmdl import normalize_expression, parse_model


def _run(fleet, **kw):
    findings = detect_fleet(fleet, load_baseline(fleet), Ledger(fleet.ledger_path))
    return remediate(fleet, findings, **kw)


def test_kind_not_in_allowlist_is_never_applied(captured):
    captured.allowlist_kinds.discard("measure.expression_drift")
    result = _run(captured)
    outcome = next(o for o in result.outcomes
                   if o.finding.kind == "measure.expression_drift")
    assert outcome.status == "skipped"
    assert "allowlist" in outcome.reason
    # the drifted DAX is still the franchise's version
    alpha = parse_model(captured.model_definition("alpha"))
    assert "FILTER" in normalize_expression(
        alpha.measure("Visits", "Total Visits").expression)


def test_empty_allowlist_applies_nothing(captured):
    captured.allowlist_kinds.clear()
    result = _run(captured, sync=True)
    assert result.count("applied") == 0


def test_expression_allowlist_gates_detection_itself(captured):
    # allowlisting 'Data Source' would compare (and flag) it; the fixture
    # deliberately leaves it out, so it never even becomes a finding
    findings = detect_fleet(captured, load_baseline(captured),
                            Ledger(captured.ledger_path))
    assert not any(f.kind == "expression.drift" and f.ref == "Data Source"
                   for f in findings)
    captured.allowlist_expressions.add("Data Source")
    findings = detect_fleet(captured, load_baseline(captured),
                            Ledger(captured.ledger_path))
    hits = [f for f in findings
            if f.kind == "expression.drift" and f.ref == "Data Source"]
    assert len(hits) == 2  # both franchises diverge — deliberately

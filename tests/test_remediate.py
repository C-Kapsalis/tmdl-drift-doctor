"""Remediation: each kind applied, idempotent, and dry-run touches nothing."""

import json

from conftest import tree_state

from tmdl_drift_doctor.baseline import load_baseline
from tmdl_drift_doctor.detect import detect_fleet
from tmdl_drift_doctor.ledger import Ledger
from tmdl_drift_doctor.remediate import remediate
from tmdl_drift_doctor.tmdl import normalize_expression, parse_model


def _run(fleet, **kw):
    findings = detect_fleet(fleet, load_baseline(fleet), Ledger(fleet.ledger_path))
    return remediate(fleet, findings, **kw)


def test_full_pass_fixes_every_remediable_finding(captured):
    result = _run(captured)
    applied = {(o.finding.model, o.finding.kind, o.finding.ref)
               for o in result.outcomes if o.status == "applied"}
    assert ("alpha", "measure.missing", "Members[New Members #]") in applied
    assert ("alpha", "measure.expression_drift", "Visits[Total Visits]") in applied
    assert ("alpha", "measure.property_drift", "Visits[Avg Visit Duration]") in applied
    assert ("alpha", "expression.drift", "Reporting Start Date") in applied
    assert ("bravo", "table.missing", "Classes") in applied
    assert ("bravo", "column.missing", "Members[MembershipTier]") in applied
    assert ("bravo", "column.property_drift", "Members[JoinDate]") in applied
    assert ("bravo", "mapping_row.missing", "Plan Map/elite") in applied
    assert result.count("failed") == 0

    # verify content landed
    alpha = parse_model(captured.model_definition("alpha"))
    assert alpha.measure("Members", "New Members #") is not None
    assert normalize_expression(
        alpha.measure("Visits", "Total Visits").expression) == "COUNTROWS(Visits)"
    assert alpha.measure("Visits", "Avg Visit Duration").properties["formatString"] == "0.0"
    assert "#date(2024, 1, 1)" in alpha.expressions["Reporting Start Date"]

    bravo = parse_model(captured.model_definition("bravo"))
    assert "Classes" in bravo.tables
    assert "Classes" in bravo.ref_tables
    assert bravo.column("Members", "MembershipTier") is not None
    assert bravo.column("Members", "JoinDate").properties["formatString"] == "yyyy-mm-dd"

    # the franchise's own work is untouched
    assert alpha.measure("Members", "Alpha Loyalty Score") is not None
    assert "alpha-warehouse" in alpha.expressions["Data Source"]


def test_remediation_is_idempotent(captured):
    _run(captured)
    findings = detect_fleet(captured, load_baseline(captured),
                            Ledger(captured.ledger_path))
    # only the advisory extra remains — nothing remediable is left
    assert {f.kind for f in findings} == {"extra.measure"}
    second = _run(captured)
    assert second.count("applied") == 0


def test_overwrite_preserves_derived_lineage_tag(captured):
    before = parse_model(captured.model_definition("alpha"))
    tag = before.measure("Visits", "Total Visits").lineage_tag
    _run(captured, kind="measure.expression_drift")
    after = parse_model(captured.model_definition("alpha"))
    assert after.measure("Visits", "Total Visits").lineage_tag == tag


def test_insert_regenerates_lineage_tag(captured):
    template_tag = parse_model(captured.template_definition).measure(
        "Members", "New Members #").lineage_tag
    _run(captured, kind="measure.missing")
    got = parse_model(captured.model_definition("alpha")).measure(
        "Members", "New Members #").lineage_tag
    assert got and got != template_tag


def test_dry_run_touches_nothing(captured, fleet_dir):
    before = tree_state(fleet_dir)
    result = _run(captured, dry_run=True, sync=True)
    assert result.count("would-apply") > 0
    assert result.count("applied") == 0
    assert tree_state(fleet_dir) == before          # models untouched
    assert not captured.ledger_path.exists()        # ledger untouched too
    # and the dry run renders diffs
    diffs = [c.diff() for o in result.outcomes for c in o.changes]
    assert any("+" in d for d in diffs)


def test_kind_filter_limits_the_run(captured):
    result = _run(captured, kind="column.")
    applied_kinds = {o.finding.kind for o in result.outcomes
                     if o.status == "applied"}
    assert applied_kinds == {"column.missing", "column.property_drift"}


def test_ledger_records_every_apply(captured):
    result = _run(captured)
    events = Ledger(captured.ledger_path).entries()
    remediated = [e for e in events if e["event"] == "remediated"]
    assert len(remediated) == result.count("applied")
    sample = remediated[0]
    assert {"ts", "kind", "model", "ref", "action", "files"} <= set(sample)


def test_advisory_extras_never_applied(captured):
    result = _run(captured, sync=True)
    extra = next(o for o in result.outcomes
                 if o.finding.kind == "extra.measure")
    assert extra.status == "skipped"
    assert "advisory" in extra.reason
    alpha = parse_model(captured.model_definition("alpha"))
    assert alpha.measure("Members", "Alpha Loyalty Score") is not None

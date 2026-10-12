"""Apply-time preconditions and --converge."""

from conftest import detect_all, edit_file, edit_fleet_yml

from tmdl_drift_doctor.baseline import capture
from tmdl_drift_doctor.fleet import load_fleet
from tmdl_drift_doctor.remediate import remediate, remediate_converge
from tmdl_drift_doctor.tmdl import parse_model


def _add_tier_measure_to_template(fleet):
    mf = fleet.template_definition / "tables" / "Members.tmdl"
    edit_file(mf, "\tpartition Members = m",
              "\tmeasure 'Tier Count' = DISTINCTCOUNT(Members[MembershipTier])\n"
              "\t\tlineageTag: 11111111-1111-4111-8111-000000000020\n\n"
              "\tpartition Members = m")
    capture(fleet)


def test_inserted_measure_waits_for_the_columns_it_reads(fleet_dir):
    edit_fleet_yml(fleet_dir, lambda d: d["allowlist"]["kinds"].remove(
        "column.missing"))
    fleet = load_fleet(fleet_dir / "fleet.yml")
    _add_tier_measure_to_template(fleet)
    result = remediate(fleet, detect_all(fleet, model_filter="bravo"))
    o = next(o for o in result.outcomes if o.finding.ref == "Members[Tier Count]")
    assert o.status == "auto_skipped_precondition"
    assert "Members[MembershipTier]" in o.reason
    assert parse_model(fleet.model_definition("bravo")).measure(
        "Members", "Tier Count") is None


def test_column_added_in_the_same_run_satisfies_it(captured):
    _add_tier_measure_to_template(captured)
    for dry in (True, False):
        result = remediate(captured, detect_all(captured, model_filter="bravo"),
                           dry_run=dry)
        o = next(o for o in result.outcomes
                 if o.finding.ref == "Members[Tier Count]")
        assert o.status == ("would-apply" if dry else "applied")


def test_converge_reaches_a_fixed_point(captured):
    # alpha's wrong-host copy ALSO has a different body: the move happens in
    # pass 1, the expression drift only becomes visible (and fixable) after
    mf = captured.model_definition("alpha") / "tables" / "Members.tmdl"
    edit_file(mf, "DIVIDE([Total Visits], [Active Members])",
              "DIVIDE([Total Visits], [Active Members]) * 1")
    one = remediate(captured, detect_all(captured))
    assert one.count("failed") == 0
    left = {f.kind for f in detect_all(captured, model_filter="alpha")}
    assert "measure.expression_drift" in left

    results = remediate_converge(captured, lambda: detect_all(captured))
    assert results[-1].count("applied") == 0
    assert 2 <= len(results) <= 4
    left = {f.kind for f in detect_all(captured, model_filter="alpha")}
    assert left == {"extra.measure"}


def test_converge_is_bounded(captured):
    calls = []

    def detect():
        calls.append(1)
        return detect_all(captured)
    remediate_converge(captured, detect, max_passes=1)
    assert len(calls) == 1

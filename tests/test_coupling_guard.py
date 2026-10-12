"""Retirements that derived work still depends on are refused (blocked),
and template-owned dependents gate the removal at apply time."""

from conftest import (detect_all, edit_file, edit_fleet_yml,
                      retire_everything_from_template)

from tmdl_drift_doctor import tmdl_edit as te
from tmdl_drift_doctor.baseline import capture
from tmdl_drift_doctor.fleet import load_fleet
from tmdl_drift_doctor.remediate import remediate
from tmdl_drift_doctor.tmdl import parse_model


def _retired(fleet):
    retire_everything_from_template(fleet)
    capture(fleet)
    return detect_all(fleet)


def _finding(findings, model, ref):
    return next(f for f in findings if f.model == model and f.ref == ref
                and f.kind.startswith("retire."))


def _drop_seats_left(fleet):
    cf = fleet.model_definition("alpha") / "tables" / "Classes.tmdl"
    cf.write_text(te.remove_block(cf.read_text(encoding="utf-8"), "measure",
                                  "Alpha Seats Left"), encoding="utf-8")


def test_derived_only_measure_blocks_column_retirement(captured):
    f = _finding(_retired(captured), "alpha", "Classes[Capacity]")
    assert f.blocked and "Classes[Alpha Seats Left]" in f.blocked[0]
    result = remediate(captured, [f], sync=True)
    assert result.outcomes[0].status == "blocked"
    assert "derived work depends on it" in result.outcomes[0].reason


def test_derived_only_relationship_blocks_table_retirement(captured):
    f = _finding(_retired(captured), "bravo", "Promotions")
    assert any("Members[JoinDate] -> Promotions[StartDate]" in b
               for b in f.blocked)


def test_ledger_retired_relationship_is_swept_not_blocking(captured):
    f = _finding(_retired(captured), "alpha", "Promotions")
    assert not f.blocked
    assert f.data["sweep_relationships"] == [
        "Visits[PromoId] -> Promotions[PromoId]"]
    # and no separate retire.relationship finding competes with the sweep
    assert not any(x.kind == "retire.relationship" for x in detect_all(captured))


def test_template_owned_dependent_gates_at_apply_time(fleet_dir):
    """alpha's template-owned 'Class Fill Rate %' still reads the column
    (the template rewrote it); without its overwrite the removal waits."""
    edit_fleet_yml(fleet_dir, lambda d: d["allowlist"]["kinds"].remove(
        "measure.expression_drift"))
    fleet = load_fleet(fleet_dir / "fleet.yml")
    capture(fleet)
    _drop_seats_left(fleet)
    f = _finding(_retired(fleet), "alpha", "Classes[Capacity]")
    assert not f.blocked
    result = remediate(fleet, detect_all(fleet), kind="retire.column", sync=True)
    o = next(o for o in result.outcomes if o.finding.ref == "Classes[Capacity]")
    assert o.status == "auto_skipped_precondition"
    assert "Classes[Class Fill Rate %]" in o.reason


def test_template_owned_dependent_fixed_in_the_same_run(captured):
    _drop_seats_left(captured)
    findings = _retired(captured)
    for dry in (True, False):
        result = remediate(captured, findings, sync=True, dry_run=dry)
        o = next(o for o in result.outcomes
                 if o.finding.ref == "Classes[Capacity]")
        assert o.status == ("would-apply" if dry else "applied"), o.reason
    alpha = parse_model(captured.model_definition("alpha"))
    assert alpha.column("Classes", "Capacity") is None


def test_sort_by_column_blocks(captured):
    cf = captured.model_definition("alpha") / "tables" / "Classes.tmdl"
    edit_file(cf, "\t\tsourceColumn: ClassName\n",
              "\t\tsourceColumn: ClassName\n\t\tsortByColumn: Capacity\n")
    _drop_seats_left(captured)
    f = _finding(_retired(captured), "alpha", "Classes[Capacity]")
    assert f.blocked == ["column(s) sort by it: Classes[ClassName]"]


def test_hierarchy_level_blocks(captured):
    cf = captured.model_definition("alpha") / "tables" / "Classes.tmdl"
    edit_file(cf, "\tpartition Classes = m",
              "\thierarchy 'Class Size'\n"
              "\t\tlineageTag: aaaa3333-3333-4333-8333-000000000099\n\n"
              "\t\tlevel Capacity\n"
              "\t\t\tlineageTag: aaaa3333-3333-4333-8333-000000000098\n"
              "\t\t\tcolumn: Capacity\n\n"
              "\tpartition Classes = m")
    _drop_seats_left(captured)
    f = _finding(_retired(captured), "alpha", "Classes[Capacity]")
    assert f.blocked == ["hierarchy level(s) use it: Classes/Class Size"]


def test_derived_only_object_on_a_retired_table_blocks(captured):
    pf = captured.model_definition("alpha") / "tables" / "Promotions.tmdl"
    edit_file(pf, "\tpartition Promotions = m",
              "\tmeasure 'Alpha Promo Count' = COUNTROWS(Promotions)\n"
              "\t\tlineageTag: aaaa4444-4444-4444-8444-000000000099\n\n"
              "\tpartition Promotions = m")
    f = _finding(_retired(captured), "alpha", "Promotions")
    assert any("Promotions[Alpha Promo Count]" in b for b in f.blocked)


def test_measure_retirement_blocked_by_a_derived_measure(captured):
    mf = captured.model_definition("alpha") / "tables" / "Members.tmdl"
    edit_file(mf, "\tpartition Members = m",
              "\tmeasure 'Alpha Peak Share' = DIVIDE([Peak Hour Visits], "
              "[Total Visits])\n"
              "\t\tlineageTag: aaaa1111-1111-4111-8111-000000000099\n\n"
              "\tpartition Members = m")
    f = _finding(_retired(captured), "alpha", "Visits[Peak Hour Visits]")
    assert any("Members[Alpha Peak Share]" in b for b in f.blocked)
    bravo = _finding(detect_all(captured), "bravo", "Visits[Peak Hour Visits]")
    assert not bravo.blocked

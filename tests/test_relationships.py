"""Relationships: parsed, compared on field signature, cascaded, retired."""

from conftest import detect_all, edit_fleet_yml

from tmdl_drift_doctor import tmdl_edit as te
from tmdl_drift_doctor.baseline import capture
from tmdl_drift_doctor.fleet import load_fleet
from tmdl_drift_doctor.ledger import Ledger
from tmdl_drift_doctor.remediate import remediate
from tmdl_drift_doctor.tmdl import parse_model, parse_relationships_text

R1 = "Visits[MemberId] -> Members[MemberId]"
R2 = "Members[MembershipTier] -> Plan Map[PlanKey]"
R3 = "Visits[PromoId] -> Promotions[PromoId]"
EXTRA = "Members[JoinDate] -> Promotions[StartDate]"


def test_parser_reads_signatures_and_normalizes_defaults():
    rels = parse_relationships_text(
        "relationship abc\n"
        "\tisActive: false\n"
        "\tfromColumn: 'Plan Map'.'Plan Key'\n"
        "\ttoColumn: Members.MembershipTier\n"
        "\n"
        "relationship def\n"
        "\tcrossFilteringBehavior: oneDirection\n"
        "\tfromColumn: Visits.MemberId\n"
        "\ttoColumn: Members.MemberId\n")
    assert set(rels) == {"Plan Map[Plan Key] -> Members[MembershipTier]", R1}
    assert rels[R1].name == "def"
    assert rels["Plan Map[Plan Key] -> Members[MembershipTier]"].properties[
        "isActive"] == "false"
    # an explicit default equals an omitted one
    assert rels[R1].properties["crossFilteringBehavior"] == "oneDirection"
    assert rels[R1].properties["toCardinality"] == "one"


def test_identity_ignores_block_names(captured):
    # alpha's blocks carry different GUIDs than the template's: no finding
    kinds = {f.kind for f in detect_all(captured, model_filter="alpha")}
    assert not any(k.startswith("relationship.") for k in kinds)


def test_relationship_kinds_detected(captured):
    got = {(f.kind, f.ref) for f in detect_all(captured, model_filter="bravo")
           if f.kind.startswith("relationship.")}
    assert got == {("relationship.missing", R2),
                   ("relationship.modified", R1),
                   ("relationship.extra", EXTRA)}


def test_baseline_snapshot_includes_relationships(captured):
    import json
    baseline = json.loads(captured.baseline_path.read_text(encoding="utf-8"))
    assert set(baseline["relationships"]) == {R1, R2, R3}
    assert baseline["relationships"][R3]["isActive"] == "false"


def test_modified_overwrites_props_and_keeps_derived_name(captured):
    result = remediate(captured, detect_all(captured), kind="relationship.")
    assert result.count("failed") == 0
    bravo = parse_model(captured.model_definition("bravo"))
    rel = bravo.relationships[R1]
    assert rel.name == "bbbb7777-0000-4000-8000-000000000001"
    assert rel.properties["crossFilteringBehavior"] == "oneDirection"


def test_missing_is_appended_after_its_endpoint_column_in_the_same_run(captured):
    result = remediate(captured, detect_all(captured))
    applied = [(o.finding.kind, o.finding.ref) for o in result.outcomes
               if o.status == "applied" and o.finding.model == "bravo"]
    assert applied.index(("column.missing", "Members[MembershipTier]")) < \
        applied.index(("relationship.missing", R2))
    bravo = parse_model(captured.model_definition("bravo"))
    assert R2 in bravo.relationships
    # the derived-only extra is advisory: never touched
    assert EXTRA in bravo.relationships


def test_missing_waits_for_its_endpoints(fleet_dir):
    edit_fleet_yml(fleet_dir, lambda d: d["allowlist"]["kinds"].remove(
        "column.missing"))
    fleet = load_fleet(fleet_dir / "fleet.yml")
    capture(fleet)
    result = remediate(fleet, detect_all(fleet), kind="relationship.missing")
    o = next(o for o in result.outcomes if o.finding.ref == R2)
    assert o.status == "auto_skipped_precondition"
    assert "Members[MembershipTier]" in o.reason
    assert R2 not in parse_model(fleet.model_definition("bravo")).relationships


def test_dry_run_counts_endpoints_added_earlier_in_the_run(captured):
    result = remediate(captured, detect_all(captured), dry_run=True)
    o = next(o for o in result.outcomes if o.finding.ref == R2)
    assert o.status == "would-apply"


def test_missing_creates_the_file_when_absent(captured):
    (captured.model_definition("bravo") / "relationships.tmdl").unlink()
    remediate(captured, detect_all(captured), kind="relationship.missing")
    bravo = parse_model(captured.model_definition("bravo"))
    assert {R1, R3} <= set(bravo.relationships)
    assert R2 not in bravo.relationships   # its endpoint column is missing


def test_retired_relationship_is_recorded_and_removed_with_sync(captured):
    rf = captured.template_definition / "relationships.tmdl"
    rf.write_text(te.remove_relationship_block(rf.read_text(encoding="utf-8"), R1),
                  encoding="utf-8")
    summary = capture(captured)
    assert ("relationship", R1) in {(e["kind"], e["ref"])
                                    for e in summary["retired"]}
    found = {(f.model, f.kind) for f in detect_all(captured) if f.ref == R1}
    assert found == {("alpha", "retire.relationship"),
                     ("bravo", "retire.relationship")}
    remediate(captured, detect_all(captured), kind="retire.relationship",
              sync=True)
    for model in ("alpha", "bravo"):
        rels = parse_model(captured.model_definition(model)).relationships
        assert R1 not in rels and R3 in rels


def test_relationship_revival_cancels_the_retirement(captured):
    rf = captured.template_definition / "relationships.tmdl"
    rf.write_text(te.remove_relationship_block(rf.read_text(encoding="utf-8"), R1),
                  encoding="utf-8")
    capture(captured)
    Ledger(captured.ledger_path).record_revival("relationship", R1,
                                                model="alpha")
    found = {f.model for f in detect_all(captured)
             if f.kind == "retire.relationship"}
    assert found == {"bravo"}

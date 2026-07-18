"""The retirement channel: capture records template drops, --sync cascades
them out of derived models, and revival protects deliberate re-adds."""

from conftest import retire_everything_from_template

from tmdl_drift_doctor import tmdl_edit as te
from tmdl_drift_doctor.baseline import capture, load_baseline
from tmdl_drift_doctor.detect import detect_fleet
from tmdl_drift_doctor.ledger import Ledger
from tmdl_drift_doctor.remediate import remediate
from tmdl_drift_doctor.tmdl import parse_mapping_rows, parse_model


def _detect(fleet):
    return detect_fleet(fleet, load_baseline(fleet), Ledger(fleet.ledger_path))


def test_recapture_records_all_retirement_kinds(captured):
    retire_everything_from_template(captured)
    summary = capture(captured)
    retired = {(e["kind"], e["ref"]) for e in summary["retired"]}
    assert retired == {
        ("measure", "Visits[Peak Hour Visits]"),
        ("column", "Classes[Capacity]"),
        ("table", "Promotions"),
        ("mapping_row", "Plan Map/legacy-gold"),
    }
    # objects of a dropped table are covered by the table entry — never
    # double-recorded
    assert ("measure", "Promotions[Active Promotions]") not in retired
    assert ("column", "Promotions[PromoId]") not in retired


def test_detect_flags_derived_models_still_carrying_retired_objects(captured):
    retire_everything_from_template(captured)
    capture(captured)
    findings = _detect(captured)
    per_model = {(f.model, f.kind, f.ref) for f in findings
                 if f.kind.startswith("retire.")}
    assert ("alpha", "retire.measure", "Visits[Peak Hour Visits]") in per_model
    assert ("bravo", "retire.measure", "Visits[Peak Hour Visits]") in per_model
    assert ("alpha", "retire.column", "Classes[Capacity]") in per_model
    assert ("alpha", "retire.table", "Promotions") in per_model
    assert ("bravo", "retire.table", "Promotions") in per_model
    assert ("alpha", "retire.mapping_row", "Plan Map/legacy-gold") in per_model
    assert ("bravo", "retire.mapping_row", "Plan Map/legacy-gold") in per_model
    # bravo has no Classes table, so no retire.column there
    assert ("bravo", "retire.column", "Classes[Capacity]") not in per_model


def test_sync_cascades_retirements_and_is_idempotent(captured):
    retire_everything_from_template(captured)
    capture(captured)
    result = remediate(captured, _detect(captured), sync=True)
    assert result.count("failed") == 0

    alpha = parse_model(captured.model_definition("alpha"))
    assert alpha.measure("Visits", "Peak Hour Visits") is None
    assert alpha.column("Classes", "Capacity") is None
    assert "Promotions" not in alpha.tables
    assert "Promotions" not in alpha.ref_tables
    rows = parse_mapping_rows(alpha.tables["Plan Map"].partitions["Plan Map"].source)
    assert "legacy-gold" not in rows
    assert set(rows) == {"basic", "plus", "elite"}   # siblings kept

    # idempotent: nothing retire.* left to do
    remaining = [f for f in _detect(captured) if f.kind.startswith("retire.")]
    assert remaining == []


def test_retirements_require_sync(captured):
    retire_everything_from_template(captured)
    capture(captured)
    result = remediate(captured, _detect(captured))   # no sync
    retire_outcomes = [o for o in result.outcomes
                       if o.finding.kind.startswith("retire.")]
    assert retire_outcomes and all(o.status == "skipped" for o in retire_outcomes)
    assert all("--sync" in o.reason for o in retire_outcomes)
    # the objects are still there
    alpha = parse_model(captured.model_definition("alpha"))
    assert alpha.measure("Visits", "Peak Hour Visits") is not None


def test_manual_revival_protects_a_deliberate_readd(captured):
    saved = retire_everything_from_template(captured)
    capture(captured)
    remediate(captured, _detect(captured), sync=True)

    # a user deliberately re-adds the retired measure to alpha
    vf = captured.model_definition("alpha") / "tables" / "Visits.tmdl"
    vf.write_text(te.insert_measure_block(vf.read_text(encoding="utf-8"),
                                          saved["measure_block"]),
                  encoding="utf-8")
    flagged = [f for f in _detect(captured) if f.kind == "retire.measure"]
    assert [(f.model, f.ref) for f in flagged] == [("alpha", "Visits[Peak Hour Visits]")]

    # ...and marks it revived for alpha only
    Ledger(captured.ledger_path).record_revival(
        "measure", "Visits[Peak Hour Visits]", model="alpha",
        note="alpha keeps its evening-peak KPI")
    assert [f for f in _detect(captured) if f.kind == "retire.measure"] == []
    # a --sync pass will not re-remove it
    remediate(captured, _detect(captured), sync=True)
    alpha = parse_model(captured.model_definition("alpha"))
    assert alpha.measure("Visits", "Peak Hour Visits") is not None


def test_capture_auto_revives_objects_returning_to_template(captured):
    saved = retire_everything_from_template(captured)
    capture(captured)
    ledger = Ledger(captured.ledger_path)
    assert ledger.is_retired("measure", "Visits[Peak Hour Visits]")

    # the maintainer brings the measure back to the template
    vf = captured.template_definition / "tables" / "Visits.tmdl"
    vf.write_text(te.insert_measure_block(vf.read_text(encoding="utf-8"),
                                          saved["measure_block"]),
                  encoding="utf-8")
    summary = capture(captured)
    revived = {(e["kind"], e["ref"]) for e in summary["revived"]}
    assert ("measure", "Visits[Peak Hour Visits]") in revived
    assert not ledger.is_retired("measure", "Visits[Peak Hour Visits]")
    # history is preserved, not erased
    events = [e["event"] for e in ledger.entries()
              if e.get("ref") == "Visits[Peak Hour Visits]"]
    assert events == ["retired", "revived"]

"""Baseline capture + the recapture discipline (stale-baseline guard)."""

import json

import pytest

from tmdl_drift_doctor import StaleBaselineError
from tmdl_drift_doctor.baseline import (
    capture,
    ensure_baseline_fresh,
    load_baseline,
)
from tmdl_drift_doctor.ledger import Ledger


def test_capture_writes_baseline(fleet):
    summary = capture(fleet)
    assert fleet.baseline_path.exists()
    baseline = json.loads(fleet.baseline_path.read_text(encoding="utf-8"))
    assert set(baseline["tables"]) == {"Members", "Visits", "Classes",
                                       "Promotions", "Plan Map"}
    assert summary["tables"] == 5
    assert summary["measures"] == 8
    assert set(baseline["mapping_rows"]["Plan Map"]) == {
        "basic", "plus", "elite", "legacy-gold"}
    assert "Reporting Start Date" in baseline["expressions"]


def test_recapture_without_template_change_records_nothing(captured):
    summary = capture(captured)
    assert summary["retired"] == []
    assert summary["revived"] == []
    assert Ledger(captured.ledger_path).entries() == []


def test_missing_baseline_raises(fleet):
    with pytest.raises(StaleBaselineError, match="run `drift-doctor capture`"):
        load_baseline(fleet)


def test_stale_baseline_guard(captured):
    baseline = load_baseline(captured)
    ensure_baseline_fresh(captured, baseline)  # fresh — no error

    # a template edit without recapture must trip the guard
    vf = captured.template_definition / "tables" / "Visits.tmdl"
    vf.write_text(vf.read_text(encoding="utf-8").replace(
        "COUNTROWS(Visits)", "COUNTROWS(VALUES(Visits))", 1), encoding="utf-8")
    with pytest.raises(StaleBaselineError, match="stale"):
        ensure_baseline_fresh(captured, baseline)

    # recapture restores the invariant
    capture(captured)
    ensure_baseline_fresh(captured, load_baseline(captured))

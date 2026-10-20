"""Retirement-channel units: the source-view ledger kind and the typed
override sections (retention contract).

The full removal behaviour is exercised end-to-end by the sync tests
(test_retirement_source_view.py, test_zmap_row_retirement.py; originally
validated dry-run on spinout-15/spinout-10); these tests pin the two newest
contracts: (1) a template view re-point is RECORDED by recapture exactly once,
(2) typed override sections resolve into the retention sets every retire.*
handler consults, and reasons are mandatory.

Ported from the user's suite (D-ledger-source-view-change,
D-overrides-typed-sections): the kernel ledger helper becomes drift-doctor
``capture`` + its JSONL ledger; the YAML overrides document becomes typed
``overrides:`` sections under the model entry in fleet.yml.
"""

import pytest
from conftest import edit_file
from port_helpers_b7_retire import (capture, copy_pair, ledger_entries,
                                    make_fleet)

from tmdl_drift_doctor import DriftDoctorError
from tmdl_drift_doctor.fleet import load_fleet


def _sv_events(fleet_yml):
    return [e for e in ledger_entries(fleet_yml)
            if e.get("kind") == "source_view_change"]


def test_source_view_change_recorded_once(tmp_path):
    uni, gro = copy_pair(tmp_path)
    fy = make_fleet(tmp_path, uni, {"fixture_growth": gro})
    capture(fy)                                   # previous baseline
    log = uni / "definition" / "tables" / "Import Log.tmdl"
    edit_file(log, '"report_import_log_data"', '"report_import_log"')

    capture(fy)                                   # recapture: one re-point
    events = _sv_events(fy)
    assert len(events) == 1
    e = events[0]
    assert (e["kind"], e["table"]) == ("source_view_change", "Import Log")
    assert (e["old_view"], e["new_view"]) == ("report_import_log_data",
                                              "report_import_log")
    # idempotent: the same diff again appends nothing (known-key dedup)
    capture(fy)
    assert len(_sv_events(fy)) == 1
    # unchanged views never record
    assert not any(ev.get("table") == "Ads" for ev in _sv_events(fy))


def _overrides_fleet(tmp_path, overrides):
    uni, gro = copy_pair(tmp_path)
    _, gro2 = copy_pair(tmp_path, growth_dir="growth_mini_plain")
    return make_fleet(tmp_path, uni, {
        "spinout-05": {"path": gro, "overrides": overrides},
        "spinout-04": gro2,
    })


def test_resolve_v2_sections(tmp_path):
    from tmdl_drift_doctor.overrides import resolve_for_model
    fy = _overrides_fleet(tmp_path, {
        "measures": [{"object": "#Report Measures[Cost]", "kind": "expression",
                      "reason": "r"}],
        "source_views": [{"table": "_Campaigns", "view": "_campaigns_n_v_3",
                          "reason": "r"}],
        "tables": [{"name": "Client-c Conversions", "reason": "r"}],
        "columns": [{"object": "__Calendar[m_cohort_qualifier_month]",
                     "kind": "extension", "reason": "r"}],
        "relationships": [{"signature": "A[x] -> B[y]", "reason": "r"}],
        "fieldparam_entries": [{"category": "app_downloads_y",
                                "entry": "Carrier", "reason": "r"}],
        "mapping_rows": [{"category": "Email Marketing",
                          "key": "client_c_conversions_x", "reason": "r"}],
    })
    fleet = load_fleet(fy)
    r = resolve_for_model(fleet, "spinout-05")
    assert r["source_views"] == {"_Campaigns": "_campaigns_n_v_3"}
    assert r["tables"] == {"Client-c Conversions"}
    assert r["columns"] == {"__Calendar": {"m_cohort_qualifier_month"}}
    assert r["relationships"] == {"A[x] -> B[y]"}
    assert r["fieldparam_entries"] == {"app_downloads_y": {"Carrier"}}
    assert r["mapping_rows"] == {"Email Marketing": {"client_c_conversions_x"}}
    # a model with no declarations resolves to empty sections, not KeyErrors
    empty = resolve_for_model(fleet, "spinout-04")
    assert empty["tables"] == set() and empty["source_views"] == {}


def test_v2_validation_is_loud(tmp_path):
    fy = _overrides_fleet(tmp_path, {
        "source_views": [{"table": "_Campaigns", "view": "_campaigns_n_v_3"}],  # no reason
        "typo_section": [{"name": "x", "reason": "r"}],
    })
    with pytest.raises(DriftDoctorError) as exc:
        load_fleet(fy)
    msg = str(exc.value)
    assert "reason" in msg and "typo_section" in msg
    # nothing is partially applied: the CLI refuses with a usage/config error
    from conftest import drift
    r = drift("detect", fleet=fy)
    assert r.exit_code == 2
    assert "reason" in r.output and "typo_section" in r.output

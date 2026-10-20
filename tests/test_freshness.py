"""Branch freshness: ``drift-doctor freshness``.

A branch cut before a retirement still carries the object. The ratchet
catches the resurrection after the merge; this command warns before it.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import write_fleet

from tmdl_drift_doctor.freshness import (FreshnessError, at_risk, in_force,
                                         render, run, unseen)

MODEL = "model Model\n\tculture: en-US\n"


def _ledger(path, events) -> Path:
    p = Path(path)
    p.write_text("".join(json.dumps(e) + "\n" for e in events),
                 encoding="utf-8")
    return p


def _retired(kind, ref):
    return {"ts": "2026-08-07T00:00:00+00:00", "event": "retired",
            "kind": kind, "ref": ref}


def _model(root: Path, rel: str, tables: dict) -> Path:
    d = root / rel / "definition"
    (d / "tables").mkdir(parents=True, exist_ok=True)
    (d / "model.tmdl").write_text(MODEL, encoding="utf-8")
    for name, body in tables.items():
        (d / "tables" / f"{name}.tmdl").write_text(body, encoding="utf-8")
    return root / rel


def _fleet(root: Path, models: dict) -> Path:
    template = root / "template" / "Shop.SemanticModel"
    template.mkdir(parents=True, exist_ok=True)
    (template / "definition").mkdir(parents=True, exist_ok=True)
    (template / "definition" / "model.tmdl").write_text(MODEL, encoding="utf-8")
    entries = {name: _model(root, f"stores/{name}/Shop.SemanticModel", tables)
               for name, tables in models.items()}
    return write_fleet(root, template, entries, state_dir=".drift-doctor")


def test_base_equals_head_sees_nothing_unseen(tmp_path):
    base = _ledger(tmp_path / "base.jsonl", [_retired("table", "Trade-ins")])
    head = _ledger(tmp_path / "head.jsonl", [_retired("table", "Trade-ins")])
    assert unseen(base, head) == []


def test_an_unseen_table_still_present_is_at_risk(tmp_path):
    base = _ledger(tmp_path / "base.jsonl", [])
    head = _ledger(tmp_path / "head.jsonl", [_retired("table", "Trade-ins")])
    fleet_yml = _fleet(tmp_path, {
        "north": {"Trade-ins": "table Trade-ins\n"},
        "south": {},
    })
    from tmdl_drift_doctor.fleet import load_fleet
    fleet = load_fleet(fleet_yml)
    at, unchecked = at_risk(fleet, unseen(base, head))
    assert at == [(("table", "Trade-ins"), ["north"])]
    assert unchecked == []


def test_a_relationship_is_judged_on_the_from_side_only(tmp_path):
    base = _ledger(tmp_path / "base.jsonl", [])
    head = _ledger(tmp_path / "head.jsonl",
                   [_retired("relationship", "Rentals[bike_id] -> Bikes[bike_id]")])
    fleet_yml = _fleet(tmp_path, {
        "south": {"Rentals": "table Rentals\n\tcolumn bike_id\n",
                  "Bikes": "table Bikes\n\tcolumn bike_id\n"},
    })
    from tmdl_drift_doctor.fleet import load_fleet
    fleet = load_fleet(fleet_yml)
    at, _ = at_risk(fleet, unseen(base, head))
    assert at == [(("relationship", "Rentals[bike_id] -> Bikes[bike_id]"),
                   ["south"])]


def test_a_quoted_measure_name_is_found(tmp_path):
    base = _ledger(tmp_path / "base.jsonl", [])
    head = _ledger(tmp_path / "head.jsonl",
                   [_retired("measure", "'Service Log'[Open Tickets]")])
    fleet_yml = _fleet(tmp_path, {
        "north": {"Service Log": "table 'Service Log'\n\tmeasure "
                                 "'Open Tickets' = COUNTROWS('Service Log')\n"},
    })
    from tmdl_drift_doctor.fleet import load_fleet
    fleet = load_fleet(fleet_yml)
    at, _ = at_risk(fleet, unseen(base, head))
    assert at == [(("measure", "'Service Log'[Open Tickets]"), ["north"])]


def test_the_host_table_is_authoritative(tmp_path):
    base = _ledger(tmp_path / "base.jsonl", [])
    head = _ledger(tmp_path / "head.jsonl", [_retired("measure", "Sales[Margin]")])
    fleet_yml = _fleet(tmp_path, {
        "north": {"Finance": "table Finance\n\tmeasure Margin = 1\n"},
    })
    from tmdl_drift_doctor.fleet import load_fleet
    fleet = load_fleet(fleet_yml)
    at, _ = at_risk(fleet, unseen(base, head))
    assert at == []


def test_a_column_on_its_own_line_is_found(tmp_path):
    base = _ledger(tmp_path / "base.jsonl", [])
    head = _ledger(tmp_path / "head.jsonl",
                   [_retired("column", "Bikes[frame_size]")])
    fleet_yml = _fleet(tmp_path, {
        "north": {"Bikes": "table Bikes\n\tcolumn frame_size\n"},
    })
    from tmdl_drift_doctor.fleet import load_fleet
    fleet = load_fleet(fleet_yml)
    at, _ = at_risk(fleet, unseen(base, head))
    assert at == [(("column", "Bikes[frame_size]"), ["north"])]


def test_an_unchecked_kind_is_listed_but_never_at_risk(tmp_path):
    base = _ledger(tmp_path / "base.jsonl", [])
    head = _ledger(tmp_path / "head.jsonl",
                   [_retired("mapping_row", "zmap:row-1")])
    fleet_yml = _fleet(tmp_path, {"north": {}})
    from tmdl_drift_doctor.fleet import load_fleet
    fleet = load_fleet(fleet_yml)
    at, unchecked = at_risk(fleet, unseen(base, head))
    assert at == []
    assert unchecked == [("mapping_row", "zmap:row-1")]


def test_an_unseen_object_gone_everywhere_is_not_at_risk(tmp_path):
    base = _ledger(tmp_path / "base.jsonl", [])
    head = _ledger(tmp_path / "head.jsonl",
                   [_retired("measure", "Sales[Old KPI]")])
    fleet_yml = _fleet(tmp_path, {"north": {"Sales": "table Sales\n"}})
    from tmdl_drift_doctor.fleet import load_fleet
    fleet = load_fleet(fleet_yml)
    at, _ = at_risk(fleet, unseen(base, head))
    assert at == []


def test_a_global_revival_cancels_the_retirement(tmp_path):
    base = _ledger(tmp_path / "base.jsonl", [])
    head = _ledger(tmp_path / "head.jsonl", [
        _retired("table", "Trade-ins"),
        {"ts": "2026-08-08T00:00:00+00:00", "event": "revived",
         "kind": "table", "ref": "Trade-ins"},
    ])
    assert unseen(base, head) == []


def test_in_force_folds_a_revival():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = _ledger(Path(d) / "l.jsonl", [
            _retired("measure", "A"),
            {"event": "revived", "kind": "measure", "ref": "A"},
        ])
        assert in_force(p) == {}


def test_the_body_file_and_the_stale_flag(tmp_path):
    base = _ledger(tmp_path / "base.jsonl", [])
    head = _ledger(tmp_path / "head.jsonl", [_retired("table", "Trade-ins")])
    fleet_yml = _fleet(tmp_path, {
        "north": {"Trade-ins": "table Trade-ins\n"},
        "south": {},
    })
    from tmdl_drift_doctor.fleet import load_fleet
    fleet = load_fleet(fleet_yml)
    body = tmp_path / "body.md"
    flag = tmp_path / "flag"
    res = run(fleet, base, head, base_label="abc123",
              body_file=body, stale_flag_file=flag)
    assert res["at_risk"]
    text = body.read_text(encoding="utf-8")
    assert "<!-- drift-doctor:freshness -->" in text
    assert "Trade-ins" in text
    assert "north" in text
    assert flag.read_text(encoding="utf-8") == "1"


def test_the_stale_flag_is_empty_when_nothing_is_at_risk(tmp_path):
    base = _ledger(tmp_path / "base.jsonl", [])
    head = _ledger(tmp_path / "head.jsonl", [_retired("table", "Trade-ins")])
    fleet_yml = _fleet(tmp_path, {"north": {"Sales": "table Sales\n"}})
    from tmdl_drift_doctor.fleet import load_fleet
    fleet = load_fleet(fleet_yml)
    flag = tmp_path / "flag"
    res = run(fleet, base, head, body_file=tmp_path / "b.md",
              stale_flag_file=flag)
    assert not res["at_risk"]
    assert flag.read_text(encoding="utf-8") == ""


def test_the_body_confirms_a_current_base(tmp_path):
    base = _ledger(tmp_path / "base.jsonl", [_retired("table", "Trade-ins")])
    head = _ledger(tmp_path / "head.jsonl", [_retired("table", "Trade-ins")])
    fleet_yml = _fleet(tmp_path, {"north": {}})
    from tmdl_drift_doctor.fleet import load_fleet
    fleet = load_fleet(fleet_yml)
    body = tmp_path / "b.md"
    run(fleet, base, head, base_label="abc123456", body_file=body)
    text = body.read_text(encoding="utf-8")
    assert "abc123456" in text
    assert "current with the main line" in text


def test_a_malformed_ledger_is_refused(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n", encoding="utf-8")
    try:
        in_force(bad)
    except FreshnessError as exc:
        assert "malformed ledger line" in str(exc)
    else:
        raise AssertionError("a malformed ledger must be refused")


def test_a_missing_ledger_is_refused(tmp_path):
    try:
        in_force(tmp_path / "nope.jsonl")
    except FreshnessError as exc:
        assert "no ledger at" in str(exc)
    else:
        raise AssertionError("a missing ledger must be refused")


def test_more_than_six_models_are_capped():
    from tmdl_drift_doctor.freshness import _model_list
    assert _model_list(["a", "b", "c", "d", "e", "f"]) == "a, b, c, d, e, f"
    assert _model_list(["a", "b", "c", "d", "e", "f", "g", "h"]) == \
        "a, b, c, d, e, f... (+2)"


def test_the_freshness_command_reports_and_exits_zero(tmp_path):
    from conftest import drift
    base = _ledger(tmp_path / "base.jsonl", [])
    head = _ledger(tmp_path / "head.jsonl", [_retired("table", "Trade-ins")])
    fleet_yml = _fleet(tmp_path, {
        "north": {"Trade-ins": "table Trade-ins\n"},
    })
    r = drift("freshness", "--base-ledger", str(base), "--head-ledger",
              str(head), fleet=fleet_yml)
    assert r.exit_code == 0, r.output
    assert "unseen: 1 entry" in r.output
    assert "at risk: table Trade-ins in north" in r.output


def test_the_freshness_command_refuses_a_malformed_ledger(tmp_path):
    from conftest import drift
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n", encoding="utf-8")
    fleet_yml = _fleet(tmp_path, {"north": {}})
    r = drift("freshness", "--base-ledger", str(bad), fleet=fleet_yml)
    assert r.exit_code == 2

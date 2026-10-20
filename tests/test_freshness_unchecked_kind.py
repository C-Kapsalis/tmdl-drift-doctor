"""A retirement kind the freshness command cannot check must never be
reported as safe.

The command says "nothing at risk" and writes "nothing can be resurrected by
this merge" when it never looked at the kind. It must say the check could not
be performed instead.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import write_fleet

from tmdl_drift_doctor.freshness import run

MODEL = "model Model\n\tculture: en-US\n"


def _ledger(path, events) -> Path:
    p = Path(path)
    p.write_text("".join(json.dumps(e) + "\n" for e in events),
                 encoding="utf-8")
    return p


def _retired(kind, ref):
    return {"ts": "2026-08-07T00:00:00+00:00", "event": "retired",
            "kind": kind, "ref": ref}


def _fleet(root: Path, models: dict) -> Path:
    template = root / "template" / "Shop.SemanticModel"
    template.mkdir(parents=True, exist_ok=True)
    (template / "definition").mkdir(parents=True, exist_ok=True)
    (template / "definition" / "model.tmdl").write_text(MODEL, encoding="utf-8")
    entries = {}
    for name, tables in models.items():
        d = root / "stores" / name / "Shop.SemanticModel" / "definition"
        (d / "tables").mkdir(parents=True, exist_ok=True)
        (d / "model.tmdl").write_text(MODEL, encoding="utf-8")
        for tname, body in tables.items():
            (d / "tables" / f"{tname}.tmdl").write_text(body, encoding="utf-8")
        entries[name] = root / "stores" / name / "Shop.SemanticModel"
    return write_fleet(root, template, entries, state_dir=".drift-doctor")


def test_the_command_never_claims_a_merge_is_safe_for_an_unchecked_kind(tmp_path):
    from conftest import drift
    base = _ledger(tmp_path / "base.jsonl", [])
    head = _ledger(tmp_path / "head.jsonl",
                   [_retired("mapping_row", "Plan Map/legacy-gold")])
    fleet_yml = _fleet(tmp_path, {"north": {}})
    r = drift("freshness", "--base-ledger", str(base), "--head-ledger",
              str(head), fleet=fleet_yml)
    assert r.exit_code == 0, r.output
    assert "nothing at risk" not in r.output
    assert "none of the unseen retirements is present" not in r.output
    # the kind it could not check must be named, and the incompleteness
    # stated
    assert "not checked" in r.output
    assert "mapping_row" in r.output


def test_the_body_never_claims_nothing_can_be_resurrected(tmp_path):
    from conftest import drift
    base = _ledger(tmp_path / "base.jsonl", [])
    head = _ledger(tmp_path / "head.jsonl",
                   [_retired("mapping_row", "Plan Map/legacy-gold")])
    fleet_yml = _fleet(tmp_path, {"north": {}})
    body = tmp_path / "body.md"
    r = drift("freshness", "--base-ledger", str(base), "--head-ledger",
              str(head), "--body-file", str(body), fleet=fleet_yml)
    assert r.exit_code == 0, r.output
    text = body.read_text(encoding="utf-8")
    assert "nothing can be resurrected" not in text
    assert "None of them is present in any derived model" not in text
    assert "could not be checked" in text
    assert "incomplete" in text


def test_a_checkable_kind_that_is_gone_still_reports_nothing_at_risk(tmp_path):
    """The safe claim survives when every unseen retirement was checked."""
    from conftest import drift
    base = _ledger(tmp_path / "base.jsonl", [])
    head = _ledger(tmp_path / "head.jsonl", [_retired("table", "Trade-ins")])
    fleet_yml = _fleet(tmp_path, {"north": {"Sales": "table Sales\n"}})
    r = drift("freshness", "--base-ledger", str(base), "--head-ledger",
              str(head), fleet=fleet_yml)
    assert r.exit_code == 0, r.output
    assert "nothing at risk" in r.output


def test_the_result_carries_the_unchecked_pairs(tmp_path):
    from tmdl_drift_doctor.fleet import load_fleet
    base = _ledger(tmp_path / "base.jsonl", [])
    head = _ledger(tmp_path / "head.jsonl",
                   [_retired("mapping_row", "Plan Map/legacy-gold")])
    fleet_yml = _fleet(tmp_path, {"north": {}})
    fleet = load_fleet(fleet_yml)
    res = run(fleet, base, head)
    assert res["not_checked"] == [("mapping_row", "Plan Map/legacy-gold")]
    assert res["at_risk"] == []

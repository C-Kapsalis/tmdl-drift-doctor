"""DC-2: a reference to a retired table is repairable.

When a table is retired from the template and a derived model still reads
it, the tool reports the finding `retire.stranded_ref` but had no repair
for it: `remediate` said the kind has no automated fix. The finding was
therefore permanent, and a model that reads a table that no longer exists
kept reading it.

Two cases, and the finding's data says which one applies:
- sensor-only: the reference appears only inside a dead sensor operand, so
  the model reads the table through a sensor expression rather than for its
  rows. The repair strips the dead sensor operand and leaves the rest of
  the expression untouched.
- data: the model reads the retired table for its rows. The repair removes
  the reference from the expression.
"""

from __future__ import annotations

from conftest import drift, drift_json, write_fleet
from port_helpers_b2_l1b import findings, load_scenario

from tmdl_drift_doctor.fleet import load_fleet
from tmdl_drift_doctor.ledger import Ledger

RETIRED = "Old Campaigns"


def _fleet(stand_in, tmp_path, scenario="live_ref"):
    root, data = load_scenario(stand_in, "stranded", scenario)
    fy = write_fleet(root, data["template"],
                     {mid: m["path"] for mid, m in data["models"].items()},
                     state_dir=str(root / "state"),
                     allowlist={"kinds": ["retire.stranded_ref"]})
    assert drift("capture", fleet=fy).exit_code == 0
    fleet = load_fleet(fy)
    ledger = Ledger(fleet.ledger_path)
    for name in data["retired_tables"]:
        ledger.record_retirement("table", name, reason="retired from the "
                                  "template")
    return root, data, fy


def _stranded(report):
    return findings(report, kind="retire.stranded_ref")


def test_the_finding_says_which_case_it_is(stand_in, tmp_path):
    """DC-2: the finding's data carries the case."""
    _root, _data, fy = _fleet(stand_in, tmp_path)
    _r, report = drift_json("detect", "--kind", "retire.stranded_ref", fleet=fy)
    assert report is not None
    found = _stranded(report)
    assert found, "no retire.stranded_ref finding"
    assert found[0].get("data", {}).get("sensor_only") in (True, False)


def _model_file(root, data, name="Helper Measures.tmdl"):
    return (root / data["models"]["bike-shop"]["path"]
            / "definition" / "tables" / name)


def _repair(fy):
    """The repair through the Python API.

    The kind is a destructive one, so it is never applied under ``--yes``:
    it needs a sync run and the interactive yes the CLI asks for. The API
    stands for that yes.
    """
    from tmdl_drift_doctor.remediate import remediate
    fleet = load_fleet(fy)
    from tmdl_drift_doctor.baseline import load_baseline
    from tmdl_drift_doctor.detect import detect_fleet
    from tmdl_drift_doctor.ledger import Ledger
    baseline = load_baseline(fleet)
    findings = detect_fleet(fleet, baseline, Ledger(fleet.ledger_path))
    return remediate(fleet, findings, kind="retire.stranded_ref", sync=True,
                     baseline=baseline)


def test_the_repair_removes_the_dead_sensor(stand_in, tmp_path):
    """DC-2: the sensor operand that names the retired table is gone."""
    root, data, fy = _fleet(stand_in, tmp_path)
    result = _repair(fy)
    assert result.count("failed") == 0, result.outcomes
    assert result.count("applied") >= 1, result.outcomes

    text = _model_file(root, data).read_text(encoding="utf-8")
    assert RETIRED not in text, (
        "the measure still names the retired table")
    # the rest of the expression survives
    assert "Clicks" in text, "the repair took more than the dead operand"


def test_a_second_detect_finds_nothing(stand_in, tmp_path):
    """DC-2: the repair converges."""
    _root, _data, fy = _fleet(stand_in, tmp_path)
    _repair(fy)
    _r, report = drift_json("detect", "--kind", "retire.stranded_ref", fleet=fy)
    assert not _stranded(report), (
        "the retired table is still referenced after the repair")


def test_a_clean_model_is_not_changed(stand_in, tmp_path):
    """DC-2: a model with no stranded reference is untouched."""
    root, data, fy = _fleet(stand_in, tmp_path, scenario="clean")
    before = _model_file(root, data).read_bytes()
    r = drift("remediate", "--kind", "retire.stranded_ref", "--yes", fleet=fy)
    assert r.exit_code == 0, r.output
    assert _model_file(root, data).read_bytes() == before, (
        "a clean model was changed")


def test_a_data_reference_is_not_repaired_without_a_person(stand_in, tmp_path):
    """DC-2: a measure that reads the retired table for its rows needs a
    person; the tool says so and changes nothing."""
    from tmdl_drift_doctor import tmdl_edit as te
    root, data, fy = _fleet(stand_in, tmp_path)
    model = _model_file(root, data)
    before = model.read_bytes()
    # the measure reads the retired table for its rows
    model.write_bytes(model.read_bytes().replace(
        b"int(not ISEMPTY('Old Campaigns'))",
        b"SUM('Old Campaigns'[clicks])"))

    result = _repair(fy)
    assert result.count("applied") == 0, result.outcomes
    assert result.count("failed") >= 1, result.outcomes
    reasons = " ".join(o.reason for o in result.outcomes)
    assert "rows" in reasons, reasons
    assert model.read_bytes() == before.replace(
        b"int(not ISEMPTY('Old Campaigns'))",
        b"SUM('Old Campaigns'[clicks])"), "the file was changed"

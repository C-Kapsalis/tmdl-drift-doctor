"""Layer 1: a connection-parameter STAMPING table must agree with the fleet.

An automation that writes connection parameters back into a model's
expressions.tmdl after the template replaces it holds its own copy of the
values. Nothing checked that copy against the fleet declaration, so a stale
value silently reverted a human fix on the next run (three stale values were
found when the original guard was written: a dataset and two project ids).
This gates the SOURCE.

Port: the original imported the hard-coded orchestration table and the
registry resolver (`config_for`). Here the stamping table is a stand-in YAML
file written from scenario.json (`stamps`, keyed by model FOLDER, values
quoted as they appear in TMDL) and the check is
`drift-doctor fleet check --stamps <file>` (B-connection-parameter-contract
rules 4-6). Expected values: project / billing project = declared, else the
fleet default `connection.project`; `v-db` and `v-BQ_db` = the resolved
dataset. Output contract the tests read (one line per problem, exit 1 when
any): an unmapped entry line contains "unmapped"; a value mismatch line
contains "expected"; a dataset-parameter split line contains "v-db",
"v-BQ_db" and "disagree".

Scenarios (tests/fixtures/b2-l1b/connection/<scenario>) and the verdict the
ORIGINAL test gave on the same data (parity-expected.json).
"""
from __future__ import annotations

import pytest
import yaml

from conftest import drift, write_fleet
from port_helpers_b2_l1b import ensure_model_dirs, load_scenario

pytestmark = [pytest.mark.p0, pytest.mark.layer1]

TEMPLATE = "universal/Growth.SemanticModel"

# scenario -> (verdict of test 1, verdict of test 2), recorded from the originals
SCENARIOS = {
    "clean": ("pass", "pass"),
    "folder_stamp": ("fail", "pass"),
    "split": ("fail", "fail"),
    "unmapped": ("fail", "pass"),
    "carve_out_reverted": ("fail", "pass"),
}


def _check(stand_in, scenario: str):
    root, data = load_scenario(stand_in, "connection", scenario)
    ensure_model_dirs(root, [TEMPLATE] + [m["path"] for m in data["models"].values()])
    models = {mid: ({"path": m["path"], "connection": m["connection"]}
                    if m.get("connection") else m["path"])
              for mid, m in data["models"].items()}
    fy = write_fleet(root, TEMPLATE, models, connection={"project": data["default_project"]})
    stamps = root / "stamps.yml"
    stamps.write_text(yaml.safe_dump(data["stamps"], sort_keys=False), encoding="utf-8")
    r = drift("fleet check", "--stamps", stamps, fleet=fy)
    lines = [ln.strip() for ln in r.output.splitlines() if ln.strip()]
    return r, lines


@pytest.mark.parametrize("scenario, verdict",
                         [(s, v[0]) for s, v in SCENARIOS.items()])
def test_orchestration_connection_values_match_the_registry_contract(stand_in, scenario, verdict):
    """Every stamped connection parameter equals the fleet's own answer, and
    every stamping entry names a fleet model folder."""
    r, lines = _check(stand_in, scenario)
    assert r.exit_code in (0, 1), r.output
    unmapped = [ln for ln in lines if "unmapped" in ln]
    mismatches = [ln for ln in lines if "expected" in ln]
    if verdict == "pass":
        assert not unmapped, (
            "stamping table names a model folder that is not in fleet.yml — add "
            "the model, or drop the dead entry:\n  - " + "\n  - ".join(unmapped))
        assert not mismatches, (
            "the stamp would repoint a model at the wrong warehouse:\n  - "
            + "\n  - ".join(mismatches))
        return
    assert r.exit_code == 1, r.output
    assert unmapped or mismatches, r.output
    if scenario == "unmapped":
        assert any("bakery_db" in ln for ln in unmapped), r.output
    if scenario == "folder_stamp":
        assert any("roastery_db" in ln and "v-db" in ln for ln in mismatches), r.output
    if scenario == "carve_out_reverted":
        assert any("spinout_13_db" in ln and "spinout_12_db" in ln for ln in mismatches), r.output


@pytest.mark.parametrize("scenario, verdict",
                         [(s, v[1]) for s, v in SCENARIOS.items()])
def test_dataset_parameters_agree_within_each_project(stand_in, scenario, verdict):
    """v-BQ_db, where declared, never diverges from v-db."""
    r, lines = _check(stand_in, scenario)
    split = [ln for ln in lines if "disagree" in ln and "v-BQ_db" in ln and "v-db" in ln]
    if verdict == "pass":
        assert not split, (
            "the two dataset parameters must name the same dataset:\n  - "
            + "\n  - ".join(split))
    else:
        assert r.exit_code == 1 and split, r.output
        assert any("roastery_db" in ln for ln in split), r.output

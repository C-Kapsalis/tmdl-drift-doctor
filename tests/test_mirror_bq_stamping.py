"""Layer 1: a template cascade must stamp the dataset parameter from the FLEET
declaration, never from the model's folder name.

Why this exists: the stamp used to derive the dataset from the `<id>_db`
folder name. That is right for most models and wrong for the one model that
reads another model's warehouse (spinout-13 reads spinout_12_db). Every
template sync wrote a dataset that does not exist over the human fix, twice,
under green checks. These tests gate the cause: the resolver itself.

Port: the original ran `scripts/bq_config_for.py <registry> <client>` over the
real client registry. Here the registry is a stand-in fleet
(tests/fixtures/b2-l1b/connection/clean/scenario.json) and the resolver is
`drift-doctor fleet connection <model>` (B-connection-parameter-contract).
Registry keys bq_project / bq_billing_project / bq_dataset are the fleet
entry's `connection` block (project / billing_project / dataset).
The workflow-text test (I0186) is not ported (CI YAML).
"""
from __future__ import annotations

import pytest

from conftest import drift, write_fleet
from port_helpers_b2_l1b import ensure_model_dirs, load_scenario

pytestmark = [pytest.mark.p0, pytest.mark.layer1]

TEMPLATE = "universal/Growth.SemanticModel"


@pytest.fixture
def fleet_file(stand_in):
    root, data = load_scenario(stand_in, "connection", "clean")
    ensure_model_dirs(root, [TEMPLATE] + [m["path"] for m in data["models"].values()])
    models = {}
    for mid, m in data["models"].items():
        entry = {"path": m["path"]}
        if m.get("connection"):
            entry["connection"] = m["connection"]
        models[mid] = entry
    fy = write_fleet(root, TEMPLATE, models,
                     connection={"project": data["default_project"]})
    return fy, data["models"]


def _run(fy, model_id: str) -> list[str]:
    r = drift("fleet connection", model_id, fleet=fy)
    assert r.exit_code == 0, r.output          # the original ran with check=True
    return r.stdout.rstrip("\n").split("\t")


def test_bq_config_always_emits_a_dataset(fleet_file):
    """Every fleet model resolves a dataset, project override or not."""
    fy, models = fleet_file
    missing = []
    for model_id in models:
        fields = _run(fy, model_id)
        if len(fields) != 3 or not fields[2].strip():
            missing.append((model_id, fields))
    assert not missing, (
        "`fleet connection` must ALWAYS print 3 tab-separated fields with a "
        "non-empty dataset (the stamp writes v-db from field 3):\n  "
        + "\n  ".join(f"{c}: {f}" for c, f in missing)
    )


def test_bq_config_dataset_matches_registry_including_the_1099_carve_out(fleet_file):
    """The dataset is the declared `connection.dataset`, else `<model id>_db`.

    Pinned explicitly for spinout-13 because that carve-out IS the bug: a
    convention-only derivation reintroduces the revert loop.
    """
    fy, models = fleet_file
    wrong = []
    for model_id, entry in models.items():
        expected = (entry.get("connection") or {}).get("dataset") or f"{model_id}_db"
        actual = _run(fy, model_id)[2]
        if actual != expected:
            wrong.append(f"{model_id}: got {actual!r}, fleet says {expected!r}")
    assert not wrong, "dataset must come from the fleet:\n  " + "\n  ".join(wrong)

    if "spinout-13" in models:
        assert _run(fy, "spinout-13")[2] == "spinout_12_db", (
            "spinout-13 must resolve to spinout_12_db (two models, one warehouse). "
            "If this fails, the cascade will write the non-existent "
            "spinout-13_db over the fix."
        )


def test_project_override_stays_opt_in(fleet_file):
    """An absent project yields an EMPTY field, so the stamp leaves the
    template's default project alone. The resolver always prints, so callers
    must test the FIELD, not the whole line."""
    fy, models = fleet_file
    wrong = []
    for model_id, entry in models.items():
        declared = (entry.get("connection") or {}).get("project")
        actual = _run(fy, model_id)[0]
        if bool(declared) != bool(actual):
            wrong.append(f"{model_id}: declared={declared!r} emitted={actual!r}")
    assert not wrong, (
        "project field must be empty exactly when the model declares no "
        "project:\n  " + "\n  ".join(wrong)
    )

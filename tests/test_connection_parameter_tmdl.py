"""
Connection-parameter coverage (A-expression-name-superset).

Every model's expressions.tmdl must declare at least the v-parameters that the template's
expressions.tmdl ships with. Extras are fine (a custom backend table can add its own
v-fromdate). Dropping a parameter the template carries breaks the workspace-parameter
override contract used during promotion.

Ported: the template names come from drift-doctor's captured template state (the original
read `v_parameter_names` from the integration baseline, which is company data and is not
copied); each fleet model is checked through the new `expression.missing` kind. Runs on the
roastery stand-in fleet (tests/fixtures/b1-l1a/fleet).
"""
from __future__ import annotations

import re

import pytest

from conftest import drift
from port_helpers_b1_l1a import copy_fleet, definition, detect, findings, primary_models

MODELS = primary_models()
_V = re.compile(r"^expression (v-[^\s=]+)", re.MULTILINE)


@pytest.fixture(scope="module")
def fleet_report(tmp_path_factory):
    root = copy_fleet(tmp_path_factory.mktemp("params"))
    return root, detect(root)[1]


def _names(path):
    return set(_V.findall(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("model", MODELS)
def test_client_params_superset_of_universal(fleet_report, model):
    """The model's expressions.tmdl declares every v-parameter the template does."""
    root, rep = fleet_report
    universal_params = _names(definition(root, "template") / "expressions.tmdl")
    if not universal_params:
        pytest.skip("template does not declare any v-parameters")
    client_expressions = definition(root, model) / "expressions.tmdl"
    if not client_expressions.exists():
        pytest.skip(f"model expressions.tmdl not found at {client_expressions}")
    missing = sorted(f["ref"] for f in findings(rep, model, "expression.missing")
                     if f["ref"].startswith("v-"))
    assert not missing, (
        f"{model} is missing {len(missing)} v-parameter(s) that the template declares: "
        f"{missing}. Every model's expressions.tmdl must be a superset of the template's."
    )


def test_missing_v_parameter_fails_listing_it(tmp_path):
    """mutation: the model lacks `v-db` -> failure listing `v-db`."""
    root = copy_fleet(tmp_path)
    f = definition(root, "spinout-05") / "expressions.tmdl"
    f.write_text(f.read_text(encoding="utf-8").replace(
        'expression v-db = "roastery_dw"', 'expression v-dbx = "roastery_dw"'), encoding="utf-8")
    r, rep = detect(root)
    refs = [x["ref"] for x in findings(rep, "spinout-05", "expression.missing")]
    assert refs == ["v-db"], r.output
    assert r.exit_code == 1


def test_connection_parameter_is_reported_but_never_written(tmp_path):
    """Spec case 3: a template-only connection parameter carries the hint and remediate
    writes nothing."""
    root = copy_fleet(tmp_path)
    f = definition(root, "spinout-05") / "expressions.tmdl"
    f.write_text(f.read_text(encoding="utf-8").replace(
        'expression v-db = "roastery_dw"', 'expression v-dbx = "roastery_dw"'), encoding="utf-8")
    before = f.read_bytes()
    r, rep = detect(root)
    hint = [x for x in findings(rep, "spinout-05", "expression.missing") if x["ref"] == "v-db"]
    assert hint and "copy the declaration from the template" in (hint[0].get("detail") or ""), r.output
    r = drift("remediate", "--kind", "expression.missing", "--model", "spinout-05", "--yes",
              fleet=root / "fleet.yml")
    assert f.read_bytes() == before, r.output
    assert "copy the declaration from the template and set this model's own value" in r.output


def test_extra_client_parameter_is_allowed(fleet_report):
    """spinout-10 declares an extra `v-fromdate tickets`: extras are allowed."""
    _, rep = fleet_report
    assert not findings(rep, "spinout-10", "expression.missing")


def test_model_without_expressions_file_is_skipped(tmp_path):
    """No expressions.tmdl: nothing to compare (original: skip)."""
    root = copy_fleet(tmp_path)
    (definition(root, "spinout-05") / "expressions.tmdl").unlink()
    _, rep = detect(root)
    assert not findings(rep, "spinout-05", "expression.missing")

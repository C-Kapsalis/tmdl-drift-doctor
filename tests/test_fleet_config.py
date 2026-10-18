"""fleet.yml validation rules without a ported test (A-fleet-config-
validation cases 3-4, D-overrides-typed-sections, A-growth-model-family)."""
from __future__ import annotations

import pytest
import yaml

from tmdl_drift_doctor import DriftDoctorError
from tmdl_drift_doctor.fleet import kind_matches, load_fleet, merge_overlay

GUID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def _models(tmp_path, *names):
    for n in ("template", *names):
        (tmp_path / n / "M.SemanticModel" / "definition").mkdir(parents=True)


def _write(tmp_path, doc):
    fy = tmp_path / "fleet.yml"
    fy.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return fy


def _base(**models):
    return {"version": 1, "template": "template/M.SemanticModel",
            "models": models}


@pytest.mark.parametrize("models,extra,needle", [
    ({"a": "a/M.SemanticModel", "b": "a/M.SemanticModel"}, {}, "also the path"),
    ({"a": {"path": "a/M.SemanticModel", "workspace": {"guid": "abc"}}}, {},
     "not a GUID"),
    ({"a": {"path": "a/M.SemanticModel", "workspace": {"verified": True}}}, {},
     "verified"),
    ({"a": {"path": "a/M.SemanticModel", "workspace": {"guid": GUID}},
      "b": {"path": "b/M.SemanticModel", "workspace": {"guid": GUID}}}, {},
     "also the workspace"),
    ({"a": {"path": "a/M.SemanticModel", "secondary_models": [
        {"name": "a2", "path": "b/M.SemanticModel"}]}}, {}, "deviations"),
    ({"a": "a/M.SemanticModel", "b": "b/M.SemanticModel"},
     {"metadata": {"total": 3}}, "metadata.total"),
    ({"a": "a/M.SemanticModel"},
     {"families": {"growth": {"suffixes": ["Growth.SemanticModel"]}}},
     "at least two"),
    ({"a": "a/M.SemanticModel"},
     {"connection_parameters": ["v-db"], "allowlist": {"expressions": ["v-db"]}},
     "connection parameter"),
])
def test_invalid_fleet_is_refused(tmp_path, models, extra, needle):
    _models(tmp_path, "a", "b")
    doc = _base(**models)
    doc.update(extra)
    with pytest.raises(DriftDoctorError, match=needle):
        load_fleet(_write(tmp_path, doc))


def test_all_errors_are_reported_at_once(tmp_path):
    _models(tmp_path, "a")
    doc = _base(a={"path": "a/M.SemanticModel", "colour": "red",
                   "overrides": {"tables": [{"name": "T"}],
                                 "typo_section": []}})
    with pytest.raises(DriftDoctorError) as exc:
        load_fleet(_write(tmp_path, doc))
    msg = str(exc.value)
    assert "colour" in msg and "reason" in msg and "typo_section" in msg


def test_secondary_model_inherits_gating_and_overrides(tmp_path):
    _models(tmp_path, "a", "b")
    doc = _base(a={"path": "a/M.SemanticModel", "gating": "advisory",
                   "gating_reason": "pilot",
                   "overrides": [{"object": "T[c]", "kind": "absent",
                                  "reason": "no such field"}],
                   "secondary_models": [{"name": "a-outlet",
                                         "path": "b/M.SemanticModel",
                                         "deviations": []}]})
    fleet = load_fleet(_write(tmp_path, doc))
    assert fleet.is_advisory("a-outlet")
    assert [o.object for o in fleet.overrides["a-outlet"]] == ["T[c]"]
    assert fleet.entry_of["a-outlet"] == "a"
    assert fleet.primary_models == ["a"]


def test_deep_merge_of_overlays():
    assert merge_overlay({"a": {"x": 1, "y": 2}, "l": [1, 2]},
                         {"a": {"y": 3}, "l": [9]}) == {"a": {"x": 1, "y": 3},
                                                        "l": [9]}


@pytest.mark.parametrize("kind,prefix,hit", [
    ("measure.missing", "measure.missing", True),
    ("measure.missing.f", "measure.missing", True),
    ("measure.expression_drift", "measure", True),
    ("measure.missing", "measure.miss", False),
    ("measures.x", "measure", False),
])
def test_dotted_prefix_matching(kind, prefix, hit):
    assert kind_matches(kind, prefix) is hit

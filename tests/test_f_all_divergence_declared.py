"""A model whose f-All diverges from the template must DECLARE it.

`f-All` is the orchestrator every analysis category's gate composes into, so a
model that extends it carries a real, permanent divergence. Structural
protection leaves a client-aware f-All alone whether or not anyone declared it,
and the drift gate reports 0 pending items either way, so the declaration is
documentation nothing enforces (fail-open). This is the missing arm: if a
model's `f-Filters[f-All]` body differs from the template's, the model must
declare an `expression` override for `f-Filters[f-All]`.

It cannot be hard: half of the divergent models were undeclared when the
original was written, so it is ratcheted instead and the floor MAY ONLY SHRINK.

Port (C-declared-divergence-audit): the original used the kernel overrides
loader, the registry (`client_model_paths`, `advisory_model_paths`) and
`TmdlIndex.get_measure`. Here: the stand-in fleet
(tests/fixtures/b2-l1b/f_all/main), drift-doctor's fleet loader
(`Fleet.models`, `Fleet.is_advisory`, `Fleet.overrides`) and model parser
(`parse_model(...).measure(...)`). Declarations are fleet.yml typed overrides
(`overrides.measures`, D-overrides-typed-sections). Each parametrized verdict
was recorded from the ORIGINAL test on the same data.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import write_fleet
from port_helpers_b2_l1b import load_scenario, scenario_data

from tmdl_drift_doctor.fleet import load_fleet
from tmdl_drift_doctor.tmdl import parse_model

_F_ALL = ("f-Filters", "f-All")
_REF = "f-Filters[f-All]"

# Models whose f-All diverges from the template and which declare NOTHING.
# MAY ONLY SHRINK -- close an entry by adding an `overrides.measures` entry
# for `f-Filters[f-All]` (kind: expression) with a reason.
KNOWN_UNDECLARED_DIVERGENT_F_ALL: set[str] = {
    "spinout_15_db/Growth.SemanticModel",
    "spinout_07_db/Growth.SemanticModel",
    "spinout_03_db/Growth.SemanticModel",
    "spinout_10_db/Growth.SemanticModel",
}

# verdict of the ORIGINAL test per stand-in model
VERDICTS = {
    "bakery": "skip",          # no f-All
    "bike-shop": "pass",       # converged
    "cycle-hub": "fail",       # divergent, undeclared, not in the floor
    "spinout-07": "fail",      # converged but still in the floor (must tighten)
    "spinout-03": "pass",      # divergent, undeclared, in the floor
    "roastery": "pass",        # divergent, declared
}

_DATA = scenario_data("f_all", "main")


def _f_all_body(definition: Path) -> str | None:
    """f-All's expression, or None when the model has no such measure."""
    measure = parse_model(definition).measure(*_F_ALL)
    if measure is None:
        return None
    return (measure.expression or "").strip()


def _declared(fleet, model: str, ref: str) -> bool:
    """True when an `expression` override for `ref` is in force for `model`.

    Accepts the typed-section shape (`{"measures": [...]}`, entries as mappings
    or objects) and the flat list of Override objects of tool 1.1.0."""
    raw = fleet.overrides.get(model) or []
    entries = []
    if isinstance(raw, dict):
        entries = list(raw.get("measures") or []) + list(raw.get("expression") or [])
    else:
        entries = list(raw)
    for e in entries:
        if isinstance(e, str):
            if e == ref:
                return True
            continue
        get = e.get if isinstance(e, dict) else (lambda k, _e=e: getattr(_e, k, None))
        if (get("object") or get("name")) == ref and (get("kind") or "expression") == "expression":
            return True
    return False


@pytest.fixture
def fleet_root(stand_in):
    root, data = load_scenario(stand_in, "f_all", "main")
    models = {}
    for mid, m in data["models"].items():
        entry = {"path": m["path"]}
        if m.get("gating"):
            entry["gating"] = m["gating"]
        if m.get("declared"):
            entry["overrides"] = {"measures": [
                {"object": ref, "kind": "expression", "reason": "adds the loyalty gate to f-All",
                 "owner": "data-team", "added": "2026-09-09"} for ref in m["declared"]]}
        models[mid] = entry
    fy = write_fleet(root, data["template"], models)
    return root, load_fleet(fy)


@pytest.fixture
def universal_f_all(fleet_root) -> str:
    _, fleet = fleet_root
    body = _f_all_body(fleet.template_definition)
    assert body, "the template declares no f-Filters[f-All] — instrument broken"
    return body


def _targets() -> list[tuple[str, str]]:
    return sorted((m["path"], mid) for mid, m in _DATA["models"].items()
                  if m.get("gating") != "advisory")


@pytest.mark.p1
@pytest.mark.layer3
@pytest.mark.parametrize("model_path,client_id", _targets(), ids=lambda v: v)
def test_divergent_f_all_is_declared(model_path, client_id, fleet_root, universal_f_all):
    """Diverge from the template's f-All => declare it, or be a declared gap."""
    root, fleet = fleet_root
    verdict = VERDICTS[client_id]
    assert not fleet.is_advisory(client_id)
    definition = root / model_path / "definition"
    if not definition.is_dir():
        pytest.skip(f"{model_path} does not resolve on disk")

    body = _f_all_body(definition)
    if body is None:
        assert verdict == "skip"
        pytest.skip("model declares no f-Filters[f-All]")

    problems = []
    if body == universal_f_all:
        if model_path in KNOWN_UNDECLARED_DIVERGENT_F_ALL:
            problems.append(
                f"{model_path}: f-All is now byte-identical to the template, so it is "
                f"no longer an undeclared divergence — the ratchet must tighten. "
                f"Remove it from KNOWN_UNDECLARED_DIVERGENT_F_ALL.")
    elif _declared(fleet, client_id, _REF):
        if model_path in KNOWN_UNDECLARED_DIVERGENT_F_ALL:
            problems.append(
                f"{model_path}: the divergence is now declared — the ratchet must "
                f"tighten. Remove it from KNOWN_UNDECLARED_DIVERGENT_F_ALL.")
    elif model_path not in KNOWN_UNDECLARED_DIVERGENT_F_ALL:
        problems.append(
            f"{model_path} ({client_id}): f-All diverges from the template but no "
            f"`f-Filters[f-All]` expression override is declared for this model, so "
            f"the divergence is invisible to the fail-open audit and nothing fails if "
            f"a sync ever stops protecting it.\n  Add an `overrides.measures` entry "
            f"with kind `expression` and a reason, or converge the body onto the "
            f"template's.")

    if verdict == "pass":
        assert not problems, "\n".join(problems)
    else:
        assert problems, f"{model_path}: expected the audit to fail"

"""Growth-architecture models must be recognised regardless of filename (A-growth-model-family).

A false negative made `spinout_02_db/insights.SemanticModel` invisible to growth-scoped
tooling and failed *silently*, reporting an absence rather than an error, which is the
expensive direction: the family list keyed on `model_path.endswith("Growth.SemanticModel")`,
so a Growth-architecture model not *named* Growth was excluded from family syncs.

Never trust a zero from a hand-rolled pattern: these assert a known-present case returns
non-zero, not merely that nothing raised.

Ported: the original's `is_growth_model_path` / `growth_clients()` / suffix constant are
replaced by drift-doctor's fleet family API (`tmdl_drift_doctor.families`) and the
`drift-doctor fleet family growth` command over the roastery stand-in fleet. The same
original file's partition-source cases belong to tmdl-preflight (A-partition-source-styles).
"""
from __future__ import annotations

import shutil

import pytest

from conftest import drift, write_fleet
from port_helpers_b1_l1a import copy_fleet


@pytest.mark.parametrize("path,expected", [
    ("spinout_10_db/Growth.SemanticModel", True),
    ("spinout_02_db/insights.SemanticModel", True),
    ("spinout_02_db/Performance.SemanticModel", False),
    ("universal/Performance.SemanticModel", False),
    ("", False),
])
def test_growth_model_path_recognition(path: str, expected: bool) -> None:
    from tmdl_drift_doctor.families import is_growth_model_path
    assert is_growth_model_path(path) is expected


def test_insights_is_a_growth_client(tmp_path) -> None:
    """spinout-02 carries the Growth architecture and must be in the growth list."""
    root = copy_fleet(tmp_path)
    r = drift("fleet family growth", fleet=root / "fleet.yml")
    assert r.exit_code == 0, r.output
    assert "spinout-02" in r.stdout.strip().split(",")


def test_growth_suffixes_are_not_reduced_to_one() -> None:
    """A single suffix is what caused the regression; keep the set plural."""
    from tmdl_drift_doctor.families import GROWTH_SUFFIXES
    assert len(GROWTH_SUFFIXES) >= 2


# ── family list behaviour (spec case 4; no original test beyond the two above) ──

def test_family_members_are_sorted_and_comma_separated(tmp_path) -> None:
    root = copy_fleet(tmp_path)
    r = drift("fleet family growth", fleet=root / "fleet.yml")
    assert r.stdout.strip() == "spinout-02,spinout-10", r.output


def test_empty_family_is_an_error(tmp_path) -> None:
    src = copy_fleet(tmp_path)
    fy = write_fleet(src, src / "universal/Performance.SemanticModel",
                     {"spinout-05": src / "spinout_05_db/Performance.SemanticModel"})
    r = drift("fleet family growth", fleet=fy)
    assert r.exit_code == 1
    assert r.stderr.strip()
    assert not r.stdout.strip()


def test_template_is_never_a_family_member(tmp_path) -> None:
    src = copy_fleet(tmp_path)
    tpl = src / "template" / "Growth.SemanticModel"
    shutil.copytree(src / "universal/Performance.SemanticModel", tpl)
    fy = write_fleet(src, tpl, {"spinout-10": src / "spinout_10_db/Growth.SemanticModel"})
    r = drift("fleet family growth", fleet=fy)
    assert r.exit_code == 0, r.output
    assert r.stdout.strip() == "spinout-10"

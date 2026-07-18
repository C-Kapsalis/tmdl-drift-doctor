"""Shared fixtures: a throwaway copy of the fixture fleet per test.

The fixture fleet is a fictional gym chain — one template model
(``GymChain``) and two franchise models (``alpha``, ``bravo``) with seeded
drift of every kind the suite detects.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tmdl_drift_doctor import tmdl_edit as te
from tmdl_drift_doctor.baseline import capture
from tmdl_drift_doctor.fleet import load_fleet

FIXTURE = Path(__file__).parent / "fixtures" / "fleet"


@pytest.fixture
def fleet_dir(tmp_path):
    dst = tmp_path / "fleet"
    shutil.copytree(FIXTURE, dst)
    return dst


@pytest.fixture
def fleet(fleet_dir):
    return load_fleet(fleet_dir / "fleet.yml")


@pytest.fixture
def captured(fleet):
    """A fleet with a fresh baseline already captured."""
    capture(fleet)
    return fleet


def retire_everything_from_template(fleet) -> dict:
    """Simulate a template maintainer retiring one object of every kind:

    * measure      — Visits[Peak Hour Visits]
    * column       — Classes[Capacity]
    * table        — Promotions
    * mapping row  — Plan Map/legacy-gold

    Returns the removed measure block (for revival tests)."""
    tdef = fleet.template_definition

    vf = tdef / "tables" / "Visits.tmdl"
    content = vf.read_text(encoding="utf-8")
    measure_block = te.extract_block(content, "measure", "Peak Hour Visits")
    vf.write_text(te.remove_block(content, "measure", "Peak Hour Visits"),
                  encoding="utf-8")

    cf = tdef / "tables" / "Classes.tmdl"
    cf.write_text(te.remove_block(cf.read_text(encoding="utf-8"),
                                  "column", "Capacity"), encoding="utf-8")

    (tdef / "tables" / "Promotions.tmdl").unlink()
    mf = tdef / "model.tmdl"
    mf.write_text(te.deregister_table(mf.read_text(encoding="utf-8"),
                                      "Promotions"), encoding="utf-8")

    pf = tdef / "tables" / "Plan Map.tmdl"
    pf.write_text(te.remove_mapping_row(pf.read_text(encoding="utf-8"),
                                        "legacy-gold"), encoding="utf-8")
    return {"measure_block": measure_block}


def tree_state(root: Path) -> dict:
    """{relative_path: content} for every model file under `root` — used to
    assert dry-run touches nothing."""
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file() and ".drift-doctor" not in p.parts:
            out[str(p.relative_to(root))] = p.read_bytes()
    return out

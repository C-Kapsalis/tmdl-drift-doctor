"""Advisory gating contract: `gating: advisory` demotes, it does not delete
— ported from the user's original suite.

An alpha model registered into the fleet while it is still being aligned to
the template fails every parity assertion by construction. Leaving it hard
taught the team that a red gate is normal; removing it means nothing watches
it. The demotion is deliberately narrow, and these tests pin both halves:

  * a FAILING finding on an advisory model is reported and does not set an
    exit code — the ratchet prints an ADVISORY block;
  * every non-advisory model stays hard-gated in the same run, so the flag
    cannot be mistaken for "stop testing it".

Port: the original read the repository's client registry through the suite's
registry helpers; here the stand-in ``tests/fixtures/b4-ratchet/fleet``
(fictional bike-shop models: gated ``spinout-10`` / ``spinout-02``, advisory
``pilot`` with a ``gating_reason``) is read through drift-doctor's fleet
loader. The ratchet half ran ``scripts/ci/check_ratchet.py`` as a subprocess;
here it is ``drift-doctor ratchet check --from-json`` with the floors at the
fleet's ``<state_dir>/floors.json``. The pytest-hook test of the original file
belongs to tmdl-preflight (inventory I0410) and is not ported here.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from conftest import FIXTURES
from port_helpers_b4_ratchet import check, item, write_floors, write_report

from tmdl_drift_doctor import DriftDoctorError
from tmdl_drift_doctor.fleet import load_fleet

STAND_IN = FIXTURES / "b4-ratchet" / "fleet"
FLEET_YML = STAND_IN / "fleet.yml"

GATED_CLIENT = "spinout-10"     # any non-advisory model of the stand-in fleet


def _raw_models() -> dict:
    return yaml.safe_load(FLEET_YML.read_text(encoding="utf-8"))["models"]


def _advisory_models(fleet) -> set:
    """The ONE reader: drift-doctor's per-model gating decision."""
    return {name for name in fleet.models if fleet.is_advisory(name)}


def _advisory_id() -> str:
    advisory = _advisory_models(load_fleet(FLEET_YML))
    if not advisory:
        pytest.skip("no model is marked `gating: advisory` — nothing to pin")
    return sorted(advisory)[0]


@pytest.fixture
def fy(stand_in):
    """A disposable copy of the stand-in fleet (its floors file is rewritten)."""
    return stand_in("b4-ratchet/fleet") / "fleet.yml"


def _dump(path: Path, items_by_model: dict) -> Path:
    return write_report(path, items_by_model,
                        generated_at="2026-08-17T00:00:00+00:00")


# --------------------------------------------------------------------------
# The fleet flag itself
# --------------------------------------------------------------------------

def test_advisory_clients_matches_the_registry():
    """The advisory reader is the ONE reader every gate uses, so it must agree
    with the yml entry for entry."""
    expected = {
        name for name, entry in _raw_models().items()
        if isinstance(entry, dict) and entry.get("gating") == "advisory"
    }
    assert _advisory_models(load_fleet(FLEET_YML)) == expected


def test_every_advisory_client_states_a_reason():
    """`gating: advisory` without `gating_reason` is silence where a decision
    belongs."""
    load_fleet(FLEET_YML)   # the loader accepts the `gating_reason` key
    missing = [
        name for name, entry in _raw_models().items()
        if isinstance(entry, dict) and entry.get("gating") == "advisory"
        and not entry.get("gating_reason")
    ]
    assert not missing, (
        f"advisory models with no gating_reason: {missing}. State why the "
        f"model is not gated and what re-gates it.")


def test_advisory_model_paths_resolve_on_disk():
    """A stale advisory path fails OPEN (nobody gates the model), so it must
    resolve."""
    fleet = load_fleet(FLEET_YML)
    for name in sorted(_advisory_models(fleet)):
        path = fleet.models[name]
        assert (path / "definition").is_dir(), (
            f"{name}: advisory model path '{path}' has no definition/ directory")


def test_stale_advisory_path_is_a_load_error(stand_in):
    """Negative control for the test above (C-advisory-model-gating Detection
    3): the loader refuses a model path without definition/."""
    root = stand_in("b4-ratchet/fleet")
    fy = root / "fleet.yml"
    fy.write_text(fy.read_text(encoding="utf-8").replace(
        "pilot_db/Pilot.SemanticModel", "pilot_db/Gone.SemanticModel"),
        encoding="utf-8")
    with pytest.raises(DriftDoctorError):
        load_fleet(fy)


# --------------------------------------------------------------------------
# The ratchet half — exit codes
# --------------------------------------------------------------------------

def test_advisory_client_rise_does_not_fail_the_ratchet(fy, tmp_path):
    """The blocking behaviour this exists to end: one more item against the
    alpha model turned the fleet gate red on somebody else's change."""
    cid = _advisory_id()
    dump = _dump(tmp_path / "d.json", {
        cid: [item("column.missing", f"c{i}") for i in range(42)],
        GATED_CLIENT: [],
    })
    write_floors(fy, {
        cid: {"total": 41, "by_category": {"column": 41}},
        GATED_CLIENT: {"total": 0, "by_category": {}}})

    r = check(fy, dump)

    assert r.exit_code == 0, r.output
    assert "ADVISORY" in r.stdout
    # Reported, not swallowed — the count must still be legible.
    assert f"{cid}: TOTAL rose 41 -> 42" in r.stdout
    assert "DRIFT RATCHET RED" not in r.stdout


def test_a_gated_client_is_still_hard_in_the_same_run(fy, tmp_path):
    """Demotion is per model. A run carrying BOTH an advisory rise and a real
    one must fail on the real one and still print the advisory."""
    cid = _advisory_id()
    dump = _dump(tmp_path / "d.json", {
        cid: [item("measure.missing", "m")],
        GATED_CLIENT: [item("measure.missing", "m")],
    })
    write_floors(fy, {
        cid: {"total": 0, "by_category": {}},
        GATED_CLIENT: {"total": 0, "by_category": {}}})

    r = check(fy, dump)

    assert r.exit_code == 1, r.output
    assert f"{GATED_CLIENT}: TOTAL rose 0 -> 1" in r.stdout
    assert "ADVISORY" in r.stdout


def test_advisory_resurrection_is_reported_but_not_fatal(fy, tmp_path):
    """`retire.*` is hard zero for every GATED model; an advisory model is not
    sync-managed, so nobody can clear a resurrection there by cascading. It
    must still be named in full — demoted, never hidden. The stand-in fleet's
    allowlist omits `retire.*` (the allowlist never decides ratchet counting)."""
    cid = _advisory_id()
    dump = _dump(tmp_path / "d.json", {
        cid: [item("retire.table", "Google Ads Keywords Quality")]})
    write_floors(fy, {cid: {"total": 0, "by_category": {}}})

    r = check(fy, dump)

    assert r.exit_code == 0, r.output
    assert "RESURRECTION" in r.stdout
    assert "Google Ads Keywords Quality" in r.stdout
    assert "DRIFT RATCHET RED" not in r.stdout


def test_resurrection_on_a_gated_client_is_untouched(fy, tmp_path):
    """The flag must not have widened the retirement escape hatch by accident
    (allowlist omits `retire.*`, the floor records `retire: 99`)."""
    dump = _dump(tmp_path / "d.json", {
        GATED_CLIENT: [item("retire.table", "Google Ads Keywords Quality")]})
    write_floors(fy, {GATED_CLIENT: {"total": 99, "by_category": {"retire": 99}}})

    r = check(fy, dump)

    assert r.exit_code == 1, r.output
    assert "DRIFT RATCHET RED — RESURRECTION" in r.stdout

"""drift-doctor ratchet: floors only go down; resurrections are hard zero."""

import json

from click.testing import CliRunner
from conftest import edit_file, edit_fleet_yml, retire_everything_from_template

from tmdl_drift_doctor.cli import main
from tmdl_drift_doctor.fleet import load_fleet


def _cli(fleet_dir, *args):
    n = 2 if args[0] == "ratchet" else 1     # `ratchet check` is a subcommand
    return CliRunner().invoke(
        main, [*args[:n], "--fleet", str(fleet_dir / "fleet.yml"), *args[n:]])


def _floors(fleet_dir):
    return json.loads((fleet_dir / ".drift-doctor" / "floors.json")
                      .read_text(encoding="utf-8"))


def _seeded(fleet_dir):
    _cli(fleet_dir, "capture")
    r = _cli(fleet_dir, "ratchet", "tighten")
    assert r.exit_code == 0, r.output
    return _floors(fleet_dir)


def test_check_requires_floors(fleet_dir):
    _cli(fleet_dir, "capture")
    r = _cli(fleet_dir, "ratchet", "check")
    assert r.exit_code == 2 and "no floors file" in r.output


def test_seed_then_check_passes(fleet_dir):
    floors = _seeded(fleet_dir)
    assert floors["version"] == 2
    assert floors["models"]["alpha"]["by_category"]["measure"] >= 1
    # advisories never count
    assert "extra" not in floors["models"]["alpha"]["by_category"]
    r = _cli(fleet_dir, "ratchet", "check")
    assert r.exit_code == 0, r.output


def test_a_rise_fails_and_tighten_never_raises(fleet_dir):
    before = _seeded(fleet_dir)
    vf = (fleet_dir / "franchises" / "bravo" / "GymChain.SemanticModel"
          / "definition" / "tables" / "Visits.tmdl")
    edit_file(vf, "measure 'Total Visits' = COUNTROWS(Visits)",
              "measure 'Total Visits' = COUNTROWS(Visits) + 0")
    r = _cli(fleet_dir, "ratchet", "check")
    assert r.exit_code == 1
    assert "bravo: TOTAL rose" in r.output
    assert "bravo: category 'measure' rose" in r.output
    assert "DRIFT RATCHET RED" in r.output
    _cli(fleet_dir, "ratchet", "tighten")
    assert _floors(fleet_dir)["models"] == before["models"]


def test_tighten_lowers_after_remediation(fleet_dir):
    _seeded(fleet_dir)
    _cli(fleet_dir, "remediate", "--yes", "--kind", "measure.")
    r = _cli(fleet_dir, "ratchet", "check")
    assert r.exit_code == 0 and "[improved] alpha" in r.output
    _cli(fleet_dir, "ratchet", "tighten")
    assert "measure" not in _floors(fleet_dir)["models"]["alpha"]["by_category"]


def test_resurrection_is_hard_zero(fleet_dir):
    _seeded(fleet_dir)
    fleet = load_fleet(fleet_dir / "fleet.yml")
    retire_everything_from_template(fleet)
    _cli(fleet_dir, "capture")
    r = _cli(fleet_dir, "ratchet", "tighten")
    floors = _floors(fleet_dir)
    assert not any(k == "retire" for m in floors["models"].values()
                   for k in m["by_category"])
    r = _cli(fleet_dir, "ratchet", "check")
    assert r.exit_code == 1
    assert "RESURRECTION" in r.output
    assert "retire.measure Visits[Peak Hour Visits]" in r.output
    assert "DRIFT RATCHET RED — RESURRECTION" in r.output


def test_refuses_filtered_scoped_or_errored_reports(fleet_dir):
    _seeded(fleet_dir)
    out = fleet_dir / "scoped.json"
    out.write_text(_cli(fleet_dir, "detect", "--json", "--model", "alpha").output,
                   encoding="utf-8")
    assert _cli(fleet_dir, "ratchet", "check", "--from-json", str(out)).exit_code == 0
    # a scoped report may lower its own model's floor, never prune
    r = _cli(fleet_dir, "ratchet", "tighten", "--from-json", str(out))
    assert r.exit_code == 0, r.output
    r = _cli(fleet_dir, "ratchet", "tighten", "--from-json", str(out),
             "--prune")
    assert r.exit_code == 2 and "--prune requires a FLEET dump" in r.output

    out.write_text(_cli(fleet_dir, "detect", "--json", "--kind", "measure.").output,
                   encoding="utf-8")
    r = _cli(fleet_dir, "ratchet", "check", "--from-json", str(out))
    assert r.exit_code == 2 and "category-filtered" in r.output

    report = json.loads(_cli(fleet_dir, "detect", "--json").output)
    report["errors"] = [{"model": "bravo", "error": "boom"}]
    out.write_text(json.dumps(report), encoding="utf-8")
    r = _cli(fleet_dir, "ratchet", "tighten", "--from-json", str(out))
    assert r.exit_code == 2 and "provider error" in r.output


def test_advisory_models_never_fail_the_check(fleet_dir):
    _seeded(fleet_dir)
    edit_fleet_yml(fleet_dir, lambda d: d["models"].__setitem__(
        "bravo", {"path": d["models"]["bravo"], "gating": "advisory"}))
    vf = (fleet_dir / "franchises" / "bravo" / "GymChain.SemanticModel"
          / "definition" / "tables" / "Visits.tmdl")
    edit_file(vf, "measure 'Total Visits' = COUNTROWS(Visits)",
              "measure 'Total Visits' = COUNTROWS(Visits) + 0")
    r = _cli(fleet_dir, "ratchet", "check")
    assert r.exit_code == 0, r.output
    assert "ADVISORY" in r.output and "bravo: TOTAL rose" in r.output
    assert "DRIFT RATCHET RED" not in r.output


def test_new_model_needs_allow_new_and_prune_drops_old(fleet_dir):
    _seeded(fleet_dir)
    path = fleet_dir / ".drift-doctor" / "floors.json"
    doc = _floors(fleet_dir)
    doc["models"]["charlie"] = doc["models"].pop("bravo")
    path.write_text(json.dumps(doc), encoding="utf-8")
    r = _cli(fleet_dir, "ratchet", "check")
    assert r.exit_code == 1 and "bravo: no floor recorded" in r.output
    _cli(fleet_dir, "ratchet", "tighten")
    assert "bravo" not in _floors(fleet_dir)["models"]
    _cli(fleet_dir, "ratchet", "tighten", "--allow-new", "--prune")
    assert set(_floors(fleet_dir)["models"]) == {"alpha", "bravo"}

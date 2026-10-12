"""drift-doctor audit (reverted fixes exit 4) and review (the human queue)."""

from click.testing import CliRunner
from conftest import edit_file, retire_everything_from_template

from tmdl_drift_doctor import tmdl_edit as te
from tmdl_drift_doctor.cli import main
from tmdl_drift_doctor.fleet import load_fleet


def _cli(fleet_dir, *args):
    return CliRunner().invoke(main, [args[0], "--fleet",
                                     str(fleet_dir / "fleet.yml"), *args[1:]])


def _defn(fleet_dir, model):
    return fleet_dir / "franchises" / model / "GymChain.SemanticModel" / "definition"


def test_audit_clean_after_remediation(fleet_dir):
    _cli(fleet_dir, "capture")
    _cli(fleet_dir, "remediate")
    r = _cli(fleet_dir, "audit")
    assert r.exit_code == 0, r.output
    assert "reverted=0" in r.output


def test_audit_catches_a_removed_addition(fleet_dir):
    _cli(fleet_dir, "capture")
    _cli(fleet_dir, "remediate")
    mf = _defn(fleet_dir, "alpha") / "tables" / "Members.tmdl"
    mf.write_text(te.remove_block(mf.read_text(encoding="utf-8"), "measure",
                                  "New Members #"), encoding="utf-8")
    r = _cli(fleet_dir, "audit")
    assert r.exit_code == 4
    assert "[REVERTED] alpha: measure.missing Members[New Members #]" in r.output


def test_audit_catches_a_resurrected_retirement(fleet_dir):
    _cli(fleet_dir, "capture")
    fleet = load_fleet(fleet_dir / "fleet.yml")
    saved = retire_everything_from_template(fleet)
    _cli(fleet_dir, "capture")
    _cli(fleet_dir, "remediate", "--sync")
    assert _cli(fleet_dir, "audit").exit_code == 0
    vf = _defn(fleet_dir, "bravo") / "tables" / "Visits.tmdl"
    vf.write_text(te.insert_measure_block(vf.read_text(encoding="utf-8"),
                                          saved["measure_block"]),
                  encoding="utf-8")
    r = _cli(fleet_dir, "audit")
    assert r.exit_code == 4
    assert "retire.measure Visits[Peak Hour Visits]" in r.output


def test_audit_treats_a_later_revival_as_superseded(fleet_dir):
    _cli(fleet_dir, "capture")
    fleet = load_fleet(fleet_dir / "fleet.yml")
    saved = retire_everything_from_template(fleet)
    _cli(fleet_dir, "capture")
    _cli(fleet_dir, "remediate", "--sync")
    vf = _defn(fleet_dir, "bravo") / "tables" / "Visits.tmdl"
    vf.write_text(te.insert_measure_block(vf.read_text(encoding="utf-8"),
                                          saved["measure_block"]),
                  encoding="utf-8")
    _cli(fleet_dir, "ledger", "--revive", "Visits[Peak Hour Visits]",
         "--kind", "measure", "--model", "bravo")
    r = _cli(fleet_dir, "audit")
    assert r.exit_code == 0, r.output
    assert "superseded" in r.output


def test_review_lists_everything_routed_to_a_human(fleet_dir):
    _cli(fleet_dir, "capture")
    fleet = load_fleet(fleet_dir / "fleet.yml")
    retire_everything_from_template(fleet)
    _cli(fleet_dir, "capture")
    out = fleet_dir / "REVIEW.md"
    r = _cli(fleet_dir, "review", "--out", str(out))
    assert r.exit_code == 0, r.output
    text = out.read_text(encoding="utf-8")
    assert "missing rule" in text
    assert "### Blocked retirements" in text
    assert "`retire.column` Classes[Capacity]" in text
    assert "`retire.table` Promotions" in text
    assert "`measure.wrong_host` Visits[Visits per Member]" in text
    assert "`measure.duplicate_host` Members[Visits per Member]" in text
    assert "Members[JoinDate] -> Promotions[StartDate]" in text   # the blocker
    assert "`extra.measure` Members[Alpha Loyalty Score]" in text
    # the review is a dry run: nothing changed
    assert not (fleet_dir / ".drift-doctor" / "ledger.jsonl").read_text(
        encoding="utf-8").count('"remediated"')


def test_review_lists_precondition_skips(fleet_dir):
    _cli(fleet_dir, "capture")
    mf = _defn(fleet_dir, "alpha") / "tables" / "Members.tmdl"
    edit_file(mf, "\tpartition Members = m",
              "\tmeasure 'Alpha VPM x2' = Members[Visits per Member] * 2\n"
              "\t\tlineageTag: aaaa1111-1111-4111-8111-000000000098\n\n"
              "\tpartition Members = m")
    out = fleet_dir / "REVIEW.md"
    _cli(fleet_dir, "review", "--out", str(out))
    text = out.read_text(encoding="utf-8")
    assert "### Fixes waiting on a precondition" in text
    assert "dangling" in text

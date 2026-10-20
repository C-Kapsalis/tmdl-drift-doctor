"""R3: what `remediate` does without a terminal (G-interactive-confirm)."""
from __future__ import annotations

from click.testing import CliRunner
from conftest import retire_everything_from_template, tree_state

from tmdl_drift_doctor.cli import main
from tmdl_drift_doctor.fleet import load_fleet


def _cli(fleet_dir, *args):
    return CliRunner().invoke(main, [args[0], "--fleet",
                                     str(fleet_dir / "fleet.yml"), *args[1:]])


def _retired(fleet_dir):
    _cli(fleet_dir, "capture")
    retire_everything_from_template(load_fleet(fleet_dir / "fleet.yml"))
    _cli(fleet_dir, "capture")


def test_plain_run_without_a_terminal_writes_nothing(fleet_dir):
    _cli(fleet_dir, "capture")
    before = tree_state(fleet_dir)
    r = _cli(fleet_dir, "remediate")
    assert tree_state(fleet_dir) == before
    assert "non-interactive" in r.output and "applied=0" in r.output


def test_yes_applies_only_the_named_kinds(fleet_dir):
    _cli(fleet_dir, "capture")
    r = _cli(fleet_dir, "remediate", "--yes", "--kind", "measure.missing")
    applied = [ln for ln in r.output.splitlines() if ln.startswith("[applied]")]
    assert applied and all("measure.missing" in ln for ln in applied)


def test_yes_never_deletes_even_with_sync(fleet_dir):
    _retired(fleet_dir)
    r = _cli(fleet_dir, "remediate", "--sync", "--yes", "--kind", "retire")
    assert "applied=0" in r.output and "skipped_destructive=" in r.output
    assert "skipped_destructive=0" not in r.output


def test_confirmed_deletions_run_without_a_terminal(fleet_dir):
    _retired(fleet_dir)
    r = _cli(fleet_dir, "remediate", "--sync", "--confirm-deletions",
             "--kind", "retire.measure")
    assert "[applied]" in r.output and "retire.measure" in r.output


def test_confirm_deletions_needs_sync(fleet_dir):
    _cli(fleet_dir, "capture")
    r = _cli(fleet_dir, "remediate", "--confirm-deletions")
    assert r.exit_code == 2

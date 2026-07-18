"""End-to-end CLI smoke tests via click's runner."""

import json
import os
import subprocess
import sys

from click.testing import CliRunner

from tmdl_drift_doctor.cli import main


def _invoke(fleet_dir, command, *args):
    runner = CliRunner()
    return runner.invoke(
        main, [command, "--fleet", str(fleet_dir / "fleet.yml"), *args])


def test_capture_then_detect_then_remediate(fleet_dir):
    r = _invoke(fleet_dir, "capture")
    assert r.exit_code == 0, r.output
    assert "baseline written" in r.output

    r = _invoke(fleet_dir, "detect")
    assert r.exit_code == 1          # remediable drift exists -> CI-friendly 1
    assert "measure.expression_drift" in r.output

    r = _invoke(fleet_dir, "detect", "--json")
    assert json.loads(r.output)      # machine-readable

    r = _invoke(fleet_dir, "remediate", "--dry-run")
    assert r.exit_code == 0, r.output
    assert "would apply" in r.output and "---" in r.output  # diffs rendered

    r = _invoke(fleet_dir, "remediate")
    assert r.exit_code == 0, r.output
    assert "applied=" in r.output

    r = _invoke(fleet_dir, "detect")
    assert r.exit_code == 0          # only the advisory extra remains
    assert "[advisory]" in r.output


def test_detect_clean_message_is_scoped_under_filters(fleet_dir):
    """A filtered detect that finds nothing must not claim the whole fleet
    is clean — other findings may exist outside the filter."""
    _invoke(fleet_dir, "capture")
    r = _invoke(fleet_dir, "detect", "--kind", "retire.")
    assert r.exit_code == 0
    assert "within kind prefix 'retire.'" in r.output
    assert "fleet matches the template" not in r.output


def test_cli_survives_legacy_console_encoding(fleet_dir):
    """detect's report contains non-cp1252 glyphs; a legacy Windows console
    must get '?' substitutions, never a UnicodeEncodeError crash."""
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    args = [sys.executable, "-m", "tmdl_drift_doctor.cli"]
    fleet = ["--fleet", str(fleet_dir / "fleet.yml")]
    subprocess.run([*args, "capture", *fleet], env=env, check=True,
                   capture_output=True)
    r = subprocess.run([*args, "detect", *fleet], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 1, r.stderr        # drift found, not a crash
    assert "UnicodeEncodeError" not in r.stderr
    assert "finding(s)" in r.stdout


def test_detect_refuses_stale_baseline(fleet_dir):
    _invoke(fleet_dir, "capture")
    vf = (fleet_dir / "template" / "GymChain.SemanticModel" / "definition"
          / "tables" / "Visits.tmdl")
    vf.write_text(vf.read_text(encoding="utf-8").replace(
        "COUNTROWS(Visits)", "COUNTROWS(VALUES(Visits))", 1), encoding="utf-8")
    r = _invoke(fleet_dir, "detect")
    assert r.exit_code != 0
    assert "stale" in r.output
    r = _invoke(fleet_dir, "detect", "--allow-stale")
    assert "stale" not in r.output


def test_ledger_show_and_revive(fleet_dir):
    _invoke(fleet_dir, "capture")
    r = _invoke(fleet_dir, "ledger")
    assert "empty" in r.output

    # retire a measure from the template, recapture, then revive it
    vf = (fleet_dir / "template" / "GymChain.SemanticModel" / "definition"
          / "tables" / "Visits.tmdl")
    from tmdl_drift_doctor import tmdl_edit as te
    vf.write_text(te.remove_block(vf.read_text(encoding="utf-8"),
                                  "measure", "Peak Hour Visits"),
                  encoding="utf-8")
    r = _invoke(fleet_dir, "capture")
    assert "retired measure: Visits[Peak Hour Visits]" in r.output

    r = _invoke(fleet_dir, "ledger", "--revive", "Visits[Peak Hour Visits]",
                "--kind", "measure", "--model", "alpha", "--note", "kept KPI")
    assert r.exit_code == 0, r.output
    assert "will not be re-removed" in r.output

    r = _invoke(fleet_dir, "ledger", "--json")
    events = [json.loads(l) for l in r.output.strip().splitlines()]
    assert [e["event"] for e in events] == ["retired", "revived"]

    # reviving something that isn't retired is an error
    r = _invoke(fleet_dir, "ledger", "--revive", "Nope[Nothing]",
                "--kind", "measure")
    assert r.exit_code != 0

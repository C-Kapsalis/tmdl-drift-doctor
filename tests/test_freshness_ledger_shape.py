"""A ledger line that is not an object is refused.

The freshness command reads two ledgers: the one the branch was cut from
and the one on the main line. A ledger is a line-per-event file, and a line
may be hand-edited or truncated by a pipeline. A line that holds valid JSON
but is not an object — an array, a string, a number — cannot be read as an
event, so the command must refuse it the way it refuses a malformed line:
exit 2, a message on standard error, and never a Python traceback.
"""

from __future__ import annotations

import json

import pytest

from tmdl_drift_doctor.freshness import FreshnessError


def _ledger(path, lines):
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def _event(kind="table", ref="Promotions", event="retired"):
    return json.dumps({"event": event, "kind": kind, "ref": ref,
                       "ts": "2026-08-07T00:00:00+00:00"})


def test_a_ledger_line_holding_an_array_is_refused(tmp_path):
    """DD-11: valid JSON, wrong shape."""
    from tmdl_drift_doctor import freshness
    base = _ledger(tmp_path / "base.jsonl", [_event()])
    head = _ledger(tmp_path / "head.jsonl", [_event(), '["not", "an", "event"]'])

    with pytest.raises(FreshnessError):
        freshness.in_force(head)


def test_a_ledger_line_holding_a_string_is_refused(tmp_path):
    """DD-11: a bare string is not an event either."""
    from tmdl_drift_doctor import freshness
    head = _ledger(tmp_path / "head.jsonl", [_event(), '"just a string"'])

    with pytest.raises(FreshnessError):
        freshness.in_force(head)


def test_a_ledger_line_holding_a_number_is_refused(tmp_path):
    """DD-11: a number is not an event either."""
    from tmdl_drift_doctor import freshness
    head = _ledger(tmp_path / "head.jsonl", [_event(), "42"])

    with pytest.raises(FreshnessError):
        freshness.in_force(head)


def test_the_command_exits_2_on_a_ledger_line_that_is_not_an_object(tmp_path):
    """DD-11: the CLI refuses with the configuration exit code."""
    from click.testing import CliRunner
    from tmdl_drift_doctor.cli import main
    base = _ledger(tmp_path / "base.jsonl", [_event()])
    head = _ledger(tmp_path / "head.jsonl", [_event(), '["not", "an", "event"]'])

    r = CliRunner().invoke(main, [
        "freshness", "--base-ledger", str(base), "--head-ledger", str(head)])
    assert r.exit_code == 2, r.output
    assert r.stderr.strip(), "the refusal must say something on stderr"
    assert "Traceback" not in r.output


def test_a_malformed_ledger_line_is_still_refused_the_same_way(tmp_path):
    """The other side of the rule: nothing about the common path changed."""
    from click.testing import CliRunner
    from tmdl_drift_doctor.cli import main
    base = _ledger(tmp_path / "base.jsonl", [_event()])
    head = _ledger(tmp_path / "head.jsonl", [_event(), "{not json"])

    r = CliRunner().invoke(main, [
        "freshness", "--base-ledger", str(base), "--head-ledger", str(head)])
    assert r.exit_code == 2, r.output
    assert "Traceback" not in r.output


def test_a_well_formed_ledger_is_still_read(tmp_path):
    """A ledger of objects is read as before."""
    from tmdl_drift_doctor import freshness
    head = _ledger(tmp_path / "head.jsonl", [_event()])
    state = freshness.in_force(head)
    assert ("table", "Promotions") in state

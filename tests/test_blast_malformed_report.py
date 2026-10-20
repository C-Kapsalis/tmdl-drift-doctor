"""A detection report the blast-radius command cannot read is refused.

`drift-doctor diff --base BASE --head HEAD` compares two saved detection
reports. The reports are artifacts: they may be hand-edited, truncated by a
pipeline, or written by another version of the tool. Whatever they are, the
command must refuse them as a configuration problem: exit 2, a message on
standard error, and never a Python traceback.
"""

from __future__ import annotations

import json


def _report(path, findings):
    doc = {
        "schema_version": 3, "generated_at": "2026-08-07T00:00:00+00:00",
        "template_tree": "cafe1234", "errors": [],
        "scope": {"fleet": True, "models": None, "alternate_model": None,
                  "targets": ["spinout-10"], "kind_filter": None,
                  "overrides": None},
        "models": {"spinout-10": {"gating": "enforced", "findings": findings,
                                  "overridden": [], "errors": 0}},
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _finding(kind="measure.missing", ref="m1"):
    return {"kind": kind, "ref": ref, "detail": "", "remediable": True,
            "actionable": True, "advisory": False}


def _diff(base, head, out):
    from click.testing import CliRunner
    from tmdl_drift_doctor.cli import main
    return CliRunner().invoke(main, [
        "diff", "--base", str(base), "--head", str(head), "--out", str(out)])


def test_a_finding_that_is_not_an_object_is_refused(tmp_path):
    """DD-10: a string where a finding belongs cannot be read."""
    base = _report(tmp_path / "base.json", [_finding()])
    head = _report(tmp_path / "head.json", ["measure.missing m2"])
    out = tmp_path / "out.md"

    r = _diff(base, head, out)
    assert r.exit_code == 2, r.output
    assert r.stderr.strip(), "the refusal must say something on stderr"
    assert "Traceback" not in r.output


def test_a_finding_that_is_not_an_object_in_the_base_is_refused(tmp_path):
    """DD-10: both sides are read the same way."""
    base = _report(tmp_path / "base.json", ["measure.missing m1"])
    head = _report(tmp_path / "head.json", [_finding()])
    out = tmp_path / "out.md"

    r = _diff(base, head, out)
    assert r.exit_code == 2, r.output
    assert "Traceback" not in r.output


def test_findings_that_are_not_a_list_are_refused(tmp_path):
    """DD-10: a findings block that is not a list cannot be read."""
    base = _report(tmp_path / "base.json", [_finding()])
    head = _report(tmp_path / "head.json", [_finding()])
    head_doc = json.loads(head.read_text(encoding="utf-8"))
    head_doc["models"]["spinout-10"]["findings"] = {"kind": "measure.missing"}
    head.write_text(json.dumps(head_doc), encoding="utf-8")
    out = tmp_path / "out.md"

    r = _diff(base, head, out)
    assert r.exit_code == 2, r.output
    assert "Traceback" not in r.output


def test_a_report_whose_top_level_is_not_an_object_is_refused(tmp_path):
    """DD-10: the same rule as the ratchet's."""
    base = tmp_path / "base.json"
    base.write_text("[]", encoding="utf-8")
    head = _report(tmp_path / "head.json", [_finding()])
    out = tmp_path / "out.md"

    r = _diff(base, head, out)
    assert r.exit_code == 2, r.output
    assert "Traceback" not in r.output


def test_a_well_formed_pair_still_works(tmp_path):
    """The other side of the rule: nothing about the common path changed."""
    base = _report(tmp_path / "base.json", [_finding()])
    head = _report(tmp_path / "head.json", [])
    out = tmp_path / "out.md"

    r = _diff(base, head, out)
    assert r.exit_code == 0, r.output
    assert out.exists()

"""`diff` refuses a file that is not a detection report.

A JSON object without a `models` block, or a report in the old
`schema_version: 1` format, used to be read as a report with no findings:
the command exited 0 and called every finding on the other side new (or
resolved) drift, with only a scope warning. It is an input error: exit 2,
a message on standard error, nothing written.
"""

from __future__ import annotations

import json

import pytest
from conftest import drift


def _report(**over):
    doc = {
        "schema_version": 2, "generated_at": "2026-08-07T00:00:00+00:00",
        "template_tree": "cafe1234", "errors": [],
        "scope": {"fleet": True, "models": None, "alternate_model": None,
                  "targets": ["north"], "kind_filter": None,
                  "overrides": None},
        "models": {"north": {"gating": "enforced", "findings": [
            {"kind": "measure.missing", "ref": "Sales[Total]", "detail": "",
             "remediable": True, "actionable": True, "advisory": False}],
            "overridden": [], "errors": 0}},
    }
    doc.update(over)
    return {k: v for k, v in doc.items() if v is not ...}


def _write(path, doc):
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


@pytest.mark.parametrize("doc", [
    {"hello": 1},
    _report(models=...),
    _report(schema_version=1),
    _report(schema_version=...),
    _report(schema_version="2"),
], ids=["no-models", "models-missing", "schema-1", "no-schema",
        "schema-string"])
@pytest.mark.parametrize("side", ["base", "head"])
def test_a_non_report_is_refused(tmp_path, doc, side):
    good = _write(tmp_path / "good.json", _report())
    bad = _write(tmp_path / "bad.json", doc)
    base, head = (bad, good) if side == "base" else (good, bad)
    out = tmp_path / "out.md"
    r = drift("diff", "--base", base, "--head", head, "--out", out)
    assert r.exit_code == 2, r.output
    assert "Traceback" not in r.output
    assert "bad.json" in r.stderr and "detect --json" in r.stderr, r.stderr
    assert not out.exists()


def test_a_report_with_no_models_is_still_a_report(tmp_path):
    """An empty fleet's report is a report: `models` is there, just empty."""
    base = _write(tmp_path / "base.json", _report(models={}))
    head = _write(tmp_path / "head.json", _report())
    out = tmp_path / "out.md"
    r = drift("diff", "--base", base, "--head", head, "--out", out)
    assert r.exit_code == 0, r.output

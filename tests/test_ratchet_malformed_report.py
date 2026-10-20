"""A saved detection report the ratchet cannot read is a configuration error.

A CI job hands `ratchet check --from-json` an artifact. The artifact may be
hand-edited, truncated by a pipeline, or written by an older version of the
tool. Whatever it is, the ratchet must refuse it the way it refuses any
other configuration problem: exit 2, a message on standard error, and never
a Python traceback.

The shapes covered here are the ones that used to crash: a report whose top
level is not an object, a report whose scope block is not an object, and a
report whose findings are not a list of objects.
"""

from __future__ import annotations

import json

from port_helpers_b4_ratchet import (
    floors_path,
    item,
    make_fleet,
    write_floors,
    write_report,
)


def _fleet(tmp_path, names=("spinout-10", "spinout-02")):
    return make_fleet(tmp_path / "fleet", list(names))


def _floors(fy):
    write_floors(fy, {"spinout-10": {"total": 0, "by_category": {}},
                      "spinout-02": {"total": 0, "by_category": {}}})
    return floors_path(fy)


def _write(path, doc):
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_a_report_whose_top_level_is_not_an_object_is_refused(tmp_path):
    """DD-9: a list at the top level is a configuration problem."""
    from conftest import drift
    fy = _fleet(tmp_path)
    _floors(fy)
    dump = _write(tmp_path / "d.json", [item("measure.missing", "m1")])

    r = drift("ratchet check", "--from-json", dump, fleet=fy)
    assert r.exit_code == 2, r.output
    assert r.stderr.strip(), "the refusal must say something on stderr"
    assert "Traceback" not in r.output


def test_a_report_whose_scope_is_not_an_object_is_refused(tmp_path):
    """DD-9: a list in the scope block is the same problem."""
    from conftest import drift
    fy = _fleet(tmp_path)
    _floors(fy)
    dump = _write(tmp_path / "d.json", {
        "schema_version": 3, "generated_at": "2026-08-07T00:00:00+00:00",
        "template_tree": "cafe1234", "errors": [], "scope": ["spinout-10"],
        "models": {"spinout-10": {"gating": "enforced", "findings": [],
                                  "overridden": [], "errors": 0}},
    })

    r = drift("ratchet check", "--from-json", dump, fleet=fy)
    assert r.exit_code == 2, r.output
    assert r.stderr.strip(), "the refusal must say something on stderr"
    assert "Traceback" not in r.output


def test_a_report_whose_findings_are_not_a_list_is_refused(tmp_path):
    """DD-9: findings that are not a list cannot be counted."""
    from conftest import drift
    fy = _fleet(tmp_path)
    _floors(fy)
    dump = _write(tmp_path / "d.json", {
        "schema_version": 3, "generated_at": "2026-08-07T00:00:00+00:00",
        "template_tree": "cafe1234", "errors": [],
        "scope": {"fleet": True, "models": None, "alternate_model": None,
                  "targets": ["spinout-10"], "kind_filter": None,
                  "overrides": None},
        "models": {"spinout-10": {"gating": "enforced",
                                  "findings": {"kind": "measure.missing"},
                                  "overridden": [], "errors": 0}},
    })

    r = drift("ratchet check", "--from-json", dump, fleet=fy)
    assert r.exit_code == 2, r.output
    assert r.stderr.strip(), "the refusal must say something on stderr"
    assert "Traceback" not in r.output


def test_a_report_whose_findings_hold_a_string_is_refused(tmp_path):
    """DD-9: a finding that is not an object cannot be counted."""
    from conftest import drift
    fy = _fleet(tmp_path)
    _floors(fy)
    dump = _write(tmp_path / "d.json", {
        "schema_version": 3, "generated_at": "2026-08-07T00:00:00+00:00",
        "template_tree": "cafe1234", "errors": [],
        "scope": {"fleet": True, "models": None, "alternate_model": None,
                  "targets": ["spinout-10"], "kind_filter": None,
                  "overrides": None},
        "models": {"spinout-10": {"gating": "enforced",
                                  "findings": ["measure.missing m1"],
                                  "overridden": [], "errors": 0}},
    })

    r = drift("ratchet check", "--from-json", dump, fleet=fy)
    assert r.exit_code == 2, r.output
    assert r.stderr.strip(), "the refusal must say something on stderr"
    assert "Traceback" not in r.output


def test_a_report_whose_models_are_not_a_mapping_is_refused(tmp_path):
    """DD-9: models that are not a mapping cannot be counted either."""
    from conftest import drift
    fy = _fleet(tmp_path)
    _floors(fy)
    dump = _write(tmp_path / "d.json", {
        "schema_version": 3, "generated_at": "2026-08-07T00:00:00+00:00",
        "template_tree": "cafe1234", "errors": [],
        "scope": {"fleet": True, "models": None, "alternate_model": None,
                  "targets": ["spinout-10"], "kind_filter": None,
                  "overrides": None},
        "models": ["spinout-10"],
    })

    r = drift("ratchet check", "--from-json", dump, fleet=fy)
    assert r.exit_code == 2, r.output
    assert "Traceback" not in r.output


def test_the_tighten_refuses_a_report_it_cannot_read_too(tmp_path):
    """DD-9: the tighten reads the same report."""
    from conftest import drift
    fy = _fleet(tmp_path)
    _floors(fy)
    dump = _write(tmp_path / "d.json", [item("measure.missing", "m1")])

    r = drift("ratchet tighten", "--from-json", dump, fleet=fy)
    assert r.exit_code == 2, r.output
    assert "Traceback" not in r.output


def test_a_well_formed_report_is_still_accepted(tmp_path):
    """The other side of the rule: nothing about the common path changed."""
    from conftest import drift
    fy = _fleet(tmp_path)
    _floors(fy)
    dump = write_report(tmp_path / "d.json", {"spinout-10": [], "spinout-02": []})

    r = drift("ratchet check", "--from-json", dump, fleet=fy)
    assert r.exit_code == 0, r.output

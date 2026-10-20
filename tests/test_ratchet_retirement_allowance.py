"""A floors file that grants a retirement allowance is a configuration error.

Resurrections are hard zero: an object the ledger proves was retired from
the template must never come back, and a floor must never license it. The
tool never writes such a key, so one in the file is a hand edit — and a
hand edit that a CI job must not read as ordinary drift.

Both the ratchet check and the ratchet tighten must refuse: exit 2, the
model and the value named on standard error, and the floors file left
byte-for-byte alone. A `[RISE]` line on standard output would read as
backlog growth, which is exactly the misreading the rule exists to prevent.
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


def _report(tmp_path, models=None):
    return write_report(tmp_path / "d.json", models or {"spinout-10": [],
                                                        "spinout-02": []})


def test_the_check_exits_2_on_a_retirement_allowance(tmp_path):
    """DD-7: the check refuses, naming the model and the value."""
    from conftest import drift
    fy = _fleet(tmp_path)
    dump = _report(tmp_path)
    write_floors(fy, {"spinout-10": {"total": 0,
                                      "by_category": {"retire": 3}},
                      "spinout-02": {"total": 0, "by_category": {}}})

    r = drift("ratchet check", "--from-json", dump, fleet=fy)
    assert r.exit_code == 2, r.output
    assert "spinout-10" in r.stderr
    assert "retire" in r.stderr
    assert "3" in r.stderr


def test_the_check_does_not_report_it_as_a_rise(tmp_path):
    """DD-7: a configuration error must not read as backlog growth."""
    from conftest import drift
    fy = _fleet(tmp_path)
    dump = _report(tmp_path)
    write_floors(fy, {"spinout-10": {"total": 0,
                                      "by_category": {"retire": 3}},
                      "spinout-02": {"total": 0, "by_category": {}}})

    r = drift("ratchet check", "--from-json", dump, fleet=fy)
    assert "[RISE]" not in r.stdout, r.output
    assert "DRIFT RATCHET RED" not in r.stdout, r.output


def test_the_tighten_exits_2_on_a_retirement_allowance(tmp_path):
    """DD-7: the tighten refuses too."""
    from conftest import drift
    fy = _fleet(tmp_path)
    dump = _report(tmp_path)
    write_floors(fy, {"spinout-10": {"total": 0,
                                      "by_category": {"retire": 3}},
                      "spinout-02": {"total": 0, "by_category": {}}})

    r = drift("ratchet tighten", "--from-json", dump, fleet=fy)
    assert r.exit_code == 2, r.output
    assert "spinout-10" in r.stderr
    assert "retire" in r.stderr


def test_the_floors_file_is_left_byte_identical(tmp_path):
    """DD-7: a refused run writes nothing."""
    from conftest import drift
    fy = _fleet(tmp_path)
    dump = _report(tmp_path)
    write_floors(fy, {"spinout-10": {"total": 0,
                                      "by_category": {"retire": 3}},
                      "spinout-02": {"total": 0, "by_category": {}}})
    before = floors_path(fy).read_bytes()

    drift("ratchet check", "--from-json", dump, fleet=fy)
    drift("ratchet tighten", "--from-json", dump, fleet=fy)

    assert floors_path(fy).read_bytes() == before


def test_the_legacy_per_kind_form_is_refused_too(tmp_path):
    """DD-7: a legacy `by_kind` file with a `retire.*` kind is the same
    accident, in the older shape."""
    from conftest import drift
    fy = _fleet(tmp_path)
    dump = _report(tmp_path)
    write_floors(fy, {"spinout-10": {"total": 3,
                                      "by_kind": {"retire.table": 3}},
                      "spinout-02": {"total": 0, "by_kind": {}}},
                  version=1)

    r = drift("ratchet check", "--from-json", dump, fleet=fy)
    assert r.exit_code == 2, r.output
    assert "spinout-10" in r.stderr
    assert "retire" in r.stderr


def test_a_retirement_allowance_in_a_scoped_report_is_refused(tmp_path):
    """DD-7: the rule does not depend on the report's scope."""
    from conftest import drift
    fy = _fleet(tmp_path)
    dump = write_report(tmp_path / "d.json",
                        {"spinout-10": [item("measure.missing", "m1")],
                         "spinout-02": []},
                        scope={"fleet": False, "models": ["spinout-10"],
                               "alternate_model": None,
                               "targets": ["spinout-10", "spinout-02"],
                               "kind_filter": None, "overrides": None})
    write_floors(fy, {"spinout-10": {"total": 0,
                                      "by_category": {"retire": 1}},
                      "spinout-02": {"total": 0, "by_category": {}}})

    r = drift("ratchet check", "--from-json", dump, fleet=fy)
    assert r.exit_code == 2, r.output
    assert "spinout-10" in r.stderr


def test_a_clean_floors_file_is_not_refused(tmp_path):
    """The other side of the rule: a floors file with no retirement head
    works as before."""
    from conftest import drift
    fy = _fleet(tmp_path)
    dump = _report(tmp_path)
    write_floors(fy, {"spinout-10": {"total": 0, "by_category": {}},
                      "spinout-02": {"total": 0, "by_category": {}}})

    r = drift("ratchet check", "--from-json", dump, fleet=fy)
    assert r.exit_code == 0, r.output

"""A no-op fleet tighten still refreshes the provenance.

A tighten that lowers nothing still measures the fleet. The floors file
must record that the fleet was measured now: the update timestamp and the
template tree at the top of the file are refreshed, and the last-tighten
block is rewritten with the report's scope, the models it covered, the
report's template tree and the time.

A scoped tighten must leave those two top-level values exactly as they
were: a one-model run must not re-stamp the whole file as if the fleet had
been measured at that commit.
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


def _read(fy):
    return json.loads(floors_path(fy).read_text(encoding="utf-8"))


def _tighten(fy, report, *extra):
    from conftest import drift
    return drift("ratchet tighten", "--from-json", report, *extra, fleet=fy)


def test_a_no_op_fleet_tighten_refreshes_the_update_timestamp(tmp_path):
    """DD-13: the file records that the fleet was measured now."""
    fy = _fleet(tmp_path)
    dump = write_report(tmp_path / "d.json", {"spinout-10": [], "spinout-02": []})
    write_floors(fy, {"spinout-10": {"total": 0, "by_category": {}},
                      "spinout-02": {"total": 0, "by_category": {}}},
                 header={"updated_at": "2020-01-01T00:00:00+00:00",
                         "template_tree": "oldtree01"})

    r = _tighten(fy, dump)
    assert r.exit_code == 0, r.output
    doc = _read(fy)
    assert doc["updated_at"] != "2020-01-01T00:00:00+00:00", (
        "a no-op fleet tighten did not refresh the update timestamp")


def test_a_no_op_fleet_tighten_refreshes_the_template_tree(tmp_path):
    """DD-13: the template tree at the top of the file is refreshed."""
    fy = _fleet(tmp_path)
    dump = write_report(tmp_path / "d.json", {"spinout-10": [], "spinout-02": []})
    write_floors(fy, {"spinout-10": {"total": 0, "by_category": {}},
                      "spinout-02": {"total": 0, "by_category": {}}},
                 header={"updated_at": "2020-01-01T00:00:00+00:00",
                         "template_tree": "oldtree01"})

    r = _tighten(fy, dump)
    assert r.exit_code == 0, r.output
    doc = _read(fy)
    assert doc["template_tree"] == "cafe1234", (
        "a no-op fleet tighten did not refresh the template tree")


def test_a_no_op_fleet_tighten_rewrites_the_last_tighten_block(tmp_path):
    """DD-13: the last-tighten block records the scope, the models, the
    report's template tree and the time."""
    fy = _fleet(tmp_path)
    dump = write_report(tmp_path / "d.json", {"spinout-10": [], "spinout-02": []})
    write_floors(fy, {"spinout-10": {"total": 0, "by_category": {}},
                      "spinout-02": {"total": 0, "by_category": {}}},
                 header={"updated_at": "2020-01-01T00:00:00+00:00",
                         "template_tree": "oldtree01"})

    r = _tighten(fy, dump)
    assert r.exit_code == 0, r.output
    doc = _read(fy)
    last = doc.get("last_tighten")
    assert last, "the last-tighten block is missing"
    assert last["scope"] == "fleet"
    assert last["models"] == ["spinout-02", "spinout-10"]
    assert last["template_tree"] == "cafe1234"
    assert last["at"]


def test_a_second_tighten_changes_the_timestamp_again(tmp_path):
    """DD-13: the block is rewritten, not left as it was."""
    fy = _fleet(tmp_path)
    dump = write_report(tmp_path / "d.json", {"spinout-10": [], "spinout-02": []})
    write_floors(fy, {"spinout-10": {"total": 0, "by_category": {}},
                      "spinout-02": {"total": 0, "by_category": {}}})
    assert _tighten(fy, dump).exit_code == 0
    first = _read(fy)

    # a pause, so a second-resolution timestamp differs
    import time
    time.sleep(1.1)

    assert _tighten(fy, dump).exit_code == 0
    second = _read(fy)
    assert second["updated_at"] != first["updated_at"], (
        "the second tighten did not refresh the update timestamp")
    assert second["last_tighten"]["at"] != first["last_tighten"]["at"], (
        "the second tighten did not rewrite the last-tighten block")


def test_a_scoped_tighten_leaves_the_top_level_values_alone(tmp_path):
    """DD-13: a one-model run must not re-stamp the whole file."""
    fy = _fleet(tmp_path)
    dump = write_report(
        tmp_path / "d.json",
        {"spinout-10": [item("measure.missing", "m1")], "spinout-02": []},
        scope={"fleet": False, "models": ["spinout-10"],
               "alternate_model": None,
               "targets": ["spinout-10", "spinout-02"],
               "kind_filter": None, "overrides": None})
    write_floors(fy, {"spinout-10": {"total": 0, "by_category": {}},
                      "spinout-02": {"total": 0, "by_category": {}}},
                 header={"updated_at": "2020-01-01T00:00:00+00:00",
                         "template_tree": "oldtree01"})

    r = _tighten(fy, dump)
    assert r.exit_code == 0, r.output
    doc = _read(fy)
    assert doc["updated_at"] == "2020-01-01T00:00:00+00:00", (
        "a scoped tighten changed the update timestamp")
    assert doc["template_tree"] == "oldtree01", (
        "a scoped tighten changed the template tree")
    assert doc["last_tighten"]["scope"] == "scoped"

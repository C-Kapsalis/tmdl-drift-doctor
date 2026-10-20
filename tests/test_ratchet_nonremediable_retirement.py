"""A retirement the sync cannot act on must still be named.

The ratchet counts findings a sync could act on. A retirement kind that no
sync can apply is still a retirement: the object the ledger proves was
retired from the template is back in a derived model, and nobody must
learn that from a report that says nothing.

So every retirement finding is named in the ratchet check output, whether or
not a sync could act on it. A retirement the sync cannot act on is reported
but does not fail the check — unless the include-advisory option is given,
which makes it fail.
"""

from __future__ import annotations

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


def _floors(fy):
    write_floors(fy, {"spinout-10": {"total": 0, "by_category": {}},
                      "spinout-02": {"total": 0, "by_category": {}}})
    return floors_path(fy)


def test_a_non_remediable_retirement_is_named(tmp_path):
    """DD-8: the object's reference appears in the output."""
    from conftest import drift
    fy = _fleet(tmp_path)
    dump = _report(tmp_path, {"spinout-10": [
        item("retire.table", "Plan Map", remediable=False)],
        "spinout-02": []})
    _floors(fy)

    r = drift("ratchet check", "--from-json", dump, fleet=fy)
    assert "Plan Map" in r.output, r.output


def test_a_non_remediable_retirement_does_not_fail_the_check(tmp_path):
    """DD-8: reported, but the run stays green without the option."""
    from conftest import drift
    fy = _fleet(tmp_path)
    dump = _report(tmp_path, {"spinout-10": [
        item("retire.table", "Plan Map", remediable=False)],
        "spinout-02": []})
    _floors(fy)

    r = drift("ratchet check", "--from-json", dump, fleet=fy)
    assert r.exit_code == 0, r.output


def test_a_non_remediable_retirement_fails_with_the_advisory_option(tmp_path):
    """DD-8: the option makes it fail."""
    from conftest import drift
    fy = _fleet(tmp_path)
    dump = _report(tmp_path, {"spinout-10": [
        item("retire.table", "Plan Map", remediable=False)],
        "spinout-02": []})
    _floors(fy)

    r = drift("ratchet check", "--from-json", dump, "--include-advisory",
              fleet=fy)
    assert r.exit_code == 1, r.output
    assert "Plan Map" in r.output


def test_a_remediable_retirement_is_still_named_and_still_fails(tmp_path):
    """The other side of the rule: nothing about the common path changed."""
    from conftest import drift
    fy = _fleet(tmp_path)
    dump = _report(tmp_path, {"spinout-10": [
        item("retire.table", "Plan Map", remediable=True)],
        "spinout-02": []})
    _floors(fy)

    r = drift("ratchet check", "--from-json", dump, fleet=fy)
    assert r.exit_code == 1, r.output
    assert "Plan Map" in r.output
    assert "RESURRECTION" in r.stdout


def test_a_non_remediable_retirement_on_an_advisory_model_is_named(tmp_path):
    """DD-8: the report does not depend on the model's gating."""
    from conftest import drift
    fy = make_fleet(tmp_path / "fleet", ["spinout-10", "spinout-02"],
                    advisory={"spinout-02": "not sync-managed"})
    dump = _report(tmp_path, {"spinout-10": [],
                              "spinout-02": [
                                  item("retire.table", "Plan Map",
                                       remediable=False)]})
    write_floors(fy, {"spinout-10": {"total": 0, "by_category": {}},
                      "spinout-02": {"total": 0, "by_category": {}}})

    r = drift("ratchet check", "--from-json", dump, fleet=fy)
    assert "Plan Map" in r.output, r.output
    assert r.exit_code == 0, r.output

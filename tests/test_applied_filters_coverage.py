"""The applied-filters measure: a visible dimension the measure does not
expose, and the regeneration that closes the gap.

The generator lives in pbip-model-forge, so these tests need it installed.
"""

from __future__ import annotations

import shutil

import pytest

from conftest import FIXTURES

pytest.importorskip("pbip_model_forge",
                    reason="pbip-model-forge is not installed")

INTRINSIC = FIXTURES / "intrinsic"


@pytest.fixture
def fleet(tmp_path):
    from tmdl_drift_doctor.baseline import capture
    from tmdl_drift_doctor.fleet import load_fleet
    root = tmp_path / "fleet"
    shutil.copytree(INTRINSIC, root)
    fleet = load_fleet(root / "fleet.yml")
    capture(fleet)
    return fleet


def findings(fleet, kind="ifilter.coverage_gap"):
    from tmdl_drift_doctor.baseline import load_baseline
    from tmdl_drift_doctor.detect import detect_fleet
    from tmdl_drift_doctor.ledger import Ledger
    found = detect_fleet(fleet, load_baseline(fleet),
                         Ledger(fleet.ledger_path))
    return [f for f in found if f.kind == kind]


def all_findings(fleet):
    from tmdl_drift_doctor.baseline import load_baseline
    from tmdl_drift_doctor.detect import detect_fleet
    from tmdl_drift_doctor.ledger import Ledger
    return detect_fleet(fleet, load_baseline(fleet),
                        Ledger(fleet.ledger_path))


def yes(f, label, changes, position=None):
    return True, ""


def test_a_visible_dimension_the_measure_does_not_expose_is_reported(fleet):
    found = findings(fleet)
    assert len(found) == 1
    assert found[0].ref == "i-Applied Filters"
    assert found[0].detail.startswith(
        "1 visible y-Selector dimension(s) are NOT exposed via i-Applied "
        "Filters in north:\n      + _Members[Region]\n")
    assert "AUTO-FIX regenerates the 'i-Applied Filters' measure from the " \
        "full visible y-Selector dimension set" in found[0].detail


def test_the_measure_is_regenerated_from_the_visible_dimensions(fleet):
    from tmdl_drift_doctor.remediate import remediate
    helpers = fleet.model_definition("north") / "tables" / "Helper Measures.tmdl"
    res = remediate(fleet, findings(fleet), confirm=yes)
    assert res.count("applied") == 1
    text = helpers.read_text(encoding="utf-8")
    assert "ISFILTERED('_Members'[Plan])" in text
    assert "ISFILTERED('_Members'[Region])" in text
    assert "CONCATENATEX" in text


def test_a_second_run_finds_nothing_left_to_fix(fleet):
    from tmdl_drift_doctor.remediate import remediate
    remediate(fleet, findings(fleet), confirm=yes)
    assert not findings(fleet)


def test_the_dry_run_writes_nothing(fleet):
    from tmdl_drift_doctor.remediate import remediate
    helpers = fleet.model_definition("north") / "tables" / "Helper Measures.tmdl"
    before = helpers.read_bytes()
    res = remediate(fleet, findings(fleet), dry_run=True, confirm=yes)
    assert res.count("would-apply") == 1
    assert helpers.read_bytes() == before


def test_nothing_is_applied_without_the_allowlist_entry(fleet):
    from tmdl_drift_doctor.remediate import remediate
    fleet.allowlist_kinds = set()
    res = remediate(fleet, findings(fleet), confirm=yes)
    assert res.count("skipped") == 1
    assert "allowlist" in res.outcomes[0].reason


def test_the_repair_is_never_batch_safe(fleet):
    from tmdl_drift_doctor.remediate import remediate
    res = remediate(fleet, findings(fleet), batch_only=True)
    assert res.count("skipped") == 1
    assert "not batch-safe" in res.outcomes[0].reason


def test_the_measure_keeps_its_own_bytes_around_the_repair(fleet):
    from tmdl_drift_doctor.remediate import remediate
    helpers = fleet.model_definition("north") / "tables" / "Helper Measures.tmdl"
    helpers.write_bytes(
        b"\xef\xbb\xbf" + helpers.read_bytes().replace(b"\n", b"\r\n"))
    remediate(fleet, findings(fleet), confirm=yes)
    after = helpers.read_bytes()
    assert after.startswith(b"\xef\xbb\xbf"), "the byte-order mark was lost"
    assert b"\n" not in after.replace(b"\r\n", b""), (
        "the repair left a bare LF in a CRLF file")


def test_a_model_without_the_measure_has_no_coverage_gap(fleet):
    from tmdl_drift_doctor import tmdl_edit as te
    helpers = fleet.model_definition("north") / "tables" / "Helper Measures.tmdl"
    text = te.read_text(helpers)
    helpers.write_text(te.remove_block(text, "measure", "i-Applied Filters"),
                       encoding="utf-8")
    assert not findings(fleet), (
        "a model that does not declare the measure has nothing to bring up "
        "to date")


def test_the_measure_is_rebuilt_after_a_retirement(fleet):
    """A retirement the model still carries leaves a stale branch in the
    applied-filters measure; the measure is rebuilt from what the model
    offers after the removal."""
    from tmdl_drift_doctor import tmdl_edit as te
    from tmdl_drift_doctor.baseline import capture
    from tmdl_drift_doctor.remediate import remediate
    template = fleet.template_definition / "tables" / "_Members.tmdl"
    text = te.read_text(template)
    template.write_text(te.remove_block(text, "column", "Plan"),
                        encoding="utf-8")
    capture(fleet)
    found = findings(fleet, "retire.ifilter_regen")
    assert len(found) == 1
    assert found[0].detail.startswith(
        "'i-Applied Filters' references object(s) this run retires (Plan) — "
        "regenerate it from the post-retirement visible y-Selector dimension "
        "set")
    fleet.allowlist_kinds.add("retire")
    helpers = fleet.model_definition("north") / "tables" / "Helper Measures.tmdl"
    picked = [f for f in all_findings(fleet)
              if f.kind in ("retire.column", "retire.fieldparam_entry",
                            "retire.ifilter_regen")]
    res = remediate(fleet, picked, sync=True, confirm=yes)
    assert res.count("applied") == 3
    members = fleet.model_definition("north") / "tables" / "_Members.tmdl"
    assert "column Plan" not in members.read_text(encoding="utf-8")
    selector = (fleet.model_definition("north") / "tables"
                / "y-Selector 1.tmdl").read_text(encoding="utf-8")
    assert "NAMEOF('_Members'[Plan])" not in selector
    text = helpers.read_text(encoding="utf-8")
    assert "ISFILTERED('_Members'[Plan])" not in text
    assert "ISFILTERED('_Members'[Region])" in text


def test_the_purge_channel_asks_for_the_rebuild_too(fleet):
    """The purge channel removes model-only objects; when the
    applied-filters measure names one of them, the measure is rebuilt after
    the purge."""
    from tmdl_drift_doctor import purge
    from tmdl_drift_doctor.detect import Finding
    from tmdl_drift_doctor.tmdl import parse_model
    model = parse_model(fleet.model_definition("north"))
    gone = [Finding(kind="purge.table", model="north", ref="_Members",
                    detail="model-only table '_Members' goes")]
    found = purge._ifilter_regen("north", model, gone)
    assert len(found) == 1
    assert found[0].kind == "purge.ifilter_regen"
    assert found[0].detail.startswith(
        "'i-Applied Filters' references object(s) this run purges "
        "(_Members) — regenerate it from the post-purge visible y-Selector "
        "dimension set")
    assert not purge._ifilter_regen("north", model, [])

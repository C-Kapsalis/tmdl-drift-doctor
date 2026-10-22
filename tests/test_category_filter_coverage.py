"""The category gates: a fact without a gate, a category without a gate in
the all-filters measure, and the facts no gate can be written for.

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


def findings(fleet, kind="ffilter.coverage_gap"):
    from tmdl_drift_doctor.baseline import load_baseline
    from tmdl_drift_doctor.detect import detect_fleet
    from tmdl_drift_doctor.ledger import Ledger
    found = detect_fleet(fleet, load_baseline(fleet),
                         Ledger(fleet.ledger_path))
    return [f for f in found if f.kind == kind]


def yes(f, label, changes, position=None):
    return True, ""


def test_a_fact_without_a_category_gate_is_reported(fleet):
    found = findings(fleet)
    assert len(found) == 1
    detail = found[0].detail
    assert detail.startswith(
        "f-measure coverage gap in north (1 to create, 0 to extend, 1 f-All "
        "gate(s) to add, 0 category(ies) ungated with no resolvable f- "
        "measure) — every fact consumed by user-facing measures needs an "
        "f-<Category> gate, AND every mapping category must be GATED in "
        "f-All:")
    assert "+ create f-Visits" in detail
    assert "+ gate f-All: Visits" in detail


def test_a_fact_no_gate_can_be_written_for_is_reported(fleet):
    found = findings(fleet, "ffilter.uncategorized")
    assert len(found) == 1
    detail = found[0].detail
    assert detail.startswith(
        "1 fact table(s) can't be auto-covered by the f-filter generator (no "
        "resolvable analysis category):\n      - Sessions:")
    assert "NO AUTO-FIX: assign the fact an analysis category" in detail


def test_a_gate_the_ledger_retired_is_not_offered_again(fleet):
    """A gate the ledger retired must not come back: the coverage check
    leaves it alone instead of asking for it every run."""
    from tmdl_drift_doctor.ledger import Ledger
    Ledger(fleet.ledger_path).record_retirement(
        "measure", "f-Filters[f-Visits]", reason="dropped from the template")
    assert not findings(fleet), (
        "a retired gate is offered again; the coverage check would fight the "
        "retirement")


def test_the_uncategorized_finding_is_report_only(fleet):
    from tmdl_drift_doctor.detect import is_remediable
    found = findings(fleet, "ffilter.uncategorized")
    assert not is_remediable(found[0].kind)
    from tmdl_drift_doctor.remediate import remediate
    res = remediate(fleet, found, confirm=yes)
    assert res.count("manual") == 1


def test_the_missing_gate_is_written(fleet):
    from tmdl_drift_doctor.remediate import remediate
    gates = fleet.model_definition("north") / "tables" / "f-Filters.tmdl"
    res = remediate(fleet, findings(fleet), confirm=yes)
    assert res.count("applied") == 1
    text = gates.read_text(encoding="utf-8")
    assert "measure 'f-Visits' = int(not ISEMPTY('Visits'))" in text
    assert 'CONTAINSSTRING(all_cats, "Visits")' in text
    assert "[f-Visits] > 0" in text


def test_a_second_run_finds_nothing_left_to_fix(fleet):
    from tmdl_drift_doctor.remediate import remediate
    remediate(fleet, findings(fleet), confirm=yes)
    assert not findings(fleet)


def test_the_dry_run_writes_nothing(fleet):
    from tmdl_drift_doctor.remediate import remediate
    gates = fleet.model_definition("north") / "tables" / "f-Filters.tmdl"
    before = gates.read_bytes()
    res = remediate(fleet, findings(fleet), dry_run=True, confirm=yes)
    assert res.count("would-apply") == 1
    assert gates.read_bytes() == before


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


def test_the_gate_table_keeps_its_own_bytes_around_the_repair(fleet):
    from tmdl_drift_doctor.remediate import remediate
    gates = fleet.model_definition("north") / "tables" / "f-Filters.tmdl"
    gates.write_bytes(
        b"\xef\xbb\xbf" + gates.read_bytes().replace(b"\n", b"\r\n"))
    remediate(fleet, findings(fleet), confirm=yes)
    after = gates.read_bytes()
    assert after.startswith(b"\xef\xbb\xbf"), "the byte-order mark was lost"
    assert b"\n" not in after.replace(b"\r\n", b""), (
        "the repair left a bare LF in a CRLF file")

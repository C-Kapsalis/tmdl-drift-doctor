"""Measure visibility: a user-facing measure that is hidden, a helper that is
visible.

The class of a measure is decided by its name alone, the way tmdl-preflight's
O009 and O011 rules decide it, so a measure hidden by mistake still reads as
user-facing. The two repairs change what a report author sees, so neither is
batch-safe.
"""

from __future__ import annotations

import shutil

import pytest

from conftest import FIXTURES

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


def findings(fleet, kind):
    from tmdl_drift_doctor.baseline import load_baseline
    from tmdl_drift_doctor.detect import detect_fleet
    from tmdl_drift_doctor.ledger import Ledger
    found = detect_fleet(fleet, load_baseline(fleet),
                         Ledger(fleet.ledger_path))
    return [f for f in found if f.kind == kind]


def yes(f, label, changes, position=None):
    return True, ""


def test_a_hidden_user_facing_measure_is_reported(fleet):
    found = findings(fleet, "visibility.hidden_user_facing")
    assert [f.ref for f in found] == ["#Report Measures[Membership]"]
    assert found[0].detail == (
        "#Report Measures[Membership] has a user-facing NAME but is hidden. "
        "Either it was hidden by mistake (show it) or it is really a helper "
        "(rename with a helper prefix — a cascade rename).")


def test_a_visible_helper_measure_is_reported(fleet):
    found = findings(fleet, "visibility.helper_visible")
    assert [f.ref for f in found] == ["Helper Measures[i-Visit Amount]"]
    assert found[0].detail == (
        "Helper Measures[i-Visit Amount] has a helper NAME but is visible "
        "and is not on the always-visible allowlist — auto-hiding.")


def test_the_always_visible_measures_are_never_reported(fleet):
    from tmdl_drift_doctor.visibility import ALWAYS_VISIBLE
    assert "i-Applied Filters" in ALWAYS_VISIBLE
    assert not [f for f in findings(fleet, "visibility.helper_visible")
                if f.ref.endswith("[i-Applied Filters]")]


def test_the_hidden_measure_is_shown_and_the_helper_is_hidden(fleet):
    from tmdl_drift_doctor.remediate import remediate
    report = fleet.model_definition("north") / "tables" / "#Report Measures.tmdl"
    helpers = fleet.model_definition("north") / "tables" / "Helper Measures.tmdl"
    res = remediate(fleet, findings(fleet, "visibility.hidden_user_facing"),
                    confirm=yes)
    assert res.count("applied") == 1
    assert "isHidden" not in report.read_text(encoding="utf-8")
    res = remediate(fleet, findings(fleet, "visibility.helper_visible"),
                    confirm=yes)
    assert res.count("applied") == 1
    text = helpers.read_text(encoding="utf-8")
    assert "\t\tisHidden: true\n" in text
    assert text.index("i-Visit Amount") < text.index("isHidden: true")


def test_a_second_run_finds_nothing_left_to_fix(fleet):
    from tmdl_drift_doctor.remediate import remediate
    remediate(fleet, findings(fleet, "visibility.hidden_user_facing"),
              confirm=yes)
    remediate(fleet, findings(fleet, "visibility.helper_visible"),
              confirm=yes)
    assert not findings(fleet, "visibility.hidden_user_facing")
    assert not findings(fleet, "visibility.helper_visible")


def test_nothing_is_applied_without_the_allowlist_entry(fleet):
    from tmdl_drift_doctor.remediate import remediate
    fleet.allowlist_kinds = set()
    res = remediate(fleet, findings(fleet, "visibility.helper_visible"),
                    confirm=yes)
    assert res.count("skipped") == 1
    assert "allowlist" in res.outcomes[0].reason


def test_the_repair_is_never_batch_safe(fleet):
    from tmdl_drift_doctor.remediate import remediate
    res = remediate(fleet, findings(fleet, "visibility.helper_visible"),
                    batch_only=True)
    assert res.count("skipped") == 1
    assert "not batch-safe" in res.outcomes[0].reason


def test_the_dry_run_writes_nothing(fleet):
    from tmdl_drift_doctor.remediate import remediate
    helpers = fleet.model_definition("north") / "tables" / "Helper Measures.tmdl"
    before = helpers.read_bytes()
    res = remediate(fleet, findings(fleet, "visibility.helper_visible"),
                    dry_run=True, confirm=yes)
    assert res.count("would-apply") == 1
    assert helpers.read_bytes() == before


def test_the_repair_keeps_the_line_endings_and_the_byte_order_mark(fleet):
    from tmdl_drift_doctor.remediate import remediate
    helpers = fleet.model_definition("north") / "tables" / "Helper Measures.tmdl"
    helpers.write_bytes(
        b"\xef\xbb\xbf" + helpers.read_bytes().replace(b"\n", b"\r\n"))
    before = helpers.read_bytes()
    res = remediate(fleet, findings(fleet, "visibility.helper_visible"),
                    confirm=yes)
    assert res.count("applied") == 1
    after = helpers.read_bytes()
    assert after.startswith(b"\xef\xbb\xbf"), "the byte-order mark was lost"
    assert b"\n" not in after.replace(b"\r\n", b""), (
        "the repair left a bare LF in a CRLF file")
    assert after.count(b"\r\n") == len(after.splitlines())
    unchanged = [ln for ln in before.replace(b"\r\n", b"\n").split(b"\n")
                 if ln.strip() and not ln.strip().startswith(b"\t\tisHidden")]
    kept = [ln for ln in after.replace(b"\r\n", b"\n").split(b"\n")
            if ln.strip()]
    assert all(ln in kept for ln in unchanged)


def test_a_scaffolding_name_hidden_on_purpose_is_not_reported():
    from tmdl_drift_doctor.visibility import is_noise_name
    assert is_noise_name("Max Date")
    assert is_noise_name("Date Filter")
    assert is_noise_name("Value")
    assert not is_noise_name("Total Visits")


def test_the_class_of_a_measure_is_decided_by_its_name():
    from tmdl_drift_doctor.visibility import is_helper_name
    assert is_helper_name("-helper Total")
    assert is_helper_name("_Total")
    assert is_helper_name("i-Tooltip")
    assert is_helper_name("w-Specific")
    assert is_helper_name("Cohort Test 2")
    assert not is_helper_name("Total Visits")
    assert not is_helper_name("z-BLANK Deals (CRT)")
    assert not is_helper_name("#DaysLeftCurrentMonth")
    assert not is_helper_name("!Revenue")

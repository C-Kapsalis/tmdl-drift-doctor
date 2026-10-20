"""Resurrection contract: `retire.*` is never floored — ported from the
user's original suite.

The regression this pins actually shipped: a merge resolved a
relationships.tmdl conflict in favour of a branch whose base predated a table
retirement, and put five relationships back as DANGLING relations, pointing at
a table that no longer existed in that model. The ratchet did fire, but it
printed the same sentence it prints for a model that is merely behind on a
cascade (``TOTAL rose 0 -> 5``) and was merged past. So the fix is not more
detection; it is refusing to express this class as a count against a floor.

The originals ran ``scripts/ci/check_ratchet.py`` as a subprocess; here every
call is ``drift-doctor ratchet check --from-json`` through the CLI entry point.
"""
from __future__ import annotations

import json

from conftest import FIXTURES
from port_helpers_b4_ratchet import (
    check,
    item,
    make_fleet,
    write_floors,
    write_report,
)

# The five resurrected relationships from the original incident.
RESURRECTED = [
    "Google Ads Keywords Quality[account_x_id] -> _Accounts[account_x_id]",
    "Google Ads Keywords Quality[ad_group_x_id] -> _Campaigns[ad_group_x_id]",
    "Google Ads Keywords Quality[date] -> __Calendar[Date]",
    "Google Ads Keywords Quality[keyword_x_id] -> _Keywords[keyword_x_id]",
    "Google Ads Keywords Quality[landing_page_x_id] -> "
    "_Landing Pages[landing_page_x_id]",
]

# Committed floors stand-in (fictional fleet; the original read the repo's
# tests/baselines/drift_ratchet_floors.json, which holds company data).
COMMITTED_FLOORS = FIXTURES / "b4-ratchet" / "fleet" / ".drift-doctor" / "floors.json"


def _fleet(tmp_path):
    return make_fleet(tmp_path / "fleet", ["spinout-10", "spinout-02"])


def test_retire_items_fail_even_when_the_floor_would_allow_them(tmp_path):
    """The exact escape hatch that must not exist: a floor generous enough to
    swallow the resurrection. Under the old logic 5 <= 5 was green."""
    fy = _fleet(tmp_path)
    dump = write_report(tmp_path / "d.json", {"spinout-10": [
        item("retire.relationship", sig) for sig in RESURRECTED]},
        generated_at="2026-08-10T00:00:00+00:00")
    write_floors(fy, {"spinout-10": {"total": 99, "by_category": {"retire": 99}}})

    r = check(fy, dump)

    assert r.exit_code == 1, r.output
    assert "RESURRECTION" in r.stdout
    # Names the objects — nobody should have to run the fleet sweep to find out
    # what came back.
    for sig in RESURRECTED:
        assert sig in r.stdout
    assert "--kind retire --sync" in r.stdout
    # And it must NOT read like ordinary backlog growth.
    assert "category 'retire' rose" not in r.stdout


def test_a_clean_fleet_is_still_green(tmp_path):
    """Hard zero is the normal fleet state, so the rule must not turn an
    already-green tree red."""
    fy = _fleet(tmp_path)
    dump = write_report(tmp_path / "d.json", {
        "spinout-10": [item("ffilter.missing", "f-X", remediable=False),
                       item("fieldparam.parity.Performance_x", "p",
                            remediable=False)],
        "spinout-02": [],
    }, generated_at="2026-08-10T00:00:00+00:00")
    write_floors(fy, {
        "spinout-10": {"total": 5, "by_category": {"ffilter": 5}},
        "spinout-02": {"total": 0, "by_category": {}}})

    r = check(fy, dump)

    assert r.exit_code == 0, r.output
    assert "no ledger-retired object resurrected" in r.stdout


def test_resurrection_reported_alongside_an_ordinary_floor_rise(tmp_path):
    """Both failures are real and both must be visible — the resurrection block
    is additional signal, not a replacement that hides a genuine rise."""
    fy = _fleet(tmp_path)
    dump = write_report(tmp_path / "d.json", {"spinout-10": [
        item("retire.table", "Google Ads Keywords Quality"),
        item("measure.missing", "m1"),
        item("measure.missing", "m2"),
    ]}, generated_at="2026-08-10T00:00:00+00:00")
    write_floors(fy, {"spinout-10": {"total": 3, "by_category": {"measure": 0}}})

    r = check(fy, dump)

    assert r.exit_code == 1, r.output
    assert "RESURRECTION" in r.stdout
    assert "Google Ads Keywords Quality" in r.stdout
    assert "category 'measure' rose 0 -> 2" in r.stdout


def test_the_committed_floors_carry_no_retire_allowance():
    """A floor for a hard-zero head is dead config at best and a licence to
    resurrect at worst. Nothing in the tree may quietly grow one."""
    committed = json.loads(COMMITTED_FLOORS.read_text(encoding="utf-8"))
    offenders = {
        mid: bc for mid, floor in (committed.get("models") or {}).items()
        if (bc := {k: v for k, v in (floor.get("by_category") or {}).items()
                   if k == "retire" and v})
    }
    assert not offenders, (
        f"floors grant a retire allowance: {offenders} — remediate the model "
        f"instead (`drift-doctor remediate --model <m> --kind retire --sync`), "
        f"or declare a `retain` override in fleet.yml")


def test_a_retire_allowance_in_floors_is_refused(tmp_path):
    """Negative case for the committed-floors rule (H-ratchet-check Cases 4 /
    Detection 1): a floors file with `retire: 1` is flagged, naming the model.

    The spec asks for exit 2 here, but the original tests above (and
    test_advisory_gating) require exit 1 when a resurrection meets a
    `retire: 99` floor, so only "fails and names the model" is pinned; see
    PORT-LOG "spec conflict"."""
    fy = _fleet(tmp_path)
    dump = write_report(tmp_path / "d.json", {"spinout-10": [], "spinout-02": []})
    write_floors(fy, {"spinout-10": {"total": 1, "by_category": {"retire": 1}},
                      "spinout-02": {"total": 0, "by_category": {}}})
    r = check(fy, dump)
    assert r.exit_code != 0, r.output
    assert "spinout-10" in r.output and "retire" in r.output

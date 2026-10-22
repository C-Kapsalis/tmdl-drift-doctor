"""The freshness body counts what the branch has not seen, and names the
base it compared against.

The body is posted as a CI comment. Its count is the number of main-line
retirements the branch has NOT seen (the head ledger's other entries are
already in the branch), and the `--base-label` the CI job passes names the
base in every body, not only in the one that says all is well.
"""

from __future__ import annotations

from test_freshness import _fleet, _ledger, _retired

from tmdl_drift_doctor.fleet import load_fleet
from tmdl_drift_doctor.freshness import run


def _two_head_one_unseen(tmp_path):
    base = _ledger(tmp_path / "base.jsonl", [_retired("table", "Trade-ins")])
    head = _ledger(tmp_path / "head.jsonl", [_retired("table", "Trade-ins"),
                                             _retired("table", "Coupons")])
    fleet = load_fleet(_fleet(tmp_path, {
        "north": {"Coupons": "table Coupons\n"}}))
    return fleet, base, head


def test_the_body_counts_only_the_unseen_entries(tmp_path):
    fleet, base, head = _two_head_one_unseen(tmp_path)
    res = run(fleet, base, head, base_label="abc123")
    assert len(res["unseen"]) == 1
    assert "predates **1 ledger entry**" in res["body"], res["body"]


def test_the_at_risk_body_names_the_base_label(tmp_path):
    fleet, base, head = _two_head_one_unseen(tmp_path)
    res = run(fleet, base, head, base_label="abc123")
    assert res["at_risk"]
    assert "`abc123`" in res["body"], res["body"]


def test_without_a_label_the_at_risk_body_is_unchanged(tmp_path):
    fleet, base, head = _two_head_one_unseen(tmp_path)
    res = run(fleet, base, head)
    assert "This branch base predates **1 ledger entry** on the main line." \
        in res["body"], res["body"]

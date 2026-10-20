"""Ratchet scope-hardening contract — ported from the user's original suite.

The ratchet pair consumes saved detection reports. How a report was produced
(model targeting, kind filter, custom overrides, provider failures) is decided
at produce time and recorded in its ``scope`` block, so consumers never guess.
Guessing wrong was executed, not hypothetical: a one-model report plus
``--prune`` deleted 13 of 14 floors; a kind-filtered fleet report fed to
tighten collapsed every floor to ~0.

The originals ran ``scripts/ci/check_ratchet.py`` / ``tighten_ratchet.py`` as
subprocesses; here every call is ``drift-doctor ratchet check|tighten
--from-json`` through the CLI entry point (exit codes are the contract CI
depends on). Report/floors shapes: see ``port_helpers_b4_ratchet``.
"""
from __future__ import annotations

import pytest

from port_helpers_b4_ratchet import (
    check,
    counted_report,
    fleet_scope,
    item,
    make_fleet,
    measure_floors,
    read_floors,
    scoped_scope,
    tighten,
    write_floors,
    write_report,
)

# 'measure.missing' is sync-actionable (counts); the head rolls up to 'measure'.
CAT = "measure.missing"


@pytest.fixture
def fy(tmp_path):
    """Fleet with models a, b, c, new ('gone' is deliberately not a member)."""
    return make_fleet(tmp_path / "fleet", ["a", "b", "c", "new"])


def _read(fy) -> dict:
    return read_floors(fy)


# ─────────────────────────── ratchet check ───────────────────────────

def test_check_rejects_category_filtered_dump(fy, tmp_path):
    d = counted_report(tmp_path / "d.json", {"a": 1},
                       scope=fleet_scope(["a"], kind_filter="ifilter"))
    measure_floors(fy, {"a": 5})
    r = check(fy, d)
    assert r.exit_code == 2, r.output
    assert "category-filtered" in r.stderr


def test_check_rejects_provider_errors(fy, tmp_path):
    d = counted_report(tmp_path / "d.json", {"a": 1}, scope=fleet_scope(["a"]),
                       provider_errors={"a": 2})
    measure_floors(fy, {"a": 5})
    r = check(fy, d)
    assert r.exit_code == 2, r.output
    assert "provider error" in r.stderr


def test_check_scoped_dump_green_and_no_prune_suggestion(fy, tmp_path):
    # Scoped to 'a' (green); 'b' has a floor but was not targeted — absence
    # means nothing, so: N-of-M line, NO prune suggestion, exit 0.
    d = counted_report(tmp_path / "d.json", {"a": 2},
                       scope=scoped_scope("a", ["a"]))
    measure_floors(fy, {"a": 5, "b": 7})
    r = check(fy, d)
    assert r.exit_code == 0, r.output
    assert "1 of 2 floors evaluated" in r.stdout
    assert "prune it" not in r.stdout


def test_check_scoped_rise_still_red(fy, tmp_path):
    d = counted_report(tmp_path / "d.json", {"a": 9},
                       scope=scoped_scope("a", ["a"]))
    measure_floors(fy, {"a": 5})
    r = check(fy, d)
    assert r.exit_code == 1
    assert "rose" in r.stdout


def test_check_fleet_failed_client_is_not_a_prune_candidate(fy, tmp_path):
    # 'b' was targeted (in scope.targets) but produced no result.
    d = counted_report(tmp_path / "d.json", {"a": 2},
                       scope=fleet_scope(["a", "b"]))
    measure_floors(fy, {"a": 5, "b": 7})
    r = check(fy, d)
    assert "detection FAILED" in r.stdout
    assert "prune it" not in r.stdout


def test_check_fleet_deregistered_gets_the_prune_suggestion(fy, tmp_path):
    d = counted_report(tmp_path / "d.json", {"a": 2}, scope=fleet_scope(["a"]))
    measure_floors(fy, {"a": 5, "gone": 7})
    r = check(fy, d)
    assert "prune it" in r.stdout


# ─────────────────────────── ratchet tighten ───────────────────────────

def test_tighten_rejects_scopeless_dump(fy, tmp_path):
    d = counted_report(tmp_path / "d.json", {"a": 1}, scope=None)  # no scope block
    measure_floors(fy, {"a": 5})
    before = _read(fy)
    r = tighten(fy, d)
    assert r.exit_code == 2, r.output
    assert "no scope block" in r.stderr
    assert _read(fy) == before, "floors must be unmodified on refusal"


@pytest.mark.parametrize("scope_kw,needle", [
    ({"kind_filter": "ifilter"}, "category-filtered"),
    ({"alternate_model": "Secondary.SemanticModel"}, "--model-scoped"),
    ({"overrides": "/tmp/other.yaml"}, "--overrides"),
], ids=["scope_kw0-category-filtered", "scope_kw1---model-scoped",
        "scope_kw2---overrides"])
def test_tighten_rejects_untrustworthy_scopes(fy, tmp_path, scope_kw, needle):
    d = counted_report(tmp_path / "d.json", {"a": 1},
                       scope=fleet_scope(["a"], **scope_kw))
    measure_floors(fy, {"a": 5})
    before = _read(fy)
    r = tighten(fy, d)
    assert r.exit_code == 2, r.output
    assert needle in r.stderr
    assert _read(fy) == before


def test_tighten_rejects_provider_errors(fy, tmp_path):
    d = counted_report(tmp_path / "d.json", {"a": 0}, scope=fleet_scope(["a"]),
                       provider_errors={"a": 1})
    measure_floors(fy, {"a": 5})
    before = _read(fy)
    r = tighten(fy, d)
    assert r.exit_code == 2
    assert _read(fy) == before


def test_tighten_scoped_touches_only_the_named_client(fy, tmp_path):
    d = counted_report(tmp_path / "d.json", {"a": 1},
                       scope=scoped_scope("a", ["a"]))
    measure_floors(fy, {"a": 5, "b": 7, "c": 3})
    r = tighten(fy, d)
    assert r.exit_code == 0, r.output
    after = _read(fy)["models"]
    assert after["a"]["total"] == 1
    assert after["b"] == {"total": 7, "by_category": {"measure": 7}}
    assert after["c"] == {"total": 3, "by_category": {"measure": 3}}


def test_tighten_prune_requires_fleet_dump(fy, tmp_path):
    d = counted_report(tmp_path / "d.json", {"a": 1},
                       scope=scoped_scope("a", ["a"]))
    measure_floors(fy, {"a": 5, "b": 7})
    before = _read(fy)
    r = tighten(fy, d, "--prune")
    assert r.exit_code == 2
    assert "--prune requires a FLEET dump" in r.stderr
    assert _read(fy) == before


def test_tighten_fleet_prune_deregistered_but_never_skipped(fy, tmp_path):
    # 'gone' absent from targets -> deregistered -> pruned.
    # 'b' in targets but absent from the results -> detection failed -> KEPT.
    d = counted_report(tmp_path / "d.json", {"a": 1},
                       scope=fleet_scope(["a", "b"]))
    measure_floors(fy, {"a": 5, "b": 7, "gone": 3})
    r = tighten(fy, d, "--prune")
    assert r.exit_code == 0, r.output
    after = _read(fy)["models"]
    assert "gone" not in after, "deregistered model must be pruned"
    assert after["b"]["total"] == 7, "a model whose detection failed is never prunable"
    assert "detection FAILED" in r.stdout


def test_tighten_meta_scoped_carries_forward_fleet_refreshes(fy, tmp_path):
    measure_floors(fy, {"a": 5, "b": 7})
    old_meta = _read(fy)

    # Scoped tighten: top-level provenance carried forward, last_tighten added.
    d = counted_report(tmp_path / "d1.json", {"a": 1},
                       scope=scoped_scope("a", ["a"]), template_tree="sc0ped01")
    assert tighten(fy, d).exit_code == 0
    meta = _read(fy)
    assert meta["template_tree"] == old_meta["template_tree"]
    assert meta["updated_at"] == old_meta["updated_at"]
    assert meta["last_tighten"]["scope"] == "scoped"
    assert meta["last_tighten"]["template_tree"] == "sc0ped01"
    assert meta["last_tighten"]["models"] == ["a"]

    # Fleet tighten: top-level provenance refreshes.
    d = counted_report(tmp_path / "d2.json", {"a": 0, "b": 2},
                       scope=fleet_scope(["a", "b"]), template_tree="f1eet002")
    assert tighten(fy, d).exit_code == 0
    meta = _read(fy)
    assert meta["template_tree"] == "f1eet002"
    assert meta["updated_at"] != old_meta["updated_at"]
    assert meta["last_tighten"]["scope"] == "fleet"


def test_tighten_scoped_allow_new_records_the_new_client(fy, tmp_path):
    d = counted_report(tmp_path / "d.json", {"new": 4},
                       scope=scoped_scope("new", ["new"]))
    measure_floors(fy, {"a": 5})
    r = tighten(fy, d, "--allow-new")
    assert r.exit_code == 0, r.output
    after = _read(fy)["models"]
    assert after["new"]["total"] == 4
    assert after["a"]["total"] == 5


# ──────────── spec cases with no original test (fresh bike-shop fleet) ────────────
# H-ratchet-check cases 11-15 and H-ratchet-tighten-scoped cases 9-12.

@pytest.fixture
def shops(tmp_path):
    return make_fleet(tmp_path / "shops", ["north", "south", "east", "west"],
                      allowlist_kinds=["measure.missing"])


def test_spec_check_counts_regardless_of_allowlist(shops, tmp_path):
    """H-ratchet-check case 11: only measure.missing is allowlisted, yet a
    column.missing still counts and a retire.table is still a resurrection."""
    d = write_report(tmp_path / "d.json", {"north": [
        item("retire.table", "Trade-ins"), item("column.missing", "Bikes[frame]")]})
    write_floors(shops, {"north": {"total": 0, "by_category": {}}})
    r = check(shops, d)
    assert r.exit_code == 1, r.output
    assert "RESURRECTION" in r.stdout and "Trade-ins" in r.stdout
    assert "north: TOTAL rose 0 -> 1" in r.stdout
    assert "north: category 'column' rose 0 -> 1" in r.stdout


def test_spec_check_rolls_kinds_up_to_heads(shops, tmp_path):
    """H-ratchet-check case 12: 1 measure.missing + 4 measure.expression_drift
    against a `measure` head floor of 5 is not a rise."""
    d = write_report(tmp_path / "d.json", {"north": [
        item("measure.missing", "Sales[Margin]"),
        *[item("measure.expression_drift", f"Sales[M{i}]") for i in range(4)]]})
    write_floors(shops, {"north": {"total": 5, "by_category": {"measure": 5}}})
    r = check(shops, d)
    assert r.exit_code == 0, r.output


def test_spec_check_reads_legacy_by_kind_floors(shops, tmp_path):
    """H-ratchet-check case 13: a legacy floors file keyed by full kind is read
    as head totals (measure.missing 3 + measure.expression_drift 2 = measure 5)."""
    d = write_report(tmp_path / "d.json", {"north": [
        item("measure.missing", f"Sales[M{i}]") for i in range(5)]})
    write_floors(shops, {"north": {"total": 5, "by_kind": {
        "measure.missing": 3, "measure.expression_drift": 2}}}, version=1)
    r = check(shops, d)
    assert r.exit_code == 0, r.output


def test_spec_check_scopeless_report_never_suggests_prune(shops, tmp_path):
    """H-ratchet-check case 14: no scope block -> compared as-is, no prune hint."""
    d = write_report(tmp_path / "d.json", {"north": [
        item("measure.missing", "Sales[M1]")]}, scope=None)
    write_floors(shops, {"north": {"total": 5, "by_category": {"measure": 5}},
                         "gone": {"total": 3, "by_category": {"measure": 3}}})
    r = check(shops, d)
    assert r.exit_code == 0, r.output
    assert "prune it" not in r.stdout


def test_spec_check_refuses_retirement_allowance(shops, tmp_path):
    """H-ratchet-check case 15: a floors file granting `retire: 2` is a
    configuration error naming the model (spec: exit 2; only non-zero is
    pinned, see PORT-LOG "spec conflict")."""
    d = write_report(tmp_path / "d.json", {"north": []})
    write_floors(shops, {"north": {"total": 2, "by_category": {"retire": 2}}})
    r = check(shops, d)
    assert r.exit_code != 0, r.output
    assert "north" in r.output and "retire" in r.output


def test_spec_tighten_never_raises(shops, tmp_path):
    """H-ratchet-tighten-scoped case 9."""
    d = write_report(tmp_path / "d.json", {"north": [
        item(CAT, f"Sales[M{i}]") for i in range(6)]})
    write_floors(shops, {"north": {"total": 2, "by_category": {"measure": 2}}})
    r = tighten(shops, d)
    assert r.exit_code == 0, r.output
    assert read_floors(shops)["models"]["north"]["total"] == 2


def test_spec_tighten_never_floors_retire(shops, tmp_path):
    """H-ratchet-tighten-scoped case 10: a new model with only resurrections
    is recorded at 0 with no `retire` head."""
    d = write_report(tmp_path / "d.json", {"west": [
        item("retire.measure", f"Sales[Old {i}]") for i in range(3)]})
    write_floors(shops, {"north": {"total": 0, "by_category": {}}})
    r = tighten(shops, d, "--allow-new")
    assert r.exit_code == 0, r.output
    west = read_floors(shops)["models"]["west"]
    assert west["total"] == 0
    assert "retire" not in west["by_category"]


def test_spec_tighten_counts_regardless_of_allowlist(tmp_path):
    """H-ratchet-tighten-scoped case 11: allowlist empty; 2 column.missing
    lower a floor of 5 to 2 (`column` 2)."""
    fy = make_fleet(tmp_path / "shops", ["north"], allowlist_kinds=[])
    d = write_report(tmp_path / "d.json", {"north": [
        item("column.missing", "Bikes[frame]"), item("column.missing", "Bikes[gears]")]})
    write_floors(fy, {"north": {"total": 5, "by_category": {"column": 5}}})
    r = tighten(fy, d)
    assert r.exit_code == 0, r.output
    assert read_floors(fy)["models"]["north"] == {"total": 2,
                                                  "by_category": {"column": 2}}


def test_spec_tighten_noop_leaves_the_floors_alone(shops, tmp_path):
    """H-ratchet-tighten-scoped case 12, read against the provenance rule.

    A no-op tighten lowers no floor, so the floors themselves are unchanged.
    The measurement, though, happened: the file records that the fleet was
    measured now, at this template tree, so a CI job that asks "when was
    this floor last measured against the live template?" gets a true
    answer.
    """
    d = write_report(tmp_path / "d.json", {
        "north": [item(CAT, "Sales[M1]")], "south": [], "east": [], "west": []})
    write_floors(shops, {"north": {"total": 1, "by_category": {"measure": 1}},
                         "south": {"total": 0, "by_category": {}},
                         "east": {"total": 0, "by_category": {}},
                         "west": {"total": 0, "by_category": {}}})
    r = tighten(shops, d)
    assert r.exit_code == 0, r.output
    doc = read_floors(shops)
    assert doc["models"]["north"] == {"total": 1,
                                      "by_category": {"measure": 1}}
    assert doc["models"]["south"] == {"total": 0, "by_category": {}}
    assert doc.get("last_tighten"), (
        "a no-op tighten did not record the measurement")
    assert doc["last_tighten"]["scope"] == "fleet"
    assert doc["last_tighten"]["models"] == ["east", "north", "south", "west"]

"""Underlay sync: fixture-based integration tests (ported).

Exercises the batch sync against a trimmed template / derived fixture pair
under tests/fixtures/sync_engine/:

  universal_mini/  7-table trim of a template model (tab-structured TMDL,
                   source_filter_dates partitions).
  growth_mini/     underlay = copy of universal_mini, plus an overlay
                   (derived-only 'Cohort Performance' data table + a
                   'Growth Measures' container with one overlay measure)
                   and a Mini.Report referencing one underlay measure
                   (#Report Measures[CTR]) and one overlay measure
                   (Growth Measures[Cohort Retention %]).
  extras/          scenario ingredients (a trimmed Campaigns table, the
                   Ads unique_clicks column block, the Campaigns->_Accounts
                   relationship block).

Two families:

  FIXTURE SELF-CHECKS  prove the pair parses, is drift-clean, and that the
  overlay-hash tripwire works.

  ENGINE SCENARIOS     mutate the tmp TEMPLATE copy (or the derived copy for
  divergence cases), run capture + detect + batch remediate through a
  fleet.yml written per test, and assert BOTH the emitted finding kinds (so a
  silently broken detector cannot pass vacuously) and the post-state, plus
  overlay untouchedness after every scenario.

Ported from the user's suite; the internal engine calls are replaced by
drift-doctor's CLI / Python API (see PORT-LOG.md, batch b8-sync).
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import date

import pytest

from conftest import drift
from port_helpers_b8_sync import (
    EXTRAS, GRO_SRC, MODEL, REP_SRC, SYNC_KINDS, UNI_SRC,
    assert_overlay_untouched, capture_and_detect, copy_pair, count_report_refs,
    is_sync_kind, overlay_hashes, pair_fleet, run_sync, sha, sync_kinds,
    tree_hashes,
)
from tmdl_drift_doctor import tmdl_edit as te
from tmdl_drift_doctor.tmdl import parse_model, parse_relationships_file

UNI_TABLES = {"#Report Measures", "Ads", "_Accounts", "__Calendar",
              "f-Filters", "x-Selector 1", "y-Selector 1"}
UNI_MEASURES = {"Cost", "Paid Clicks #", "Paid Impressions #", "CTR", "CPM",
                "f-Spend"}


def _measures(defn) -> dict:
    """{measure name: Measure} across every table of a definition folder."""
    out = {}
    for t in parse_model(defn).tables.values():
        out.update(t.measures)
    return out


def _norm(expr) -> str:
    from tmdl_drift_doctor.tmdl import normalize_expression
    return normalize_expression(expr)


# ─── template-copy mutations (scenario ingredients) ───

def _uni_report_file(uni_defn):
    return uni_defn / "tables" / "#Report Measures.tmdl"


def _mutate_add_measure(uni_defn) -> str:
    """Scenario (a): a NEW user-facing measure appears in the template."""
    name = "Cost per Click"
    block = (f"\tmeasure '{name}' = DIVIDE([Cost],[Paid Clicks #])\n"
             f"\t\tformatString: #,0.00\n"
             f"\t\tdisplayFolder: Ads\n"
             f"\t\tlineageTag: {uuid.uuid4()}")
    f = _uni_report_file(uni_defn)
    content = te.insert_measure_block(f.read_text(encoding="utf-8"), block)
    te.assert_tmdl_valid(content)
    f.write_text(content, encoding="utf-8")
    return name


def _mutate_rename_ctr(uni_defn) -> tuple:
    """Scenario (b): the template renames CTR -> '-CTR' (prefix drift), incl.
    its NAMEOF reference on the template's x-Selector 1 metric axis."""
    old, new = "CTR", "-CTR"
    f = _uni_report_file(uni_defn)
    text = f.read_text(encoding="utf-8")
    assert f"\tmeasure {old} =" in text, "fixture drifted: CTR declaration"
    f.write_text(text.replace(f"\tmeasure {old} =", f"\tmeasure '{new}' =", 1),
                 encoding="utf-8")
    x = uni_defn / "tables" / "x-Selector 1.tmdl"
    x.write_text(
        x.read_text(encoding="utf-8").replace(f"[{old}]", f"[{new}]"),
        encoding="utf-8")
    return old, new


def _mutate_change_cost_expr(uni_defn) -> str:
    """Scenario (c): the template changes Cost's DAX body."""
    f = _uni_report_file(uni_defn)
    content = f.read_text(encoding="utf-8")
    block = te.extract_block(content, "measure", "Cost")
    assert block and "SUM(Ads[cost])" in block, "fixture drifted: Cost body"
    new_block = block.replace("SUM(Ads[cost])", "SUM(Ads[cost]) + 0", 1)
    updated = te.replace_block(content, "measure", "Cost", new_block)
    assert updated is not None
    te.assert_tmdl_valid(updated)
    f.write_text(updated, encoding="utf-8")
    return "SUM(Ads[cost]) + 0"


def _mutate_add_campaigns_table(uni_defn) -> None:
    """Scenario (d): the template gains the trimmed Campaigns fact table plus
    its relationship into _Accounts."""
    shutil.copy2(EXTRAS / "Campaigns.tmdl", uni_defn / "tables" / "Campaigns.tmdl")
    model_f = uni_defn / "model.tmdl"
    model_f.write_text(
        te.register_table(model_f.read_text(encoding="utf-8"), "Campaigns"),
        encoding="utf-8")
    rel_f = uni_defn / "relationships.tmdl"
    rel_block = (EXTRAS / "campaigns_accounts.relationship.tmdl").read_text(
        encoding="utf-8")
    rel_f.write_text(
        te.append_relationship_block(rel_f.read_text(encoding="utf-8"),
                                     rel_block),
        encoding="utf-8")


def _mutate_diverge_growth_ads_view(growth_defn) -> None:
    """Make the derived Ads partition read a DIFFERENT source view than the
    template's (source-divergent table)."""
    f = growth_defn / "tables" / "Ads.tmdl"
    content = f.read_text(encoding="utf-8")
    assert '"ads_n"' in content
    f.write_text(content.replace('"ads_n"', '"ads_custom_n"'), encoding="utf-8")


def _mutate_add_unique_clicks(uni_defn) -> None:
    """The template adds the unique_clicks column to Ads."""
    block = (EXTRAS / "ads_unique_clicks.column.tmdl").read_text(
        encoding="utf-8").rstrip("\n")
    f = uni_defn / "tables" / "Ads.tmdl"
    content = te.insert_column_block(f.read_text(encoding="utf-8"), block)
    te.assert_tmdl_valid(content)
    f.write_text(content, encoding="utf-8")


ADS_CAL_REL_GUID = "5308195d-e93f-4da5-8a9e-b0086650a65f"


def _mutate_growth_rel_inactive(growth_defn) -> None:
    """The derived model flips Ads.date -> __Calendar.Date to isActive: false."""
    f = growth_defn / "relationships.tmdl"
    content = f.read_text(encoding="utf-8")
    assert ADS_CAL_REL_GUID in content
    f.write_text(
        content.replace(f"relationship {ADS_CAL_REL_GUID}\n",
                        f"relationship {ADS_CAL_REL_GUID}\n\tisActive: false\n"),
        encoding="utf-8")


# ════════════════════════════════════════════════════════════════════
# FIXTURE SELF-CHECKS
# ════════════════════════════════════════════════════════════════════

def test_fixture_universal_mini_parses(tmp_path):
    model = parse_model(UNI_SRC / "definition")
    assert set(model.tables) == UNI_TABLES
    assert set(_measures(UNI_SRC / "definition")) == UNI_MEASURES
    assert len(parse_relationships_file(
        UNI_SRC / "definition" / "relationships.tmdl")) == 2
    # The template snapshot (drift-doctor capture) records the fact tables,
    # the source view per table and the shared parameter names.
    copy_pair(tmp_path)
    fy = pair_fleet(tmp_path)
    r = drift("capture", fleet=fy)
    assert r.exit_code == 0, r.output
    snap = json.loads((tmp_path / ".drift-doctor" / "baseline.json")
                      .read_text(encoding="utf-8"))
    assert snap["fact_tables"] == ["Ads"]
    assert snap["table_source_views"] == {
        "Ads": "ads_n", "_Accounts": "_accounts_n", "__Calendar": "__calendar_n"}
    assert set(snap["expressions"]) == {
        "v-fromdate", "v-daysAgo", "v-isRolling", "v-rows_limit"}
    # zero parse warnings
    assert "Warning: Failed to parse" not in r.output


def test_fixture_growth_mini_is_clean_superset(tmp_path):
    """growth_mini must be drift-clean against universal_mini: underlay
    identical, overlay recognized as allowed customization."""
    uni = parse_model(UNI_SRC / "definition")
    gro = parse_model(GRO_SRC / "definition")
    assert set(gro.tables) == UNI_TABLES | {"Cohort Performance", "Growth Measures"}
    assert "Cohort Retention %" in _measures(GRO_SRC / "definition"), \
        "overlay measure missing"
    assert len(parse_relationships_file(
        GRO_SRC / "definition" / "relationships.tmdl")) == 3

    copy_pair(tmp_path)
    fy = pair_fleet(tmp_path)
    assert drift("capture", fleet=fy).exit_code == 0
    r = drift("detect", fleet=fy)
    # no failure reasons
    assert r.exit_code == 0, r.output
    _fleet, findings, _report = capture_and_detect(tmp_path)
    kinds = [f.kind for f in findings]
    assert [k for k in kinds if k in ("measure.expression_drift",
                                      "expression.drift")] == []
    assert [k for k in kinds if k == "relationship.missing"] == []
    # the overlay relationship is ALLOWED (endpoint on a derived-only table):
    # it is the only extra relationship and is never flagged
    extra = sorted(set(gro.relationships) - set(uni.relationships))
    assert extra == ["Cohort Performance[date] -> __Calendar[Date]"]
    flagged = [f.ref for f in findings
               if f.kind.startswith("relationship.extra") and not f.advisory]
    assert flagged == []
    assert [f.ref for f in findings if f.kind.startswith("relationship.extra")
            and f.ref != extra[0]] == []
    assert "Warning: Failed to parse" not in r.output


def test_fixture_tmdl_structure_is_tab_indented():
    """Block lookup depends on REAL tabs at exact depths — pin that every
    fixture object declaration sits at one hard tab, and that the tool's own
    write guard accepts every fixture table file."""
    space_decl = re.compile(r"^ +(?:measure|column|partition|hierarchy)\b")
    tab_decl = re.compile(r"^\t(?:measure|column|partition|hierarchy)\b",
                          re.MULTILINE)
    for model in (UNI_SRC, GRO_SRC):
        for f in sorted((model / "definition" / "tables").glob("*.tmdl")):
            content = f.read_text(encoding="utf-8")
            te.assert_tmdl_valid(content)  # tool write guard
            assert tab_decl.search(content), (
                f"{f.name}: no tab-indented object declarations")
            for ln in content.split("\n"):
                assert not space_decl.match(ln), (
                    f"{f.name}: space-indented object declaration: {ln[:60]!r}")
    uni_report = (UNI_SRC / "definition" / "tables" / "#Report Measures.tmdl"
                  ).read_text(encoding="utf-8")
    assert te.block_span(uni_report, "measure", "CTR") is not None
    gm = (GRO_SRC / "definition" / "tables" / "Growth Measures.tmdl"
          ).read_text(encoding="utf-8")
    assert te.block_span(gm, "measure", "Cohort Retention %") is not None


def test_fixture_report_loads_and_references_both_layers():
    report_roots = [REP_SRC]
    for p in REP_SRC.rglob("*.json"):
        json.loads(p.read_text(encoding="utf-8"))  # must be valid JSON
    # Every binding form the cascade rewrites: for CTR that is
    # 3 Measure.Property + 2 queryRef + 1 nativeQueryRef + 1 gated projection
    # displayName.
    assert count_report_refs(report_roots, "CTR") == 7
    assert count_report_refs(report_roots, "Cohort Retention %") == 6
    visual = (REP_SRC / "definition" / "pages" / "main_page" / "visuals"
              / "mini_matrix" / "visual.json").read_text(encoding="utf-8")
    assert '"#Report Measures.CTR"' in visual and '"CTR"' in visual
    bookmark = (REP_SRC / "definition" / "bookmarks" / "mini.bookmark.json"
                ).read_text(encoding="utf-8")
    assert '"#Report Measures.CTR"' in bookmark


def test_fixture_extras_fragments():
    camp = (EXTRAS / "Campaigns.tmdl").read_text(encoding="utf-8")
    te.assert_tmdl_valid(camp)
    assert camp.startswith("///") or camp.startswith("table")
    assert te.block_span(camp, "column", "account_x_id") is not None
    col = (EXTRAS / "ads_unique_clicks.column.tmdl").read_text(encoding="utf-8")
    assert col.startswith("\tcolumn unique_clicks")
    rel = (EXTRAS / "campaigns_accounts.relationship.tmdl").read_text(
        encoding="utf-8")
    assert "Campaigns.account_x_id" in rel and "_Accounts.account_x_id" in rel


def test_overlay_hash_canary_trips_on_overlay_mutation(tmp_path):
    """(g) A deliberately broken 'sync' that mutates an overlay file MUST be
    caught by the overlay-hash assertion helper — prove the net trips."""
    _, growth_model = copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    before = overlay_hashes(growth_defn)
    victim = growth_defn / "tables" / "Growth Measures.tmdl"
    victim.write_text(
        victim.read_text(encoding="utf-8")
        + f"\n\tmeasure 'broken sync artifact' = 1\n\t\tlineageTag: {uuid.uuid4()}\n",
        encoding="utf-8")
    with pytest.raises(AssertionError, match="OVERLAY"):
        assert_overlay_untouched(before, growth_defn)


# ════════════════════════════════════════════════════════════════════
# ENGINE SCENARIOS
# ════════════════════════════════════════════════════════════════════

def test_sync_adds_new_universal_measure(tmp_path):
    """(a) measure ADDED to the template -> present in growth after sync."""
    uni_defn, growth_model = copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    name = _mutate_add_measure(uni_defn)
    before = overlay_hashes(growth_defn)

    run = run_sync(tmp_path)

    assert sync_kinds(run.findings) == ["measure.missing"], (
        "expected exactly ONE sync item (the added measure); a silently "
        f"broken detector yields none — got {sync_kinds(run.findings)}")
    assert run.failed == 0
    assert run.applied >= 1

    gro = _measures(growth_defn)
    assert name in gro, f"'{name}' was not spliced into growth"
    uni_m = _measures(uni_defn)[name]
    assert _norm(gro[name].expression) == _norm(uni_m.expression)
    assert_overlay_untouched(before, growth_defn)


def test_sync_renames_measure_with_full_cascade(tmp_path):
    """(b) measure RENAMED in the template -> declaration + DAX refs +
    report/bookmark refs cascaded in growth."""
    uni_defn, growth_model = copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    report_root = growth_model.parent / "Mini.Report"
    old, new = _mutate_rename_ctr(uni_defn)
    before = overlay_hashes(growth_defn)

    run = run_sync(tmp_path)

    assert sync_kinds(run.findings).count("measure.renamed") == 1, (
        f"rename detector did not emit the rename — got {sync_kinds(run.findings)}")
    assert run.failed == 0

    gro = _measures(growth_defn)
    assert new in gro, f"declaration not renamed to '{new}'"
    assert old not in gro, f"old declaration '{old}' remains"
    # DAX reference cascade (growth's x-Selector 1 NAMEOF entry)
    x = (growth_defn / "tables" / "x-Selector 1.tmdl").read_text(encoding="utf-8")
    assert f"[{new}]" in x and f"[{old}]" not in x
    # report + bookmark cascade
    assert count_report_refs([report_root], old) == 0, (
        "report still references the OLD measure name — cascade failed")
    assert count_report_refs([report_root], new) == 7
    visual = (report_root / "definition" / "pages" / "main_page" / "visuals"
              / "mini_matrix" / "visual.json").read_text(encoding="utf-8")
    assert f'"#Report Measures.{new}"' in visual  # queryRef rewritten
    assert_overlay_untouched(before, growth_defn)


def test_sync_overwrites_expression_drift(tmp_path):
    """(c, positive) expression CHANGED in the template -> growth overwritten."""
    uni_defn, growth_model = copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    _mutate_change_cost_expr(uni_defn)
    before = overlay_hashes(growth_defn)

    run = run_sync(tmp_path)

    assert sync_kinds(run.findings) == ["measure.expression_drift"], (
        f"expected exactly the Cost drift item — got {sync_kinds(run.findings)}")
    assert run.failed == 0
    assert run.applied >= 1

    gro_cost = _measures(growth_defn)["Cost"]
    uni_cost = _measures(uni_defn)["Cost"]
    assert _norm(gro_cost.expression) == _norm(uni_cost.expression)
    assert_overlay_untouched(before, growth_defn)


def test_override_blocks_expression_overwrite(tmp_path):
    """(c, negative) same drift, but the measure is override-declared ->
    file NOT overwritten; the drift lands in the overridden bucket."""
    uni_defn, growth_model = copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    _mutate_change_cost_expr(uni_defn)
    report_file = growth_defn / "tables" / "#Report Measures.tmdl"
    file_before = sha(report_file)
    before = overlay_hashes(growth_defn)

    overrides = [{"object": "#Report Measures[Cost]", "kind": "expression",
                  "reason": "fixture: intentional divergence",
                  "owner": "ckapsalis", "added": "2026-07-03"}]
    run = run_sync(tmp_path, overrides=overrides)

    assert sha(report_file) == file_before, (
        "overridden expression drift was STILL overwritten — override "
        "contract broken")
    overridden_bucket = [o for o in run.model_report().get("overridden", [])
                         if o["kind"] == "measure.expression_drift"]
    skipped_override = run.count("overridden", "skipped_override")
    assert overridden_bucket or skipped_override >= 1, (
        "no trace of the override: neither an overridden report entry nor a "
        "skipped_override count")
    assert_overlay_untouched(before, growth_defn)


def test_sync_splices_new_table_with_relationship(tmp_path):
    """(d) table ADDED to the template (with a relationship to _Accounts) ->
    table + relationship spliced into growth."""
    uni_defn, growth_model = copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    _mutate_add_campaigns_table(uni_defn)
    before = overlay_hashes(growth_defn)

    run = run_sync(tmp_path)

    assert sync_kinds(run.findings) == ["relationship.missing", "table.missing"], (
        f"expected the table gap + its _Accounts relationship — got "
        f"{sync_kinds(run.findings)}")
    assert run.failed == 0
    assert run.applied >= 1

    assert (growth_defn / "tables" / "Campaigns.tmdl").exists()
    gro = parse_model(growth_defn)
    assert "Campaigns" in gro.tables
    # measures ride along with the table copy
    assert "countrows campaigns" in _measures(growth_defn)
    model = (growth_defn / "model.tmdl").read_text(encoding="utf-8")
    assert "ref table Campaigns" in model and '"Campaigns"' in model
    rels = parse_relationships_file(growth_defn / "relationships.tmdl")
    assert "Campaigns[account_x_id] -> _Accounts[account_x_id]" in rels, (
        "Campaigns -> _Accounts relationship was not spliced")
    assert_overlay_untouched(before, growth_defn)


def test_sync_is_idempotent(tmp_path):
    """(f) after one sync of the combined mutations, a second run applies
    nothing and changes no bytes (model + report)."""
    uni_defn, growth_model = copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    report_root = growth_model.parent / "Mini.Report"
    _mutate_add_measure(uni_defn)
    _mutate_rename_ctr(uni_defn)
    _mutate_change_cost_expr(uni_defn)
    _mutate_add_campaigns_table(uni_defn)
    before = overlay_hashes(growth_defn)

    run1 = run_sync(tmp_path)
    assert len(sync_kinds(run1.findings)) >= 4
    assert run1.failed == 0
    assert run1.applied >= 1
    assert_overlay_untouched(before, growth_defn)

    # Run to QUIESCENCE first: a rename can cascade a follow-up that a
    # single pass only sees on the next pass.
    for _ in range(3):
        if run_sync(tmp_path).applied == 0:
            break

    # Fresh detect against the mutated template — the second run must be a
    # no-op, semantically AND byte-wise.
    model_snapshot = tree_hashes(growth_defn)
    report_snapshot = tree_hashes(report_root)
    run2 = run_sync(tmp_path)
    applied2_sync = [k for k, _ref in run2.applied_items if is_sync_kind(k)]
    assert len(applied2_sync) == 0, (
        f"second run applied {len(applied2_sync)} sync item(s) — sync is not "
        f"idempotent; second-run sync items: {sync_kinds(run2.findings)}")
    assert run2.failed == 0
    assert tree_hashes(growth_defn) == model_snapshot, (
        "second run changed model bytes — sync is not idempotent")
    assert tree_hashes(report_root) == report_snapshot, (
        "second run changed report bytes — sync is not idempotent")


def _enrichment_note(run, tmp_path):
    """The view-enrichment notice, from the run result or the registry
    (<state_dir>/view_enrichment_outstanding.json)."""
    for attr in ("notices", "view_enrichment"):
        note = getattr(run.result, attr, None)
        if note:
            return note
    reg = tmp_path / ".drift-doctor" / "view_enrichment_outstanding.json"
    if reg.exists():
        entries = json.loads(reg.read_text(encoding="utf-8")).get("entries", [])
        return [e for e in entries if e.get("table") == "Ads"]
    return None


def test_divergent_view_column_added_with_enrichment_note(tmp_path):
    """(divergence, columns) growth's Ads reads a DIFFERENT view than the
    template. A template column addition on that table IS still applied
    (batch contract: apply + view-enrichment notice, not a skip)."""
    uni_defn, growth_model = copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    _mutate_diverge_growth_ads_view(growth_defn)
    _mutate_add_unique_clicks(uni_defn)
    before = overlay_hashes(growth_defn)

    run = run_sync(tmp_path)
    # sanity: detection sees the divergence
    assert "Ads" in (run.model_report().get("source_divergent_tables") or {}), (
        "fixture mutation failed to register Ads as source-divergent")

    assert sync_kinds(run.findings) == ["column.missing"], (
        f"expected exactly the unique_clicks column gap — got "
        f"{sync_kinds(run.findings)}")
    assert run.failed == 0
    assert run.applied >= 1

    assert parse_model(growth_defn).column("Ads", "unique_clicks") is not None, (
        "column addition on a source-divergent table was NOT applied — "
        "batch contract says apply + view-enrichment notice")
    assert _enrichment_note(run, tmp_path), (
        "no view-enrichment entry found (run result or "
        "view_enrichment_outstanding.json registry)")

    # second run: nothing left to do
    run2 = run_sync(tmp_path)
    assert run2.applied == 0
    assert_overlay_untouched(before, growth_defn)


def test_declared_absent_column_is_never_spliced(tmp_path):
    """(overrides, columns) growth declares the template's new
    Ads[unique_clicks] ABSENT: its view has no such field, so a splice would
    break refresh. Detection must emit NO column.missing item, the sync must
    leave the column out, and the declaration must surface as an ACK — never a
    silent skip."""
    uni_defn, growth_model = copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    _mutate_add_unique_clicks(uni_defn)
    before = overlay_hashes(growth_defn)

    overrides = [{"object": "Ads[unique_clicks]", "kind": "absent",
                  "reason": "fixture: growth's Ads view carries no unique_clicks field",
                  "owner": "ckapsalis", "added": date(2026, 9, 8)}]
    run = run_sync(tmp_path, overrides=overrides)
    mr = run.model_report()

    # detection honoured the declaration at the source...
    col_overridden = [(o["kind"], o["ref"], o["override"]) for o in mr["overridden"]
                      if o["kind"].startswith("column.")]
    assert col_overridden == [("column.missing", "Ads[unique_clicks]", {
        "object": "Ads[unique_clicks]", "kind": "absent",
        "reason": "fixture: growth's Ads view carries no unique_clicks field",
        "owner": "ckapsalis", "added": "2026-09-08"})]
    assert [f for f in mr["findings"]
            if f["kind"] in ("column.missing", "extra.column")
            and f["ref"].startswith("Ads[")] == []
    assert mr["counts"]["by_kind"].get("column.missing", 0) == 0
    # ...and ACK'd it, so the sync output names the suppression
    acks = [o for o in run.result.outcomes
            if o.status in ("overridden", "skipped_override")
            and o.finding.ref == "Ads[unique_clicks]"]
    assert acks and "absent" in acks[0].reason, [
        (o.status, o.finding.ref, o.reason) for o in run.result.outcomes]

    assert "column.missing" not in sync_kinds(run.findings), (
        f"declared-absent column still emitted as a splice item: "
        f"{sync_kinds(run.findings)}")
    assert run.failed == 0

    assert parse_model(growth_defn).column("Ads", "unique_clicks") is None, (
        "declared-absent column was spliced anyway")
    assert_overlay_untouched(before, growth_defn)

    # defense in depth: even a stale finding that still carries the gap must
    # be refused by the batch apply (override re-check at apply time)
    from tmdl_drift_doctor.baseline import load_baseline
    from tmdl_drift_doctor.detect import Finding
    from tmdl_drift_doctor.remediate import remediate
    stale = Finding(kind="column.missing", model=MODEL, ref="Ads[unique_clicks]",
                    detail="stale finding carried over from an earlier detect")
    res = remediate(run.fleet, [stale], baseline=load_baseline(run.fleet))
    assert [o for o in res.outcomes if o.status == "applied"] == []
    assert parse_model(growth_defn).column("Ads", "unique_clicks") is None


def test_divergent_endpoint_relationship_not_rewritten(tmp_path):
    """(divergence, relationships) a relationship.modified whose endpoint
    table is source-divergent must be SKIPPED in batch mode."""
    uni_defn, growth_model = copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    _mutate_diverge_growth_ads_view(growth_defn)
    _mutate_growth_rel_inactive(growth_defn)
    before = overlay_hashes(growth_defn)

    run = run_sync(tmp_path)
    assert "Ads" in (run.model_report().get("source_divergent_tables") or {})

    assert sync_kinds(run.findings) == ["relationship.modified"], (
        f"expected exactly the modified Ads->__Calendar relationship — got "
        f"{sync_kinds(run.findings)}")
    assert run.failed == 0

    rels = (growth_defn / "relationships.tmdl").read_text(encoding="utf-8")
    block = rels.split(ADS_CAL_REL_GUID)[1].split("relationship")[0]
    assert "isActive: false" in block, (
        "the sync REWROTE a modified relationship on a source-divergent "
        "endpoint — batch mode must skip it (skipped_divergent)")
    assert run.count("skipped_divergent") >= 1, (
        "relationship was preserved but no skipped_divergent count")
    assert_overlay_untouched(before, growth_defn)


def test_ifilter_category_smoke(tmp_path):
    """ifilter.* is included in sync mode. The minis carry no i- measures,
    so this pins only that enabling the kind cannot crash or corrupt the
    overlay on a pristine pair."""
    _uni_defn, growth_model = copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    before = overlay_hashes(growth_defn)

    run = run_sync(tmp_path, allowed=tuple(SYNC_KINDS) + ("ifilter",))
    assert run.failed == 0, (
        "ifilter regeneration failed on the fixture pair")
    assert_overlay_untouched(before, growth_defn)


def test_expression_cascade_ignores_lineage_tag_only_difference(tmp_path):
    """A lineageTag is per-model identity metadata, not parameter content —
    two models carrying the same `v-fromdate` VALUE legitimately hold
    different tags (this very fixture pair does).

    Pins both directions:
      - tag-only difference  -> NO expression.drift finding
      - value difference     -> finding fires, and the cascade writes the
                                template's VALUE while preserving the
                                derived model's lineageTag
    """
    _uni_defn, growth_model = copy_pair(tmp_path)
    growth_defn = growth_model / "definition"

    _fleet, findings, _report = capture_and_detect(tmp_path)
    phantom = [f for f in findings if f.kind == "expression.drift"]
    assert not phantom, (
        f"expression.drift fired on a lineageTag-only difference: "
        f"{[f.ref for f in phantom]}")

    # Now a REAL value divergence: change the derived v-fromdate date.
    cli_f = growth_defn / "expressions.tmdl"
    cli_text = cli_f.read_text(encoding="utf-8")
    assert "#date(2025, 1, 1)" in cli_text
    cli_f.write_text(cli_text.replace("#date(2025, 1, 1)",
                                      "#date(2019, 6, 6)", 1),
                     encoding="utf-8")
    cli_tag = next(l.strip() for l in cli_f.read_text(encoding="utf-8").split("\n")
                   if l.lstrip().startswith("lineageTag:"))

    run = run_sync(tmp_path)
    fired = [f for f in run.findings if f.kind == "expression.drift"]
    assert len(fired) == 1, f"expected exactly one cascade item, got {fired}"
    assert run.failed == 0

    after = cli_f.read_text(encoding="utf-8")
    assert "#date(2025, 1, 1)" in after, "the template's value was not cascaded"
    assert "#date(2019, 6, 6)" not in after
    assert cli_tag in after, (
        "the derived model's lineageTag must survive the cascade — the "
        "template's tag must not be stamped into the derived model")


# ════════════════════════════════════════════════════════════════════
# wrong-host drift: verbatim block MOVE, never a body overwrite
# ════════════════════════════════════════════════════════════════════

WRONG_HOST_ONLY = ("measure.wrong_host",)


def _mutate_growth_relocate_ctr(growth_defn, extend: bool) -> str:
    """Derived-side wrong host: move CTR's block in GROWTH from the template's
    host ('#Report Measures') onto 'Ads'; optionally extend the DAX first (a
    derived-only body extension an overwrite remedy would destroy). Returns
    the exact block that ends up on the wrong host."""
    src_f = growth_defn / "tables" / "#Report Measures.tmdl"
    dst_f = growth_defn / "tables" / "Ads.tmdl"
    src = src_f.read_text(encoding="utf-8")
    block = te.extract_block(src, "measure", "CTR")
    assert block is not None, "fixture drifted: CTR not on #Report Measures"
    if extend:
        block = block.replace(
            "DIVIDE(", "DIVIDE( /* client-extension #484 */ ", 1)
        assert "client-extension #484" in block
    src_f.write_text(te.remove_block(src, "measure", "CTR"),
                     encoding="utf-8")
    dst_f.write_text(
        te.insert_measure_block(dst_f.read_text(encoding="utf-8"), block),
        encoding="utf-8")
    te.assert_tmdl_valid(dst_f.read_text(encoding="utf-8"))
    return block


def _wrong_host(findings):
    return [f for f in findings if f.kind == "measure.wrong_host"]


def test_wrong_host_client_extension_moved_verbatim(tmp_path):
    """The derived model hosts CTR on the wrong table WITH a derived-only body
    extension -> the block is RELOCATED byte-identically (extension +
    lineageTag survive); an overwrite remedy would destroy the DAX."""
    _uni_defn, growth_model = copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    block = _mutate_growth_relocate_ctr(growth_defn, extend=True)
    before = overlay_hashes(growth_defn)

    run = run_sync(tmp_path, allowed=WRONG_HOST_ONLY)

    wrong_host = _wrong_host(run.findings)
    assert [f.ref for f in wrong_host] == ["#Report Measures[CTR]"]
    assert wrong_host[0].data.get("from_table") == "Ads"
    assert run.failed == 0

    report_after = (growth_defn / "tables" / "#Report Measures.tmdl").read_text(
        encoding="utf-8")
    ads_after = (growth_defn / "tables" / "Ads.tmdl").read_text(encoding="utf-8")
    moved = te.extract_block(report_after, "measure", "CTR")
    assert moved == block, (
        "the derived block did not survive the move byte-identically — "
        "the remedy must NEVER touch the DAX")
    assert "client-extension #484" in moved
    assert te.extract_block(ads_after, "measure", "CTR") is None, (
        "CTR still declared on the wrong host after the move")
    te.assert_tmdl_valid(report_after)
    te.assert_tmdl_valid(ads_after)
    assert_overlay_untouched(before, growth_defn)


def test_wrong_host_body_equal_is_emitted_and_moved(tmp_path):
    """A body-EQUAL wrong host is still drift — emitted with the same move
    remedy."""
    _uni_defn, growth_model = copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    block = _mutate_growth_relocate_ctr(growth_defn, extend=False)

    run = run_sync(tmp_path, allowed=WRONG_HOST_ONLY)

    wrong_host = _wrong_host(run.findings)
    assert [f.ref for f in wrong_host] == ["#Report Measures[CTR]"], (
        "body-equal wrong-host measure was not detected")
    assert "body matches the template" in wrong_host[0].detail
    assert run.failed == 0
    report_after = (growth_defn / "tables" / "#Report Measures.tmdl").read_text(
        encoding="utf-8")
    assert te.extract_block(report_after, "measure", "CTR") == block


def test_wrong_host_move_skipped_on_qualified_reference(tmp_path):
    """A fully-qualified `Ads[CTR]` reference elsewhere in the model would
    dangle if the block moved -> the move is refused and both files stay
    byte-identical."""
    _uni_defn, growth_model = copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    _mutate_growth_relocate_ctr(growth_defn, extend=False)
    acc_f = growth_defn / "tables" / "_Accounts.tmdl"
    ref_block = (f"\tmeasure '-helper qualref 484' = Ads[CTR] + 0\n"
                 f"\t\tlineageTag: {uuid.uuid4()}")
    acc_f.write_text(
        te.insert_measure_block(acc_f.read_text(encoding="utf-8"), ref_block),
        encoding="utf-8")

    report_f = growth_defn / "tables" / "#Report Measures.tmdl"
    ads_f = growth_defn / "tables" / "Ads.tmdl"
    report_before, ads_before = sha(report_f), sha(ads_f)

    run = run_sync(tmp_path, allowed=WRONG_HOST_ONLY)

    assert [f.ref for f in _wrong_host(run.findings)] == ["#Report Measures[CTR]"]
    assert sha(report_f) == report_before and sha(ads_f) == ads_before, (
        "move proceeded despite a fully-qualified reference to the current "
        "host — those refs now dangle")


def test_wrong_host_move_skipped_when_host_table_absent(tmp_path):
    """The template's host table doesn't exist in the derived model -> the
    move is refused (table.missing owns creating it); nothing changes."""
    uni_defn, growth_model = copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    # the template gains a table growth lacks, hosting a measure growth
    # carries on a different table
    _mutate_add_campaigns_table(uni_defn)
    zed = (f"\tmeasure 'Zed 484' = COUNTROWS(Campaigns)\n"
           f"\t\tlineageTag: {uuid.uuid4()}")
    camp_f = uni_defn / "tables" / "Campaigns.tmdl"
    camp_f.write_text(
        te.insert_measure_block(camp_f.read_text(encoding="utf-8"), zed),
        encoding="utf-8")
    report_f = growth_defn / "tables" / "#Report Measures.tmdl"
    report_f.write_text(
        te.insert_measure_block(
            report_f.read_text(encoding="utf-8"),
            f"\tmeasure 'Zed 484' = COUNTROWS(Ads)\n"
            f"\t\tlineageTag: {uuid.uuid4()}"),
        encoding="utf-8")
    report_before = sha(report_f)

    run = run_sync(tmp_path, allowed=WRONG_HOST_ONLY)

    wrong_host = _wrong_host(run.findings)
    assert [f.ref for f in wrong_host] == ["Campaigns[Zed 484]"]
    assert wrong_host[0].data.get("from_table") == "#Report Measures"
    assert sha(report_f) == report_before, (
        "move fired although the derived model lacks the template's host table")
    assert te.extract_block(report_f.read_text(encoding="utf-8"),
                            "measure", "Zed 484") is not None

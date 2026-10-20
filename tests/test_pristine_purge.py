"""Pristine-category purge (purge.*): the owner's law.

Ported from the user's layer-3 integration suite (``test_pristine_purge.py``).
Runs drift-doctor's sync detection and remediation on the sync_engine minis
(``tests/fixtures/sync_engine/``) written as a two-model fleet per test:

  - an encoded z-Map is installed on both minis (3 categories, so the
    infrastructure threshold is meaningful); the growth copy gains client-only
    content in the 'Performance' category;
  - PRISTINE client (no divergent view, no custom coupling): the client-only
    x/y entries, their client-only measure, the client-only column on Ads, the
    client-only z-Map row and the client-only template-to-template
    relationship are ALL purged; the digest has no fieldparam needs-decision
    lines;
  - NON-pristine client (divergent Ads view AND a custom-coupling
    relationship): kept + digest, nothing purged; the data-extension entry is
    kept silently;
  - cohort-named entries are untouched and never listed;
  - fleet.yml retention overrides (fieldparam_entries / mapping_rows) win;
  - a second run is a byte-level no-op.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from conftest import drift_json, write_fleet
from port_helpers_b6_change import (
    EXTRAS,
    GRO_SRC,
    SOURCE_IDENTITY,
    UNI_SRC,
    ZMAP_TABLE,
    axis_entries,
    insert_axis_row,
    install_zmap,
    zmap_rows,
)
from tmdl_drift_doctor import tmdl_edit as te
from tmdl_drift_doctor.tmdl import parse_relationships_text

PURGE_KINDS = ("purge.fieldparam_entry", "purge.measure", "purge.column",
               "purge.relationship", "purge.zmap_row", "purge.table")
MODEL = "fixture_growth"

# 3 categories -> a table serving one category sits at 1/3 (< 0.5, kept);
# __Calendar (Date_y everywhere) and #Report Measures (Blank Dimension_y in 2)
# cross the threshold and are excluded as shared infrastructure.
UNI_ZMAP_ROWS = [
    ["Performance", "", "Performance_x"],
    ["Performance", "", "Date_y"],
    ["Performance", "", "Blank Dimension_y"],
    ["Performance", "", "perf_dims_y"],
    ["Directory", "", "Account_y"],
    ["Directory", "", "Service_y"],
    ["Directory", "", "Date_y"],
    ["Blanks", "", "Blank Dimension_y"],
    ["Blanks", "", "Date_y"],
]
CLIENT_ONLY_ZROW = ["Performance", "", "custom_perf_y"]

X_ENTRY = ('\t\t\t\t    ( "Custom Ads Metric", '
           'NAMEOF ( [Custom Ads Metric] ), 99, "Performance_x" ),')
Y_ENTRY_COLUMN = ('\t\t\t\t("Ad Network", NAMEOF(\'Ads\'[network]), 50, '
                  '"perf_dims_y", "Other"),')
Y_ENTRY_COHORT = ('\t\t\t\t("Cohort Split", '
                  'NAMEOF(\'Cohort Performance\'[date]), 60, '
                  '"perf_dims_y", "Other"),')
X_ENTRY_KEPT = ('\t\t\t\t    ( "Client Kept Special", '
                'NAMEOF ( [Custom Const] ), 98, "Performance_x" ),')
REL_GUID = "99999999-9999-9999-9999-999999999999"
REL_BLOCK = (f"relationship {REL_GUID}\n"
             f"\tfromColumn: Ads.cost\n"
             f"\ttoColumn: _Accounts.account_x_id\n")
CUSTOM_REL_GUID = "88888888-8888-8888-8888-888888888888"
CUSTOM_REL_BLOCK = (f"relationship {CUSTOM_REL_GUID}\n"
                    f"\tfromColumn: Ads.date\n"
                    f"\ttoColumn: Campaigns.date\n")

MEASURE_BLOCK = ("\tmeasure 'Custom Ads Metric' = SUM(Ads[cost]) + 1\n"
                 "\t\tlineageTag: 00000000-1111-2222-3333-555555555555\n")
CONST_MEASURE_BLOCK = ("\tmeasure 'Custom Const' = 1 + 1\n"
                       "\t\tlineageTag: 00000000-1111-2222-3333-"
                       "666666666666\n")
COLUMN_BLOCK = ("\tcolumn network\n"
                "\t\tdataType: string\n"
                "\t\tlineageTag: 00000000-1111-2222-3333-777777777777\n"
                "\t\tsummarizeBy: none\n"
                "\t\tsourceColumn: network\n\n"
                "\t\tannotation SummarizationSetBy = Automatic")

MEASURES_FILE = "#Report Measures.tmdl"
X_AXIS = "x-Selector 1.tmdl"
Y_AXIS = "y-Selector 1.tmdl"


# ─────────────────────────── helpers ───────────────────────────

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_pair(tmp_path: Path, *, zmap: bool = True):
    uni_model = tmp_path / "universal_mini" / "Performance.SemanticModel"
    growth_model = tmp_path / "growth_mini" / "Growth.SemanticModel"
    shutil.copytree(UNI_SRC, uni_model)
    shutil.copytree(GRO_SRC, growth_model)
    if zmap:
        install_zmap(uni_model / "definition", UNI_ZMAP_ROWS)
        install_zmap(growth_model / "definition",
                     UNI_ZMAP_ROWS + [CLIENT_ONLY_ZROW])
    return uni_model, growth_model


def _fleet(tmp_path: Path, uni_model: Path, growth_model: Path,
           overrides: dict | None = None) -> Path:
    entry = {"path": growth_model}
    if overrides:
        entry["overrides"] = overrides
    return write_fleet(
        tmp_path, uni_model, {MODEL: entry},
        mapping_tables=[ZMAP_TABLE],
        purge={"enabled": True},
        protected_name_pattern="cohort",
        source_identity=SOURCE_IDENTITY,
        allowlist={"kinds": list(PURGE_KINDS)},
    )


def _detect(fleet_yml: Path, *, sync: bool = True):
    """Fresh capture of the template, then detection (sync mode or not)."""
    from tmdl_drift_doctor.baseline import capture, load_baseline
    from tmdl_drift_doctor.detect import detect_fleet
    from tmdl_drift_doctor.fleet import load_fleet
    from tmdl_drift_doctor.ledger import Ledger
    fleet = load_fleet(fleet_yml)
    capture(fleet)
    baseline = load_baseline(fleet)
    findings = detect_fleet(fleet, baseline, Ledger(fleet.ledger_path),
                            sync=sync)
    return fleet, baseline, findings


def _run_purge(fleet_yml: Path):
    """Sync-mode detection plus a purge-only remediation pass (the Python API
    stands for the interactive yes the CLI requires for deletions)."""
    from tmdl_drift_doctor.remediate import remediate
    fleet, baseline, findings = _detect(fleet_yml)
    result = remediate(fleet, findings, kind="purge", sync=True,
                       baseline=baseline)
    return findings, result


def _purges(findings):
    return [f for f in findings if f.kind.startswith("purge.")]


def _fieldparam_decision_lines(result):
    return [d for d in result.digest
            if d["kind"] == "fieldparam_entry"
            and "needs-decision" in d["note"]]


def _add_client_only_content(growth_defn: Path) -> None:
    """The pristine scenario's client-only content in 'Performance':
    x entry + its client-only measure, y entries (column + cohort) under the
    client-only-on-axes key perf_dims_y, a client-only column on Ads, and a
    client-only relationship between two template tables."""
    mfile = growth_defn / "tables" / MEASURES_FILE
    mfile.write_text(te.insert_measure_block(mfile.read_text(encoding="utf-8"),
                                             MEASURE_BLOCK), encoding="utf-8")
    ads = growth_defn / "tables" / "Ads.tmdl"
    ads.write_text(te.insert_column_block(ads.read_text(encoding="utf-8"),
                                          COLUMN_BLOCK), encoding="utf-8")
    insert_axis_row(growth_defn / "tables" / X_AXIS, X_ENTRY, '"Cost",')
    insert_axis_row(growth_defn / "tables" / Y_AXIS, Y_ENTRY_COLUMN,
                    '"Account Name",')
    insert_axis_row(growth_defn / "tables" / Y_AXIS, Y_ENTRY_COHORT,
                    '"Ad Network",')
    rel_f = growth_defn / "relationships.tmdl"
    rel_f.write_text(te.append_relationship_block(
        rel_f.read_text(encoding="utf-8"), REL_BLOCK), encoding="utf-8")


def _add_kept_special(growth_defn: Path) -> None:
    mfile = growth_defn / "tables" / MEASURES_FILE
    mfile.write_text(te.insert_measure_block(mfile.read_text(encoding="utf-8"),
                                             CONST_MEASURE_BLOCK),
                     encoding="utf-8")
    insert_axis_row(growth_defn / "tables" / X_AXIS, X_ENTRY_KEPT, '"Cost",')


def _make_non_pristine(growth_defn: Path) -> None:
    """BOTH customization signals for 'Performance': Ads feeds a divergent
    view AND a client-built relationship couples Ads to a client-custom
    table."""
    ads = growth_defn / "tables" / "Ads.tmdl"
    content = ads.read_text(encoding="utf-8")
    assert '"ads_n"' in content
    ads.write_text(content.replace('"ads_n"', '"ads_custom_n"'),
                   encoding="utf-8")
    shutil.copy(EXTRAS / "Campaigns.tmdl",
                growth_defn / "tables" / "Campaigns.tmdl")
    rel_f = growth_defn / "relationships.tmdl"
    rel_f.write_text(te.append_relationship_block(
        rel_f.read_text(encoding="utf-8"), CUSTOM_REL_BLOCK), encoding="utf-8")


# ─────────────────────────── tests ───────────────────────────

def test_purge_categories_are_in_the_sync_allowlist():
    from tmdl_drift_doctor.detect import REMEDIABLE_KINDS, SYNC_ONLY_KINDS
    for kind in PURGE_KINDS:
        assert kind in REMEDIABLE_KINDS
        assert kind in SYNC_ONLY_KINDS


def test_pristine_category_client_only_content_is_purged(tmp_path):
    uni_model, growth_model = _copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    _add_client_only_content(growth_defn)

    findings, result = _run_purge(_fleet(tmp_path, uni_model, growth_model))
    got = sorted({f.kind for f in _purges(findings)})
    assert got == ["purge.column", "purge.fieldparam_entry",
                   "purge.measure", "purge.relationship",
                   "purge.zmap_row"], got
    assert result.count("failed") == 0 and result.count("applied") >= 5

    # entries: client-only x + y gone, everything else (incl. cohort) intact
    xnames = axis_entries(growth_defn / "tables" / X_AXIS)
    ynames = axis_entries(growth_defn / "tables" / Y_AXIS)
    assert ("Performance_x", "Custom Ads Metric",
            "Custom Ads Metric") not in xnames
    assert ("Performance_x", "Cost", "Cost") in xnames
    assert ("perf_dims_y", "Ad Network", "network") not in ynames
    # blanket COHORT EXCEPTION: untouched, in place
    assert ("perf_dims_y", "Cohort Split", "date") in ynames

    # the client-only measure + column are gone; template content intact
    measures = (growth_defn / "tables" / MEASURES_FILE).read_text(
        encoding="utf-8")
    assert te.block_span(measures, "measure", "Custom Ads Metric") is None
    assert te.block_span(measures, "measure", "Cost") is not None
    ads = (growth_defn / "tables" / "Ads.tmdl").read_text(encoding="utf-8")
    assert te.block_span(ads, "column", "network") is None
    assert te.block_span(ads, "column", "cost") is not None

    # the client-only template<->template relationship is gone; the
    # template's own relationships survive
    rels = (growth_defn / "relationships.tmdl").read_text(encoding="utf-8")
    assert REL_GUID not in rels
    assert "Ads[date] -> __Calendar[Date]" in parse_relationships_text(rels)

    # the client-only z-Map row key is gone, shared rows intact
    rows = zmap_rows(growth_defn / "tables" / f"{ZMAP_TABLE}.tmdl")
    assert CLIENT_ONLY_ZROW not in rows
    assert ["Performance", "", "Performance_x"] in rows

    # judgment surface: NO fieldparam needs-decision lines for the pristine
    # category (the digest split routes them to the purge channel)
    assert _fieldparam_decision_lines(result) == []


def test_pristine_purge_is_idempotent(tmp_path):
    uni_model, growth_model = _copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    _add_client_only_content(growth_defn)
    fy = _fleet(tmp_path, uni_model, growth_model)

    _findings, result = _run_purge(fy)
    assert result.count("failed") == 0 and result.count("applied") >= 5
    watched = sorted((growth_defn / "tables").glob("*.tmdl")) + \
        [growth_defn / "relationships.tmdl"]
    before = {p: _sha(p) for p in watched}

    findings2, result2 = _run_purge(fy)
    assert not _purges(findings2), (
        "second run re-detected purge items — not convergent")
    assert result2.count("applied") == 0
    assert {p: _sha(p) for p in watched} == before, "second run changed bytes"


def test_non_pristine_category_keeps_and_digests(tmp_path):
    uni_model, growth_model = _copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    _add_client_only_content(growth_defn)
    # a kept-category judgment entry whose measure touches no divergent table
    _add_kept_special(growth_defn)
    _make_non_pristine(growth_defn)
    fy = _fleet(tmp_path, uni_model, growth_model)

    _r, report = drift_json("detect", "--sync", fleet=fy)
    assert "Ads" in (report["models"][MODEL].get("source_divergent_tables")
                     or {}), (
        "fixture mutation failed to register Ads as source-divergent")
    findings, result = _run_purge(fy)
    perf_purges = [f for f in _purges(findings) if "Performance" in f.ref]
    assert perf_purges == [], (
        f"customized category (divergent view AND custom coupling) must "
        f"keep its client-only content: {[f.ref for f in perf_purges]}")
    # no purge may remove the divergent-hosted column or the Ads coupling
    assert not [f for f in findings if f.kind == "purge.column"]
    assert not [f for f in findings if f.kind == "purge.measure"]

    # entries survive
    xnames = axis_entries(growth_defn / "tables" / X_AXIS)
    ynames = axis_entries(growth_defn / "tables" / Y_AXIS)
    assert ("Performance_x", "Custom Ads Metric",
            "Custom Ads Metric") in xnames
    assert ("Performance_x", "Client Kept Special", "Custom Const") in xnames
    assert ("perf_dims_y", "Ad Network", "network") in ynames

    # rule 5: the judgment entry IS digest-listed ...
    lines = _fieldparam_decision_lines(result)
    assert any("Client Kept Special" in d["name"] for d in lines), lines
    # ... rule 3: the data-extension entries are kept SILENTLY
    assert not any("Ad Network" in d["name"] for d in lines), lines
    assert not any("Custom Ads Metric" in d["name"] for d in lines), lines
    assert result.count("failed") == 0


def test_overrides_retention_always_wins(tmp_path):
    uni_model, growth_model = _copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    _add_client_only_content(growth_defn)
    _add_kept_special(growth_defn)
    overrides = {
        "fieldparam_entries": [{"category": "Performance_x",
                                "entry": "Client Kept Special",
                                "reason": "client asked for this alias"}],
        "mapping_rows": [{"category": "Performance", "key": "custom_perf_y",
                          "reason": "client-specific slicer mapping"}],
    }
    fy = _fleet(tmp_path, uni_model, growth_model, overrides=overrides)

    _findings, result = _run_purge(fy)
    assert result.count("failed") == 0

    # the declared entry + z-Map row survive; the undeclared ones purge
    xnames = axis_entries(growth_defn / "tables" / X_AXIS)
    assert ("Performance_x", "Client Kept Special", "Custom Const") in xnames
    assert ("Performance_x", "Custom Ads Metric",
            "Custom Ads Metric") not in xnames
    rows = zmap_rows(growth_defn / "tables" / f"{ZMAP_TABLE}.tmdl")
    assert CLIENT_ONLY_ZROW in rows

    retained = [d for d in result.digest
                if "retained by client override" in d["note"]]
    assert {d["kind"] for d in retained} == {"fieldparam_entry", "zmap_row"}
    # retention is never a needs-decision judgment line
    assert _fieldparam_decision_lines(result) == []


def test_interactive_mode_emits_nothing(tmp_path):
    uni_model, growth_model = _copy_pair(tmp_path)
    _add_client_only_content(growth_model / "definition")
    _fleet_, _b, findings = _detect(_fleet(tmp_path, uni_model, growth_model),
                                    sync=False)
    assert not _purges(findings), (
        "pristine purge is a sync-mode law — a run without --sync must not "
        "offer it")


def test_minis_without_zmap_are_untouched(tmp_path):
    """A model pair with no z-Map (the raw minis) yields no purge items and
    no verdict crash — the purge needs the shared-category surface."""
    uni_model, growth_model = _copy_pair(tmp_path, zmap=False)
    _fleet_, _b, findings = _detect(_fleet(tmp_path, uni_model, growth_model))
    assert not _purges(findings)

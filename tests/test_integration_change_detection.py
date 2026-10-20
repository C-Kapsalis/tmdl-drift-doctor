"""Change detection: a derived model must be a superset of the template.

Ported from the user's layer-3 integration suite
(``test_integration_change_detection.py``, the six in-scope tests). The
original compared hand-built snapshot dicts or registry models through its own
drift analysis; here every case runs drift-doctor (``capture``, ``detect``,
``ratchet tighten|check --total-only``) on a fleet.yml written per test.

Stand-in fleet: ``tests/fixtures/b6-change/roastery`` (fictional coffee
roastery). The original tests were run unmodified against the same models
(original folder naming) and their verdicts are in parity-expected.json.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from conftest import drift, drift_json, edit_fleet_yml, write_fleet
from port_helpers_b6_change import (
    GAP_KINDS,
    mini_model,
    roastery_fleet,
    swap_variant,
)


# ───────────────────────── helpers ─────────────────────────

def _mini_fleet(tmp_path, table, template_cols, derived_cols, overrides=None):
    root = tmp_path / "fleet"
    tpl = mini_model(root / "template", table, template_cols)
    der = mini_model(root / "derived", table, derived_cols)
    entry = {"path": der}
    if overrides:
        entry["overrides"] = overrides
    fy = write_fleet(root, tpl, {"derived": entry},
                     allowlist={"kinds": ["column.missing"]})
    assert drift("capture", fleet=fy).exit_code == 0
    return fy


def _detect(fy, model="derived"):
    r, rep = drift_json("detect", fleet=fy)
    assert rep is not None, r.output
    return r, rep["models"][model]


def _refs(items, kind):
    return sorted(f["ref"] for f in items if f["kind"] == kind)


def _gaps(model_report):
    return sorted((f["kind"], f["ref"]) for f in model_report["findings"]
                  if f["kind"] in GAP_KINDS)


# ───────────────────── column overrides (I0465, I0466) ─────────────────────

def test_columns_override_acks_flagged_extra(tmp_path):
    """A declared client-only column on a shared table is acknowledged, never
    flagged; an undeclared extra on the same table still flags."""
    ovr = {"columns": [{"object": "__Calendar[m_cohort_qualifier_month]",
                        "kind": "extension",
                        "reason": "client cohort qualifier"}]}
    fy = _mini_fleet(tmp_path, "__Calendar", ["Date", "Month", "Year"],
                     ["Date", "Month", "Year", "m_cohort_qualifier_month",
                      "custom_extra"], overrides=ovr)
    _r, m = _detect(fy)
    # column drift == {missing: [], flagged_extra: [custom_extra]}
    assert _refs(m["findings"], "column.missing") == []
    assert _refs(m["findings"], "extra.column") == ["__Calendar[custom_extra]"]
    # acked extras == [m_cohort_qualifier_month]
    assert _refs(m["overridden"], "extra.column") == [
        "__Calendar[m_cohort_qualifier_month]"]
    # flagged total 1
    assert len(_refs(m["findings"], "extra.column")) == 1

    # no overrides -> both extras flag, nothing acknowledged
    fy2 = _mini_fleet(tmp_path / "plain", "__Calendar", ["Date", "Month", "Year"],
                      ["Date", "Month", "Year", "m_cohort_qualifier_month",
                       "custom_extra"])
    _r, m2 = _detect(fy2)
    assert _refs(m2["findings"], "extra.column") == [
        "__Calendar[custom_extra]", "__Calendar[m_cohort_qualifier_month]"]
    assert m2["overridden"] == []


def test_columns_override_acks_missing_absent(tmp_path):
    """A template column the client declared absent is acknowledged, never a
    missing-column gap; an undeclared missing column still fails."""
    meta = {"reason": "view has no report_name", "owner": "owner-a",
            "added": "2026-09-08"}
    table = "Google Play Installs"
    cols = ["date", "installs", "report_name"]

    def absent(*names):
        return {"columns": [dict(meta, object=f"{table}[{n}]", kind="absent")
                            for n in names]}

    fy = _mini_fleet(tmp_path / "one", table, cols, ["date"],
                     overrides=absent("report_name"))
    r, m = _detect(fy)
    assert _refs(m["findings"], "column.missing") == [f"{table}[installs]"]
    acked = [f for f in m["overridden"] if f["kind"] == "column.missing"]
    assert [f["ref"] for f in acked] == [f"{table}[report_name]"]
    assert {k: acked[0]["override"][k] for k in meta} == meta
    # col_missing_total == 1; fail reasons == ["1 missing column(s)"]
    assert m["counts"]["by_kind"].get("column.missing") == 1
    assert r.exit_code == 1

    # declare the other one too -> no column drift at all
    fy2 = _mini_fleet(tmp_path / "both", table, cols, ["date"],
                      overrides=absent("report_name", "installs"))
    r2, m2 = _detect(fy2)
    assert _refs(m2["findings"], "column.missing") == []
    assert "column.missing" not in m2["counts"]["by_kind"]
    assert r2.exit_code == 0
    assert _refs(m2["overridden"], "column.missing") == [
        f"{table}[installs]", f"{table}[report_name]"]

    # no overrides -> both fail
    fy3 = _mini_fleet(tmp_path / "none", table, cols, ["date"])
    _r3, m3 = _detect(fy3)
    assert _refs(m3["findings"], "column.missing") == [
        f"{table}[installs]", f"{table}[report_name]"]
    assert m3["overridden"] == []
    assert m3["counts"]["by_kind"].get("column.missing") == 2


# ───────────────────── template capture (I0467) ─────────────────────

METRIC_KEYS = [
    "measures", "f_measures", "user_facing_measures", "helper_measures",
    "fact_tables", "dax_expressions", "nameof_references", "relationships",
    "many_to_one", "family_tables", "table_column_counts", "table_columns",
    "family_table_refs",
]
PROVENANCE_KEYS = ["template_tree", "template_commit", "captured_at", "generator"]


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _strip_times(doc):
    doc = json.loads(json.dumps(doc))
    doc.pop("captured_at", None)
    (doc.get("provenance") or {}).pop("captured_at", None)
    return doc


def test_capture_universal_baseline(tmp_path):
    """Maintenance hook in the original (it wrote the metric snapshot plus the
    provenance stamp). Port: `capture` stores every template metric next to
    the object snapshot, and a recapture of an unchanged template is identical
    apart from the capture timestamp."""
    fy = roastery_fleet(tmp_path, {"clean": "clean"})
    root = fy.parent
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=port@example.com", "-c", "user.name=port",
         "commit", "-qm", "roastery template")

    assert drift("capture", fleet=fy).exit_code == 0
    path = root / ".drift-doctor" / "baseline.json"
    first = json.loads(path.read_text(encoding="utf-8"))
    metrics = first["metrics"]
    for key in METRIC_KEYS:
        assert key in metrics, key
    # the stand-in template's values, as the original snapshot recorded them
    assert metrics["measures"] == 4
    assert metrics["f_measures"] == 0
    assert metrics["user_facing_measures"] == 4
    assert metrics["helper_measures"] == 0
    assert metrics["dax_expressions"] == 4
    assert metrics["nameof_references"] == 0
    assert metrics["relationships"] == 2
    assert metrics["many_to_one"] == 2
    assert metrics["table_column_counts"] == {
        "Beans": 2, "Roastery Measures": 1, "Roasts": 4, "__Calendar": 2}
    for key in PROVENANCE_KEYS:
        assert first["provenance"][key], key

    assert drift("capture", fleet=fy).exit_code == 0
    second = json.loads(path.read_text(encoding="utf-8"))
    assert _strip_times(second) == _strip_times(first)


# ───────────────────── superset gate (I0468) ─────────────────────

DRIFT_GAPS = sorted([
    ("measure.missing", "Roastery Measures[Kilos per Batch]"),
    ("measure.expression_drift", "Roastery Measures[Total Batches]"),
    ("column.missing", "Beans[origin]"),
    ("relationship.missing", "Roasts[roast_date] -> __Calendar[Date]"),
    ("relationship.modified", "Roasts[bean_id] -> Beans[bean_id]"),
])


@pytest.mark.parametrize("variant,verdict", [
    ("clean", "pass"),
    ("drift", "fail"),
    ("divergent", "pass"),
])
def test_pattern_change_detection(tmp_path, variant, verdict):
    """Gaps (missing tables/measures/columns/relationships), expression drift
    and modified relationships fail; a source-divergent table's extra column
    is acknowledged, not drift."""
    fy = roastery_fleet(tmp_path, {variant: variant})
    assert drift("capture", fleet=fy).exit_code == 0
    r, m = _detect(fy, variant)
    if verdict == "fail":
        assert r.exit_code == 1, r.output
        assert _gaps(m) == DRIFT_GAPS
    else:
        assert r.exit_code == 0, r.output
        assert _gaps(m) == []
    if variant == "divergent":
        assert "Roasts" in (m.get("source_divergent_tables") or {})
        assert "Roasts[roast_level]" not in [f["ref"] for f in m["findings"]]


# ───────────────────── per-model totals (I0469, I0470) ─────────────────────

RECORDED = {
    "clean": {"cascade_gaps": 0, "col_missing": 0, "expr_drift": 0,
              "rel_missing": 0, "rel_modified": 0, "total": 0},
    "divergent": {"cascade_gaps": 0, "col_missing": 0, "expr_drift": 0,
                  "rel_missing": 0, "rel_modified": 0, "total": 0},
    "drift": {"cascade_gaps": 1, "col_missing": 1, "expr_drift": 1,
              "rel_missing": 1, "rel_modified": 1, "total": 5},
    "regress": {"cascade_gaps": 2, "col_missing": 0, "expr_drift": 0,
                "rel_missing": 0, "rel_modified": 0, "total": 2},
}


def _totals_fleet(tmp_path):
    fy = roastery_fleet(tmp_path, {n: n for n in RECORDED})
    assert drift("capture", fleet=fy).exit_code == 0
    r = drift("ratchet tighten", "--total-only", fleet=fy)
    assert r.exit_code == 0, r.output
    return fy


def _totals(fy):
    doc = json.loads((fy.parent / ".drift-doctor" / "drift_totals.json")
                     .read_text(encoding="utf-8"))
    return doc.get("models", doc)


def test_capture_per_client_drift_baseline(tmp_path):
    """Maintenance hook in the original: one entry per derived model (never
    the template) with the five components and their total."""
    fy = _totals_fleet(tmp_path)
    totals = _totals(fy)
    assert set(totals) == set(RECORDED)
    for name, expected in RECORDED.items():
        assert {k: totals[name][k] for k in expected} == expected, name


@pytest.mark.parametrize("model,variant,verdict", [
    ("clean", "clean", "pass"),
    ("drift", "drift", "pass"),
    ("regress", "regress", "pass"),            # equal total
    ("regress", "regress_more", "fail"),       # one more missing measure: 2 -> 3
    ("regress", "regress_shift", "pass"),      # +1 column, -2 measures: 2 -> 1
])
def test_per_client_drift_regression(tmp_path, model, variant, verdict):
    """A model's drift total must not grow beyond its recorded total; a
    decrease, or a component rise offset by a larger fall, passes."""
    fy = _totals_fleet(tmp_path)
    if variant != model:
        swap_variant(fy, model, variant)
    r = drift("ratchet check", "--total-only", fleet=fy)
    if verdict == "fail":
        assert r.exit_code == 1, r.output
        assert model in r.output
    else:
        assert r.exit_code == 0, r.output


def test_per_client_drift_regression_unrecorded_model_is_skipped(tmp_path):
    """Original: a client missing from the recorded totals skips. Port: the
    model is named in a skip note and the check passes."""
    fy = _totals_fleet(tmp_path)
    late = fy.parent / "models" / "latecomer"
    import shutil
    shutil.copytree(fy.parent / "models" / "clean", late)
    edit_fleet_yml(fy.parent, lambda d: d["models"].__setitem__(
        "latecomer", "models/latecomer/Roastery.SemanticModel"))
    r = drift("ratchet check", "--total-only", fleet=fy)
    assert r.exit_code == 0, r.output
    assert "latecomer" in r.output and "skip" in r.output.lower()


def test_total_only_mode_differs_from_the_head_ratchet(tmp_path):
    """C-template-superset-detect case 4: the shift that passes the
    total-only mode (+1 column.missing, -2 measure.missing) still fails the
    default head-level ratchet on head `column`."""
    fy = roastery_fleet(tmp_path, {"regress": "regress"})
    assert drift("capture", fleet=fy).exit_code == 0
    assert drift("ratchet tighten", fleet=fy).exit_code == 0
    assert drift("ratchet tighten", "--total-only", fleet=fy).exit_code == 0
    swap_variant(fy, "regress", "regress_shift")
    assert drift("ratchet check", "--total-only", fleet=fy).exit_code == 0
    r = drift("ratchet check", fleet=fy)
    assert r.exit_code == 1 and "column" in r.output

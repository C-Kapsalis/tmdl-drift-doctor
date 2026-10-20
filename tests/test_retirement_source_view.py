"""
Retirement channel ``retire.source_view`` — end-to-end on the minis.

The ledger kind ``source_view_change`` records a template source-view
re-point (``table:old->new``) at recapture; ``remediate --sync`` must FOLLOW
it in every model still reading the old view — in BOTH partition spellings:

  bare    Source = source_filter_dates("old", ...)
  quoted  Source = #"source_filter_dates"("old", ...)

The quoted form was a production silent-no-op class in the original engine
(the rewrite only matched the bare form, so the re-point 'succeeded' without
changing the file) — pinned here forever.

Also pinned: a model on a DIFFERENT declared-override view is retained (+
digest), an undeclared different view is needs-decision (+ digest, untouched),
a model already on the new view is a no-op, and a second run is
byte-idempotent.

Ported from the user's suite (D-retire-source-view). The original engine
calls (build_context / collect / run_batch / SYNC_RETIREMENT_CATEGORIES) are
replaced by a tmp fleet.yml over the minis: ``capture`` before and after the
template re-point (which records the ledger event), then drift-doctor's
detect + ``remediate(sync=True)`` Python API, and ``remediate --sync --dry-run
--digest-json`` for the decision digest.
"""

from __future__ import annotations

import re
from pathlib import Path

from port_helpers_b7_retire import (applied, assert_overlay_untouched,
                                    capture, copy_pair, detect,
                                    detect_and_apply, digest, make_fleet,
                                    overlay_hashes, tree_hashes)

OLD_VIEW, NEW_VIEW = "ads_n", "ads_n_v2"
TABLE = "Ads"
KIND = "retire.source_view"
KINDS = ["retire.source_view", "retire.table", "retire.column",
         "retire.measure", "retire.mapping_row", "retire.relationship"]


# ════════════════════════════════════════════════════════════════════
# helpers
# ════════════════════════════════════════════════════════════════════

def _repoint_template(uni_defn: Path) -> None:
    """The template itself already follows the new view (real life: the ledger
    entry is recorded BECAUSE the template re-pointed)."""
    f = uni_defn / "tables" / f"{TABLE}.tmdl"
    content = f.read_text(encoding="utf-8")
    assert f'"{OLD_VIEW}"' in content, "fixture drifted: Ads partition view"
    f.write_text(content.replace(f'"{OLD_VIEW}"', f'"{NEW_VIEW}"'),
                 encoding="utf-8")


def _set_growth_view(growth_defn: Path, view: str) -> None:
    f = growth_defn / "tables" / f"{TABLE}.tmdl"
    content = f.read_text(encoding="utf-8")
    assert f'"{OLD_VIEW}"' in content
    f.write_text(content.replace(f'"{OLD_VIEW}"', f'"{view}"'),
                 encoding="utf-8")


def _quote_growth_sfd_call(growth_defn: Path) -> None:
    """Rewrite the mini partition to the QUOTED #"source_filter_dates" call
    form."""
    f = growth_defn / "tables" / f"{TABLE}.tmdl"
    content = f.read_text(encoding="utf-8")
    assert "source_filter_dates(" in content
    assert '#"source_filter_dates"' not in content
    f.write_text(content.replace("source_filter_dates(",
                                 '#"source_filter_dates"(', 1),
                 encoding="utf-8")


def _growth_view(growth_defn: Path) -> str | None:
    """Test-side reader: the classic helper-call form, then the inlined
    incremental-refresh form."""
    content = (growth_defn / "tables" / f"{TABLE}.tmdl").read_text(encoding="utf-8")
    m = (re.search(r'(?:#")?source_filter_dates"?\s*\(\s*"([^"]+)"', content)
         or re.search(r'#"v-db"\s*&\s*"\."\s*&\s*"([^"]+)"', content))
    return m.group(1) if m else None


# Inlined incremental-refresh partition source: the view sits inside the folded
# SQL as & #"v-db" & "." & "ads_n" & , not in a source_filter_dates() call.
_IR_SRC_ADS = (
    'let ir_dfilter = " cast(date as date) >= parse_date(\'%m/%d/%Y\', \'" '
    '& Date.ToText(DateTime.Date(RangeStart), "MM/dd/yyyy") & "\') '
    'and cast(date as date) < parse_date(\'%m/%d/%Y\', \'" '
    '& Date.ToText(DateTime.Date(RangeEnd), "MM/dd/yyyy") & "\') '
    'and cast(date as date) >= parse_date(\'%m/%d/%Y\', \'" '
    '& Date.ToText(#"v-fromdate", "MM/dd/yyyy") & "\') ", '
    'ir_qstr = "SELECT * FROM `" & #"v-BQ_project" & "." & #"v-db" & "." '
    '& "' + OLD_VIEW + '" & "` where " & ir_dfilter & #"v-rows_limit", '
    'ir_src = Value.NativeQuery(GoogleBigQuery.Database([CommandTimeout=#duration(0,3,0,0), '
    'BillingProject=#"v-BQ_billing_project"]){[Name= #"v-BQ_project" ]}[Data], '
    'ir_qstr, null, [EnableFolding=true]) in ir_src'
)


def _ir_inline_growth_call(growth_defn: Path) -> None:
    """Rewrite the mini Ads partition into the incremental-refresh inlined form:
    the view name sits in `& #"v-db" & "." & "ads_n" &`, present in BOTH the
    partition source and a refreshPolicy.sourceExpression."""
    f = growth_defn / "tables" / f"{TABLE}.tmdl"
    lines = f.read_text(encoding="utf-8").split("\n")
    for i, l in enumerate(lines):
        if l.lstrip().startswith("Source = ") and "source_filter_dates" in l:
            indent = l[:len(l) - len(l.lstrip())]
            comma = "," if l.rstrip().endswith(",") else ""
            lines[i] = f"{indent}Source = {_IR_SRC_ADS}{comma}"
            break
    p = next(i for i, l in enumerate(lines) if re.match(r"\tpartition .* = m$", l))
    s = next(i for i in range(p, len(lines)) if lines[i] == "\t\tsource =")
    mstart = mend = s + 1
    while (mend < len(lines) and lines[mend].strip()
           and len(lines[mend]) - len(lines[mend].lstrip("\t")) > 2):
        mend += 1
    mbody = lines[mstart:mend]
    refresh = ["\trefreshPolicy", "\t\tpolicyType: basic",
               "\t\trollingWindowGranularity: year", "\t\trollingWindowPeriods: 6",
               "\t\tincrementalGranularity: month", "\t\tincrementalPeriods: 3",
               "\t\tsourceExpression ="] + mbody
    ins = refresh + [""]
    if lines[p - 1].strip() != "":
        ins = [""] + ins
    lines[p:p] = ins
    f.write_bytes("\n".join(lines).encode("utf-8"))


def _setup(tmp_path, *, overrides=None):
    """Two-model fleet; baseline captured, then the template re-points Ads and
    is recaptured (this records the ledger's source_view_change)."""
    uni, gro = copy_pair(tmp_path)
    entry = {"path": gro}
    if overrides:
        entry["overrides"] = overrides
    fy = make_fleet(tmp_path, uni, {"fixture_growth": entry}, kinds=KINDS)
    capture(fy)
    _repoint_template(uni / "definition")
    capture(fy)
    return fy, uni / "definition", gro / "definition"


def _sv_digest(fy, tmp_path, name="digest.json") -> list:
    return [d for d in digest(fy, tmp_path, "retire.", name)
            if d["kind"] == "source_view"]


def _ads(growth_defn: Path) -> str:
    return (growth_defn / "tables" / f"{TABLE}.tmdl").read_text(encoding="utf-8")


# ════════════════════════════════════════════════════════════════════
# scenarios
# ════════════════════════════════════════════════════════════════════

def test_bare_partition_is_repointed(tmp_path):
    """(1) bare `source_filter_dates("old"` on the ledger's old view ->
    exactly one retire.source_view finding, applied, file re-pointed."""
    fy, uni_defn, growth_defn = _setup(tmp_path)
    before = overlay_hashes(uni_defn, growth_defn)

    findings, result = detect_and_apply(fy)
    sv_items = [f for f in findings if f.kind == KIND]

    assert len(sv_items) == 1, (
        f"expected exactly one retire.source_view finding — got "
        f"{[f.ref for f in sv_items]}")
    assert sv_items[0].ref == f"{TABLE}: {OLD_VIEW} -> {NEW_VIEW}"
    assert len(applied(result, KIND)) == 1 and result.count("failed") == 0
    assert _growth_view(growth_defn) == NEW_VIEW, (
        "bare-form partition was NOT re-pointed")
    assert f'"{OLD_VIEW}"' not in _ads(growth_defn)
    assert_overlay_untouched(before, uni_defn, growth_defn)


def test_quoted_partition_is_repointed(tmp_path):
    """(2) QUOTED `#"source_filter_dates"("old"` form must ALSO re-point (and
    keep the quoted spelling)."""
    fy, uni_defn, growth_defn = _setup(tmp_path)
    _quote_growth_sfd_call(growth_defn)
    assert _growth_view(growth_defn) == OLD_VIEW  # detection reads the quoted form
    before = overlay_hashes(uni_defn, growth_defn)

    findings, result = detect_and_apply(fy)
    sv_items = [f for f in findings if f.kind == KIND]

    assert (len(sv_items) == 1 and len(applied(result, KIND)) == 1
            and result.count("failed") == 0)
    content = _ads(growth_defn)
    assert f'#"source_filter_dates"("{NEW_VIEW}"' in content, (
        "quoted-form partition was NOT re-pointed — the silent no-op class "
        "regressed")
    assert f'"{OLD_VIEW}"' not in content
    assert_overlay_untouched(before, uni_defn, growth_defn)


def test_ir_inlined_partition_is_repointed(tmp_path):
    """(2b) inlined form — the view lives in `& #"v-db" & "." & "old" &`, in
    BOTH the partition source and the refreshPolicy.sourceExpression.
    Detection must read it and the re-point must rewrite EVERY occurrence."""
    fy, uni_defn, growth_defn = _setup(tmp_path)
    _ir_inline_growth_call(growth_defn)
    assert _growth_view(growth_defn) == OLD_VIEW  # detection reads the IR form
    before = overlay_hashes(uni_defn, growth_defn)

    findings, result = detect_and_apply(fy)
    sv_items = [f for f in findings if f.kind == KIND]

    assert (len(sv_items) == 1 and len(applied(result, KIND)) == 1
            and result.count("failed") == 0)
    content = _ads(growth_defn)
    assert content.count(f'& "." & "{NEW_VIEW}"') == 2, (
        "IR re-point must rewrite BOTH the partition source and the "
        "refreshPolicy.sourceExpression — a partition-only rewrite leaves the "
        "refreshPolicy pointing at the retired view")
    assert f'"{OLD_VIEW}"' not in content
    assert_overlay_untouched(before, uni_defn, growth_defn)


def test_declared_override_view_is_retained_with_digest(tmp_path):
    """(3) model on a DIFFERENT view DECLARED in its overrides (source_views
    section) -> no finding, partition untouched, digest notes the retention."""
    from tmdl_drift_doctor.fleet import load_fleet
    from tmdl_drift_doctor.overrides import resolve_for_model
    fy, uni_defn, growth_defn = _setup(tmp_path, overrides={"source_views": [{
        "table": TABLE, "view": "ads_custom_n",
        "reason": "fixture: declared divergent view",
        "owner": "data-team", "added": "2026-07-07"}]})
    _set_growth_view(growth_defn, "ads_custom_n")
    before = overlay_hashes(uni_defn, growth_defn)

    assert (resolve_for_model(load_fleet(fy), "fixture_growth")["source_views"]
            == {TABLE: "ads_custom_n"})
    findings, result = detect_and_apply(fy)

    assert ([f for f in findings if f.kind == KIND] == []
            and applied(result, KIND) == [] and result.count("failed") == 0)
    assert _growth_view(growth_defn) == "ads_custom_n", (
        "declared-override view was rewritten — retention contract broken")
    dig = _sv_digest(fy, tmp_path)
    assert len(dig) == 1 and "retained by" in dig[0]["note"], dig
    assert_overlay_untouched(before, uni_defn, growth_defn)


def test_undeclared_divergent_view_is_needs_decision(tmp_path):
    """(3b) same different view WITHOUT a declaration -> needs-decision
    digest, partition untouched (never guess between old and new)."""
    fy, uni_defn, growth_defn = _setup(tmp_path)
    _set_growth_view(growth_defn, "ads_custom_n")

    findings, result = detect_and_apply(fy)

    assert ([f for f in findings if f.kind == KIND] == []
            and applied(result, KIND) == [] and result.count("failed") == 0)
    assert _growth_view(growth_defn) == "ads_custom_n"
    dig = _sv_digest(fy, tmp_path)
    assert len(dig) == 1 and "needs-decision" in dig[0]["note"], dig


def test_client_already_on_new_view_is_noop(tmp_path):
    """(4) model already reads the new view -> nothing to do: no finding, no
    digest, no writes."""
    fy, uni_defn, growth_defn = _setup(tmp_path)
    _set_growth_view(growth_defn, NEW_VIEW)
    snapshot = tree_hashes(growth_defn)

    findings, result = detect_and_apply(fy)

    assert ([f for f in findings if f.kind == KIND] == []
            and applied(result, KIND) == [] and result.count("failed") == 0)
    assert _sv_digest(fy, tmp_path) == []
    assert tree_hashes(growth_defn) == snapshot, (
        "no-op scenario changed model bytes")


def test_second_run_is_idempotent(tmp_path):
    """(5) after a successful re-point, a fresh detect + remediate applies
    nothing and changes no bytes."""
    fy, uni_defn, growth_defn = _setup(tmp_path)

    _f1, result1 = detect_and_apply(fy)
    assert len(applied(result1, KIND)) == 1 and result1.count("failed") == 0
    assert _growth_view(growth_defn) == NEW_VIEW

    snapshot = tree_hashes(growth_defn)
    findings2, result2 = detect_and_apply(fy)
    assert ([f for f in findings2 if f.kind == KIND] == []
            and applied(result2, KIND) == [] and result2.count("failed") == 0)
    assert _sv_digest(fy, tmp_path, "digest2.json") == []
    assert tree_hashes(growth_defn) == snapshot, (
        "second run changed model bytes — re-point is not idempotent")

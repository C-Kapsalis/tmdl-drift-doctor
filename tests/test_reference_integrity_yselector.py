"""Axis-table (y family) floors against the template (E-template-count-floor).

Ported from tests/layers/layer3_integration/test_reference_integrity_yselector.py:
  * test_yselector_tables_exist         -> metric ``family_tables``
  * test_yselector_tables_have_columns  -> metric ``table_columns`` (COUNT floor:
    a renamed or swapped column does not trip it; ``column.missing`` still
    reports it)
  * test_yselector_reference_count      -> metric ``family_table_refs``
  * test_yselector_plot_specific_exists -> ``family_tables`` names y-Plot Specific 1
Stand-in template: y-Selector 1 (4 columns, 2 references) and
y-Plot Specific 1 (3 columns, 1 reference).
"""
import pytest

from port_helpers_b3_floors import (vparams, build_fleet, count_findings, detect,
                                    findings)


@pytest.mark.parametrize("variant,missing", vparams([
    ("clean", []),
    ("fam_missing_yps", ["y-Plot Specific 1"]),
]))
def test_yselector_tables_exist(tmp_path, variant, missing):
    fy = build_fleet(tmp_path, variant)
    r, report = detect(fy)
    got = sorted(n for f in count_findings(report, "family_tables") for n in f["data"]["missing"])
    assert got == missing
    if missing:
        assert r.exit_code == 1


@pytest.mark.parametrize("variant,short,col_missing", vparams([
    ("clean", {}, []),
    ("ycols_drop", {"y-Selector 1": (3, 4)}, ["y-Selector 1[y-Selector 1 Category]"]),
    ("ycols_rename", {}, ["y-Selector 1[y-Selector 1 Category]"]),
    ("ycols_swap", {}, ["y-Selector 1[y-Selector 1 Category]"]),
    ("y_client_only", {}, []),   # client-only y-Selector 9: no floor
]))
def test_yselector_tables_have_columns(tmp_path, variant, short, col_missing):
    fy = build_fleet(tmp_path, variant)
    r, report = detect(fy)
    got = {f["data"]["table"]: (f["data"]["client"], f["data"]["template"])
           for f in count_findings(report, "table_columns")}
    assert got == short
    assert [f["ref"] for f in findings(report, "column.missing")] == col_missing
    if short:
        assert r.exit_code == 1


@pytest.mark.parametrize("variant,short", vparams([
    ("clean", {}),
    ("yref_drop", {"y-Selector 1": (1, 2)}),
    ("y_client_unused", {"y-Selector 7": (0, None)}),
]))
def test_yselector_reference_count(tmp_path, variant, short):
    fy = build_fleet(tmp_path, variant)
    r, report = detect(fy)
    got = {f["data"]["table"]: (f["data"]["client"], f["data"].get("template"))
           for f in count_findings(report, "family_table_refs")
           if f["data"].get("table")}
    assert got == short
    if short:
        assert r.exit_code == 1


@pytest.mark.parametrize("variant,present", vparams([
    ("clean", True),
    ("fam_missing_yps", False),
]))
def test_yselector_plot_specific_exists(tmp_path, variant, present):
    fy = build_fleet(tmp_path, variant)
    r, report = detect(fy)
    missing = {n for f in count_findings(report, "family_tables") for n in f["data"]["missing"]}
    assert ("y-Plot Specific 1" not in missing) is present
    if not present:
        assert r.exit_code == 1

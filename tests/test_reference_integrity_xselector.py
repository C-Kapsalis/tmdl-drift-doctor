"""Axis-table (x family) floors against the template (E-template-count-floor).

Ported from tests/layers/layer3_integration/test_reference_integrity_xselector.py:
  * test_xselector_tables_exist        -> metric ``family_tables``
  * test_xselector_tables_have_columns -> metric ``table_columns`` (a COUNT floor)
  * test_xselector_reference_count     -> metric ``family_table_refs``
The original's single ``x-Selector`` prefix covered both x families; the
configured prefixes here are ``x-Selector`` and ``x-Plot Specific``.
Stand-in template: x-Selector 1 (3 columns, 2 measure references) and
x-Plot Specific 1 (3 columns, 3 measure references).
"""
import pytest

from port_helpers_b3_floors import (vparams, COUNT_KIND, STARTER_ALLOWLIST, build_fleet,
                                    count_findings, detect, findings)


@pytest.mark.parametrize("variant,missing", vparams([
    ("clean", []),
    ("fam_missing_xps", ["x-Plot Specific 1"]),
]))
def test_xselector_tables_exist(tmp_path, variant, missing):
    fy = build_fleet(tmp_path, variant)
    r, report = detect(fy)
    hits = count_findings(report, "family_tables")
    got = sorted(n for f in hits for n in f["data"]["missing"])
    assert got == missing
    if missing:
        assert r.exit_code == 1


@pytest.mark.parametrize("variant,short", vparams([
    ("clean", {}),
    ("xcols_drop", {"x-Selector 1": (2, 3)}),
    ("x_client_only_cols", {}),   # client-only x-Selector 9: no floor
]))
def test_xselector_tables_have_columns(tmp_path, variant, short):
    fy = build_fleet(tmp_path, variant)
    r, report = detect(fy)
    got = {f["data"]["table"]: (f["data"]["client"], f["data"]["template"])
           for f in count_findings(report, "table_columns")}
    assert got == short
    if short:
        assert r.exit_code == 1


@pytest.mark.parametrize("variant,short", vparams([
    ("clean", {}),
    ("xref_drop", {"x-Plot Specific 1": (2, 3)}),   # one consuming measure deleted
    ("x_client_unused", {"x-Selector 7": (0, None)}),  # client-only, 0 refs: "appears unused"
    ("xref_ten", {}),   # BUGFIX: refs to x-Selector 10 count only toward x-Selector 10
]))
def test_xselector_reference_count(tmp_path, variant, short):
    fy = build_fleet(tmp_path, variant)
    r, report = detect(fy)
    got = {f["data"]["table"]: (f["data"]["client"], f["data"].get("template"))
           for f in count_findings(report, "family_table_refs")
           if f["data"].get("table")}
    assert got == short
    if variant == "xref_drop":
        totals = [f for f in count_findings(report, "family_table_refs")
                  if not f["data"].get("table")]
        assert [(t["data"]["client"], t["data"]["template"]) for t in totals] == [(2, 3)]
    if short:
        assert r.exit_code == 1


def test_count_floor_fails_regardless_of_allowlist(tmp_path):
    """Spec case 10: with the five-kind starter allowlist (no table.missing,
    no model.*), a missing axis table still makes detect exit 1 with both a
    table.missing and a count-floor finding."""
    fy = build_fleet(tmp_path, "fam_missing_xps", allow=STARTER_ALLOWLIST)
    r, report = detect(fy)
    assert r.exit_code == 1, r.output
    assert "x-Plot Specific 1" in [f["ref"] for f in findings(report, "table.missing")]
    assert findings(report, COUNT_KIND)

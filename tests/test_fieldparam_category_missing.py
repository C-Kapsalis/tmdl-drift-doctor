"""DC-4: a row whose category is missing from the mapping table is reported.

When the template's field-parameter table uses an analysis category the
derived model does not have in its mapping table, the tool reported
nothing: the category was simply missing. The user had no way to learn that
the derived model's mapping table needs a row for that category.
"""

from __future__ import annotations

from pathlib import Path

from port_helpers_b6_change import (
    ZMAP_TABLE,
    install_zmap,
)
from test_pristine_purge import (
    X_AXIS,
    MODEL,
    UNI_ZMAP_ROWS,
    _copy_pair,
    _detect,
    _fleet,
)

# the category the template's mapping table carries and the derived
# model's does not
TEMPLATE_ONLY = ["Wholesale", "", "wholesale_x"]


def _template_with_the_category(tmp_path):
    """The template's mapping table carries a category the derived model's
    does not, and the template's field parameters use it."""
    uni_model, growth_model = _copy_pair(tmp_path)
    install_zmap(uni_model / "definition", UNI_ZMAP_ROWS + [TEMPLATE_ONLY])
    axis = uni_model / "definition" / "tables" / X_AXIS
    text = axis.read_text(encoding="utf-8")
    axis.write_text(text.replace(
        '"Performance_x"',
        '"wholesale_x"', 1), encoding="utf-8")
    return uni_model, growth_model


def test_a_category_the_model_does_not_have_is_reported(tmp_path):
    """DC-4: the finding names the category."""
    uni_model, growth_model = _template_with_the_category(tmp_path)

    _fleet_obj, _baseline, findings = _detect(
        _fleet(tmp_path, uni_model, growth_model))
    missing = [f for f in findings if f.kind == "fieldparam.category_missing"]
    assert missing, "no fieldparam.category_missing finding"
    assert "Wholesale" in missing[0].detail
    assert MODEL in missing[0].detail


def test_the_finding_is_report_only(tmp_path):
    """DC-4: nothing writes the mapping table for it."""
    from tmdl_drift_doctor.remediate import remediate
    uni_model, growth_model = _template_with_the_category(tmp_path)
    before = (growth_model / "definition" / "tables"
              / f"{ZMAP_TABLE}.tmdl").read_bytes()

    fy = _fleet(tmp_path, uni_model, growth_model)
    fleet, baseline, findings = _detect(fy)
    remediate(fleet, findings, kind="fieldparam", sync=True, baseline=baseline)

    after = (growth_model / "definition" / "tables"
             / f"{ZMAP_TABLE}.tmdl").read_bytes()
    assert after == before, "the mapping table was written"


def test_a_model_with_every_category_is_not_reported(tmp_path):
    """DC-4: the other side of the rule."""
    uni_model, growth_model = _copy_pair(tmp_path)
    _fleet_obj, _baseline, findings = _detect(
        _fleet(tmp_path, uni_model, growth_model))
    assert not [f for f in findings
                if f.kind == "fieldparam.category_missing"], (
        "a category the model has was reported as missing")

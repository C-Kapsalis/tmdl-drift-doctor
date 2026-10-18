"""Template f-measures present in the client (drift half, preflight-deferred).

The template-superset half of
tests/layers/layer3_integration/test_integration_f_categories.py::test_f_measure_pattern_validation:
every template f-measure must exist in the client (extras allowed). The
pattern-classification half is ported in tmdl-preflight (C-f-measure-pattern).
Here it is drift-doctor ``detect`` reporting ``measure.missing``.
"""
import pytest

from port_helpers_b3_floors import vparams, build_fleet, detect, refs


@pytest.mark.parametrize("variant,missing", vparams([
    ("clean", []),
    ("f_drop", ["f-Filters[f-Stores]"]),
]))
def test_f_measure_pattern_validation(tmp_path, variant, missing):
    fy = build_fleet(tmp_path, variant)
    r, report = detect(fy)
    assert refs(report, "measure.missing") == missing
    assert r.exit_code == (1 if missing else 0), r.output

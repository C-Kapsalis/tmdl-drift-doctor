"""Template helper measures present in the client (drift half, preflight-deferred).

The template-superset half of
tests/layers/layer3_integration/test_integration_helper_measures.py::test_helper_measure_naming_convention:
every template helper-named measure must exist in the client (extras
allowed). The displayFolder half is ported in tmdl-preflight. Here it is
drift-doctor ``detect`` reporting ``measure.missing``.
"""
import pytest

from port_helpers_b3_floors import vparams, build_fleet, detect, refs


@pytest.mark.parametrize("variant,missing", vparams([
    ("clean", []),
    ("helper_drop", ["Helper Measures[-helper Margin Base]"]),
]))
def test_helper_measure_naming_convention(tmp_path, variant, missing):
    fy = build_fleet(tmp_path, variant)
    r, report = detect(fy)
    assert refs(report, "measure.missing") == missing
    assert r.exit_code == (1 if missing else 0), r.output

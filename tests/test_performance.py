"""Measure count floor (E-template-count-floor, metric ``measures``).

Ported from tests/layers/layer2_dax/test_performance.py::test_measures_exist
(template: 17 measures). Also pins the stand-in baseline (X0021): the template
metrics drift-doctor captures equal the counts the ORIGINAL baseline capture
recorded for the same template (tests/fixtures/b3-floors/integration_baseline.json).
"""
import pytest

from port_helpers_b3_floors import (vparams, baseline, build_fleet, count_findings,
                                    detect)


@pytest.mark.parametrize("variant,client", vparams([
    ("clean", None),
    ("measure_drop", 16),
    ("measure_extra", None),
]))
def test_measures_exist(tmp_path, variant, client):
    fy = build_fleet(tmp_path, variant)
    r, report = detect(fy)
    hits = count_findings(report, "measures")
    if client is None:
        assert hits == []
    else:
        assert len(hits) == 1, hits
        d = hits[0]["data"]
        assert (d["client"], d["template"], d["floor"]) == (client, 17, 17)
        assert r.exit_code == 1


def test_captured_template_metrics_match_the_stand_in_baseline(tmp_path):
    """A client reduced to one dimension table falls below every floor, so each
    finding exposes the captured template value."""
    b = baseline()
    fy = build_fleet(tmp_path, "bare")
    r, report = detect(fy)
    assert r.exit_code == 1

    def template_of(metric):
        hits = [f for f in count_findings(report, metric)
                if not (f.get("data") or {}).get("table")]
        assert len(hits) == 1, (metric, hits)
        return hits[0]["data"]["template"]

    assert template_of("measures") == b["total_measures"] == 17
    assert template_of("dax_expressions") == b["dax_expressions_count"] == 18
    assert template_of("nameof_references") == b["nameof_references_count"] == 2
    assert template_of("relationships") == b["total_relationships"] == 3
    assert template_of("many_to_one") == b["many_to_one_relationships"] == 3

    missing = set()
    for f in count_findings(report, "family_tables"):
        missing.update(f["data"]["missing"])
    assert missing == set(b["xselector_tables_list"]) | set(b["yselector_tables_list"])

    per_table = {f["data"]["table"]: f["data"]["template"]
                 for f in count_findings(report, "family_table_refs")
                 if f["data"].get("table")}
    assert per_table == b["table_reference_counts"]

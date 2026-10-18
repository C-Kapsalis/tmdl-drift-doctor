"""DAX expression count floor (E-template-count-floor, metric ``dax_expressions``).

Ported from tests/layers/layer2_dax/test_dax_parsable.py::test_dax_expressions_exist.
The original compared the client's extracted measures + calculated columns with
``dax_expressions_count`` of the committed universal baseline; here the
template metric comes from ``drift-doctor capture`` on the bike-shop stand-in
(template: 17 measures + 1 calculated column = 18).
"""
import pytest

from port_helpers_b3_floors import vparams, build_fleet, count_findings, detect


@pytest.mark.parametrize("variant,client", vparams([
    ("clean", None),
    ("measure_drop", 17),     # Sales[Order Count] deleted
    ("measure_extra", None),  # a client-only measure added
]))
def test_dax_expressions_exist(tmp_path, variant, client):
    fy = build_fleet(tmp_path, variant)
    r, report = detect(fy)
    hits = count_findings(report, "dax_expressions")
    if client is None:
        assert hits == []
    else:
        assert len(hits) == 1, hits
        d = hits[0]["data"]
        assert (d["client"], d["template"], d["floor"]) == (client, 18, 18)
        assert r.exit_code == 1

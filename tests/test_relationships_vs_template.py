"""Relationship floors and casing against the template (drift-doctor halves of
the original layer-1 relationship suite).

Ported from tests/layers/layer1_structural/test_relationships.py:
  * test_relationship_count_at_least_universal  -> metric ``relationships``
  * test_relationship_cardinality_valid (floor half; the cardinality value
    check stays in tmdl-preflight)             -> metric ``many_to_one``
  * test_universal_performance_relationship_casing -> kind
    ``relationship.casing_mismatch`` (B-relationship-casing-vs-template)

The original read a real client model through the registry and the committed
universal baseline; here each case runs ``detect`` on a variant of the
bike-shop stand-in (tests/fixtures/b3-floors). The template's first
relationship sits on line 1 of relationships.tmdl, so the clean case also pins
"a relationship on line 1 counts".
"""
import pytest

from conftest import drift
from port_helpers_b3_floors import (vparams, build_fleet, count_findings, detect,
                                    findings)


@pytest.mark.parametrize("variant,client", vparams([
    ("clean", None),        # 3 of 3 (first relationship on line 1)
    ("rel_drop", 2),        # one relationship deleted
    ("rel_extra", None),    # a client-only relationship added: no upper bound
]))
def test_relationship_count_at_least_universal(tmp_path, variant, client):
    fy = build_fleet(tmp_path, variant)
    r, report = detect(fy)
    hits = count_findings(report, "relationships")
    if client is None:
        assert hits == []
        if variant == "clean":
            assert r.exit_code == 0, r.output
    else:
        assert len(hits) == 1, hits
        d = hits[0]["data"]
        assert (d["client"], d["template"], d["floor"]) == (client, 3, 3)
        assert r.exit_code == 1


@pytest.mark.parametrize("variant,client,total_finding", vparams([
    ("clean", None, False),     # no cardinality lines: TMDL defaults many/one
    ("rel_drop", 2, True),
    ("m2o_drop", 2, False),     # one relationship turned one-to-one
    ("rel_extra", None, False),
]))
def test_relationship_cardinality_valid(tmp_path, variant, client, total_finding):
    """Floor half only: many-to-one count >= the template's."""
    fy = build_fleet(tmp_path, variant)
    r, report = detect(fy)
    hits = count_findings(report, "many_to_one")
    if client is None:
        assert hits == []
    else:
        assert len(hits) == 1, hits
        d = hits[0]["data"]
        assert (d["client"], d["template"]) == (client, 3)
        assert r.exit_code == 1
    assert bool(count_findings(report, "relationships")) is total_finding


CASING = "relationship.casing_mismatch"


@pytest.mark.parametrize("variant,expected", vparams([
    ("clean", 0),
    ("casing", 2),               # toColumn: _stores.Store_Id vs _Stores[store_id]
    ("casing_client_only", 0),   # endpoint table not in the template: ignored
]))
def test_universal_performance_relationship_casing(tmp_path, variant, expected):
    fy = build_fleet(tmp_path, variant)
    r, report = detect(fy)
    hits = findings(report, CASING)
    assert len(hits) == expected, hits
    if expected:
        text = " ".join(f"{h['ref']} {h.get('detail', '')} {h.get('template_value')} "
                        f"{h.get('derived_value')}" for h in hits)
        assert "_Stores" in text and "_stores" in text
        assert "store_id" in text and "Store_Id" in text
        assert r.exit_code == 1


def test_casing_mismatch_fix_rewrites_to_template_spelling(tmp_path):
    """Spec Fix: the endpoint is rewritten to the canonical spelling (safe,
    batch-safe), and a second run finds nothing."""
    fy = build_fleet(tmp_path, "casing")
    r = drift("remediate", "--kind", CASING, "--yes", fleet=fy)
    assert r.exit_code == 0, r.output
    rel = (tmp_path / "bike-shop" / "Performance.SemanticModel" / "definition"
           / "relationships.tmdl").read_text(encoding="utf-8")
    assert "toColumn: _Stores.store_id" in rel and "_stores" not in rel
    _, report = detect(fy)
    assert findings(report, CASING) == []

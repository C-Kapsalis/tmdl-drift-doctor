"""NAMEOF reference floor and entry-scoped overrides for secondary models.

Ported from tests/layers/layer2_dax/test_nameof_resolver.py:
  * test_find_all_nameof_references -> metric ``nameof_references``
    (E-template-count-floor): floor = template count minus template NAMEOF
    references whose target column the model declares ``absent``.
  * test_client_id_for_resolves_secondary_models -> overrides declared on a
    fleet ENTRY apply to that entry's secondary semantic model
    (C-override-declarations case 12/13); a secondary path that is not on disk
    is a usage error, never a silent "no overrides".

The original read client_overrides.yaml (`columns:` + `kind: absent`) through
the registry; here the declaration is a fleet.yml override
(``object: Sales[discount]``, ``kind: absent``).
"""
import shutil

import pytest

from conftest import drift, write_fleet
from port_helpers_b3_floors import (vparams, STANDIN, TEMPLATE_REL, build_fleet,
                                    count_findings, detect, findings,
                                    model_copy, overridden)

ABSENT_DISCOUNT = [{"object": "Sales[discount]", "kind": "absent",
                    "reason": "the bike-shop source view has no discount field",
                    "owner": "owner-a", "added": "2026-10-01"}]


@pytest.mark.parametrize("variant,declared,client", vparams([
    ("clean", False, None),               # 2 of 2
    ("nameof_drop", False, 1),            # the NAMEOF(Sales[discount]) measure deleted
    ("nameof_drop_absent", True, None),   # same, column declared absent: floor 2 - 1
]))
def test_find_all_nameof_references(tmp_path, variant, declared, client):
    fy = build_fleet(tmp_path, variant,
                     overrides=ABSENT_DISCOUNT if declared else None)
    r, report = detect(fy)
    hits = count_findings(report, "nameof_references")
    if client is None:
        assert hits == []
    else:
        assert len(hits) == 1, hits
        d = hits[0]["data"]
        assert (d["client"], d["template"], d["adjustment"], d["floor"]) == (client, 2, 0, 2)
        # the original also asserted every item is a reference record: the
        # finding's counts are plain integers computed from parsed references
        assert all(isinstance(d[k], int) for k in ("client", "template", "adjustment", "floor"))
        assert r.exit_code == 1
    if declared:
        acks = [f for f in overridden(report) if f["ref"] == "Sales[discount]"]
        assert acks and acks[0]["kind"] == "column.missing"
        assert "Sales[discount]" not in [f["ref"] for f in findings(report, "column.missing")]


def _secondary_fleet(tmp_path, *, on_disk: bool):
    template = tmp_path / TEMPLATE_REL
    shutil.copytree(STANDIN / TEMPLATE_REL, template)
    primary = model_copy(tmp_path / "bike-shop" / "Performance.SemanticModel", "clean")
    outlet = tmp_path / "bike-shop" / "Outlet.SemanticModel"
    if on_disk:
        model_copy(outlet, "column_absent")
    entry = {"path": primary, "overrides": ABSENT_DISCOUNT,
             "secondary_models": [{"name": "Shop Outlet", "path": str(outlet.relative_to(tmp_path)),
                                   "deviations": []}]}
    return write_fleet(tmp_path, template, {"bike-shop": entry},
                       allowlist={"kinds": ["column.missing", "measure.missing"]})


@pytest.mark.parametrize("on_disk", [True, False], ids=["secondary-on-disk", "secondary-gone"])
def test_client_id_for_resolves_secondary_models(tmp_path, on_disk):
    fy = _secondary_fleet(tmp_path, on_disk=on_disk)
    if not on_disk:
        # original: "no registered secondary model resolves on disk" -> fail
        r = drift("capture", fleet=fy)
        assert r.exit_code == 2
        assert "not found" in r.output and "Outlet.SemanticModel" in r.output
        return
    assert drift("capture", fleet=fy).exit_code == 0
    r, report = detect(fy, "--model", "Shop Outlet")
    acks = [f for f in overridden(report, "Shop Outlet") if f["ref"] == "Sales[discount]"]
    assert acks and acks[0]["kind"] == "column.missing", report
    assert acks[0]["override"]["reason"].startswith("the bike-shop source view")
    assert findings(report, "column.missing", "Shop Outlet") == []
    assert r.exit_code == 0, r.output

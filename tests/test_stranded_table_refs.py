"""No live DAX may reference a ledger-retired, model-absent table.

A ledger retirement deletes a table, but nothing swept general measure bodies
for surviving references to it. The result is live DAX naming a table that no
longer exists anywhere in the model: unresolvable, invisible to the drift gate,
and import-blocking. Found carriers when the original guard was written: a
helper-measures table with 26 references (client-c era) and a filter measure
with one leftover `+ int(not ISEMPTY('Client-c Campaigns'))` term.

Scope is the retirement LEDGER's table names plus each table's absence from
the model under scan; only uncommented lines count.

Port: the original read `universal_retirements.yaml` and globbed
`*/*.SemanticModel/definition`. Here the ledger is drift-doctor's
(`Ledger.record_retirement("table", ...)`), the models are the stand-in fleet,
and the scan is `drift-doctor detect --kind retire.stranded_ref --json`
(B-stranded-retired-table-refs: always reported regardless of allowlist).
Scenarios: tests/fixtures/b2-l1b/stranded/<scenario>; verdicts recorded from
the ORIGINAL test on the same data.
"""
from __future__ import annotations

import pytest

from conftest import drift, drift_json, write_fleet
from port_helpers_b2_l1b import findings, load_scenario

from tmdl_drift_doctor.fleet import load_fleet
from tmdl_drift_doctor.ledger import Ledger

SCENARIOS = {
    "clean": "pass",
    "live_ref": "fail",
    "comment_only": "pass",
    "still_declared": "pass",
    "empty_ledger": "fail",
}


@pytest.mark.p0
@pytest.mark.layer1
@pytest.mark.parametrize("scenario, verdict", list(SCENARIOS.items()))
def test_no_stranded_refs_to_retired_absent_tables(stand_in, scenario, verdict):
    root, data = load_scenario(stand_in, "stranded", scenario)
    fy = write_fleet(root, data["template"],
                     {mid: m["path"] for mid, m in data["models"].items()},
                     allowlist={"kinds": ["retire"]})
    r = drift("capture", fleet=fy)
    assert r.exit_code == 0, r.output
    fleet = load_fleet(fy)
    ledger = Ledger(fleet.ledger_path)
    for name in data["retired_tables"]:
        ledger.record_retirement("table", name, reason="retired from the template")

    retired = sorted({ref for (kind, ref) in ledger.active_retirements() if kind == "table"})
    if verdict == "fail" and scenario == "empty_ledger":
        assert not retired      # the original's first assertion is what fails
        r = drift("detect", "--kind", "retire.stranded_ref", fleet=fy)
        assert r.exit_code == 2, r.output   # spec: empty ledger is an error when requested
        return
    assert retired, "retirement ledger unreadable or empty"

    r, report = drift_json("detect", "--kind", "retire.stranded_ref", fleet=fy)
    assert report is not None, r.output
    violations = [f"{f['model']}: {f.get('ref')} {f.get('detail', '')}"
                  for f in findings(report, kind="retire.stranded_ref")]
    if verdict == "pass":
        assert not violations, (
            f"{len(violations)} live DAX reference(s) to ledger-retired, "
            f"model-absent table(s) — unresolvable, import-blocking class:\n  "
            + "\n  ".join(violations[:40]))
    else:
        assert len(violations) == 1, violations
        assert "Old Campaigns" in violations[0]
        assert r.exit_code == 1, r.output

"""
Sync-run artifact scoping.

Pins the contract that a SCOPED remediate run (a ``--model`` / ``--kind``
subset) records only the models it actually touched:

  - the sync receipt (``<state_dir>/last_sync.json``) stamps provenance
    (generated_at / template_tree) PER MODEL ENTRY; carried-over entries keep
    their stamps verbatim, so a one-model run never re-attests the fleet's
    history to its own template tree. Legacy (schema 1) entries inherit the
    old file's top-level stamps once — never the current run's.
  - the run prints/reports/notifies from THIS run's view-enrichment entries
    only; the outstanding registry keeps its merge-persistence semantics
    (out-of-scope entries pass through untouched).

Gate compatibility: the receipt check reads only models[<name>].applied —
these tests also pin that the applied lists survive the merge unchanged.

Ported from the user's suite (D-sync-receipt, D-view-enrichment-registry).
Replacements: the original receipt writer + RunCounts -> drift-doctor's
``tmdl_drift_doctor.receipt.write_sync_receipt(state_dir, RunResult)`` fed
with remediate outcome objects; the stubbed one-client sync run -> a real
``drift-doctor remediate --yes --kind column.missing --model spinout-02`` on a
two-model fleet whose spinout-02 ``_Campaigns`` table is source-divergent and
lacks a template column (the splice queues an enrichment notice); the issue
notifier -> fleet.yml ``notify_command``. Entry keys category/object_id ->
kind/ref; stamp tmdl_commit -> template_tree; top-level ``clients`` ->
``models``.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FIXTURES, drift, edit_file
from port_helpers_b7_retire import copy_pair, make_fleet

from tmdl_drift_doctor import tmdl_edit as te
from tmdl_drift_doctor.detect import Finding
from tmdl_drift_doctor.remediate import Outcome, RunResult


# ─────────────────────────── helpers ───────────────────────────

def _result(model: str, applied_items: list) -> RunResult:
    return RunResult(outcomes=[
        Outcome(finding=Finding(kind=k, model=model, ref=r), status="applied")
        for k, r in applied_items])


LEGACY_RECEIPT = {
    "schema_version": 1,
    "generated_at": "2026-08-10T11:14:14+00:00",
    "template_tree": "f3dd5c7f14e5000f5f1b9e3286479970a447ec11",
    "models": {
        "spinout-02": {"applied": [
            {"kind": "measure.expression_drift",
             "ref": "Helper Measures[-helper A]"}]},
        "spinout-05": {"applied": [
            {"kind": "ffilter.coverage_gap",
             "ref": "f-Filters[f-Email]"}]},
        "Growth": {"applied": [
            {"kind": "retire.zmap_row", "ref": "zmap:row-1"}]},
    },
}


@pytest.fixture()
def receipt_path(tmp_path):
    state = tmp_path / ".drift-doctor"
    state.mkdir()
    p = state / "last_sync.json"
    p.write_text(json.dumps(LEGACY_RECEIPT, indent=2), encoding="utf-8")
    return p


def _write(receipt_path: Path, result: RunResult) -> None:
    from tmdl_drift_doctor.receipt import write_sync_receipt
    write_sync_receipt(receipt_path.parent, result)


# ────────────────────── the receipt writer ──────────────────────

def test_scoped_receipt_restamps_only_targeted_client(receipt_path):
    _write(receipt_path, _result("spinout-02", [
        ("measure.expression_drift", "Helper Measures[-helper B]")]))
    data = json.loads(receipt_path.read_text(encoding="utf-8"))

    # per-entry provenance, no fleet-wide top-level restamp
    assert data["schema_version"] == 2
    assert "generated_at" not in data and "template_tree" not in data

    # the targeted model carries this run's items + a fresh stamp
    m = data["models"]["spinout-02"]
    assert m["applied"] == [{"kind": "measure.expression_drift",
                             "ref": "Helper Measures[-helper B]"}]
    assert m["template_tree"] != LEGACY_RECEIPT["template_tree"]
    assert m["generated_at"] != LEGACY_RECEIPT["generated_at"]

    # untargeted models: applied lists verbatim (the cascade gate's input),
    # stamps inherited from the legacy file's top level — NOT ours
    for label in ("spinout-05", "Growth"):
        entry = data["models"][label]
        assert entry["applied"] == LEGACY_RECEIPT["models"][label]["applied"]
        assert entry["generated_at"] == LEGACY_RECEIPT["generated_at"]
        assert entry["template_tree"] == LEGACY_RECEIPT["template_tree"]


def test_carried_over_stamps_survive_subsequent_scoped_runs(receipt_path):
    _write(receipt_path, _result("spinout-02",
                                 [("measure.expression_drift", "T[m1]")]))
    first = json.loads(receipt_path.read_text(encoding="utf-8"))
    _write(receipt_path, _result("spinout-05",
                                 [("ffilter.coverage_gap", "f-Filters[f-X]")]))
    second = json.loads(receipt_path.read_text(encoding="utf-8"))

    # spinout-02 was not in the second run: its entry (stamps included) is
    # identical to what the first run wrote
    assert second["models"]["spinout-02"] == first["models"]["spinout-02"]
    # spinout-05 was restamped by the second run
    assert (second["models"]["spinout-05"]["generated_at"]
            != LEGACY_RECEIPT["generated_at"])
    # Growth rode through both runs with its legacy provenance intact
    assert (second["models"]["Growth"]["template_tree"]
            == LEGACY_RECEIPT["template_tree"])


def test_noop_run_leaves_receipt_untouched(receipt_path):
    before = receipt_path.read_bytes()
    _write(receipt_path, _result("spinout-02", []))
    assert receipt_path.read_bytes() == before


def test_committed_receipt_stand_in_carries_over_verbatim(tmp_path):
    """Stand-in for the committed receipt (tests/fixtures/sync_receipts/
    last_run.json; the original file held company data): a schema-2 receipt
    is carried over entry by entry by the next scoped run."""
    state = tmp_path / ".drift-doctor"
    state.mkdir()
    p = state / "last_sync.json"
    shutil.copy2(FIXTURES / "sync_receipts" / "last_run.json", p)
    committed = json.loads(p.read_text(encoding="utf-8"))
    assert committed["schema_version"] == 2

    _write(p, _result("north-store", [("measure.missing", "Store Measures[Tune-ups]")]))
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["models"]["south-store"] == committed["models"]["south-store"]
    assert data["models"]["north-store"]["applied"] == [
        {"kind": "measure.missing", "ref": "Store Measures[Tune-ups]"}]


# ─────────────────── scoped run: report / notify / registry ───────────────────

OUT_OF_SCOPE_ENTRY = json.loads(
    (FIXTURES / "sync_receipts" / "view_enrichment_outstanding.json")
    .read_text(encoding="utf-8"))["entries"][0]

_NOTIFIER = """\
import sys
from pathlib import Path
Path(sys.argv[1]).open("a", encoding="utf-8").write(sys.stdin.read().strip() + "\\n")
"""


@pytest.fixture()
def scoped_run(tmp_path):
    """One-model remediate run; spinout-02's _Campaigns reads a customised
    view (_campaigns_n_v9) and lacks the template column 'Campaign X'."""
    uni, gro = copy_pair(tmp_path, growth_dir="spinout_02_db")
    _, gro575 = copy_pair(tmp_path, growth_dir="spinout_10_db")
    camp = gro / "definition" / "tables" / "_Campaigns.tmdl"
    edit_file(camp, '"_campaigns_n"', '"_campaigns_n_v9"')
    camp.write_text(te.remove_block(camp.read_text(encoding="utf-8"),
                                    "column", "Campaign X"), encoding="utf-8")

    notifier = tmp_path / "notify.py"
    notifier.write_text(_NOTIFIER, encoding="utf-8")
    notified_file = tmp_path / "notified.jsonl"
    fy = make_fleet(
        tmp_path, uni, {"spinout-02": gro, "spinout-10": gro575},
        kinds=["column.missing"],
        notify_command=f'"{sys.executable}" "{notifier}" "{notified_file}"')

    state = tmp_path / ".drift-doctor"
    state.mkdir(exist_ok=True)
    outstanding = state / "view_enrichment_outstanding.json"
    shutil.copy2(FIXTURES / "sync_receipts" / "view_enrichment_outstanding.json",
                 outstanding)
    report = state / "view_enrichment_report.json"

    assert drift("capture", fleet=fy).exit_code == 0
    r = drift("remediate", "--yes", "--kind", "column.missing",
              "--model", "spinout-02", fleet=fy)
    notified = ([json.loads(line) for line in
                 notified_file.read_text(encoding="utf-8").splitlines() if line]
                if notified_file.exists() else [])
    return SimpleNamespace(rc=r.exit_code, output=r.output, notified=notified,
                           outstanding=outstanding, report=report)


def test_scoped_run_notifies_this_runs_entries_only(scoped_run):
    assert scoped_run.rc == 0, scoped_run.output
    assert len(scoped_run.notified) == 1
    models = {e["model"] for e in scoped_run.notified[0]}
    assert models == {"spinout-02"}  # never the registry's spinout-10


def test_scoped_run_report_carries_this_runs_entries_only(scoped_run):
    report = json.loads(scoped_run.report.read_text(encoding="utf-8"))
    assert {e["model"] for e in report["entries"]} == {"spinout-02"}


def test_registry_merge_keeps_out_of_scope_entries_untouched(scoped_run):
    reg = json.loads(scoped_run.outstanding.read_text(encoding="utf-8"))
    by_key = {(e["model"], e["table"]): e for e in reg["entries"]}
    # persistence: the committed out-of-scope entry rode through verbatim
    assert by_key[("spinout-10", "Google Play Installs")] == OUT_OF_SCOPE_ENTRY
    # and this run's entry was added
    assert ("spinout-02", "_Campaigns") in by_key

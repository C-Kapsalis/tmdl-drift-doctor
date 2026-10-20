"""DC-1: a purge of a client-only table is detected, not only applicable.

The tool already knows how to remove a client-only table from a derived
model, and it already lists the kind among the removable ones, but no
detection ever produced that finding. A client-only table that nothing
reads any more therefore stayed in the model forever, and the user never
learned that the tool could remove it.
"""

from __future__ import annotations

from pathlib import Path

from port_helpers_b6_change import EXTRAS
from test_pristine_purge import (
    _copy_pair,
    _detect,
    _fleet,
)

CLIENT_TABLE = "Campaigns"


def _add_client_only_table(growth_defn: Path) -> None:
    """A table the template does not have, with nothing reading it."""
    src = EXTRAS / "Campaigns.tmdl"
    dst = growth_defn / "tables" / f"{CLIENT_TABLE}.tmdl"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def test_a_client_only_table_is_detected_as_a_purge(tmp_path):
    """DC-1: the finding names the table."""
    uni_model, growth_model = _copy_pair(tmp_path)
    _add_client_only_table(growth_model / "definition")

    _fleet_obj, _baseline, findings = _detect(
        _fleet(tmp_path, uni_model, growth_model))
    tables = [f for f in findings if f.kind == "purge.table"]
    assert tables, "no purge.table finding for the client-only table"
    assert CLIENT_TABLE in tables[0].ref


def test_the_client_only_table_is_removed(tmp_path):
    """DC-1: the fix removes the file and deregisters the table."""
    from tmdl_drift_doctor.remediate import remediate
    uni_model, growth_model = _copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    _add_client_only_table(growth_defn)

    fleet_yml = _fleet(tmp_path, uni_model, growth_model)
    fleet, baseline, findings = _detect(fleet_yml)
    result = remediate(fleet, findings, kind="purge", sync=True,
                       baseline=baseline)
    assert result.count("failed") == 0, result.outcomes

    table_file = growth_defn / "tables" / f"{CLIENT_TABLE}.tmdl"
    assert not table_file.exists(), "the client-only table file is still there"
    model_file = (growth_defn / "model.tmdl").read_text(encoding="utf-8")
    assert CLIENT_TABLE not in model_file, (
        "the model still registers the removed table")


def test_a_second_detect_finds_nothing(tmp_path):
    """DC-1: the purge converges."""
    from tmdl_drift_doctor.remediate import remediate
    uni_model, growth_model = _copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    _add_client_only_table(growth_defn)

    fleet_yml = _fleet(tmp_path, uni_model, growth_model)
    fleet, baseline, findings = _detect(fleet_yml)
    remediate(fleet, findings, kind="purge", sync=True, baseline=baseline)

    _fleet2, _baseline2, findings2 = _detect(fleet_yml)
    assert not [f for f in findings2 if f.kind == "purge.table"], (
        "the client-only table is still reported after the purge")


def test_a_table_a_surviving_measure_reads_is_kept(tmp_path):
    """DC-1: the rule keeps a table that still has a reader."""
    from tmdl_drift_doctor import tmdl_edit as te
    uni_model, growth_model = _copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    _add_client_only_table(growth_defn)
    mfile = growth_defn / "tables" / "#Report Measures.tmdl"
    mfile.write_text(te.insert_measure_block(
        mfile.read_text(encoding="utf-8"),
        "\tmeasure 'Campaign Total' = SUM(Campaigns[clicks])\n"
        "\t\tlineageTag: 00000000-1111-2222-3333-555555555555\n"),
        encoding="utf-8")

    _fleet_obj, _baseline, findings = _detect(
        _fleet(tmp_path, uni_model, growth_model))
    assert not [f for f in findings if f.kind == "purge.table"], (
        "a table a surviving measure reads was reported as a purge")

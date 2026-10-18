"""Fleet cascade summary (the original tests/report_cascade.py CLI script,
which had no tests of its own).

Ported from the user's suite (D-fleet-cascade-summary) on a stand-in fleet:
template = the b7 universal mini, model ``clean`` = the growth mini (a clean
superset), model ``gap`` = the growth mini without the template measure
``#Report Measures[CTR]``. The registry + baseline + analyze_drift of the
original are replaced by ``drift-doctor detect`` (summary line) and
``detect --csv``. Expected values were recorded by running the original
script against the same stand-in (parity-expected.json):

    [OK  ] clean / [GAP ] gap (- missing: CTR)
    Summary: 1/2 clients in cascade
    clean,11,7,3,1,6,1,0,0,2,0,0,0
    gap,11,6,3,1,5,1,1,0,2,0,0,0
    unknown client -> exit 2
"""

from __future__ import annotations

import csv
import io
import re

import pytest
from conftest import drift
from port_helpers_b7_retire import copy_pair, make_fleet

from tmdl_drift_doctor import tmdl_edit as te

HEADER = ["model", "tables", "measures", "fact_tables", "filter_measures",
          "user_facing", "helpers", "gaps", "flagged", "allowed", "expr_drift",
          "col_drift", "rel_drift"]


@pytest.fixture()
def cascade_fleet(tmp_path):
    uni, clean = copy_pair(tmp_path, growth_dir="clean")
    _, gap = copy_pair(tmp_path, growth_dir="gap")
    mf = gap / "definition" / "tables" / "#Report Measures.tmdl"
    mf.write_text(te.remove_block(mf.read_text(encoding="utf-8"),
                                  "measure", "CTR"), encoding="utf-8")
    fy = make_fleet(tmp_path, uni, {"clean": clean, "gap": gap},
                    kinds=["measure.missing"])
    assert drift("capture", fleet=fy).exit_code == 0
    return fy


def test_cascade_summary_marks_the_gap_model(cascade_fleet):
    r = drift("detect", fleet=cascade_fleet)
    out = r.output
    assert re.search(r"\b1/2\b[^\n]*in cascade", out), out
    gap_section = out[out.index("gap"):]
    assert "CTR" in gap_section


def test_cascade_summary_csv_rows(cascade_fleet):
    r = drift("detect", "--csv", fleet=cascade_fleet)
    rows = list(csv.reader(io.StringIO(r.stdout)))
    assert rows[0] == HEADER
    by_model = {row[0]: row for row in rows[1:]}
    assert by_model["clean"] == ["clean", "11", "7", "3", "1", "6", "1",
                                 "0", "0", "2", "0", "0", "0"]
    assert by_model["gap"] == ["gap", "11", "6", "3", "1", "5", "1",
                               "1", "0", "2", "0", "0", "0"]


def test_cascade_summary_unknown_model_is_usage_error(cascade_fleet):
    r = drift("detect", "--model", "nope", fleet=cascade_fleet)
    assert r.exit_code == 2

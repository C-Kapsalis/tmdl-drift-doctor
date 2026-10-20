"""`apply_measure_renamings` must reach partition sources written WITHOUT ``` fences.

The fenced pattern (`source = ```…```) was the only partition form the renamer
handled, and x-Selector / y-Selector field-parameter tables — the largest concentration
of `NAMEOF()` measure references in the model — are frequently written in the
FENCELESS form:

    partition 'x-Selector Main Plot-…' = calculated
        mode: import
        source =
                {
                    ( "Cost", NAMEOF ( [Cost] ), -10, "Performance_x" ),
                    …

Measured on PBI#545: 61 of 340 `[CPA]` references lived in fenceless sources. The
renamer rewrote the display label beside them (a separate pass) and left the
NAMEOF bound to the dead name, producing
`( "Cost/ Conversion", NAMEOF ( [CPA] ), … )` — a field parameter pointing at a
measure that no longer exists. That does not raise: the calculated table fails to
evaluate, which takes `_Analysis Category` with it (see
test_xyselector_axis_nameof_resolution).
"""
from __future__ import annotations

import pytest

# Ported: the growth renaming script's `apply_renamings_to_file(path, map, backup_fn=...)`
# is replaced by drift-doctor's rename-rewrite entry point for one file and a measure
# rename map (H-rename-reference-rewrite). It returns (measures_modified, replacements);
# snapshots are off by default, so no backup hook is passed.

FENCELESS = (
    "table 'x-Selector 1'\n"
    "\tpartition 'x-Selector Main Plot-abc' = calculated\n"
    "\t\tmode: import\n"
    "\t\tsource =\n"
    "\t\t\t\t\n"
    "\t\t\t\t{\n"
    "\t\t\t\t    ( \"Cost\", NAMEOF ( [Cost] ), -10, \"Performance_x\" ),\n"
    "\t\t\t\t    ( \"CPA\", NAMEOF ( [CPA] ), 4, \"Performance_x\" ),\n"
    "\t\t\t\t    ( \"CVR\", NAMEOF ( '#Report Measures'[CPA] ), 8, \"Performance_x\" )\n"
    "\t\t\t\t}\n"
    "\n"
    "\tannotation X = 1\n"
)

FENCED = (
    "table 'x-Selector 2'\n"
    "\tpartition p = calculated\n"
    "\t\tmode: import\n"
    "\t\tsource = ```\n"
    "\t\t\t\t{ ( \"CPA\", NAMEOF ( [CPA] ), 4, \"Performance_x\" ) }\n"
    "\t\t\t\t```\n"
)


def _run(tmp_path, text):
    f = tmp_path / "t.tmdl"
    f.write_text(text, encoding="utf-8")
    from tmdl_drift_doctor.renames import rewrite_measure_references
    _, n = rewrite_measure_references(f, {"CPA": "Cost/ Conversion"})
    return f.read_text(encoding="utf-8"), n


def test_fenceless_partition_source_is_rewritten(tmp_path):
    out, n = _run(tmp_path, FENCELESS)
    assert n == 2, f"expected both NAMEOF targets rewritten, got {n}"
    assert "NAMEOF ( [CPA] )" not in out
    assert "'#Report Measures'[CPA]" not in out
    assert "NAMEOF ( [Cost/ Conversion] )" in out
    assert "'#Report Measures'[Cost/ Conversion]" in out


def test_fenceless_pass_does_not_swallow_the_following_section(tmp_path):
    """The body ends at the first line not indented deeper than `source`."""
    out, _ = _run(tmp_path, FENCELESS)
    assert out.rstrip().endswith("\tannotation X = 1")
    assert out.count("annotation X = 1") == 1


def test_fenced_partition_still_works(tmp_path):
    out, n = _run(tmp_path, FENCED)
    assert n == 1
    assert "[Cost/ Conversion]" in out


def test_unrelated_measures_are_untouched(tmp_path):
    out, _ = _run(tmp_path, FENCELESS)
    assert 'NAMEOF ( [Cost] )' in out, "a name that merely SHARES a prefix must not move"

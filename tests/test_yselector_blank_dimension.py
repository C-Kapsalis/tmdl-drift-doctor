"""Layer 1 (foundational data model): every y-Selector field-parameter axis
must carry its `Blank Dimension_y` row — the "no legend selected" option.

Why this gate exists: that row's NAMEOF points at a zero-width placeholder
column on `#Report Measures` (`[z]` on the numbered axes, `[z_space]` on
`y-Plot Specific 1`). When an axis has no blank row the report cannot express
"no legend", and a visual still pinned to a retired placeholder becomes a stale
binding: a combo chart collapses to one measure-addressed series and the bar
labels inherit the line's coloured label boxes. Three live models shipped no
blank row when the original gate was written.

The canonical row is taken PER TABLE NAME from the template, since the
placeholder target legitimately differs between axes. Commented-out rows do not
count. An axis whose tuples have a different arity than the template's row is
reported and skipped by the repair rather than mangled.

Auto-fix: on detection the test runs the repair and re-checks. `PBI_AUTOFIX`
defaults to `0`, so the test fails plainly; explicit value `1` enables the
repair branch.

Port: the original ran `fix_yselector_blank_dimension.py --check|--fix <definition>`
on every discovered model (template hard-coded to universal/Performance).
Here: kind `fieldparam.blank_row` (B-fieldparam-blank-option-row) — check =
`drift-doctor detect --kind fieldparam.blank_row --model M` (exit 0 clean,
1 findings, 2 template has no blank row), fix =
`drift-doctor remediate --kind fieldparam.blank_row --model M --yes` (an
additive, batch-safe repair; R3). Parametrized over the stand-in fleet models
(tests/fixtures/b2-l1b/blank/<scenario>) and both PBI_AUTOFIX values; each
verdict was recorded from the ORIGINAL test on the same data. The original
also visited the template itself and the sync-engine minis (pass); the port
visits only fleet models.
"""
from __future__ import annotations

import os

import pytest

from conftest import drift, write_fleet
from port_helpers_b2_l1b import load_scenario

pytestmark = [pytest.mark.p0, pytest.mark.layer1]

KIND = "fieldparam.blank_row"

# (scenario, model, PBI_AUTOFIX) -> verdict of the ORIGINAL test on the same data
VERDICTS = {
    ("main", "bike-shop", "0"): "pass", ("main", "bike-shop", "1"): "pass",
    ("main", "roastery", "0"): "fail", ("main", "roastery", "1"): "pass",
    ("main", "wide-hub", "0"): "fail", ("main", "wide-hub", "1"): "fail",
    ("main", "cafe", "0"): "fail", ("main", "cafe", "1"): "pass",
    ("main", "kiosk", "0"): "skip", ("main", "kiosk", "1"): "skip",
    ("no_template", "bike-shop", "0"): "fail", ("no_template", "bike-shop", "1"): "fail",
    ("no_template", "roastery", "0"): "fail", ("no_template", "roastery", "1"): "fail",
    ("no_template", "wide-hub", "0"): "fail", ("no_template", "wide-hub", "1"): "fail",
    ("no_template", "cafe", "0"): "fail", ("no_template", "cafe", "1"): "fail",
    ("no_template", "kiosk", "0"): "skip", ("no_template", "kiosk", "1"): "skip",
}

# models the original repaired (check exit 1 -> fix -> check exit 0) under PBI_AUTOFIX=1
REPAIRED = {("main", "roastery"), ("main", "cafe")}


def _autofix_enabled() -> bool:
    """PBI_AUTOFIX defaults to 0; only explicit value 1 enables repair."""
    return os.getenv("PBI_AUTOFIX", "0") != "0"


def _setup(stand_in, scenario):
    root, data = load_scenario(stand_in, "blank", scenario)
    fy = write_fleet(root, data["template"],
                     {mid: m["path"] for mid, m in data["models"].items()},
                     allowlist={"kinds": [KIND]})
    assert drift("capture", fleet=fy).exit_code == 0
    return root, data, fy


def _gate(stand_in, scenario, model, trace=None):
    """The original test body; returns normally on pass, raises Failed/Skipped.
    `trace` records the check exit codes (first / second)."""
    trace = {} if trace is None else trace
    root, data, fy = _setup(stand_in, scenario)
    definition = root / data["models"][model]["path"] / "definition"
    if not (definition / "tables").is_dir():
        pytest.skip("model has no tables directory")

    first = drift("detect", "--kind", KIND, "--model", model, fleet=fy)
    trace["first"] = first.exit_code
    if first.exit_code == 0:
        return
    if first.exit_code == 2:
        pytest.fail(f"canonical template unavailable:\n{first.output}")

    if not _autofix_enabled():
        pytest.fail(
            f"y-Selector axis missing its Blank Dimension_y row in {model} "
            f"(PBI_AUTOFIX=0, no auto-repair):\n{first.output}\nRun "
            f"`drift-doctor remediate --kind {KIND} --model {model}`.")

    fix = drift("remediate", "--kind", KIND, "--model", model, "--yes", fleet=fy)
    second = drift("detect", "--kind", KIND, "--model", model, fleet=fy)
    trace["second"] = second.exit_code
    if second.exit_code == 0:
        text = (definition / "tables" / "y-Selector 3.tmdl").read_text(encoding="utf-8")
        assert "NAMEOF('#Report Measures'[z]), 99" in text
        return
    pytest.fail(
        "Auto-fix did NOT resolve the missing Blank Dimension_y row — manual "
        f"intervention required (an arity mismatch needs a hand-built row).\n"
        f"Remaining:\n{second.output}\nFix output:\n{fix.output}")


@pytest.mark.parametrize("scenario, definition, autofix, verdict",
                         [(s, m, a, v) for (s, m, a), v in VERDICTS.items()])
def test_yselector_axes_carry_blank_dimension_row(stand_in, monkeypatch, scenario,
                                                  definition, autofix, verdict):
    monkeypatch.setenv("PBI_AUTOFIX", autofix)
    if verdict == "skip":
        with pytest.raises(pytest.skip.Exception):
            _gate(stand_in, scenario, definition)
        pytest.skip("model has no tables directory")
    if verdict == "fail":
        with pytest.raises(pytest.fail.Exception) as exc:
            _gate(stand_in, scenario, definition)
        msg = str(exc.value)
        if scenario == "no_template":
            assert "canonical template unavailable" in msg
        elif autofix == "0":
            assert "missing its Blank Dimension_y row" in msg
        else:
            assert "manual intervention required" in msg
        return
    trace = {}
    _gate(stand_in, scenario, definition, trace)
    if (scenario, definition) in REPAIRED:
        # the stand-in must exercise the repair branch, as it did in the original
        assert trace == {"first": 1, "second": 0}, trace

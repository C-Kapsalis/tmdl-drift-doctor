"""client-growth-cohort carve-out guardrails for 'z-Selector Format' (A-client-extension-markers).

Models may extend the dynamic-format calculation group with cohort-specific triggers (e.g.
ISINSCOPE on the z-Toggle Cohort Type column so cohort visuals resolve formats). Those
extensions are wrapped in `-- BEGIN client-growth-cohort` / `-- END client-growth-cohort`
markers and cut out by drift-doctor's marker-stripping API before any model<->template
expression comparison.

Two invariants protect that contract:

1. LEAK-BACK GATE: the template must never carry the marker or any cohort-toggle reference:
   the cohort tables do not exist there, so a leaked block would break the model outright.
2. EXCISION SAFETY: wherever the marker appears, BEGIN/END must pair up and stripping must
   remove the whole block (no stray marker text left behind), otherwise the drift comparison
   reads a half-excised expression.

Runs on the roastery stand-in fleet (tests/fixtures/b1-l1a/fleet): the template
`universal/Performance` has no block, spinout_02_db/insights carries one balanced block.
The original globbed the real repository and used the kernel DAX helper to strip.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from port_helpers_b1_l1a import FLEET_SRC, copy_fleet


def strip_marker_blocks(text, tag):
    # imported lazily so a tool without the API fails these tests, not collection
    from tmdl_drift_doctor.markers import strip_marker_blocks as _strip
    return _strip(text, tag)

_BEGIN = re.compile(r"^[ \t]*--[ \t]*BEGIN client-growth-cohort\b", re.MULTILINE)
_END = re.compile(r"^[ \t]*--[ \t]*END client-growth-cohort\b", re.MULTILINE)
FORMAT_FILE = "z-Selector Format.tmdl"


def _format_selector_files(root: Path):
    """Every model's 'z-Selector Format.tmdl' (template + all models)."""
    return sorted(root.glob(f"*/*.SemanticModel/definition/tables/{FORMAT_FILE}"))


def _template_file(root: Path) -> Path:
    return root / "universal" / "Performance.SemanticModel" / "definition" / "tables" / FORMAT_FILE


def _leak_problems(root: Path) -> list:
    universal = _template_file(root)
    assert universal.exists(), f"template z-Selector Format not found: {universal}"
    content = universal.read_text(encoding="utf-8")
    problems = []
    if "client-growth-cohort" in content:
        problems.append("template z-Selector Format carries a client-growth-cohort marker")
    if "z-Toggle Cohort Type" in content:
        problems.append("template z-Selector Format references the cohort toggle")
    return problems


def _balance_problems(root: Path) -> list:
    files = _format_selector_files(root)
    assert files, "no z-Selector Format.tmdl found anywhere"
    problems = []
    for path in files:
        content = path.read_text(encoding="utf-8")
        begins = len(_BEGIN.findall(content))
        ends = len(_END.findall(content))
        if begins != ends:
            problems.append(f"{path}: {begins} BEGIN vs {ends} END markers")
            continue
        if begins:
            stripped = strip_marker_blocks(content, "client-growth-cohort")
            if "client-growth-cohort" in stripped:
                problems.append(f"{path}: marker text survives stripping")
    return problems


def test_universal_format_selector_has_no_growth_cohort_block():
    """Leak-back gate: the template must never inherit the model-only cohort trigger."""
    problems = _leak_problems(FLEET_SRC)
    assert not problems, problems


def test_growth_cohort_blocks_are_balanced_and_strippable():
    """Excision safety: BEGIN/END pair up and stripping removes the whole span."""
    problems = _balance_problems(FLEET_SRC)
    assert not problems, "client-growth-cohort marker problems:\n" + "\n".join(problems)


@pytest.mark.parametrize("variant", ["marker", "toggle"])
def test_template_leak_fails(tmp_path, variant):
    root = copy_fleet(tmp_path)
    f = _template_file(root)
    text = f.read_text(encoding="utf-8")
    if variant == "marker":
        anchor = "\t\t\t\t\t    SELECTEDMEASUREFORMATSTRING()"
        text = text.replace(anchor, "\t\t\t\t\t    -- BEGIN client-growth-cohort\n"
                                    "\t\t\t\t\t    -- END client-growth-cohort\n" + anchor)
    else:
        text = text.replace("ISINSCOPE('Orders'[OrderId])",
                            "ISINSCOPE('z-Toggle Cohort Type'[Cohort Type])")
    f.write_text(text, encoding="utf-8")
    assert _leak_problems(root)


def test_unbalanced_markers_fail(tmp_path):
    root = copy_fleet(tmp_path)
    f = root / "spinout_02_db" / "insights.SemanticModel" / "definition" / "tables" / FORMAT_FILE
    text = f.read_text(encoding="utf-8")
    end = "\t\t\t\t\t    -- END client-growth-cohort\n"
    assert end in text
    f.write_text(text.replace(end, ""), encoding="utf-8")
    problems = _balance_problems(root)
    assert any("1 BEGIN vs 0 END" in p for p in problems), problems


def test_strip_restores_template_text_and_leaves_plain_text_alone():
    """Spec case 3: stripping removes each BEGIN..END span inclusive, every other line is
    byte-identical; text with no markers comes back unchanged."""
    template = _template_file(FLEET_SRC).read_text(encoding="utf-8")
    model = (FLEET_SRC / "spinout_02_db" / "insights.SemanticModel" / "definition" / "tables"
             / FORMAT_FILE).read_text(encoding="utf-8")
    assert model != template
    assert strip_marker_blocks(model, "client-growth-cohort") == template
    assert strip_marker_blocks(template, "client-growth-cohort") == template

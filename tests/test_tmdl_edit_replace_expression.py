"""replace_measure_expression must change the DAX body and nothing else.

`tmdl_edit.replace_measure_expression` delegates to
`TmdlMeasureUpdater.replace_measure_safely`. Its non-backtick branch ended the
expression at `\\n\\t(displayFolder|lineageTag|annotation|measure)` — ONE tab —
but a measure's properties sit at TWO tabs, so the span ran on past them and
past the next measure's `///` block to that measure's declaration. On the
common two-line shape

    \\tmeasure 'X' =
    \\t\\t\\tDAX
    \\t\\tformatString: ...

it returned ok=True having deleted formatString / displayFolder / lineageTag
AND the following measure's description, and `assert_tmdl_valid` agreed with
the result. On the one-line shape `\\tmeasure 'X' = DAX` it returned ok=False.

Every test here asserts the WHOLE output string, so "only the expression body
changed" is checked byte for byte rather than by looking for one surviving
property. The last tests prove the post-write preservation guard raises on a
crafted drop, independent of the span detector it is guarding.

Ported from the user's suite (batch b8-sync): the remediation edit wrapper and
the growth updater are replaced by drift-doctor's
``tmdl_edit.replace_measure_expression`` (guard owner) and its injectable
span-replacer seam ``tmdl_edit.measure_expression_replacer``; the two
real-model tests read a fictional stand-in measures table.
"""
from pathlib import Path

import pytest

from tmdl_drift_doctor import TmdlEditError
from tmdl_drift_doctor import tmdl_edit as te

# The guard raises a runtime error; the tool's own edit-guard error is
# accepted as that runtime error.
GUARD_ERRORS = (RuntimeError, TmdlEditError)

STANDIN_ROOT = Path(__file__).resolve().parent / "fixtures" / "b8-sync"
CLIENT_A_MEASURES = (
    STANDIN_ROOT / 'spinout_12_db/Growth.SemanticModel/definition/tables'
    / 'Client-a Measures.tmdl'
)

HEAD = [
    "table 'Test Measures'",
    "\tlineageTag: 00000000-0000-0000-0000-000000000000",
    "",
]
NEXT = [
    "\t/// The next measure's description, line one.",
    "\t/// And line two — must survive the edit above it.",
    "\tmeasure 'Next Measure' = 1",
    "\t\tformatString: 0",
    "\t\tlineageTag: 22222222-2222-2222-2222-222222222222",
    "",
]
TAIL = [
    "\tpartition 'Test Measures' = m",
    "\t\tmode: import",
    "\t\tsource =",
    "\t\t\t\tlet",
    "\t\t\t\t    Source = #table({}, {})",
    "\t\t\t\tin",
    "\t\t\t\t    Source",
    "",
    "\tannotation PBI_ResultType = Table",
    "",
]
PROPS = [
    "\t\tformatString: #,##0.00",
    "\t\tdisplayFolder: Test Folder",
    "\t\tlineageTag: 11111111-1111-1111-1111-111111111111",
    "",
    "\t\tannotation PBI_FormatHint = {\"isGeneralNumber\":true}",
    "",
]


def _file(target_lines):
    return "\n".join(HEAD + [
        "\t/// Target measure description.",
    ] + target_lines + PROPS + NEXT + TAIL)


# ── the two-line shape that broke (bare `=` + one indented DAX line) ──

TWO_LINE = _file([
    "\tmeasure 'Target' =",
    "\t\t\tDIVIDE([A], [B])",
])


def test_two_line_replaces_only_the_body():
    ok, out = te.replace_measure_expression(TWO_LINE, "Target", "IF(1=1, DIVIDE([A], [B]))")
    assert ok
    assert out == _file([
        "\tmeasure 'Target' =",
        "\t\t\tIF(1=1, DIVIDE([A], [B]))",
    ])
    te.assert_tmdl_valid(out)


def test_two_line_multiline_replacement_keeps_properties():
    ok, out = te.replace_measure_expression(
        TWO_LINE, "Target", "VAR x = [A]\nRETURN\n    DIVIDE(x, [B])")
    assert ok
    assert out == _file([
        "\tmeasure 'Target' =",
        "\t\t\tVAR x = [A]",
        "\t\t\tRETURN",
        "\t\t\t    DIVIDE(x, [B])",
    ])


def test_bare_multiline_with_blank_line_inside_dax():
    src = _file([
        "\tmeasure 'Target' =",
        "\t\t\tVAR x = [A]",
        "",
        "\t\t\tRETURN x",
    ])
    ok, out = te.replace_measure_expression(src, "Target", "[B]")
    assert ok
    assert out == _file([
        "\tmeasure 'Target' =",
        "\t\t\t[B]",
    ])


# ── the one-line shape (returned ok=False before) ──

ONE_LINE = _file(["\tmeasure 'Target' = DIVIDE([A], [B])"])


def test_one_line_is_replaceable():
    ok, out = te.replace_measure_expression(ONE_LINE, "Target", "DIVIDE([A], [C])")
    assert ok
    assert out == _file(["\tmeasure 'Target' = DIVIDE([A], [C])"])


def test_one_line_to_multiline_becomes_bare_form():
    ok, out = te.replace_measure_expression(ONE_LINE, "Target", "VAR x = [A]\nRETURN x")
    assert ok
    assert out == _file([
        "\tmeasure 'Target' =",
        "\t\t\tVAR x = [A]",
        "\t\t\tRETURN x",
    ])


def test_bare_to_single_line_keeps_bare_form():
    """Shape is preserved: a bare-`=` measure stays bare even for one line."""
    ok, out = te.replace_measure_expression(TWO_LINE, "Target", "[C]")
    assert ok
    assert out == _file(["\tmeasure 'Target' =", "\t\t\t[C]"])


# ── the triple-backtick shape (i-filter regen path) ──

def test_backtick_form_replaces_only_the_body():
    src = _file([
        "\tmeasure 'Target' = ```",
        "\t\t\tVAR x = [A]",
        "\t\t\tRETURN x",
        "\t\t```",
    ])
    ok, out = te.replace_measure_expression(src, "Target", "VAR y = [B]\nRETURN y")
    assert ok
    assert out == _file([
        "\tmeasure 'Target' = ```",
        "\t\t\t\t\tVAR y = [B]",
        "\t\t\t\t\tRETURN y",
        "\t\t```",
    ])


# ── last measure before the partition / annotation tail ──

def test_last_measure_before_partition():
    src = "\n".join(HEAD + NEXT[:-1] + [
        "",
        "\tmeasure 'Target' =",
        "\t\t\t[A]",
        "\t\tlineageTag: 11111111-1111-1111-1111-111111111111",
        "",
    ] + TAIL)
    ok, out = te.replace_measure_expression(src, "Target", "[B]")
    assert ok
    assert out == src.replace("\t\t\t[A]\n", "\t\t\t[B]\n")


def test_missing_measure_returns_false_unchanged():
    ok, out = te.replace_measure_expression(TWO_LINE, "No Such Measure", "1")
    assert not ok
    assert out == TWO_LINE


# ── the preservation guard, independent of the span detector ──

def _crafted(mutate):
    def fake(content, name, new_dax):
        return True, mutate(content)
    return fake


def test_guard_raises_when_a_target_property_is_dropped(monkeypatch):
    monkeypatch.setattr(
        te, "measure_expression_replacer",
        _crafted(lambda c: c.replace(
            "\t\tlineageTag: 11111111-1111-1111-1111-111111111111\n", "")))
    with pytest.raises(GUARD_ERRORS, match="Target"):
        te.replace_measure_expression(TWO_LINE, "Target", "[C]")


def test_guard_raises_when_the_next_description_is_dropped(monkeypatch):
    monkeypatch.setattr(
        te, "measure_expression_replacer",
        _crafted(lambda c: c.replace(
            "\t/// And line two — must survive the edit above it.\n", "")))
    with pytest.raises(GUARD_ERRORS):
        te.replace_measure_expression(TWO_LINE, "Target", "[C]")


def test_guard_raises_when_a_property_value_is_altered(monkeypatch):
    """A stricter bar than 'some lineageTag still exists': the target's OWN
    properties must be byte-identical."""
    monkeypatch.setattr(
        te, "measure_expression_replacer",
        _crafted(lambda c: c.replace(
            "11111111-1111-1111-1111-111111111111",
            "33333333-3333-3333-3333-333333333333")))
    with pytest.raises(GUARD_ERRORS):
        te.replace_measure_expression(TWO_LINE, "Target", "[C]")


# ── the pinned production repro, on a stand-in measures table ──

def _real_or_skip(name):
    content = CLIENT_A_MEASURES.read_text(encoding='utf-8')
    # the stand-in always carries the measure (the original skipped when the
    # real model dropped it)
    assert f"\tmeasure '{name}' =" in content, (
        f"{name!r} missing from the Client-a Measures.tmdl stand-in")
    return content


def test_real_client_a_two_line_measure():
    name = "^Revenue per First View"
    content = _real_or_skip(name)
    new = "IF(1=1, DIVIDE([^Streaming Revenue from First Views (no VAT)], [^First Views #]))"
    ok, out = te.replace_measure_expression(content, name, new)
    assert ok
    before, after = content.split("\n"), out.split("\n")
    assert len(before) == len(after)
    changed = [(b, a) for b, a in zip(before, after) if b != a]
    assert len(changed) == 1, changed
    assert changed[0][1] == "\t\t\t" + new
    te.assert_tmdl_valid(out)


def test_real_client_a_one_line_measure():
    name = "^Content Cost per First View"
    content = _real_or_skip(name)
    ok, out = te.replace_measure_expression(content, name, "1")
    assert ok
    before, after = content.split("\n"), out.split("\n")
    changed = [(b, a) for b, a in zip(before, after) if b != a]
    assert len(before) == len(after) and len(changed) == 1, changed
    assert changed[0][1] == f"\tmeasure '{name}' = 1"

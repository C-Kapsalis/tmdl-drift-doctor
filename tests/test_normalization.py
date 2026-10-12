"""Comparison normalization: Desktop re-saves are not drift; content is."""

from conftest import detect_all

from tmdl_drift_doctor.tmdl import (canonicalize_meta, dax_references,
                                    normalize_expression, strip_comments)


def test_meta_bag_order_and_spacing_are_canonical():
    a = '#date(2024, 1, 1) meta [IsParameterQuery = true, Type = "Date"]'
    b = '#date(2024, 1, 1) meta [Type="Date",IsParameterQuery=true]'
    assert normalize_expression(a) == normalize_expression(b)


def test_meta_values_still_matter():
    a = '"x" meta [Type = "Text"]'
    b = '"x" meta [Type = "Date"]'
    assert normalize_expression(a) != normalize_expression(b)


def test_meta_inside_a_string_is_left_alone():
    s = '"meta [b, a]"'
    assert canonicalize_meta(s) == s


def test_comment_markers_inside_strings_are_content():
    assert strip_comments('"https://host/a" // gone') == '"https://host/a"  '
    a = '"https://portal.example/reports"'
    b = '"https://portal.example/legacy"'
    assert normalize_expression(a) != normalize_expression(b)


def test_comments_outside_strings_are_ignored():
    a = "COUNTROWS(Visits) -- trailing note\n"
    b = "COUNTROWS(Visits) // another\n/* block */"
    assert normalize_expression(a) == normalize_expression(b) == "COUNTROWS(Visits)"


def test_apostrophe_in_a_bracket_name_is_not_a_quote():
    expr = "[Member's Count] + 1 // note"
    assert normalize_expression(expr) == "[Member's Count] + 1"


def test_dax_references_skip_strings_and_comments():
    refs = dax_references(
        "SUM(Classes[Capacity]) + [Total Visits] + 'Plan Map'[PlanKey] "
        "// Visits[Old]\n + \"Members[Text]\"")
    assert refs["qualified"] == {("Classes", "Capacity"), ("Plan Map", "PlanKey")}
    assert refs["bare"] == {"Total Visits"}


def test_fixture_meta_reorder_is_not_drift_but_url_change_is(captured):
    found = {(f.model, f.ref) for f in detect_all(captured)
             if f.kind == "expression.drift"}
    assert ("bravo", "Reporting Start Date") not in found   # reordered bag
    assert ("bravo", "Portal URL") not in found
    assert ("alpha", "Portal URL") in found                 # https:// change

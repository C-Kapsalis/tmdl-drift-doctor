"""The minimal TMDL reader: shapes, properties, expressions, mapping rows."""

from tmdl_drift_doctor.tmdl import (
    normalize_expression,
    parse_mapping_rows,
    parse_model,
)


def test_parses_tables_columns_measures(fleet):
    model = parse_model(fleet.template_definition)
    assert set(model.tables) == {"Members", "Visits", "Classes",
                                 "Promotions", "Plan Map"}
    members = model.tables["Members"]
    assert set(members.columns) == {"MemberId", "JoinDate", "MembershipTier"}
    assert set(members.measures) == {"Active Members", "New Members #",
                                     "_Member Base"}
    assert members.columns["JoinDate"].properties["formatString"] == "yyyy-mm-dd"
    assert members.columns["MemberId"].properties["dataType"] == "int64"


def test_single_line_expression(fleet):
    m = parse_model(fleet.template_definition).measure("Members", "Active Members")
    assert normalize_expression(m.expression) == "DISTINCTCOUNT(Members[MemberId])"
    assert m.properties["formatString"] == "#,0"


def test_bare_multiline_expression(fleet):
    m = parse_model(fleet.template_definition).measure("Members", "New Members #")
    norm = normalize_expression(m.expression)
    assert norm.startswith("VAR _start = DATE(2024, 1, 1)")
    assert "CALCULATE" in norm
    # properties that follow the body are NOT part of the expression
    assert "formatString" not in m.expression
    assert m.properties["formatString"] == "#,0"


def test_fenced_expression(fleet):
    m = parse_model(fleet.template_definition).measure("Visits", "Peak Hour Visits")
    norm = normalize_expression(m.expression)
    assert "HOUR(Visits[VisitDate]) IN {17, 18, 19}" in norm
    assert "```" not in norm


def test_hidden_flag_is_a_property(fleet):
    m = parse_model(fleet.template_definition).measure("Members", "_Member Base")
    assert m.properties.get("isHidden") == "true"


def test_lineage_tags_excluded_from_properties(fleet):
    m = parse_model(fleet.template_definition).measure("Members", "Active Members")
    assert "lineageTag" not in m.properties
    assert m.lineage_tag.startswith("11111111")


def test_expressions_file(fleet):
    model = parse_model(fleet.template_definition)
    assert set(model.expressions) == {"Reporting Start Date", "Data Source"}
    assert "#date(2024, 1, 1)" in model.expressions["Reporting Start Date"]
    assert "lineageTag" not in model.expressions["Reporting Start Date"]


def test_model_ref_tables(fleet):
    model = parse_model(fleet.template_definition)
    assert model.ref_tables == ["Members", "Visits", "Classes",
                                "Promotions", "Plan Map"]


def test_mapping_rows(fleet):
    model = parse_model(fleet.template_definition)
    part = model.tables["Plan Map"].partitions["Plan Map"]
    rows = parse_mapping_rows(part.source)
    assert set(rows) == {"basic", "plus", "elite", "legacy-gold"}
    assert rows["legacy-gold"] == '{"legacy-gold", "Legacy Gold", 119}'


def test_normalization_ignores_comments_and_whitespace():
    a = "COUNTROWS ( Visits )  // per day\n"
    b = "COUNTROWS(Visits)"
    assert normalize_expression(a) != normalize_expression(b)  # spacing inside parens differs
    assert normalize_expression("COUNTROWS(Visits) // per day") == \
        normalize_expression("COUNTROWS(Visits)")
    assert normalize_expression("/* header */\nCOUNTROWS(Visits)") == \
        normalize_expression("  COUNTROWS(Visits)  ")

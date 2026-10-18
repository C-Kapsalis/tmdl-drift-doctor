"""Shared expressions: block ownership, comment-level comparison, duplicates
and template-only names (G-expression-cascade, H-expression-merge,
A-expression-name-superset).

Each test builds a two-model fleet of tiny models in tmp_path whose only
interesting file is expressions.tmdl.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from tmdl_drift_doctor import tmdl_edit as te
from tmdl_drift_doctor.baseline import capture, load_baseline
from tmdl_drift_doctor.detect import detect_fleet
from tmdl_drift_doctor.fleet import load_fleet
from tmdl_drift_doctor.ledger import Ledger
from tmdl_drift_doctor.remediate import remediate
from tmdl_drift_doctor.tmdl import expression_spans

TAG_T = "\tlineageTag: 11111111-0000-4000-8000-000000000001"
TAG_D = "\tlineageTag: 22222222-0000-4000-8000-000000000002"


def _model(root: Path, expressions: str) -> Path:
    d = root / "Shop.SemanticModel" / "definition"
    d.mkdir(parents=True)
    (d / "model.tmdl").write_text("model Model\n\tculture: en-US\n",
                                  encoding="utf-8")
    (d / "expressions.tmdl").write_text(expressions, encoding="utf-8")
    return d.parent


def _fleet(tmp_path, template_expr, derived_expr, *, allow_expr=("A",),
           kinds=("expression.drift", "expression.missing"), **extra):
    tpl = _model(tmp_path / "template", template_expr)
    der = _model(tmp_path / "shop", derived_expr)
    doc = {"version": 1, "template": str(tpl.relative_to(tmp_path)),
           "models": {"shop": str(der.relative_to(tmp_path))},
           "allowlist": {"kinds": list(kinds), "expressions": list(allow_expr)}}
    doc.update(extra)
    fy = tmp_path / "fleet.yml"
    fy.write_text(yaml.safe_dump(doc), encoding="utf-8")
    fleet = load_fleet(fy)
    capture(fleet)
    return fleet, der / "definition" / "expressions.tmdl"


def _findings(fleet):
    return detect_fleet(fleet, load_baseline(fleet), Ledger(fleet.ledger_path))


def _fix(fleet):
    return remediate(fleet, _findings(fleet), baseline=load_baseline(fleet))


def test_doc_comment_belongs_to_the_declaration_below():
    text = "/// doc A\nexpression A = 1\n\n/// doc B\nexpression B = 2\n"
    spans = expression_spans(text)
    lines = text.split("\n")
    assert [(n, lines[s:e]) for n, s, e in spans] == [
        ("A", ["/// doc A", "expression A = 1"]),
        ("B", ["/// doc B", "expression B = 2"]),
    ]


def test_replacing_a_block_never_moves_the_next_doc_comment(tmp_path):
    fleet, dfile = _fleet(
        tmp_path,
        f"/// doc A\nexpression A = 1\n{TAG_T}\n\n/// doc B\nexpression B = 2\n",
        f"expression A = 0\n{TAG_D}\n\n/// keep C\nexpression C = 3\n",
        kinds=("expression.drift",))
    _fix(fleet)
    assert dfile.read_text(encoding="utf-8") == (
        f"/// doc A\nexpression A = 1\n{TAG_D}\n\n/// keep C\nexpression C = 3\n")


def test_comment_only_change_is_drift_and_the_template_doc_is_copied(tmp_path):
    fleet, dfile = _fleet(tmp_path,
                          f"/// new doc\nexpression A = 1\n{TAG_T}\n",
                          f"/// old doc\nexpression A = 1\n{TAG_D}\n")
    drift = [f for f in _findings(fleet) if f.kind == "expression.drift"]
    assert [f.ref for f in drift] == ["A"]
    _fix(fleet)
    assert "/// new doc" in dfile.read_text(encoding="utf-8")
    assert not [f for f in _findings(fleet) if f.kind == "expression.drift"]


def test_block_without_a_tag_gets_one_fresh_tag(tmp_path):
    fleet, dfile = _fleet(tmp_path, f"expression A = 1\n{TAG_T}\n",
                          "expression A = 0\n")
    _fix(fleet)
    text = dfile.read_text(encoding="utf-8")
    tags = [ln for ln in text.split("\n") if "lineageTag:" in ln]
    assert len(tags) == 1 and TAG_T.strip() not in text


def test_allowlisted_name_the_template_lacks_is_no_finding(tmp_path):
    fleet, _ = _fleet(tmp_path, "expression B = 1\n", "expression A = 0\n"
                      "expression B = 1\n", allow_expr=("A",))
    assert not [f for f in _findings(fleet) if f.kind == "expression.drift"]


def test_every_duplicate_declaration_is_compared_and_fixed(tmp_path):
    tpl = f"expression A = 1\n{TAG_T}\n\nexpression A = 1\n{TAG_T}\n"
    der = f"expression A = 1\n{TAG_D}\n\nexpression A = 9\n{TAG_D}\n"
    fleet, dfile = _fleet(tmp_path, tpl, der)
    assert [f.ref for f in _findings(fleet) if f.kind == "expression.drift"] \
        == ["A"]
    _fix(fleet)
    text = dfile.read_text(encoding="utf-8")
    assert "expression A = 9" not in text and text.count(TAG_D.strip()) == 2


def test_template_only_names_are_missing_and_added_alphabetically(tmp_path):
    tpl = ("expression RoastDate = \"2024-01-01\"\n"
           "expression Zeta = 1\nexpression Alpha = 2\n")
    der = "expression RoastDate = \"2023-06-30\"\nexpression ShopRegion = \"EU\"\n"
    fleet, dfile = _fleet(tmp_path, tpl, der, allow_expr=("RoastDate",))
    found = sorted((f.kind, f.ref) for f in _findings(fleet)
                   if f.kind.startswith("expression."))
    assert found == [("expression.drift", "RoastDate"),
                     ("expression.missing", "Alpha"),
                     ("expression.missing", "Zeta")]
    _fix(fleet)
    names = [n for n, _s, _e in expression_spans(dfile.read_text(encoding="utf-8"))]
    assert names == ["RoastDate", "ShopRegion", "Alpha", "Zeta"]
    assert not [f for f in _findings(fleet) if f.kind.startswith("expression.")]
    before = dfile.read_bytes()
    _fix(fleet)
    assert dfile.read_bytes() == before


def test_missing_is_reported_even_with_an_empty_cascade_list(tmp_path):
    fleet, _ = _fleet(tmp_path, "expression A = 1\nexpression B = 2\n",
                      "expression A = 0\n", allow_expr=())
    found = [(f.kind, f.ref) for f in _findings(fleet)
             if f.kind.startswith("expression.")]
    assert found == [("expression.missing", "B")]


def test_connection_parameter_is_reported_with_a_hint_and_never_written(tmp_path):
    fleet, dfile = _fleet(tmp_path, "expression v-db = \"dw\"\n",
                          "expression v-other = 1\n", allow_expr=(),
                          connection_parameters=["v-db"])
    missing = [f for f in _findings(fleet) if f.kind == "expression.missing"]
    assert "set this model's own value" in missing[0].detail
    before = dfile.read_bytes()
    result = _fix(fleet)
    assert dfile.read_bytes() == before
    assert result.count("applied") == 0


def test_measure_insert_indents_column_zero_lines():
    content = "table T\n\tpartition T = m\n\t\tmode: import\n"
    out = te.insert_measure_block(content, "measure X = 1\nlineageTag: x")
    assert "\tmeasure X = 1\n\tlineageTag: x\n\n\tpartition" in out


def test_block_stops_before_a_stray_table_line():
    for stray in ("\ttable U", "table U"):
        content = ("table T\n\tmeasure X = 1\n\t\tformatString: 0\n"
                   f"{stray}\n\tcolumn c\n")
        assert te.extract_block(content, "measure", "X") == \
            "\tmeasure X = 1\n\t\tformatString: 0"


def test_fenced_body_keyword_lines_do_not_end_the_block():
    content = ("table T\n\tmeasure X = ```\n\tcolumn x\n\t\t```\n"
               "\t\tformatString: 0\n\n\tcolumn y\n")
    block = te.extract_block(content, "measure", "X")
    assert "\tcolumn x" in block and "formatString: 0" in block
    assert "column y" not in block


def test_fenced_dax_closing_parenthesis_rule():
    content = ("table T\n\tmeasure 'Target' = ```\n\t\t\t1\n\t\t```\n"
               "\t\tlineageTag: 1\n")
    ok, out = te.replace_measure_expression(
        content, "Target", 'CONCATENATE(\n    [A]\n) & "x"\n)')
    assert ok
    assert ("\t\t\t\t\tCONCATENATE(\n\t\t\t\t\t[A]\n\t\t\t) & \"x\"\n\t\t\t)\n"
            "\t\t```") in out


def test_fenced_declaration_with_trailing_text_or_no_close_fails():
    bad = "table T\n\tmeasure 'Target' = ``` x\n\t\t\t1\n\t\t```\n"
    assert te.replace_measure_expression(bad, "Target", "2") == (False, bad)
    open_ = "table T\n\tmeasure 'Target' = ```\n\t\t\t1\n"
    assert te.replace_measure_expression(open_, "Target", "2") == (False, open_)

"""Renames: pairing template renames, reference rewrites, the rename map,
the fleet-wide `rename measure` command and `remediate --ref`."""

import json

import pytest
import yaml

from conftest import detect_all, drift, edit_file, edit_fleet_yml

from tmdl_drift_doctor import tmdl_edit as te
from tmdl_drift_doctor.baseline import capture
from tmdl_drift_doctor.fleet import load_fleet
from tmdl_drift_doctor.remediate import remediate
from tmdl_drift_doctor.renames import (
    RenameMap,
    core_name,
    format_decimals,
    rename_labels,
    rewrite_column_text,
    rewrite_measure_text,
    rewrite_model,
    rewrite_relationship_endpoints,
    split_column_path,
)
from tmdl_drift_doctor.tmdl import parse_model


# ───────────── core names (pairing) ─────────────

def test_core_name_strips_one_prefix():
    for name in ("-CTR", "_CTR", "#CTR", "x-CTR", " ctr ", "CTR"):
        assert core_name(name) == "ctr"
    assert core_name("xy-CTR") == "xy-ctr"
    assert core_name("--CTR") == "-ctr"


def _rename_in_template(fleet, old, new, table="Members"):
    f = fleet.template_definition / "tables" / f"{table}.tmdl"
    edit_file(f, f"\tmeasure '{old}' =", f"\tmeasure '{new}' =")
    capture(fleet)


def test_template_rename_is_one_finding_and_cascades(captured):
    _rename_in_template(captured, "Active Members", "#Active Members")
    alpha = [f for f in detect_all(captured, model_filter="alpha")
             if "Active Members" in f.ref]
    assert [(f.kind, f.ref) for f in alpha] == [
        ("measure.renamed", "Active Members -> #Active Members")]
    captured.allowlist_kinds.add("measure.renamed")
    res = remediate(captured, alpha)
    assert res.count("applied") == 1, [o.reason for o in res.outcomes]
    model = parse_model(captured.model_definition("alpha"))
    assert model.measure("Members", "#Active Members") is not None
    assert model.measure("Members", "Active Members") is None
    text = "".join(p.read_text(encoding="utf-8") for p in
                   (captured.model_definition("alpha") / "tables").glob("*"))
    assert "[Active Members]" not in text and "[#Active Members]" in text
    again = [f for f in detect_all(captured, model_filter="alpha")
             if f.kind == "measure.renamed"]
    assert again == []


def test_two_candidates_with_one_core_are_not_paired(captured):
    f = captured.template_definition / "tables" / "Members.tmdl"
    edit_file(f, "\tmeasure 'Active Members' =", "\tmeasure '#Active Members' =")
    edit_file(f, "\tmeasure '_Member Base' =",
              "\tmeasure '!Active Members' = 1\n\t\tlineageTag: "
              "cccc0000-0000-4000-8000-000000000001\n\n"
              "\tmeasure '_Member Base' =")
    capture(captured)
    kinds = {f.kind for f in detect_all(captured, model_filter="alpha")
             if "Active Members" in f.ref}
    assert "measure.renamed" not in kinds and "measure.missing" in kinds


def test_override_declared_template_name_is_not_paired(fleet_dir):
    def mutate(d):
        d["models"]["alpha"] = {
            "path": d["models"]["alpha"],
            "overrides": [{"object": "Members[#Active Members]",
                           "kind": "absent", "reason": "not used here"}]}
    edit_fleet_yml(fleet_dir, mutate)
    fleet = load_fleet(fleet_dir / "fleet.yml")
    capture(fleet)
    _rename_in_template(fleet, "Active Members", "#Active Members")
    kinds = {f.kind for f in detect_all(fleet, model_filter="alpha")}
    assert "measure.renamed" not in kinds


# ───────────── reference rewrite ─────────────

def test_self_reference_guard():
    text = "table T\n\n\tmeasure Total = [Old Total] * 1\n"
    out, _m, n = rewrite_measure_text(text, {"Old Total": "Total"})
    assert out == text and n == 0


def test_measure_match_is_case_insensitive_and_whole_name():
    text = ("table T\n\n\tmeasure Check = [MARGIN] + [Margin Rate] + "
            "'Shop'[margin]\n")
    out, measures, n = rewrite_measure_text(text, {"margin": "Gross Margin"})
    assert n == 2 and measures == 1
    assert "[Gross Margin] + [Margin Rate] + 'Shop'[Gross Margin]" in out


def test_string_literal_comparisons_are_rewritten():
    text = ('table T\n\n\tmeasure Pick =\n\t\t\tSWITCH(TRUE(), sel = '
            '"\'Shop Measures\'[Avg Ticket]", CALCULATE([Avg Ticket]), 0)\n')
    out, _m, n = rewrite_measure_text(text, {"Avg Ticket": "Revenue per Order"})
    assert n == 2
    assert "\"'Shop Measures'[Revenue per Order]\"" in out


def test_text_outside_expressions_is_untouched():
    text = ("table T\n\n\t/// see [Old]\n\tmeasure A = [Old]\n"
            "\t\tdisplayFolder: [Old]\n")
    out, _m, n = rewrite_measure_text(text, {"Old": "New"})
    assert n == 1
    assert "/// see [Old]" in out and "displayFolder: [Old]" in out


def test_empty_new_name_is_ignored():
    text = "table T\n\n\tmeasure A = [Draft]\n"
    assert rewrite_measure_text(text, {"Draft": ""})[0] == text


def test_column_rename_rewrites_qualified_refs_and_relationships():
    text = ("table Calendar\n\n\tmeasure Days = DISTINCTCOUNT(Calendar[Calendar"
            " Date]) + COUNT([Calendar Date])\n")
    out, n = rewrite_column_text(text, {"Calendar.Calendar Date": "Calendar.Date"})
    assert n == 1
    assert "DISTINCTCOUNT('Calendar'[Date])" in out
    assert "COUNT([Calendar Date])" in out          # bare is never touched
    rel = ("relationship r1\n\tfromColumn: Visits.Day\n"
           "\ttoColumn: Calendar.'Calendar Date'\n")
    out, n = rewrite_relationship_endpoints(
        rel, {"Calendar.Calendar Date": "Calendar.Date"})
    assert n == 1 and "\ttoColumn: 'Calendar'.'Date'" in out


def test_rewrite_model_dry_run_counts_and_writes_nothing(captured):
    d = captured.model_definition("alpha")
    before = {p: p.read_bytes() for p in (d / "tables").glob("*.tmdl")}
    res = rewrite_model(d, {"Total Visits": "Visit Count"}, dry_run=True)
    assert res.replacements >= 1 and res.files_modified
    assert {p: p.read_bytes() for p in (d / "tables").glob("*.tmdl")} == before


# ───────────── labels ─────────────

def test_label_rename_is_gated_on_nameof():
    text = ('\t\t\t\t( "Avg Ticket", NAMEOF ( [Avg Ticket] ), 3, "Sales" ),\n'
            '\t\t\t\t( "Avg Ticket", NAMEOF ( [Basket Size] ), 4, "Sales" ),\n')
    out, n = rename_labels(text, "Avg Ticket", "Avg Ticket", "Revenue per Order")
    assert n == 1
    assert '( "Revenue per Order", NAMEOF ( [Avg Ticket] ), 3, "Sales" )' in out
    assert '( "Avg Ticket", NAMEOF ( [Basket Size] ), 4, "Sales" )' in out


# ───────────── the rename map ─────────────

def _with_renames(fleet_dir, renames, **entry):
    def mutate(d):
        d["renames"] = renames
        for name, extra in entry.items():
            d["models"][name] = {"path": d["models"][name], **extra}
    edit_fleet_yml(fleet_dir, mutate)
    return fleet_dir / "fleet.yml"


def test_renames_check(fleet_dir):
    fy = _with_renames(fleet_dir, {"measures": {
        "!Visits": "Total Visits", "Old KPI": "Net KPI", "Draft KPI": ""}})
    r = drift("renames check", fleet=fy)
    assert r.exit_code == 1
    assert "alpha: 1 valid mapping(s)" in r.output
    assert "Old KPI -> Net KPI (target doesn't exist)" in r.output


def test_renames_profiles_replace_the_default_map(fleet_dir):
    fy = _with_renames(
        fleet_dir,
        {"measures": {"Old KPI": "Net KPI"},
         "profiles": {"retail": {"measures": {"!Visits": "Total Visits"}}}},
        alpha={"rename_profile": "retail"})
    r = drift("renames check", "--model", "alpha", fleet=fy)
    assert r.exit_code == 0 and "alpha: 1 valid mapping(s)" in r.output


def test_rename_map_paths_lookup_and_round_trip(tmp_path):
    assert split_column_path("Calendar.Calendar Date") == ("Calendar",
                                                           "Calendar Date")
    assert split_column_path("Store.Region.Name") == ("Store.Region", "Name")
    assert split_column_path("NoDot") == (None, None)
    m = RenameMap.from_config({"measures": {"A": "B"},
                               "columns": {"Calendar.Calendar Date":
                                           "Calendar.Date"}})
    assert m.column("Calendar", "Calendar Date") == ("Calendar", "Date")
    assert m.column("Calendar", "Nope") is None
    path = tmp_path / "map.json"
    m.dump(path)
    other = RenameMap(measures={"X": "Y"})
    other.load(path)
    assert other.measures == {"X": "Y", "A": "B"}
    assert json.loads(path.read_text())["measures"] == {"A": "B"}
    assert RenameMap().load(tmp_path / "missing.json").measures == {}


def test_renames_apply_rewrites_references(fleet_dir):
    fy = _with_renames(fleet_dir, {"measures": {"Total Visits": "Visit Count"}})
    r = drift("renames apply", "--yes", fleet=fy)
    assert r.exit_code == 0, r.output
    text = (fleet_dir / "franchises/alpha/GymChain.SemanticModel/definition/"
            "tables/Visits.tmdl").read_text(encoding="utf-8")
    assert "[Total Visits]" not in text


# ───────────── rename measure (fleet-wide) ─────────────

def test_rename_measure_everywhere(fleet_dir):
    fy = fleet_dir / "fleet.yml"
    r = drift("rename measure", "Total Visits", "Visit Count", "--dry-run",
              fleet=fy)
    assert r.exit_code == 0, r.output
    assert "template: labels 0 decl 1" in r.output
    r = drift("rename measure", "Total Visits", "Visit Count", "--yes",
              fleet=fy)
    assert r.exit_code == 0, r.output
    for rel in ("template/GymChain.SemanticModel",
                "franchises/alpha/GymChain.SemanticModel",
                "franchises/bravo/GymChain.SemanticModel"):
        model = parse_model(fleet_dir / rel / "definition")
        assert "Visit Count" in model.measure_hosts()
        assert "Total Visits" not in model.measure_hosts()
    r = drift("rename measure", "Total Visits", "Visit Count", "--yes",
              fleet=fy)
    assert r.exit_code == 1 and "no model declares" in r.output


def test_rename_measure_reports_precision_loss(fleet_dir):
    tdef = fleet_dir / "template/GymChain.SemanticModel/definition"
    (tdef / "tables" / "Format Picker.tmdl").write_text(
        "table 'Format Picker'\n\tlineageTag: 1\n\n"
        "\tmeasure Fmt = \n\t\t\tSWITCH(TRUE(),\n"
        "\t\t\t\tCONTAINSSTRING(sel, \"cost\"), \"#,##0.00\",\n"
        "\t\t\t\tCONTAINSSTRING(sel, \"visits\"), \"#,##0.00\",\n"
        "\t\t\t\t\"#,##0\")\n", encoding="utf-8")
    edit_fleet_yml(fleet_dir, lambda d: d.update(
        renames={"format_table": "Format Picker"}))
    fy = fleet_dir / "fleet.yml"
    r = drift("rename measure", "Total Visits", "Attendance", "--dry-run",
              fleet=fy)
    assert r.exit_code == 2, r.output
    assert "#,##0.00" in r.output and "--format-arm" in r.output
    r = drift("rename measure", "Total Visits", "Attendance", "--dry-run",
              "--format-arm", "template=#,##0.00", "--model", "template",
              fleet=fy)
    assert r.exit_code == 0, r.output
    assert "arm 1" in r.output


def test_format_decimals():
    assert format_decimals("P2") == 2
    assert format_decimals("#,##0.00") == 2
    assert format_decimals("#,##0") == 0
    assert format_decimals("General Date") is None


# ───────────── remediate --ref ─────────────

def test_ref_pushes_one_measure_and_keeps_the_lineage_tag(captured, fleet_dir):
    fy = fleet_dir / "fleet.yml"
    vf = captured.model_definition("alpha") / "tables" / "Visits.tmdl"
    tag = te.first_lineage_tag(te.extract_block(
        vf.read_text(encoding="utf-8"), "measure", "Total Visits"))
    r = drift("remediate", "--ref", "Total Visits", "--yes", fleet=fy)
    assert r.exit_code == 0, r.output
    block = te.extract_block(vf.read_text(encoding="utf-8"), "measure",
                             "Total Visits")
    assert "COUNTROWS(Visits)" in block and te.first_lineage_tag(block) == tag
    r = drift("remediate", "--ref", "Total Visits", "--yes", fleet=fy)
    assert "applied=0" in r.output and "unchanged" in r.output


def test_ref_unknown_measure_is_a_usage_error(captured, fleet_dir):
    r = drift("remediate", "--ref", "No Such Measure", "--yes",
              fleet=fleet_dir / "fleet.yml")
    assert r.exit_code == 2 and "does not exist in the template" in r.output


def test_ref_absent_needs_add_missing(captured, fleet_dir):
    fy = fleet_dir / "fleet.yml"
    r = drift("remediate", "--ref", "New Members #", "--yes",
              "--model", "alpha", fleet=fy)
    assert "--add-missing" in r.output and "applied=0" in r.output
    r = drift("remediate", "--ref", "New Members #", "--yes", "--add-missing",
              "--model", "alpha", fleet=fy)
    assert "applied=1" in r.output, r.output
    assert parse_model(captured.model_definition("alpha")).measure(
        "Members", "New Members #") is not None


# ───────────── table registration ─────────────

def test_query_order_registration_and_removal():
    model = ('model Model\n\nannotation PBI_QueryOrder = ["A","B","C"]\n\n'
             "ref table A\nref table B\nref table C\n")
    assert '["A","B","C","D"]' in te.register_query_order(model, "D")
    assert te.register_query_order(model, "A") == model
    for name, left in (("A", '["B","C"]'), ("B", '["A","C"]'),
                       ("C", '["A","B"]')):
        out = te.deregister_table(model, name)
        assert left in out and f"ref table {name}\n" not in out
        assert te.deregister_table(out, name) == out
    quoted = "model M\n\nref table 'Store Orders'\n"
    assert "Store Orders" not in te.deregister_table(quoted, "Store Orders")


def test_calculated_table_copy_leaves_the_query_order_alone(captured):
    tdef = captured.template_definition
    (tdef / "tables" / "Date Picker.tmdl").write_text(
        "table 'Date Picker'\n\tlineageTag: 11111111-1111-4111-8111-1111111111aa"
        "\n\n\tpartition 'Date Picker' = calculated\n\t\tmode: import\n"
        "\t\tsource = CALENDARAUTO()\n", encoding="utf-8")
    mf = tdef / "model.tmdl"
    mf.write_text(te.register_table(mf.read_text(encoding="utf-8"),
                                    "Date Picker"), encoding="utf-8")
    amodel = captured.model_definition("alpha") / "model.tmdl"
    amodel.write_text(amodel.read_text(encoding="utf-8")
                      + '\nannotation PBI_QueryOrder = ["Members"]\n',
                      encoding="utf-8")
    capture(captured)
    f = [f for f in detect_all(captured, model_filter="alpha")
         if f.kind == "table.missing"]
    assert remediate(captured, f).count("applied") == 1
    text = amodel.read_text(encoding="utf-8")
    assert "ref table 'Date Picker'" in text
    assert '["Members"]' in text


def test_missing_axis_table_gates_whatever_the_allowlist(fleet_dir):
    tdef = fleet_dir / "template/GymChain.SemanticModel/definition"
    (tdef / "tables" / "x-Plot Specific 1.tmdl").write_text(
        "table 'x-Plot Specific 1'\n\tlineageTag: "
        "22222222-2222-4222-8222-2222222222aa\n\n"
        "\tcolumn Pick\n\t\tdataType: string\n\t\tsourceColumn: [Value1]\n",
        encoding="utf-8")
    mf = tdef / "model.tmdl"
    mf.write_text(te.register_table(mf.read_text(encoding="utf-8"),
                                    "x-Plot Specific 1"), encoding="utf-8")
    edit_fleet_yml(fleet_dir, lambda d: d["allowlist"].update(
        kinds=["measure.missing"]))
    fy = fleet_dir / "fleet.yml"
    assert drift("capture", fleet=fy).exit_code == 0
    r = drift("detect", "--kind", "table.missing", "--model", "alpha", fleet=fy)
    assert r.exit_code == 1 and "x-Plot Specific 1" in r.output
    before = sorted((fleet_dir / "franchises/alpha").rglob("*.tmdl"))
    drift("remediate", "--kind", "table.missing", "--yes", fleet=fy)
    assert sorted((fleet_dir / "franchises/alpha").rglob("*.tmdl")) == before

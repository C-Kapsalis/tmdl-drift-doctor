"""Analysis groups on a bike-shop model: the graph and plan, every drop
layer, the end-to-end drop and the table drop."""

import json

import pytest

from conftest import drift, write_fleet

from tmdl_drift_doctor import groups as gp
from tmdl_drift_doctor import mapping_codec as mc
from tmdl_drift_doctor.fieldparams import read_rows
from tmdl_drift_doctor.fleet import load_fleet
from tmdl_drift_doctor.tmdl import parse_model

AREA_ROWS = [["Sales", "", "Revenue_x"], ["Sales", "", "Store_y"],
             ["Service", "", "Repairs_x"], ["Service", "", "Store_y"],
             ["Service", "", "Mechanic_y"], ["Rentals", "", "Rent_x"]]

TAG = iter(range(10 ** 6))


def _tag():
    return f"60000000-0000-4000-8000-{next(TAG):012d}"


def fact(name, cols):
    q = f"'{name}'" if " " in name else name
    body = "".join(f"\tcolumn {c}\n\t\tdataType: string\n\t\tlineageTag: "
                   f"{_tag()}\n\t\tsourceColumn: {c}\n\n" for c in cols)
    return (f"table {q}\n\tlineageTag: {_tag()}\n\n{body}"
            f"\tpartition {q} = m\n\t\tmode: import\n\t\tsource = x\n")


def axis(name, rows):
    body = ",\n".join("\t\t\t\t" + r for r in rows)
    return (f"table '{name}'\n\tlineageTag: {_tag()}\n\n"
            f"\tcolumn '{name}'\n\t\tlineageTag: {_tag()}\n"
            f"\t\tsourceColumn: [Value1]\n\n"
            f"\tpartition '{name}' = calculated\n\t\tmode: import\n"
            f"\t\tsource =\n\t\t\t\t{{\n{body}\n\t\t\t\t}}\n")


def measures(name, items):
    body = "".join(f"\tmeasure {n} = {e}\n\t\tlineageTag: {_tag()}\n\n"
                   for n, e in items)
    return f"table '{name}'\n\tlineageTag: {_tag()}\n\n{body}"


MAP = ("table 'Area Map'\n\tlineageTag: 61000000-0000-4000-8000-000000000001"
       "\n\n\tcolumn Area\n\t\tdataType: string\n\t\tlineageTag: "
       "61000000-0000-4000-8000-000000000002\n\t\tsourceColumn: Area\n\n"
       "\tpartition 'Area Map' = m\n\t\tmode: import\n\t\tsource =\n"
       "\t\t\t\tlet\n\t\t\t\t    Source = Table.FromRows(Json.Document("
       "Binary.Decompress(Binary.FromText(\"{b64}\", BinaryEncoding.Base64),"
       " Compression.Deflate)))\n\t\t\t\tin\n\t\t\t\t    Source\n")

SELECTED = ("\tmeasure 'Selected Value' =\n"
            "\t\t\tVAR SelectedField = SELECTEDVALUE('Axis Measures 1'[Axis "
            "Measures 1 Fields])\n"
            "\t\t\tRETURN SWITCH(TRUE(),\n"
            "\t\t\tSelectedField = \"'Orders'[Revenue]\", [Revenue],\n"
            "\t\t\tSelectedField = \"'Workshop'[-Repair Count]\", "
            "[-Repair Count],\n"
            "\t\t\tSelectedField = \"'Orders'[Margin]\", DIVIDE([Revenue], "
            "[-Repair Count]),\n"
            "\t\t\tBLANK())\n"
            f"\t\tlineageTag: 62000000-0000-4000-8000-000000000001\n\n")

GATES = ("\tmeasure 'gate-Any' =\n\t\t\tVAR basic = TRUE()\n"
         "\t\t\t\t&& IF([gate-Sales], TRUE(), TRUE())\n"
         "\t\t\t\t&& IF([gate-Service], TRUE(), TRUE())\n"
         "\t\t\tVAR alt = FALSE()\n"
         "\t\t\t\t|| IF([gate-Sales], TRUE(), FALSE())\n"
         "\t\t\t\t|| IF([gate-Service], TRUE(), FALSE())\n"
         "\t\t\tRETURN basic || alt\n"
         "\t\tlineageTag: 63000000-0000-4000-8000-000000000001\n\n"
         "\tmeasure gate-Sales = 1\n\t\tlineageTag: "
         "63000000-0000-4000-8000-000000000002\n\n"
         "\tmeasure gate-Service = 1\n\t\tlineageTag: "
         "63000000-0000-4000-8000-000000000003\n\n"
         "\tmeasure gate-Services = 1\n\t\tlineageTag: "
         "63000000-0000-4000-8000-000000000004\n")


def bookmark(name, values):
    vals = [[{"Literal": {"Value": f"'{v}'"}}] for v in values]
    state = {"filters": {"byExpr": [{"filter": {
        "Version": 2, "From": [{"Name": "a", "Entity": "Area Map", "Type": 0}],
        "Where": [{"Condition": {"In": {"Expressions": [{"Column": {
            "Expression": {"SourceRef": {"Source": "a"}},
            "Property": "Area"}}], "Values": vals}}}]}}]}} if values else {}
    return {"name": name, "displayName": name, "explorationState": state}


def build(tmp_path, *, with_rent=True, margin=True):
    root = tmp_path / "shop" / "Shop.SemanticModel"
    d = root / "definition"
    (d / "tables").mkdir(parents=True)
    tables = {
        "Orders": fact("Orders", ["Amount", "StoreId"]),
        "Workshop": fact("Workshop", ["Job", "StoreId"]),
        "Rentals Log": fact("Rentals Log", ["Fee", "Day"]),
        "Stores": fact("Stores", ["StoreId", "Store Name"]),
        "Area Map": MAP.replace("{b64}", mc.encode_rows(AREA_ROWS)),
        "Axis Measures 1": axis("Axis Measures 1", [
            '("Revenue", NAMEOF([Revenue]), 0, "Revenue_x")',
            '("Repairs", NAMEOF(\'Workshop\'[-Repair Count]), 1, "Repairs_x")',
            '("Rent", NAMEOF([Rent Income]), 2, "Rent_x")']),
        "Axis Dims 1": axis("Axis Dims 1", [
            '("Store", NAMEOF(\'Stores\'[Store Name]), 0, "Store_y")',
            '("Mechanic", NAMEOF(\'Workshop\'[Job]), 1, "Mechanic_y")']),
        "Shop Measures": measures("Shop Measures", [
            ("Revenue", "SUM(Orders[Amount])"),
            ("'-Repair Count'", "COUNTROWS('Workshop')"),
            ("'Busy Flag'", "IF(ISFILTERED('Stores') || "
                            "ISFILTERED('Rentals Log'), 1)"),
            ("'Uses Rentals'", "-- uses 'Workshop'\n\t\t\t1")]
            + ([("'Rent Income'", "SUM('Rentals Log'[Fee])")]
               if with_rent else [])) + (SELECTED if margin else "\n".join(
                   ln for ln in SELECTED.split("\n") if "Margin" not in ln)),
        "Gates": measures("Gates", []) + GATES,
    }
    for n, text in tables.items():
        (d / "tables" / f"{n}.tmdl").write_text(text, encoding="utf-8")
    refs = "".join(f"ref table '{n}'\n" for n in tables)
    (d / "model.tmdl").write_text(
        "model Model\n\tculture: en-US\n\nannotation Area_Service = \"x\"\n\n"
        "annotation Area_Services_Plus = \"y\"\n\n"
        "annotation PBI_QueryOrder = [\"Orders\",\"Workshop\",\"Stores\"]\n\n"
        + refs, encoding="utf-8")
    (d / "relationships.tmdl").write_text(
        "relationship r1\n\tfromColumn: Workshop.StoreId\n"
        "\ttoColumn: Stores.StoreId\n\nrelationship r2\n"
        "\tfromColumn: Orders.StoreId\n\ttoColumn: Stores.StoreId\n",
        encoding="utf-8")
    rep = tmp_path / "shop" / "Shop.Report" / "definition"
    (rep / "bookmarks").mkdir(parents=True)
    (rep / "report.json").write_text("{}", encoding="utf-8")
    for n, vals in (("bm1", ["Service"]), ("bm2", ["Service", "Sales"]),
                    ("++New Report", ["Service"]), ("bm4", [])):
        (rep / "bookmarks" / f"{n}.bookmark.json").write_text(
            json.dumps(bookmark(n, vals), indent=2), encoding="utf-8")
    fy = write_fleet(tmp_path, root, {"north": root},
                     table_roles={"dimension": ["Stores"],
                                  "other": ["Area Map"]},
                     grouping={"mapping_table": "Area Map",
                               "selector_tables": ["Axis Measures *"],
                               "dimension_selector_tables": ["Axis Dims *"],
                               "row_drop_tables": ["Axis Measures *",
                                                   "Axis Dims *"],
                               "annotation_prefix": "Area_",
                               "non_attributing_field_groups": []},
                     gates={"prefix": "gate-", "aggregate": "gate-Any"},
                     bookmarks={"group_column": "Area"})
    return load_fleet(fy), root


def test_plan_splits_exclusive_from_shared(tmp_path):
    fleet, root = build(tmp_path)
    g = gp.build_graph(fleet, root / "definition")
    plan = gp.make_plan(g, ["Service"])
    assert plan["present"] == ["Service"]
    assert plan["exclusive"]["field_groups"] == ["Mechanic_y", "Repairs_x"]
    assert plan["shared"]["field_groups"] == {"Store_y": ["Sales"]}
    assert plan["exclusive"]["fact_tables"] == ["Workshop"]
    both = gp.make_plan(g, ["Service", "Sales"])
    assert "Store_y" in both["exclusive"]["field_groups"]
    assert both["exclusive"]["fact_tables"] == ["Orders", "Workshop"]
    none = gp.make_plan(g, ["Hiking"])
    assert none["absent"] == ["Hiking"] and not none["present"]


def test_dead_fact_tables_count_sensors_and_ignore_comments(tmp_path):
    fleet, root = build(tmp_path, with_rent=False)
    g = gp.build_graph(fleet, root / "definition")
    assert g.dead == {"Rentals Log": 1}
    # a field string inside a selector measure still reads the table
    g = gp.build_graph(fleet, root / "definition",
                       ignore_measures={"-Repair Count"})
    assert "Workshop" not in g.dead
    g = gp.build_graph(fleet, root / "definition",
                       ignore_measures={"-Repair Count", "Selected Value"})
    assert "Workshop" in g.dead             # its only readers would be gone


def test_plan_command_writes_stable_json(tmp_path):
    fleet, root = build(tmp_path)
    fy = fleet.path
    out1, out2 = tmp_path / "a.json", tmp_path / "b.json"
    r = drift("groups plan", "--model", "north", "--drop", "Service",
              "--json", out1, fleet=fy)
    assert r.exit_code == 0, r.output
    assert "shared field group Store_y: kept by Sales" in r.output
    drift("groups plan", "--model", "north", "--drop", "Service",
          "--json", out2, fleet=fy)
    assert out1.read_bytes() == out2.read_bytes()
    assert drift("groups plan", "--model", "nope", "--drop", "Service",
                 fleet=fy).exit_code == 2


def test_layers_dry_run(tmp_path):
    fleet, root = build(tmp_path)
    d = root / "definition"
    c = gp.conf(fleet)
    plan = gp.make_plan(gp.build_graph(fleet, d), ["Service"])
    ex = plan["exclusive"]["field_groups"]
    ch = []
    r = gp.layer_selector_rows(d, c, ex, ch)
    assert r["rows_removed"] == 2 and len(r["files"]) == 2
    ch = []
    ms = gp.dropped_axis_measures(d, c, ex)
    assert ms == ["-Repair Count"]
    r = gp.layer_selector_branches(d, c, ms, ch)
    assert r["lines_removed"] == 1          # the Margin arm stays
    ch = []
    r = gp.layer_annotation(d, c, ["Service"], ch)
    assert r["keys"] == ["Area_Service"]
    ch = []
    r = gp.layer_gates(d, fleet, ["Service"], ch)
    assert r["gates_removed"] == {"gate-Service": "Gates.tmdl"}
    assert r["aggregate_lines"] == {"gate-Service": 2}
    ch = []
    r = gp.layer_bookmarks(root, fleet, c, ["Service"], ch)
    assert [b["display_name"] for b in r["bookmarks"]] == ["bm1"]
    ch = []
    r = gp.layer_bookmarks(root, fleet, c, ["Service", "Sales"], ch)
    assert sorted(b["display_name"] for b in r["bookmarks"]) == ["bm1", "bm2"]
    ch = []
    r = gp.layer_mapping(d, c, ["Service"], ch)
    assert r["removed"] == 3 and r["per_group"] == {"Service": 3}


def test_drop_end_to_end(tmp_path):
    fleet, root = build(tmp_path, margin=False)
    d = root / "definition"
    before = {p: p.read_bytes() for p in root.parent.rglob("*") if p.is_file()}
    r = drift("groups drop", "--model", "north", "--drop", "Service",
              fleet=fleet.path)
    assert r.exit_code == 0, r.output
    assert {p: p.read_bytes() for p in root.parent.rglob("*")
            if p.is_file()} == before          # dry run writes nothing
    r = drift("groups drop", "--model", "north", "--drop", "Service",
              "--apply", fleet=fleet.path)
    assert r.exit_code == 0, r.output
    model = parse_model(d)
    assert "Selected Value" in model.measure_hosts()
    assert "Workshop" not in model.tables
    assert not (d / "tables" / "Workshop.tmdl").exists()
    assert "-Repair Count" not in model.measure_hosts()
    assert "gate-Service" not in model.measure_hosts()
    assert "gate-Services" in model.measure_hosts()
    assert "[gate-Service]" not in (d / "tables" / "Gates.tmdl").read_text()
    assert [r.display for r in read_rows(d / "tables" / "Axis Dims 1.tmdl")[0]]\
        == ["Store"]
    rows = mc.extract_rows((d / "tables" / "Area Map.tmdl").read_text())
    assert not [x for x in rows if x[0] == "Service"]
    text = (d / "model.tmdl").read_text()
    assert "Area_Service " not in text and "Area_Services_Plus" in text
    assert "ref table 'Workshop'" not in text and '"Workshop"' not in text
    assert "r1" not in (d / "relationships.tmdl").read_text()
    bm = root.parent / "Shop.Report" / "definition" / "bookmarks"
    assert not (bm / "bm1.bookmark.json").exists()
    assert (bm / "bm2.bookmark.json").exists()
    snap = {p: p.read_bytes() for p in root.parent.rglob("*") if p.is_file()}
    drift("groups drop", "--model", "north", "--drop", "Service", "--apply",
          fleet=fleet.path)
    assert {p: p.read_bytes() for p in root.parent.rglob("*")
            if p.is_file()} == snap


SENSOR_CASES = [
    ("\t\t\tISINSCOPE('Stores'[Name]) ||\n\t\t\tISINSCOPE('Rentals Log'[Day]) "
     "||\n\t\t\tISINSCOPE('Stores'[City])\n",
     "\t\t\tISINSCOPE('Stores'[Name]) ||\n\t\t\tISINSCOPE('Stores'[City])\n"),
    ("ISFILTERED('Stores') ||\n ISFILTERED('Rentals Log')",
     "ISFILTERED('Stores')"),
    ("IF(ISFILTERED(Stores) || ISFILTERED('Rentals Log'), 1)",
     "IF(ISFILTERED(Stores), 1)"),
    ("NOT ISEMPTY('Rentals Log') && [x]", "[x]"),
]


@pytest.mark.parametrize("before, after", SENSOR_CASES)
def test_sensor_operands_leave_their_chain_well_formed(before, after):
    out, n = gp.remove_sensor_operands(before, "Rentals Log",
                                       gp.DEFAULTS["sensor_functions"])
    assert (out, n) == (after, 1)


def test_drop_table_reaches_every_surface(tmp_path):
    fleet, root = build(tmp_path)
    d = root / "definition"
    (d / "cultures").mkdir()
    qa = {"Version": "1.0", "Entities": {
        "rentals_log": {"Definition": {"Binding": {
            "ConceptualEntity": "Rentals Log"}}},
        "orders": {"Definition": {"Binding": {"ConceptualEntity": "Orders"}}}},
        "Relationships": {"r": {"Binding": {"Entity": "rentals_log.day"}},
                          "o": {"Binding": {"Entity": "orders"}}}}
    body = "\n".join("\t\t" + ln for ln in json.dumps(qa, indent=2).split("\n"))
    (d / "cultures" / "en-US.tmdl").write_text(
        "cultureInfo en-US\n\n\tlinguisticMetadata =\n" + body
        + "\n\t\tcontentType: json\n", encoding="utf-8")
    vis = (root.parent / "Shop.Report" / "definition" / "pages" / "p1"
           / "visuals" / "v1")
    vis.mkdir(parents=True)
    visual = ('{\n  "visual": {\n    "query": {\n      "queryState": {\n'
              '        "Values": {\n          "projections": [\n'
              '            {\n              "field": {\n'
              '                "Column": {\n                  "Expression": '
              '{\n                    "SourceRef": {\n                      '
              '"Entity": "Rentals Log"\n                    }\n'
              '                  },\n                  "Property": "Fee"\n'
              '                }\n              },\n              "queryRef": '
              '"Rentals Log.Fee"\n            }\n          ]\n        }\n'
              '      }\n    },\n    "width": 372.66666666666669\n  }\n}\n')
    (vis / "visual.json").write_text(visual, encoding="utf-8")
    changes = []
    rep = gp.drop_table(fleet, root, "Rentals Log", changes)
    gp.write_changes(changes)
    assert rep["sensors"] == {"Shop Measures.tmdl": 1}
    assert any("SUM('Rentals Log'[Fee])" in m for m in rep["manual_review"])
    assert rep["registration"] and rep["qa_entities"] == 1
    assert rep["qa_relationships"] == 1
    culture = (d / "cultures" / "en-US.tmdl").read_text()
    assert "rentals_log" not in culture and culture.startswith(
        "cultureInfo en-US\n\n\tlinguisticMetadata =\n")
    assert culture.endswith("\t\tcontentType: json\n")
    out = (vis / "visual.json").read_text()
    assert "Rentals Log" not in out and "372.66666666666669" in out
    assert not (d / "tables" / "Rentals Log.tmdl").exists()
    assert "ISFILTERED('Rentals Log')" not in (
        d / "tables" / "Shop Measures.tmdl").read_text()

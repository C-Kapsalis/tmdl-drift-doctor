"""Selector arms, add-branch, named purges, superseded measures, group
annotations, the configured column purge, opt-in extra deletes and the
retirement-cascade deltas."""

from conftest import detect_all, drift, edit_file, edit_fleet_yml

from tmdl_drift_doctor import tmdl_edit as te
from tmdl_drift_doctor.baseline import capture
from tmdl_drift_doctor.fleet import load_fleet
from tmdl_drift_doctor.groups import annotate
from tmdl_drift_doctor.remediate import remediate
from tmdl_drift_doctor.selectors import dangling_arms, plan_branch, remove_lines
from tmdl_drift_doctor.tmdl import parse_model

ROUTER = (
    "table 'Fixture Measures'\n"
    "\tlineageTag: 70000000-0000-4000-8000-000000000001\n\n"
    "\tmeasure 'Live Measure' = 1\n"
    "\t\tlineageTag: 70000000-0000-4000-8000-000000000002\n\n"
    "\tmeasure Router =\n"
    "\t\t\tSWITCH(\n"
    "\t\t\t\tSELECTEDVALUE('x-Plot Specific 2'[x-Plot Specific 2 Fields]),\n"
    "\t\t\t\t\"'Fixture Measures'[Live Measure]\", [Live Measure],\n"
    "\t\t\t\t\"'Fixture Measures'[Gone Measure]\", [Gone Measure],\n"
    "\t\t\t\t-- \"'Fixture Measures'[Parked]\", [Parked],\n"
    "\t\t\t\tBLANK())\n"
    "\t\tlineageTag: 70000000-0000-4000-8000-000000000003\n")


def test_dangling_arm_is_found_and_removed(tmp_path):
    d = tmp_path / "definition" / "tables"
    d.mkdir(parents=True)
    (d / "Fixture Measures.tmdl").write_text(ROUTER, encoding="utf-8")
    arms = dangling_arms(tmp_path / "definition", {"Live Measure", "Router"})
    assert arms == [("Fixture Measures.tmdl", 11, ["Gone Measure"])]
    out = remove_lines(ROUTER, {11})
    assert "[Gone Measure]" not in out and "[Live Measure]," in out
    assert "-- \"'Fixture Measures'[Parked]\"" in out


def test_removing_the_last_arm_drops_its_separator():
    text = ("SWITCH(x,\n\t\"'T'[A]\", [A],\n\t\"'T'[B]\", [B],\n)\n")
    assert remove_lines(text, {3}) == "SWITCH(x,\n\t\"'T'[A]\", [A]\n)\n"


HELPER = ("table 'Helper Measures'\n"
          "\tlineageTag: 71000000-0000-4000-8000-000000000001\n\n"
          "\tmeasure 'Picked Value' =\n"
          "\t\t\tVAR sel = SELECTEDVALUE('Metric Picker'[Metric Fields])\n"
          "\t\t\tRETURN SWITCH(TRUE(),\n"
          "\t\t\t\tsel = \"'Shop Measures'[Revenue]\", CALCULATE([Revenue]),\n"
          "\t\t\t\tsel = \"'Shop Measures'[Orders]\", CALCULATE([Orders]),\n"
          "\t\t\t\tBLANK())\n"
          "\t\tlineageTag: 71000000-0000-4000-8000-000000000002\n")


def test_add_branch_plan():
    status, new, n = plan_branch(HELPER, "Basket Size", "Revenue")
    assert n == 1 and status.startswith("insert")
    lines = new.split("\n")
    i = next(k for k, l in enumerate(lines) if "[Revenue]\"" in l)
    assert lines[i + 1] == ("\t\t\t\tsel = \"'Shop Measures'[Basket Size]\", "
                            "CALCULATE([Basket Size]),")
    assert plan_branch(new, "Basket Size", "Revenue")[0].startswith("already")
    assert plan_branch(HELPER, "X", "Nope")[0].startswith("REFUSED")
    two = HELPER.replace("BLANK())", "\"'Shop Measures'[Revenue]\" = sel, "
                         "[Revenue],\n\t\t\t\tBLANK())")
    partial, _n2, _ = plan_branch(two.replace("[Orders]\", CALCULATE"
                                              "([Orders])", "[Basket Size]\","
                                              " CALCULATE([Basket Size])"),
                                  "Basket Size", "Revenue")
    assert partial.startswith("REFUSED: partial")
    crlf = HELPER.replace("\n", "\r\n")
    _s, new, _n = plan_branch(crlf, "Basket Size", "Revenue")
    assert "\r\n" in new and "\n" not in new.replace("\r\n", "")


def test_add_branch_command(fleet_dir):
    path = fleet_dir / "franchises/alpha/GymChain.SemanticModel/definition/" \
        "tables/Helper Measures.tmdl"
    path.write_text(HELPER, encoding="utf-8")
    fy = fleet_dir / "fleet.yml"
    r = drift("add-branch", "--measure", "Basket Size", "--sibling", "Revenue",
              fleet=fy)
    assert r.exit_code == 0 and "to insert" in r.output
    assert path.read_text(encoding="utf-8") == HELPER
    r = drift("add-branch", "--measure", "Basket Size", "--sibling", "Revenue",
              "--yes", fleet=fy)
    assert r.exit_code == 0 and "[Basket Size]" in path.read_text()
    r = drift("add-branch", "--measure", "Z", "--sibling", "Nope", "--yes",
              fleet=fy)
    assert r.exit_code == 1


def test_purge_named_measures(captured, fleet_dir):
    fy = fleet_dir / "fleet.yml"
    vf = captured.model_definition("alpha") / "tables" / "Visits.tmdl"
    before = vf.read_bytes()
    r = drift("purge", "--measure", "Total Visits", "--model", "alpha",
              fleet=fy)
    assert "BLOCKED" in r.output and r.exit_code == 1
    r = drift("purge", "--measure", "Peak Hour Visits", "--model", "alpha",
              fleet=fy)
    assert "would remove" in r.output and vf.read_bytes() == before
    r = drift("purge", "--measure", "Peak Hour Visits", "--model", "alpha",
              "--apply", fleet=fy)
    assert "not confirmed" in r.output and vf.read_bytes() == before
    r = drift("purge", "--measure", "Peak Hour Visits", "--model", "alpha",
              "--apply", "--yes", fleet=fy)
    assert r.exit_code == 0 and "[removed]" in r.output
    assert parse_model(captured.model_definition("alpha")).measure(
        "Visits", "Peak Hour Visits") is None
    r = drift("purge", "--measure", "Peak Hour Visits", "--model", "alpha",
              "--apply", "--yes", fleet=fy)
    assert "nothing to remove" in r.output


def test_retire_superseded_measure(captured, fleet_dir):
    fy = fleet_dir / "fleet.yml"
    r = drift("retire", "Total Visits", "--new", "Avg Visit Duration",
              fleet=fy)
    assert "REFUSED" in r.output              # DAX still calls it
    assert drift("retire", "X", fleet=fy).exit_code == 2
    r = drift("retire", "Peak Hour Visits", "--drop", fleet=fy)
    assert "dry run" in r.output
    r = drift("retire", "Peak Hour Visits", "--drop", "--yes", fleet=fy)
    assert "retired" in r.output
    assert parse_model(captured.model_definition("bravo")).measure(
        "Visits", "Peak Hour Visits") is None


def test_groups_annotate():
    text = ("model Model\n\tculture: en-US\n\n"
            "annotation Category_Retail = \"Hand-written retail text\"\n"
            "annotation Category_Legacy = \"Old group\"\n\nref table Sales\n")
    new, n = annotate(text, ["Retail", "Web Shop", "Kiosk"], "Category_",
                      {"Web_Shop": "Online store traffic"})
    assert n == {"kept": 1, "added": 2, "removed": 1, "total": 3}
    assert "Category_Legacy" not in new and "\n\n\n" not in new
    order = [l for l in new.split("\n") if l.startswith("annotation")]
    assert order == ['annotation Category_Kiosk = "Kiosk metrics"',
                     'annotation Category_Retail = "Hand-written retail text"',
                     'annotation Category_Web_Shop = "Online store traffic"']
    assert new.index("Category_Web_Shop") < new.index("ref table Sales")
    assert annotate(new, ["Retail", "Web Shop", "Kiosk"], "Category_",
                    {"Web_Shop": "Online store traffic"})[0] == new


def test_configured_column_purge_is_sync_only(captured, fleet_dir):
    mf = captured.model_definition("alpha") / "tables" / "Members.tmdl"
    edit_file(mf, "\tpartition Members = m",
              "\tcolumn updated_at_db\n\t\tdataType: dateTime\n"
              "\t\tlineageTag: 72000000-0000-4000-8000-000000000001\n"
              "\t\tsourceColumn: updated_at_db\n\n\tpartition Members = m")
    edit_fleet_yml(fleet_dir, lambda d: d.update(column_purge={
        "names": ["updated_at_db"], "exceptions": ["Visits[updated_at_db]"]}))
    fleet = load_fleet(fleet_dir / "fleet.yml")
    assert not [f for f in detect_all(fleet) if f.kind == "column.purge_listed"]
    found = [f for f in detect_all(fleet, sync=True)
             if f.kind == "column.purge_listed"]
    assert [f.ref for f in found] == ["Members[updated_at_db]"]
    fleet.allowlist_kinds.add("column.purge_listed")
    res = remediate(fleet, found, sync=True, batch_only=True,
                    allow_destructive=False)
    assert res.count("skipped_destructive") == 1
    res = remediate(fleet, found, sync=True)
    assert res.count("applied") == 1 and res.notices


def test_extra_delete_is_offered_only_interactively(captured):
    extra = [f for f in detect_all(captured, model_filter="alpha")
             if f.kind == "extra.measure"]
    assert extra
    res = remediate(captured, extra)
    assert {o.status for o in res.outcomes} == {"skipped"}
    res = remediate(captured, extra, offer_extra_delete=True, batch_only=True)
    assert {o.status for o in res.outcomes} == {"skipped_destructive"}
    asked = []

    def confirm(f, label, changes, position=None):
        asked.append(label)
        return True, ""

    res = remediate(captured, extra, offer_extra_delete=True, confirm=confirm)
    assert res.count("applied") == len(extra) and asked
    ref = extra[0].ref
    assert not [f for f in detect_all(captured, model_filter="alpha")
                if f.ref == ref]


def test_retired_alias_and_protected_names(captured, fleet_dir):
    vf = captured.model_definition("bravo") / "tables" / "Visits.tmdl"
    edit_file(vf, "\tpartition Visits = m",
              "\tmeasure '!Peak Hour Visits' = 1\n\t\tlineageTag: "
              "73000000-0000-4000-8000-000000000001\n\n\tpartition Visits = m")
    from conftest import retire_everything_from_template
    retire_everything_from_template(captured)
    capture(captured)
    found = {f.ref: f for f in detect_all(captured, model_filter="bravo")
             if f.kind == "retire.measure"}
    assert found["Visits[!Peak Hour Visits]"].data.get("alias")
    edit_fleet_yml(fleet_dir, lambda d: d.update(
        protected_name_pattern="peak hour"))
    fleet = load_fleet(fleet_dir / "fleet.yml")
    blocked = [f for f in detect_all(fleet, model_filter="bravo")
               if f.kind == "retire.measure"]
    assert blocked and all(f.blocked for f in blocked)

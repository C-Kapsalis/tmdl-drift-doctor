"""Field-parameter kinds on a small bike-shop fleet: schema modernization,
cross-level identity, plot-specific extras, template row merge, dangling
rows, retag, and writer edge cases."""

import pytest

from conftest import detect_all, drift, write_fleet

from tmdl_drift_doctor import TmdlEditError
from tmdl_drift_doctor.baseline import capture
from tmdl_drift_doctor.fieldparams import (
    FieldParamSchemaError,
    build_schema_plan,
    modernize_schema,
    nameof_target,
    parse_rows,
    read_rows,
    retag,
    rewrite_rows_text,
)
from tmdl_drift_doctor.fleet import load_fleet
from tmdl_drift_doctor.remediate import remediate

MODEL = "model Model\n\tculture: en-US\n\n"

SALES = ("table Sales\n\tlineageTag: 10000000-0000-4000-8000-000000000001\n\n"
         "\tmeasure Revenue = SUM(Sales[amount])\n"
         "\t\tlineageTag: 10000000-0000-4000-8000-000000000002\n\n"
         "\tmeasure Units = COUNTROWS(Sales)\n"
         "\t\tlineageTag: 10000000-0000-4000-8000-000000000003\n\n"
         "\tcolumn amount\n\t\tdataType: double\n"
         "\t\tlineageTag: 10000000-0000-4000-8000-000000000004\n"
         "\t\tsourceColumn: amount\n\n"
         "\tcolumn Region\n\t\tdataType: string\n"
         "\t\tlineageTag: 10000000-0000-4000-8000-000000000005\n"
         "\t\tsourceColumn: Region\n\n"
         "\tcolumn Store\n\t\tdataType: string\n"
         "\t\tlineageTag: 10000000-0000-4000-8000-000000000006\n"
         "\t\tsourceColumn: Store\n\n"
         "\tpartition Sales = m\n\t\tmode: import\n\t\tsource = x\n")


def _columns(name: str, extra: int) -> str:
    names = [name, f"{name} Fields", f"{name} Order", f"{name} Field Category"]
    names += [f"{name} Extra {n}" for n in range(1, extra + 1)]
    out = []
    for n, col in enumerate(names, 1):
        out.append(f"\tcolumn '{col}'\n\t\tlineageTag: "
                   f"2000000{n}-0000-4000-8000-{abs(hash(name)) % 10**12:012d}"
                   f"\n\t\tsummarizeBy: none\n\t\tsourceColumn: [Value{n}]\n")
    return "\n".join(out)


def axis(name: str, rows: list, extra: int = 1) -> str:
    body = ",\n".join("\t\t\t\t" + r for r in rows)
    return (f"table '{name}'\n\tlineageTag: 30000000-0000-4000-8000-000000000001"
            f"\n\n{_columns(name, extra)}\n"
            f"\tpartition '{name}' = calculated\n\t\tmode: import\n"
            f"\t\tsource =\n\t\t\t\t{{\n{body}\n\t\t\t\t}}\n\n"
            f"\tannotation PBI_Id = 1\n")


def row(display, ref, order, group, *extras):
    fields = [f'"{display}"', f"NAMEOF({ref})", str(order), f'"{group}"']
    fields += [f'"{e}"' for e in extras]
    return "(" + ", ".join(fields) + ")"


def make_model(root, tables: dict):
    d = root / "definition"
    (d / "tables").mkdir(parents=True)
    refs = "".join(f"ref table '{n}'\n" for n in tables)
    (d / "model.tmdl").write_text(MODEL + refs, encoding="utf-8")
    for name, text in tables.items():
        (d / "tables" / f"{name}.tmdl").write_text(text, encoding="utf-8")
    return root


def make_fleet(tmp_path, template_tables, derived_tables, **keys):
    t = make_model(tmp_path / "t" / "Shop.SemanticModel", template_tables)
    m = make_model(tmp_path / "m" / "Shop.SemanticModel", derived_tables)
    fy = write_fleet(tmp_path, t, {"north": m}, **keys)
    fleet = load_fleet(fy)
    capture(fleet)
    return fleet, m / "definition" / "tables"


REV = row("Revenue", "[Revenue]", 1, "Sales_g", "Other")
UNITS = row("Units", "[Units]", 2, "Sales_g", "Other")
REGION = row("Region", "'Sales'[Region]", 1, "Geo_y", "Other")
STORE = row("Store", "'Sales'[Store]", 2, "Geo_y", "Other")


# ───────────── schema ─────────────

def test_schema_modernize_adds_the_template_column_and_widens_rows(tmp_path):
    tmpl = axis("x-Selector 1", [REV, UNITS], extra=1)
    narrow = axis("x-Selector 1", [row("Revenue", "[Revenue]", 1, "Sales_g")],
                  extra=0)
    fleet, tables = make_fleet(
        tmp_path, {"Sales": SALES, "x-Selector 1": tmpl},
        {"Sales": SALES, "x-Selector 1": narrow},
        fieldparam_tables=["x-Selector *"],
        allowlist={"kinds": ["fieldparam.schema_modernize"]})
    f = [x for x in detect_all(fleet) if x.kind.startswith("fieldparam.schema")]
    assert [(x.kind, x.ref) for x in f] == [
        ("fieldparam.schema_modernize", "x-Selector 1")]
    res = remediate(fleet, f)
    assert res.count("applied") == 1, [o.reason for o in res.outcomes]
    rows, arity = read_rows(tables / "x-Selector 1.tmdl")
    assert arity == 5 and rows[0].extras == ["Other"]
    assert "x-Selector 1 Extra 1" in (tables / "x-Selector 1.tmdl").read_text()
    assert not [x for x in detect_all(fleet)
                if x.kind.startswith("fieldparam.schema")]


def test_schema_block_names_the_derived_only_column(tmp_path):
    t = tmp_path / "t.tmdl"
    d = tmp_path / "d.tmdl"
    t.write_text(axis("x-Selector 1", [REV], extra=1), encoding="utf-8")
    d.write_text(axis("x-Selector 1", [row("Revenue", "[Revenue]", 1,
                                           "Sales_g", "Other", "Cost")],
                      extra=2), encoding="utf-8")
    before = d.read_bytes()
    with pytest.raises(FieldParamSchemaError,
                       match="x-Selector 1 Extra 2"):
        build_schema_plan(d, t)
    with pytest.raises(FieldParamSchemaError):
        modernize_schema(d, t)
    assert d.read_bytes() == before


# ───────────── cross-level identity and plot-specific extras ─────────────

def test_cross_level_row_is_added_to_the_level_lacking_it(tmp_path):
    levels = {f"y-Selector {n}": axis(f"y-Selector {n}", [REGION, STORE])
              for n in (1, 2, 3)}
    derived = dict(levels)
    derived["y-Selector 2"] = axis("y-Selector 2", [STORE])
    fleet, tables = make_fleet(
        tmp_path, {"Sales": SALES, **levels}, {"Sales": SALES, **derived},
        allowlist={"kinds": ["fieldparam.cross_level"]})
    one = (tables / "y-Selector 1.tmdl").read_bytes()
    found = [x for x in detect_all(fleet)
             if x.kind.startswith("fieldparam.cross_level")]
    assert [(x.kind, x.ref) for x in found] == [
        ("fieldparam.cross_level.Geo_y", "y-Selector/Geo_y")]
    assert remediate(fleet, found).count("applied") == 1
    assert [r.display for r in read_rows(tables / "y-Selector 2.tmdl")[0]] \
        == ["Region", "Store"]
    assert (tables / "y-Selector 1.tmdl").read_bytes() == one
    assert not [x for x in detect_all(fleet)
                if x.kind.startswith("fieldparam.cross_level")]


def test_plot_specific_extra_is_registered_in_the_levels(tmp_path):
    levels = {f"y-Selector {n}": axis(f"y-Selector {n}", [REGION])
              for n in (1, 2)}
    variant = axis("y-Plot Specific 1", [REGION, STORE])
    fleet, tables = make_fleet(
        tmp_path, {"Sales": SALES, **levels},
        {"Sales": SALES, **levels, "y-Plot Specific 1": variant},
        allowlist={"kinds": ["fieldparam.plot_specific_extra"]})
    before = (tables / "y-Plot Specific 1.tmdl").read_bytes()
    found = [x for x in detect_all(fleet)
             if x.kind.startswith("fieldparam.plot_specific_extra")]
    assert [x.ref for x in found] == ["y-Plot Specific 1/Geo_y"]
    assert remediate(fleet, found).count("applied") == 1
    for n in (1, 2):
        assert "Store" in [r.display for r in
                           read_rows(tables / f"y-Selector {n}.tmdl")[0]]
    assert (tables / "y-Plot Specific 1.tmdl").read_bytes() == before


# ───────────── template rows and dangling rows ─────────────

def test_template_row_missing_is_added(tmp_path):
    fleet, tables = make_fleet(
        tmp_path, {"Sales": SALES, "x-Selector 1": axis("x-Selector 1",
                                                        [REV, UNITS])},
        {"Sales": SALES, "x-Selector 1": axis("x-Selector 1", [REV])},
        allowlist={"kinds": ["fieldparam.row_missing"]})
    found = [x for x in detect_all(fleet) if x.kind == "fieldparam.row_missing"]
    assert [x.ref for x in found] == ["x-Selector 1/Units"]
    assert remediate(fleet, found).count("applied") == 1
    assert [r.display for r in read_rows(tables / "x-Selector 1.tmdl")[0]] \
        == ["Revenue", "Units"]


def test_dangling_rows_are_listed_and_removed(tmp_path):
    rows = [row("Revenue", "'Sales'[Revenue]", 1, "Sales_g", "Other"),
            row("Gone", "[Gone]", 2, "Sales_g", "Other"),
            row("Rev (case)", "'sales'[revenue]", 3, "Sales_g", "Other"),
            row("Quoted", "'Sales'['Units']", 4, "Sales_g", "Other")]
    text = axis("x-Selector 1", rows).replace(
        "\t\t\t\t}", "\t\t\t\t-- (\"Parked\", NAMEOF([Gone]), 9, \"Sales_g\", "
        "\"Other\"),\n\t\t\t\t}")
    fleet, tables = make_fleet(
        tmp_path, {"Sales": SALES}, {"Sales": SALES, "x-Selector 1": text},
        allowlist={"kinds": ["fieldparam.dangling_row"]})
    found = [x for x in detect_all(fleet) if x.kind == "fieldparam.dangling_row"]
    assert len(found) == 1 and "Gone" in found[0].detail
    assert "Rev (case)" not in found[0].detail
    assert remediate(fleet, found).count("applied") == 1
    out = (tables / "x-Selector 1.tmdl").read_text(encoding="utf-8")
    assert [r.display for r in parse_rows(out)[0]] == [
        "Revenue", "Rev (case)", "Quoted"]
    assert '-- ("Parked", NAMEOF([Gone])' in out
    assert not [x for x in detect_all(fleet)
                if x.kind == "fieldparam.dangling_row"]


def test_nameof_target_forms():
    assert nameof_target("NAMEOF('Sales'['Units'])") == ("Sales", "Units")
    assert nameof_target("NAMEOF ( [Revenue] )") == ("", "Revenue")
    assert nameof_target("NAMEOF(Sales[Region])") == ("Sales", "Region")
    assert nameof_target("NAMEOF('It''s'[x])") == ("It's", "x")


# ───────────── retag ─────────────

RETAG = axis("y-Selector 1", [
    row("Region", "'Sales'[Region]", 1, "m_attr_y", "Other"),
    row("Resolved", "'Sales'[Store]", 2, "m_attr_y", "Other"),
    row("Region", "'Sales'[Region]", 3, "other_y", "Other")]).replace(
    "\t\t\t\t}", "\t\t\t\t-- (\"Old\", NAMEOF([x]), 9, \"m_attr_y\", \"O\"),\n"
    "\t\t\t\t}")


def test_retag_moves_only_the_named_rows_of_the_named_group():
    out, hit = retag(RETAG, "m_attr_y", "geo_y", ["Region"])
    assert hit == ["Region"]
    groups = [(r.display, r.group) for r in parse_rows(out)[0]]
    assert groups == [("Region", "geo_y"), ("Resolved", "m_attr_y"),
                      ("Region", "other_y")]
    assert out.count('"geo_y"') == 1


@pytest.mark.parametrize("source, displays", [
    ("m_attr_y", ["Old"]),            # a parked row is never re-armed
    ("m_attr_y", ["Region", "Nope"]),
    ("other_y", ["Resolved"]),
])
def test_retag_fails_loudly_rather_than_half_retagging(source, displays):
    from tmdl_drift_doctor import DriftDoctorError
    with pytest.raises(DriftDoctorError, match="retag incomplete"):
        retag(RETAG, source, "geo_y", displays)


def test_retag_command(tmp_path):
    f = tmp_path / "y-Selector 1.tmdl"
    f.write_text(RETAG, encoding="utf-8")
    r = drift("fieldparam retag", "--from", "m_attr_y", "--to", "geo_y",
              "--display", "Region", str(f))
    assert r.exit_code == 0, r.output
    assert '"geo_y"' in f.read_text(encoding="utf-8")
    r = drift("fieldparam retag", "--from", "m_attr_y", "--to", "geo_y",
              "--display", "Region", str(f))
    assert r.exit_code == 1 and "retag incomplete" in r.output


# ───────────── writer edge cases ─────────────

def test_writer_refuses_a_table_without_a_constructor(tmp_path):
    text = "table T\n\n\tpartition T = m\n\t\tmode: import\n\t\tsource = x\n"
    with pytest.raises(TmdlEditError, match="left untouched"):
        rewrite_rows_text(text, [], 4)


def test_writer_drops_stray_fences_after_a_bare_constructor():
    text = axis("x-Selector 1", [REV]).replace(
        "\t\t\t\t}\n", "\t\t\t\t}\n\t\t\t\t```\n\t\t\t\t```\n")
    rows, arity = parse_rows(text)
    out = rewrite_rows_text(text, rows, arity)
    assert "```" not in out
    assert rewrite_rows_text(out, *parse_rows(out)) == out

"""Incremental-refresh parity: missing policies, window drift, malformed
policies and declared windows, on a two-model bike-shop fleet."""

from conftest import detect_all, write_fleet

from tmdl_drift_doctor.baseline import capture
from tmdl_drift_doctor.fleet import load_fleet
from tmdl_drift_doctor.refresh import parse_policy
from tmdl_drift_doctor.remediate import remediate

SOURCE = ("\t\tsource =\n\t\t\t\tlet\n\t\t\t\t    Source = sales(\"{view}\", "
          "RangeStart, RangeEnd)\n\t\t\t\tin\n\t\t\t\t    Source\n")


def policy(rolling="6", source_view="sales_n"):
    return ("\trefreshPolicy\n\t\tpolicyType: basic\n"
            "\t\trollingWindowGranularity: year\n"
            f"\t\trollingWindowPeriods: {rolling}\n"
            "\t\tincrementalGranularity: month\n\t\tincrementalPeriods: 3\n"
            "\t\tsourceExpression =\n\t\t\t\tlet\n\t\t\t\t    Source = "
            f"sales(\"{source_view}\", RangeStart, RangeEnd)\n\t\t\t\tin\n"
            "\t\t\t\t    Source\n\n")


def sales(with_policy=None, view="sales_n", extra_partition=False):
    text = ("table Sales\n\tlineageTag: 40000000-0000-4000-8000-000000000001\n\n"
            "\tcolumn amount\n\t\tdataType: double\n"
            "\t\tlineageTag: 40000000-0000-4000-8000-000000000002\n"
            "\t\tsourceColumn: amount\n\n")
    text += with_policy or ""
    text += "\tpartition Sales = m\n\t\tmode: import\n" + SOURCE.format(view=view)
    if extra_partition:
        text += "\n\tpartition Sales2 = m\n\t\tmode: import\n" + \
            SOURCE.format(view=view)
    return text + "\n\tannotation PBI_ResultType = Table\n"


def build(tmp_path, template_text, model_text, **entry):
    for side, text in (("t", template_text), ("m", model_text)):
        d = tmp_path / side / "Shop.SemanticModel" / "definition"
        (d / "tables").mkdir(parents=True)
        (d / "model.tmdl").write_text("model Model\n\nref table Sales\n",
                                      encoding="utf-8")
        (d / "tables" / "Sales.tmdl").write_text(text, encoding="utf-8")
    m = tmp_path / "m" / "Shop.SemanticModel"
    fy = write_fleet(tmp_path, tmp_path / "t" / "Shop.SemanticModel",
                     {"north": {"path": m, **entry}},
                     allowlist={"kinds": ["refresh_policy"]})
    fleet = load_fleet(fy)
    capture(fleet)
    return fleet, m / "definition" / "tables" / "Sales.tmdl"


def _refresh(fleet):
    return [f for f in detect_all(fleet) if f.kind.startswith("refresh_policy")]


def test_missing_policy_is_added_from_the_models_own_source(tmp_path):
    fleet, path = build(tmp_path, sales(policy()), sales(view="north_sales"))
    found = _refresh(fleet)
    assert [(f.kind, f.ref) for f in found] == [("refresh_policy.missing",
                                                 "Sales")]
    res = remediate(fleet, found)
    assert res.count("applied") == 1, [o.reason for o in res.outcomes]
    pol = parse_policy(path.read_text(encoding="utf-8"))
    assert pol.scalars["rollingWindowPeriods"] == "6"
    assert "north_sales" in pol.source            # never the template's view
    assert _refresh(fleet) == []


def test_window_drift_is_rewritten_and_owes_a_full_refresh(tmp_path):
    fleet, path = build(tmp_path, sales(policy("6")), sales(policy("3")))
    found = _refresh(fleet)
    assert [f.kind for f in found] == ["refresh_policy.window_drift"]
    assert "rollingWindowPeriods '3' -> '6'" in found[0].detail
    res = remediate(fleet, found)
    assert res.count("applied") == 1
    assert parse_policy(path.read_text(encoding="utf-8")).scalars[
        "rollingWindowPeriods"] == "6"
    assert [d["note"].split(":")[0] for d in res.digest] == [
        "full refresh owed"]


def test_declared_window_is_the_expected_one(tmp_path):
    fleet, _p = build(tmp_path, sales(policy("6")), sales(policy("3")),
                      overrides={"refresh_windows": [
                          {"table": "Sales", "kind": "window",
                           "rollingWindowPeriods": 3,
                           "reason": "three years of history only"}]})
    assert _refresh(fleet) == []


def test_declared_absent_policy_is_not_reported(tmp_path):
    fleet, _p = build(tmp_path, sales(policy()), sales(),
                      overrides={"refresh_windows": [
                          {"table": "Sales", "kind": "absent",
                           "reason": "small table, full reload is fine"}]})
    assert _refresh(fleet) == []


def test_malformed_policy_is_realigned(tmp_path):
    fleet, path = build(tmp_path, sales(policy()),
                        sales(policy(source_view="stale_view")))
    found = [f for f in _refresh(fleet) if f.kind == "refresh_policy.invalid"]
    assert len(found) == 1 and "sourceExpression" in found[0].detail
    assert remediate(fleet, found).count("applied") == 1
    assert "stale_view" not in path.read_text(encoding="utf-8")
    assert not [f for f in _refresh(fleet)
                if f.kind == "refresh_policy.invalid"]


def test_client_only_and_ineligible_are_report_only(tmp_path):
    fleet, _p = build(tmp_path, sales(), sales(policy()))
    assert [f.kind for f in _refresh(fleet)] == ["refresh_policy.client_only"]
    fleet, path = build(tmp_path / "b", sales(policy()),
                        sales(extra_partition=True))
    found = _refresh(fleet)
    assert found[0].kind == "refresh_policy.missing"
    assert "2 partitions" in found[0].detail
    before = path.read_bytes()
    res = remediate(fleet, found)
    assert res.count("skipped") == 1 and path.read_bytes() == before

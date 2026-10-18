"""Per-model overrides: validated loudly, never remediated, always ACKed."""

import json

import pytest
from click.testing import CliRunner
from conftest import detect_all, edit_fleet_yml, retire_everything_from_template

from tmdl_drift_doctor import DriftDoctorError
from tmdl_drift_doctor.baseline import capture
from tmdl_drift_doctor.cli import main
from tmdl_drift_doctor.fleet import load_fleet
from tmdl_drift_doctor.remediate import remediate
from tmdl_drift_doctor.tmdl import parse_model

ALPHA = "franchises/alpha/GymChain.SemanticModel"


def _set_overrides(fleet_dir, entries, model="alpha", path=ALPHA):
    def mutate(d):
        d["models"][model] = {"path": path, "overrides": entries}
    edit_fleet_yml(fleet_dir, mutate)


ENTRY = {"object": "Visits[Total Visits]", "kind": "expression",
         "reason": "alpha excludes zero-minute visits by contract",
         "owner": "data-team", "added": "2026-09-30"}


def _cli(fleet_dir, *args):
    return CliRunner().invoke(main, [args[0], "--fleet",
                                     str(fleet_dir / "fleet.yml"), *args[1:]])


@pytest.mark.parametrize("entry, message", [
    ({**ENTRY, "reason": "  "}, "`reason` must be a non-empty string"),
    ({**ENTRY, "kind": "ignore"}, "`kind` must be one of"),
    ({**ENTRY, "colour": "red"}, "unknown key(s) colour"),
    ({k: v for k, v in ENTRY.items() if k != "object"}, "`object`"),
])
def test_schema_errors_fail_loading(fleet_dir, entry, message):
    _set_overrides(fleet_dir, [entry])
    with pytest.raises(DriftDoctorError, match=message.replace("(", r"\(")
                       .replace(")", r"\)")):
        load_fleet(fleet_dir / "fleet.yml")


def test_unknown_model_key_fails_loading(fleet_dir):
    edit_fleet_yml(fleet_dir, lambda d: d["models"].__setitem__(
        "alpha", {"path": ALPHA, "gate": "advisory"}))
    with pytest.raises(DriftDoctorError, match="unknown key"):
        load_fleet(fleet_dir / "fleet.yml")


def test_unresolvable_object_fails_detection(fleet_dir):
    _set_overrides(fleet_dir, [{**ENTRY, "object": "Visits[Totl Visits]"}])
    fleet = load_fleet(fleet_dir / "fleet.yml")
    capture(fleet)
    with pytest.raises(DriftDoctorError, match="Totl Visits"):
        detect_all(fleet)
    r = _cli(fleet_dir, "detect")
    assert r.exit_code == 2 and "do not resolve" in r.output


def test_overridden_finding_is_acked_not_remediated(fleet_dir):
    _set_overrides(fleet_dir, [ENTRY])
    fleet = load_fleet(fleet_dir / "fleet.yml")
    capture(fleet)
    f = next(f for f in detect_all(fleet) if f.ref == "Visits[Total Visits]")
    assert f.overridden
    result = remediate(fleet, detect_all(fleet))
    o = next(o for o in result.outcomes if o.finding.ref == "Visits[Total Visits]"
             and o.finding.model == "alpha")
    assert o.status == "overridden" and "zero-minute" in o.reason
    alpha = parse_model(fleet.model_definition("alpha"))
    assert "FILTER" in alpha.measure("Visits", "Total Visits").expression

    r = _cli(fleet_dir, "detect")
    assert "ACK measure.expression_drift" in r.output
    assert "owner data-team, added 2026-09-30" in r.output
    assert "overridden=1" in r.output
    report = json.loads(_cli(fleet_dir, "detect", "--json").output)
    assert report["models"]["alpha"]["overridden"][0]["override"]["reason"]
    assert report["models"]["alpha"]["counts"]["overridden"] == 1
    assert all(x["ref"] != "Visits[Total Visits]"
               for x in report["models"]["alpha"]["findings"])


def test_overridden_findings_never_fail_detect(fleet_dir):
    _set_overrides(fleet_dir, [ENTRY])
    fleet = load_fleet(fleet_dir / "fleet.yml")
    capture(fleet)
    remediate(fleet, detect_all(fleet))       # everything else fixed
    r = _cli(fleet_dir, "detect")
    assert r.exit_code == 0, r.output


def test_absent_table_covers_its_relationships(fleet_dir):
    _set_overrides(fleet_dir, [{"object": "Classes", "kind": "absent",
                                "reason": "bravo runs no classes"}],
                   model="bravo", path="franchises/bravo/GymChain.SemanticModel")
    fleet = load_fleet(fleet_dir / "fleet.yml")
    capture(fleet)
    f = next(f for f in detect_all(fleet, model_filter="bravo")
             if f.kind == "table.missing")
    assert f.overridden and f.override.reason == "bravo runs no classes"


def test_retain_override_protects_a_retired_object(fleet_dir):
    fleet = load_fleet(fleet_dir / "fleet.yml")
    capture(fleet)
    retire_everything_from_template(fleet)
    capture(fleet)
    _set_overrides(fleet_dir, [{"object": "Visits[Peak Hour Visits]",
                                "kind": "retain",
                                "reason": "alpha keeps its evening KPI"}])
    fleet = load_fleet(fleet_dir / "fleet.yml")
    remediate(fleet, detect_all(fleet), sync=True)
    alpha = parse_model(fleet.model_definition("alpha"))
    assert alpha.measure("Visits", "Peak Hour Visits") is not None
    bravo = parse_model(fleet.model_definition("bravo"))
    assert bravo.measure("Visits", "Peak Hour Visits") is None


def test_retain_must_name_a_retired_object(fleet_dir):
    _set_overrides(fleet_dir, [{"object": "Visits[Total Visits]",
                                "kind": "retain", "reason": "x"}])
    fleet = load_fleet(fleet_dir / "fleet.yml")
    capture(fleet)
    with pytest.raises(DriftDoctorError, match="retain"):
        detect_all(fleet)

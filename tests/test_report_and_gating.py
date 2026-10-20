"""detect --json schema v2, the actionable predicate, and per-model gating."""

import json

from click.testing import CliRunner
from conftest import edit_fleet_yml

from tmdl_drift_doctor.cli import main


def _cli(fleet_dir, *args):
    return CliRunner().invoke(main, [args[0], "--fleet",
                                     str(fleet_dir / "fleet.yml"), *args[1:]])


def test_json_v2_shape(fleet_dir):
    _cli(fleet_dir, "capture")
    r = _cli(fleet_dir, "detect", "--json")
    assert r.exit_code == 1
    report = json.loads(r.output)
    assert report["schema_version"] == 2
    assert report["generated_at"]
    assert "template_tree" in report          # null outside a git work tree
    assert report["scope"] == {"fleet": True, "models": None,
                               "alternate_model": None,
                               "targets": ["alpha", "bravo"],
                               "kind_filter": None, "overrides": None}
    assert report["errors"] == []
    alpha = report["models"]["alpha"]
    assert alpha["gating"] == "enforced"
    assert {"findings", "overridden", "counts"} <= set(alpha)
    f = next(x for x in alpha["findings"] if x["kind"] == "measure.missing")
    assert f["actionable"] is True
    extra = next(x for x in alpha["findings"] if x["kind"] == "extra.measure")
    assert extra["actionable"] is False
    assert alpha["counts"]["actionable"] == sum(
        1 for x in alpha["findings"] if x["actionable"])


def test_json_scope_records_filters(fleet_dir):
    _cli(fleet_dir, "capture")
    report = json.loads(_cli(fleet_dir, "detect", "--json", "--model", "bravo",
                             "--kind", "relationship.").output)
    assert report["scope"] == {"fleet": False, "models": ["bravo"],
                               "alternate_model": None,
                               "targets": ["bravo"],
                               "kind_filter": "relationship.",
                               "overrides": None}


def test_not_allowlisted_findings_do_not_fail_detect(fleet_dir):
    def keep_only_advisories(d):
        d["allowlist"]["kinds"] = []
    edit_fleet_yml(fleet_dir, keep_only_advisories)
    _cli(fleet_dir, "capture")
    r = _cli(fleet_dir, "detect")
    assert r.exit_code == 0, r.output
    assert "[not allowlisted]" in r.output


def test_advisory_gating_never_sets_the_exit_code(fleet_dir):
    def advisory(d):
        for name in ("alpha", "bravo"):
            d["models"][name] = {"path": d["models"][name], "gating": "advisory"}
    edit_fleet_yml(fleet_dir, advisory)
    _cli(fleet_dir, "capture")
    r = _cli(fleet_dir, "detect")
    assert r.exit_code == 0, r.output
    assert "(gating: advisory)" in r.output
    assert "measure.missing" in r.output       # still printed
    report = json.loads(_cli(fleet_dir, "detect", "--json").output)
    assert report["models"]["alpha"]["gating"] == "advisory"


def test_unparseable_model_is_an_error_not_a_crash(fleet_dir):
    _cli(fleet_dir, "capture")
    bad = (fleet_dir / "franchises" / "bravo" / "GymChain.SemanticModel"
           / "definition" / "tables" / "Broken.tmdl")
    bad.write_text("column without a table\n", encoding="utf-8")
    r = _cli(fleet_dir, "detect", "--json")
    assert r.exit_code == 3
    report = json.loads(r.output)
    assert report["errors"][0]["model"] == "bravo"
    assert "alpha" in report["models"]

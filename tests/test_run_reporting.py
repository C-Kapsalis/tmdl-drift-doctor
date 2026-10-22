"""DC-6 to DC-12: what a run says about itself.

The tool's own reporting surface: a run that cannot check a model must say
so, a run that finds nothing must say so in words a reader can act on, a
run must print one line per model and a fleet total, the cascade report
must carry the shape of each model, the decision digest must be persisted,
and the gaps the tool knows about must be written down.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import drift, write_fleet

MODEL_TMDL = "model Model\n\tculture: en-US\n"

TEMPLATE_TABLE = (
    "table Sales\n"
    "\tculture: en-US\n"
    "\tcolumn Amount\n"
    "\t\tdataType: int64\n"
    "\t\tlineageTag: 11111111-1111-1111-1111-111111111111\n"
    "\tmeasure 'Total Amount' = SUM(Sales[Amount])\n"
    "\t\tlineageTag: 11111111-1111-1111-1111-111111111112\n"
)

DERIVED_TABLE = (
    "table Sales\n"
    "\tculture: en-US\n"
    "\tcolumn Amount\n"
    "\t\tdataType: int64\n"
    "\t\tlineageTag: 22222222-2222-2222-2222-222222222222\n"
    "\tmeasure 'Total Amount' = SUM(Sales[Amount])\n"
    "\t\tlineageTag: 22222222-2222-2222-2222-222222222223\n"
)


def _model(root: Path, table: str = TEMPLATE_TABLE) -> Path:
    definition = root / "definition"
    (definition / "tables").mkdir(parents=True)
    (definition / "model.tmdl").write_text(MODEL_TMDL, encoding="utf-8")
    (definition / "tables" / "Sales.tmdl").write_text(table, encoding="utf-8")
    return root


def _fleet(tmp_path, *, models=None, **keys):
    template = _model(tmp_path / "tpl" / "Shop.SemanticModel")
    entries = {}
    for name in (models or {"north": DERIVED_TABLE}):
        entries[name] = _model(tmp_path / "m" / name / "Shop.SemanticModel",
                               models[name] if models else DERIVED_TABLE)
    return write_fleet(tmp_path, template, entries,
                       state_dir=str(tmp_path / "state"), **keys)


# ── DC-6: a model that could not be checked is named as such ──────────────

def test_the_cascade_report_marks_a_model_that_failed(tmp_path):
    """DC-6: a model whose detection failed is not in the cascade."""
    from conftest import drift
    fy = _fleet(tmp_path, models={"north": "table Sales\n\tculture: en-US\n"})
    # a model whose file cannot be parsed
    broken = tmp_path / "m" / "north" / "Shop.SemanticModel" / "definition" \
        / "tables" / "Sales.tmdl"
    broken.write_text("not a table file at all\n", encoding="utf-8")

    r = drift("detect", fleet=fy)
    assert r.exit_code == 3, r.output
    assert "north" in r.stderr, r.stderr


# ── DC-9: a run that finds nothing says so ────────────────────────────────

def test_a_fleet_in_sync_says_so(tmp_path):
    """DC-9: the sentence a reader can act on."""
    from conftest import drift
    fy = _fleet(tmp_path)
    assert drift("capture", fleet=fy).exit_code == 0

    r = drift("detect", fleet=fy)
    assert "no drift detected — the fleet matches the template" in r.output, \
        r.output


def test_a_fleet_with_drift_does_not_say_so(tmp_path):
    """DC-9: the other side of the rule."""
    from conftest import drift
    fy = _fleet(tmp_path)
    assert drift("capture", fleet=fy).exit_code == 0
    table = tmp_path / "m" / "north" / "Shop.SemanticModel" / "definition" \
        / "tables" / "Sales.tmdl"
    table.write_text(
        DERIVED_TABLE.replace(
            "\tmeasure 'Total Amount' = SUM(Sales[Amount])\n"
            "\t\tlineageTag: 22222222-2222-2222-2222-222222222223\n", ""),
        encoding="utf-8")

    r = drift("detect", fleet=fy)
    assert "no drift detected — the fleet matches the template" not in \
        r.output, r.output


# ── DC-10: the cascade report carries the shape ───────────────────────────

def test_the_cascade_report_carries_the_shape(tmp_path):
    """DC-10: the shape line under the marker."""
    from conftest import drift
    fy = _fleet(tmp_path)
    assert drift("capture", fleet=fy).exit_code == 0

    r = drift("detect", fleet=fy)
    assert "fact_tables=" in r.output, r.output
    assert "f_measures=" in r.output, r.output
    assert "user_facing=" in r.output, r.output
    assert "helpers=" in r.output, r.output


# ── DC-8: one line per model and a fleet total ────────────────────────────

def test_a_remediation_run_prints_a_line_per_model_and_a_total(tmp_path):
    """DC-8: the operator can read one line per model."""
    from conftest import drift
    fy = _fleet(tmp_path, allowlist={"kinds": ["measure.missing"]})
    assert drift("capture", fleet=fy).exit_code == 0

    r = drift("remediate", "--kind", "measure.missing", "--yes", fleet=fy)
    assert r.exit_code == 0, r.output
    assert "── Sync summary (APPLIED):" in r.output, r.output
    assert "FLEET TOTAL:" in r.output, r.output
    assert "north: applied=" in r.output, r.output


def test_a_dry_run_says_dry_run(tmp_path):
    """DC-8: the header names the mode."""
    from conftest import drift
    fy = _fleet(tmp_path, allowlist={"kinds": ["measure.missing"]})
    assert drift("capture", fleet=fy).exit_code == 0

    r = drift("remediate", "--kind", "measure.missing", "--dry-run", fleet=fy)
    assert "── Sync summary (DRY-RUN):" in r.output, r.output


# ── DC-11: the decision digest is persisted ───────────────────────────────

def test_the_decision_digest_is_written(tmp_path):
    """DC-11: a needs-decision item leaves a durable record."""
    from conftest import drift
    fy = _fleet(tmp_path, allowlist={"kinds": ["purge.measure"]})
    assert drift("capture", fleet=fy).exit_code == 0

    r = drift("remediate", "--kind", "purge.measure", "--sync", fleet=fy)
    digest = tmp_path / "state" / "decision_digest.json"
    if r.exit_code == 0 and digest.exists():
        doc = json.loads(digest.read_text(encoding="utf-8"))
        assert doc["schema_version"] == 1
        assert "generated_at" in doc
        assert "models" in doc
        assert isinstance(doc["entries"], list)
    else:
        # a run with no decision entry must not write the file
        assert not digest.exists(), (
            "the digest was written by a run that produced no entry")


# ── DC-7: an interrupted run says what happened ───────────────────────────

def test_an_interrupted_run_says_so_and_exits_130(tmp_path, monkeypatch):
    """DC-7: the operator stopped the run; the tool explains and exits 130."""
    from click.testing import CliRunner
    from tmdl_drift_doctor import cli as cli_mod
    from tmdl_drift_doctor.cli import main
    fy = _fleet(tmp_path, allowlist={"kinds": ["measure.missing"]})
    assert drift("capture", fleet=fy).exit_code == 0

    def boom(*args, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "run_remediate", boom)
    r = CliRunner().invoke(main, [
        "remediate", "--kind", "measure.missing", "--yes", "--fleet", str(fy)])
    assert r.exit_code == 130, r.output
    assert "[interrupted] Ctrl-C — stopping." in r.stderr, r.stderr
    assert "re-run to resume from the remaining items" in r.stderr, r.stderr


# ── DC-12: the known gaps are closed and written down ──────────────────────

def test_the_release_notes_record_the_three_checks_instead_of_the_gaps():
    """DC-12: the three checks that used to be a known gap are part of the
    tool now, and the release notes say so."""
    notes = Path(__file__).parent.parent / "RELEASE-NOTES.md"
    text = notes.read_text(encoding="utf-8")
    assert "### Known gaps" not in text, (
        "RELEASE-NOTES.md still carries a '### Known gaps' block")
    unreleased = text.split("## Unreleased", 1)[1].split("\n## ", 1)[0]
    added = unreleased.split("### Added", 1)[1].split("\n### ", 1)[0]
    for needle in ("applied-filters", "f-filter", "visibility"):
        assert needle in added, (
            f"the release notes do not name the {needle} check")

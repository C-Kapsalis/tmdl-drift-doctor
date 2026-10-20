"""
Layer 1: a model's `Category_*` annotations must equal the ORG-SPECIFIC categories its
`z-Map Dimension-Measure Groupings` mapping declares (A-category-annotation-sync).

Ported from the original layer-1 gate. ORG-SPECIFIC = the model's mapping minus the template's
mapping. Two hard directions:

  1. every `Category_*` annotation must name a category that model's own mapping contains
     (kind `category_annotation.orphaned`, no fixer: removing an annotation removes a live
     category key, which is a human call);
  2. every org-specific category must be annotated (kind `category_annotation.undeclared`,
     additive fixer through `remediate`).

The accepted-exceptions baseline is two-directional and shrink-only
(`category_annotation_baseline.json`, model key `<parent folder>/<model folder>`); in the
stand-in it accepts the two orphaned annotations `Competition` and `Email Marketing` on
`spinout_02_db/insights.SemanticModel`, as the original baseline does.

Runs on the roastery stand-in fleet (tests/fixtures/b1-l1a/fleet). The original read the real
client models and its kernel helper; detection is now `drift-doctor detect`, repair is
`drift-doctor remediate --kind category_annotation.undeclared`.
"""
from __future__ import annotations

import pytest

from conftest import drift
from port_helpers_b1_l1a import (
    ZMAP_FILE,
    copy_fleet,
    definition,
    detect,
    findings,
    model_key,
    read_zmap_rows,
    write_zmap_rows,
    zmap_models,
)

MODELS = zmap_models()
IDS = [model_key(rel) for _, rel in MODELS]
NAMES = [n for n, _ in MODELS]


@pytest.fixture(scope="module")
def fleet_report(tmp_path_factory):
    root = copy_fleet(tmp_path_factory.mktemp("annotations"))
    return detect(root)


@pytest.mark.parametrize("model", NAMES, ids=IDS)
def test_every_annotation_names_a_real_category(fleet_report, model):
    """An annotation naming nothing is a category that maps to nothing.

    Ratcheted, not absolute: `spinout_02_db/insights` carries two such annotations that
    predate the gate; they are recorded in the baseline rather than fixed blind. A NEW
    orphan fails.
    """
    _, rep = fleet_report
    orphaned = sorted(f["ref"] for f in findings(rep, model, "category_annotation.orphaned"))
    assert not orphaned, (
        f"{model}: {orphaned} is annotated but absent from the x/y mapping, so consumers see a "
        f"category that maps to nothing. Retire or repoint the annotation."
    )


@pytest.mark.parametrize("model", NAMES, ids=IDS)
def test_no_new_undeclared_categories(fleet_report, model):
    """Every org-specific category (mapping minus the template's mapping) carries an annotation."""
    _, rep = fleet_report
    missing = sorted(f["ref"] for f in findings(rep, model, "category_annotation.undeclared"))
    assert not missing, (
        f"{model}: the x/y mapping carries {missing}, which the template's mapping does not and no "
        f"`Category_*` annotation declares. Repair with "
        f"`drift-doctor remediate --kind category_annotation.undeclared`."
    )


# ── tamper tests: mutate the INPUT, never the predicate (original docstring table) ──

def _model_tmdl(root, name):
    return definition(root, name) / "model.tmdl"


def test_added_unknown_annotation_fails(tmp_path):
    """mutation: add `annotation Category_Not_A_Thing` to a client -> 1 failure."""
    root = copy_fleet(tmp_path)
    f = _model_tmdl(root, "spinout-10")
    f.write_text(f.read_text(encoding="utf-8") + '\nannotation Category_Not_A_Thing = "x"\n',
                 encoding="utf-8")
    r, rep = detect(root)
    refs = [x["ref"] for x in findings(rep, "spinout-10", "category_annotation.orphaned")]
    assert refs and any("Not A Thing" in ref for ref in refs), r.output
    others = [m for m in NAMES if m != "spinout-10"
              and findings(rep, m, "category_annotation.orphaned")]
    assert not others


def test_baselined_orphan_is_scoped_to_its_model(tmp_path):
    """The baseline accepts `Competition` on spinout_02_db/insights only: the same
    annotation on another model fails."""
    root = copy_fleet(tmp_path)
    f = _model_tmdl(root, "spinout-10")
    f.write_text(f.read_text(encoding="utf-8") + '\nannotation Category_Competition = "x"\n',
                 encoding="utf-8")
    _, rep = detect(root)
    assert [x["ref"] for x in findings(rep, "spinout-10", "category_annotation.orphaned")]
    assert not findings(rep, "spinout-02", "category_annotation.orphaned")


def test_deleted_annotation_is_undeclared(tmp_path):
    """mutation: delete the client-only category's annotation -> 1 failure (exit 1)."""
    root = copy_fleet(tmp_path)
    f = _model_tmdl(root, "spinout-10")
    text = f.read_text(encoding="utf-8")
    line = 'annotation Category_Subscriptions = "Coffee subscription boxes"\n'
    assert line in text
    f.write_text(text.replace(line, ""), encoding="utf-8")
    r, rep = detect(root)
    refs = [x["ref"] for x in findings(rep, "spinout-10", "category_annotation.undeclared")]
    assert refs and any("Subscriptions" in ref for ref in refs)
    assert r.exit_code == 1, r.output


def test_repair_declares_the_category_and_recheck_is_clean(tmp_path):
    """With repair enabled the fixer runs and the re-check must be empty. Without a git
    history the restored value is the placeholder `"<Category> metrics"`."""
    root = copy_fleet(tmp_path)
    f = _model_tmdl(root, "spinout-10")
    f.write_text(f.read_text(encoding="utf-8").replace(
        'annotation Category_Subscriptions = "Coffee subscription boxes"\n', ""), encoding="utf-8")
    detect(root)
    r = drift("remediate", "--kind", "category_annotation.undeclared", "--model", "spinout-10",
              "--yes", fleet=root / "fleet.yml")
    assert r.exit_code == 0, r.output
    assert 'annotation Category_Subscriptions = "Subscriptions metrics"' in f.read_text(encoding="utf-8")
    _, rep = detect(root)
    assert not findings(rep, "spinout-10", "category_annotation.undeclared")
    # the property key is not a category and was never touched
    assert "annotation Category_Subscriptions_RequiredServices" in f.read_text(encoding="utf-8")


def test_fixer_placeholder_manual_and_idempotent(tmp_path):
    """Spec case 4 (fixer script, no original test): a new `Loyalty Points` category gets a
    placeholder annotation; `Paid & Organic` cannot round-trip to a key and is reported
    manual (nothing written for it); a second fix run writes nothing."""
    root = copy_fleet(tmp_path)
    table = definition(root, "spinout-10") / "tables" / ZMAP_FILE
    rows = read_zmap_rows(table) + [["Loyalty Points", "Shop", "m_points"],
                                    ["Paid & Organic", "Web", "m_blended"]]
    write_zmap_rows(table, rows)
    r, rep = detect(root)
    refs = sorted(x["ref"] for x in findings(rep, "spinout-10", "category_annotation.undeclared"))
    assert any("Loyalty Points" in x for x in refs) and any("Paid & Organic" in x for x in refs)
    assert r.exit_code == 1
    f = _model_tmdl(root, "spinout-10")
    r1 = drift("remediate", "--kind", "category_annotation.undeclared", "--model", "spinout-10",
               "--yes", fleet=root / "fleet.yml")
    text = f.read_text(encoding="utf-8")
    assert 'annotation Category_Loyalty_Points = "Loyalty Points metrics"' in text
    assert "Paid" not in text.split("annotation Category_Loyalty_Points")[1]
    assert "manual" in r1.output.lower()
    _, rep = detect(root)
    left = [x["ref"] for x in findings(rep, "spinout-10", "category_annotation.undeclared")]
    assert left and all("Paid & Organic" in x for x in left)   # stays red
    drift("remediate", "--kind", "category_annotation.undeclared", "--model", "spinout-10",
          "--yes", fleet=root / "fleet.yml")
    assert f.read_text(encoding="utf-8") == text

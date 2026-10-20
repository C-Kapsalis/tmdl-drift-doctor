"""The blast radius of a change: ``drift-doctor diff``.

Two detection reports in, one Markdown file out. The report answers the two
questions a reviewer asks before a template change lands: which objects
does it add to the fleet's drift, and which does it resolve.
"""

from __future__ import annotations

import json

from conftest import write_fleet

from tmdl_drift_doctor.blast import DiffError, render, write_diff


def _report(models: dict, *, fleet: bool = True, kind_filter=None,
            models_scope=None, tree="cafe1234") -> dict:
    scope = {
        "fleet": fleet,
        "models": models_scope,
        "alternate_model": None,
        "targets": sorted(models),
        "kind_filter": kind_filter,
        "overrides": None,
    }
    return {
        "schema_version": 2,
        "generated_at": "2026-08-07T00:00:00+00:00",
        "template_tree": tree,
        "scope": scope,
        "errors": [],
        "models": {
            name: {
                "gating": "enforced",
                "findings": [
                    {"kind": kind, "ref": ref, "detail": "", "remediable": True,
                     "actionable": True, "advisory": False}
                    for kind, ref in items
                ],
                "overridden": [],
                "counts": {"actionable": len(items)},
            }
            for name, items in models.items()
        },
    }


def _write(path, doc):
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_no_delta_says_so_and_cites_both_reports(tmp_path):
    doc = _report({"north": [("measure.missing", "Sales[Total]")]})
    body = render(doc, doc)
    assert "adds or resolves no actionable drift" in body
    assert "<!-- drift-doctor:blast-radius -->" in body
    assert "cafe1234" in body


def test_one_new_measure_in_two_models(tmp_path):
    base = _report({"north": [], "south": [], "east": []})
    head = _report({
        "north": [("measure.missing", "Sales[Gift Cards]")],
        "south": [("measure.missing", "Sales[Gift Cards]")],
        "east": [],
    })
    body = render(base, head)
    assert "**1 object(s) in 2 model(s).**" in body
    assert ("- **measure** `Sales[Gift Cards]` (measure.missing) - new drift "
            "in 2 model(s)") in body
    assert "| north | 1 | 0 | +1 | measure +1 |" in body
    assert "| south | 1 | 0 | +1 | measure +1 |" in body
    assert "**+2 / -0** actionable item(s) across 2 of 3 model(s)." in body


def test_a_resolution_is_reported_as_resolved(tmp_path):
    base = _report({"east": [("column.missing", "Beans[origin]")]})
    head = _report({"east": []})
    body = render(base, head)
    assert "resolves drift in 1 model(s)" in body
    assert "| east | 0 | 1 | -1 | column -1 |" in body
    assert "**+0 / -1**" in body


def test_a_mixed_model_breaks_down_by_kind(tmp_path):
    base = _report({"north": [("column.missing", "Beans[origin]")]})
    head = _report({
        "north": [("measure.missing", "Sales[A]"),
                  ("measure.missing", "Sales[B]")],
    })
    body = render(base, head)
    assert "| north | 2 | 1 | +1 | measure +2, column -1 |" in body


def test_more_than_fifteen_objects_are_capped(tmp_path):
    base = _report({"north": []})
    head = _report({
        "north": [("measure.missing", f"Sales[M{i}]") for i in range(20)],
    })
    body = render(base, head)
    assert "**20 object(s) in 1 model(s).**" in body
    assert "- ...and 5 more object(s)" in body


def test_a_scoped_head_warns_about_phantom_deltas(tmp_path):
    base = _report({"north": [], "south": []})
    head = _report({"north": []}, fleet=False, models_scope=["north"])
    body = render(base, head)
    assert "the head report is model-scoped" in body
    assert "phantom deltas" in body


def test_a_kind_filtered_base_warns(tmp_path):
    base = _report({"north": []}, kind_filter="measure.")
    head = _report({"north": []})
    body = render(base, head)
    assert "the base report is kind-filtered" in body


def test_a_report_without_a_scope_block_warns(tmp_path):
    doc = _report({"north": []})
    del doc["scope"]
    body = render(doc, doc)
    assert "the base report carries no scope block" in body
    assert "the head report carries no scope block" in body


def test_a_model_only_in_head_counts_as_a_change(tmp_path):
    base = _report({"north": [], "south": []})
    head = _report({
        "north": [], "south": [],
        "west": [("measure.missing", "Sales[X]"),
                 ("measure.missing", "Sales[Y]"),
                 ("measure.missing", "Sales[Z]")],
    })
    body = render(base, head)
    assert "| west | 3 | 0 | +3 | measure +3 |" in body
    assert "**+3 / -0** actionable item(s) across 1 of 3 model(s)." in body


def test_the_footer_cites_both_template_trees(tmp_path):
    base = _report({"north": []}, tree="aaaaaaaaa")
    head = _report({"north": []}, tree="bbbbbbbbb")
    body = render(base, head)
    assert "`aaaaaaaaa`" in body and "`bbbbbbbbb`" in body


def test_write_diff_writes_the_file_and_summarizes(tmp_path):
    base = _write(tmp_path / "base.json",
                  _report({"north": [("measure.missing", "Sales[X]")]}))
    head = _write(tmp_path / "head.json", _report({"north": []}))
    out = tmp_path / "out.md"
    res = write_diff(base, head, out)
    assert out.exists()
    assert res["path"] == str(out)
    assert res["models"] == 1
    assert res["objects"] == 1
    assert res["stale"] is False
    assert out.read_text(encoding="utf-8").endswith("\n")


def test_write_diff_refuses_an_unreadable_report(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    head = _write(tmp_path / "head.json", _report({"north": []}))
    try:
        write_diff(bad, head, tmp_path / "out.md")
    except DiffError as exc:
        assert "could not be read" in str(exc)
    else:
        raise AssertionError("an unreadable report must be refused")


def test_write_diff_refuses_a_missing_report(tmp_path):
    head = _write(tmp_path / "head.json", _report({"north": []}))
    try:
        write_diff(tmp_path / "nope.json", head, tmp_path / "out.md")
    except DiffError as exc:
        assert "no report at" in str(exc)
    else:
        raise AssertionError("a missing report must be refused")


def test_the_diff_command_writes_the_file_and_reports(tmp_path):
    from conftest import drift
    base = _write(tmp_path / "base.json",
                  _report({"north": [("measure.missing", "Sales[X]")]}))
    head = _write(tmp_path / "head.json", _report({"north": []}))
    out = tmp_path / "out.md"
    r = drift("diff", "--base", str(base), "--head", str(head),
              "--out", str(out))
    assert r.exit_code == 0, r.output
    assert "blast radius written" in r.output
    assert out.exists()


def test_the_diff_command_refuses_an_unreadable_report(tmp_path):
    from conftest import drift
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    head = _write(tmp_path / "head.json", _report({"north": []}))
    r = drift("diff", "--base", str(bad), "--head", str(head),
              "--out", str(tmp_path / "o.md"))
    assert r.exit_code == 2

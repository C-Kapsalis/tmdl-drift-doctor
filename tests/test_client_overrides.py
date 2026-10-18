"""Override declarations: the loader is a GATE component (ported suite).

A malformed override must fail LOUDLY: a typo silently disabling an override
would let a sync clobber intentional derived-model work, or keep the gate
failing on a declared divergence. These tests pin the loud-failure contract:

  - missing/empty `reason`               -> DriftDoctorError
  - unknown `kind`                       -> DriftDoctorError
  - bare name with kind: expression      -> DriftDoctorError (needs Table[Measure])
  - name that does not resolve in the template -> DriftDoctorError
  - well-formed declarations             -> parsed + per-model resolved shape

Ported from the original suite. The original loader read a separate
overrides YAML (``clients.<id>.measures|columns|source_views``); drift-doctor
keeps declarations in ``fleet.yml`` under ``models.<name>.overrides`` with
typed sections (D-overrides-typed-sections). Each test writes a fleet.yml in
tmp_path over the fictional roastery stand-in template
(``tests/fixtures/b5-overrides/roastery``), loads it with ``load_fleet``,
captures the baseline and resolves with ``resolve_overrides``; the resolved
per-model shape is read with ``overrides.resolve_for_model`` and the
rename-guard name set with ``overrides.override_measure_names``.
"""

from __future__ import annotations

import datetime
import shutil

import pytest
from conftest import FIXTURES, write_fleet

from tmdl_drift_doctor import DriftDoctorError

TEMPLATE_SRC = FIXTURES / "b5-overrides" / "roastery" / "Roastery.SemanticModel"

# Names the original drew from the template index (first two measures, first
# table with a column); fixed here to the stand-in's objects.
FULL = "Sales[Revenue]"
BARE = "Cups Sold"
TABLE, COLUMN = "Sales", "OrderId"


@pytest.fixture
def api():
    """The tool surface these tests call (imported lazily so a missing
    function is a per-test error, not a collection error)."""
    from tmdl_drift_doctor import overrides as ov
    from tmdl_drift_doctor.baseline import capture, load_baseline
    from tmdl_drift_doctor.fleet import load_fleet
    from tmdl_drift_doctor.ledger import Ledger

    class _Api:
        resolve_for_model = staticmethod(ov.resolve_for_model)
        override_measure_names = staticmethod(ov.override_measure_names)

        @staticmethod
        def load(fleet_yml):
            fleet = load_fleet(fleet_yml)
            capture(fleet)
            ov.resolve_overrides(fleet, load_baseline(fleet),
                                 Ledger(fleet.ledger_path))
            return fleet
    return _Api


def _fleet(tmp_path, overrides_05: dict):
    """fleet.yml: template + spinout-05 (declares `overrides_05`) +
    spinout-04 (declares nothing)."""
    template = tmp_path / "template" / "Roastery.SemanticModel"
    shutil.copytree(TEMPLATE_SRC, template)
    models = {}
    for name in ("spinout-05", "spinout-04"):
        dst = tmp_path / name / "Roastery.SemanticModel"
        shutil.copytree(TEMPLATE_SRC, dst)
        models[name] = dst
    return write_fleet(
        tmp_path, template,
        {"spinout-05": {"path": models["spinout-05"],
                        "overrides": overrides_05},
         "spinout-04": models["spinout-04"]})


def _good():
    return {"measures": [
        {"object": FULL, "kind": "expression",
         "reason": "client keeps a custom body", "owner": "owner-a",
         "added": datetime.date(2026, 7, 3)},
        {"object": BARE, "kind": "absent",
         "reason": "client intentionally lacks it"},
    ]}


def test_good_file_loads_and_resolves(tmp_path, api):
    fleet = api.load(_fleet(tmp_path, _good()))
    resolved = api.resolve_for_model(fleet, "spinout-05")
    assert FULL in resolved["expression"]
    assert resolved["expression"][FULL]["reason"] == "client keeps a custom body"
    assert BARE in resolved["absent"]
    # other models resolve to empty (no accidental cross-model bleed)
    empty = api.resolve_for_model(fleet, "spinout-04")
    assert not empty["expression"] and not empty["absent"]
    # rename-drift guard sees BOTH kinds as bare names
    names = api.override_measure_names(resolved)
    assert BARE in names and FULL[FULL.rindex("[") + 1:-1] in names


def test_empty_or_missing_reason_fails_loudly(tmp_path, api):
    for i, bad_reason in enumerate(({"reason": ""}, {"reason": "   "}, {})):
        root = tmp_path / f"case{i}"
        root.mkdir()
        entry = {"object": FULL, "kind": "expression", **bad_reason}
        fy = _fleet(root, {"measures": [entry]})
        with pytest.raises(DriftDoctorError, match="reason"):
            api.load(fy)


def test_unknown_kind_fails_loudly(tmp_path, api):
    fy = _fleet(tmp_path, {"measures": [
        {"object": FULL, "kind": "keep-forever", "reason": "typo'd kind"}]})
    with pytest.raises(DriftDoctorError, match="kind"):
        api.load(fy)


def test_expression_kind_requires_full_name(tmp_path, api):
    fy = _fleet(tmp_path, {"measures": [
        {"object": BARE, "kind": "expression",
         "reason": "bare name is ambiguous for an expression override"}]})
    with pytest.raises(DriftDoctorError, match="Table\\[Measure\\]"):
        api.load(fy)


def test_unresolvable_name_fails_loudly(tmp_path, api):
    fy = _fleet(tmp_path, {"measures": [
        {"object": "#Report Measures[No Such Measure Anywhere]",
         "kind": "expression",
         "reason": "name typo must not silently disable the override"}]})
    with pytest.raises(DriftDoctorError,
                       match="does not resolve in the template"):
        api.load(fy)


def test_wrong_version_fails_loudly(tmp_path):
    from tmdl_drift_doctor.fleet import load_fleet
    fy = _fleet(tmp_path, {})
    fy.write_text(fy.read_text(encoding="utf-8").replace(
        "version: 1", "version: 2", 1), encoding="utf-8")
    with pytest.raises(DriftDoctorError, match="version"):
        load_fleet(fy)


def test_explicit_missing_path_fails_default_missing_is_empty(tmp_path):
    # Partial port (I0428): overrides live inside fleet.yml, so only the
    # explicit-missing-path half has a counterpart. The original's
    # default-file-absent half (load_overrides(None)) is dropped.
    from tmdl_drift_doctor.fleet import load_fleet
    with pytest.raises(DriftDoctorError, match="not found"):
        load_fleet(tmp_path / "nope.yml")


# -- `columns:` + `kind: absent` ---------------------------------------------
# A column the derived model's source cannot supply must be DECLARABLE, or the
# cascade re-splices it after every sync and refresh breaks ("column does not
# exist in the rowset"). Same doctrine as every other absent declaration:
# resolves in the template or fails the load.

def test_columns_kind_absent_loads_and_resolves(tmp_path, api):
    fleet = api.load(_fleet(tmp_path, {"columns": [
        {"object": f"{TABLE}[{COLUMN}]", "kind": "absent",
         "reason": "client view has no such field — never splice it",
         "owner": "owner-b", "added": datetime.date(2026, 9, 8)},
        # retain direction: the kind-less original entry is `kind: extension`
        {"object": f"{TABLE}[client_only_extra]", "kind": "extension",
         "reason": "purge-direction entry: retained client-only column"},
    ]}))
    resolved = api.resolve_for_model(fleet, "spinout-05")
    # missing direction lands in its OWN bucket, keyed table -> {column: meta}
    assert resolved["columns_absent"] == {
        TABLE: {COLUMN: {"reason": "client view has no such field — never "
                                   "splice it",
                         "owner": "owner-b",
                         "added": datetime.date(2026, 9, 8)}}}
    # the retain-direction entry keeps its own meaning, untouched
    assert resolved["columns"] == {TABLE: {"client_only_extra"}}
    # no cross-model bleed
    assert not api.resolve_for_model(fleet, "spinout-04")["columns_absent"]


def test_columns_kind_absent_unresolvable_fails_loudly(tmp_path, api):
    cases = [
        ({"object": f"{TABLE}[No Such Column Anywhere]", "kind": "absent",
          "reason": "a typo must fail the load, never silently suppress "
                    "nothing"}, "does not exist in the template"),
        # ...and so must a table the template does not have
        ({"object": "No Such Table Anywhere[whatever]", "kind": "absent",
          "reason": "typo'd table"}, "does not exist in the template"),
        # ...and a column entry naming no column at all (the tool's object is
        # one `Table[column]` string: empty or missing column part)
        ({"object": f"{TABLE}[]", "kind": "absent",
          "reason": "no column named"}, "column.*required"),
        ({"object": TABLE, "kind": "absent",
          "reason": "no column named"}, "column.*required"),
    ]
    for i, (entry, needle) in enumerate(cases):
        root = tmp_path / f"case{i}"
        root.mkdir()
        fy = _fleet(root, {"columns": [entry]})
        with pytest.raises(DriftDoctorError, match=needle):
            api.load(fy)


def test_kind_still_rejected_on_source_views(tmp_path, api):
    """Opening `columns` to a direction must not open `source_views`: nothing
    consumes a missing-direction declaration there, so it stays a loud error."""
    fy = _fleet(tmp_path, {"source_views": [
        {"table": TABLE, "view": "some_view", "kind": "absent",
         "reason": "inert declaration"}]})
    with pytest.raises(DriftDoctorError) as exc:
        api.load(fy)
    msg = str(exc.value)
    assert "kind" in msg and "source_views" in msg

"""
Field-parameter schema modernization (fieldparam.schema_modernize), ported.

Fixture-based integration tests for the schema-modernize detector and its
fixer, against the sync_engine minis (tests/fixtures/sync_engine/).

OWNER RULE (core-not-superset): a client-ONLY axis column (present on the
derived model, absent from the template — e.g. spinout-05's `Value5` /
`x-Selector N Is Cost Related` / `Value6` cost-grouping feeding its Cohorts
report) is CLIENT CUSTOMIZATION, never "legacy schema" to strip. The
modernizer must NEVER drop it (auto-dropping it truncated every tuple and
broke the calculated table — MdxScript 'SYNTAXERROR'). So a table carrying
client-only axis columns is left UNTOUCHED and surfaced as a report-only
`fieldparam.schema_blocked` finding. Tables whose column set is a subset of
the template's still modernize.

Port (C-fieldparam-schema-modernize): the original planner/modernizer,
provider collection, remediation context and batch console are replaced by
drift-doctor: a fleet.yml per test (template = universal_mini, model
`fixture_growth` = growth_mini, `fieldparam_tables` globs, the modernize kind
allowlisted), `capture`, `detect` findings and `remediate` outcomes through
the Python API, and the planner exposed as
`tmdl_drift_doctor.fieldparams.build_schema_plan / modernize_schema /
FieldParamSchemaError`. Field counts come from `fieldparams.read_rows`.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

import pytest
from conftest import FIXTURES as _FIXTURES
from conftest import detect_all, write_fleet

from tmdl_drift_doctor import tmdl_edit as te
from tmdl_drift_doctor.tmdl import parse_table_file


FIXTURES = _FIXTURES / "sync_engine"
UNI_SRC = FIXTURES / "universal_mini" / "Performance.SemanticModel"
GRO_SRC = FIXTURES / "growth_mini" / "Growth.SemanticModel"

MODEL = "fixture_growth"
MODERNIZE = "fieldparam.schema_modernize"
BLOCKED = "fieldparam.schema_blocked"

OVERLAY_FILES = ("tables/Cohort Performance.tmdl",
                 "tables/Growth Measures.tmdl")

CUSTOM_ROW_LEGACY = ('\t\t\t\t( "Client Special", NAMEOF ( [CPM] ), 99, '
                     '"Performance_x", "Acq", "Cost" ),')
DISABLED_ROW = ('\t\t\t\t// ( "Old Thing", NAMEOF ( [CPM] ), 50, '
                '"Performance_x", "X", "Y" ),')


# ─────────────────────────── helpers ───────────────────────────

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ordered_columns(path: Path) -> list:
    """Port: the original's column-order helper -> the tool's table parser."""
    return list(parse_table_file(path).columns)


def extract_entries_from_file(path: Path):
    """Port: the original row reader -> `fieldparams.read_rows`."""
    from tmdl_drift_doctor.fieldparams import read_rows
    return read_rows(path)


def _copy_pair(tmp_path: Path):
    uni_model = tmp_path / "universal_mini" / "Performance.SemanticModel"
    growth_model = tmp_path / "growth_mini" / "Growth.SemanticModel"
    shutil.copytree(UNI_SRC, uni_model)
    shutil.copytree(GRO_SRC, growth_model)
    return uni_model / "definition", growth_model


def _make_fleet(tmp_path: Path, uni_defn: Path, growth_model: Path):
    """Port of the original context builder: a fleet.yml over the copied
    minis, then a captured baseline of the template."""
    from tmdl_drift_doctor.baseline import capture
    from tmdl_drift_doctor.fleet import load_fleet
    fy = write_fleet(tmp_path, uni_defn.parent, {MODEL: growth_model},
                     fieldparam_tables=["x-Selector *", "y-Selector *"],
                     allowlist={"kinds": [MODERNIZE]})
    fleet = load_fleet(fy)
    capture(fleet)
    return fleet


def _legacy_col_block(name: str, source: str, inferred: bool = False) -> str:
    lines = [f"\tcolumn '{name}'" if " " in name else f"\tcolumn {name}",
             "\t\tlineageTag: 00000000-1111-2222-3333-444444444444",
             "\t\tsummarizeBy: none"]
    if inferred:
        lines.append("\t\tisNameInferred")
    lines += [f"\t\tsourceColumn: [{source}]", "",
              "\t\tannotation SummarizationSetBy = Automatic"]
    return "\n".join(lines)


def _legacyize_x(growth_defn: Path) -> Path:
    """Give growth's x-Selector 1 client-ONLY columns (spinout-05-class):
    +Value5 +'x-Selector 1 Is Cost Related', 6-field tuples, one custom row,
    one disabled row. The template has none of these — so this is client
    customization."""
    f = growth_defn / "tables" / "x-Selector 1.tmdl"
    content = f.read_text(encoding="utf-8")
    content = te.insert_column_block(
        content, _legacy_col_block("Value5", "Value5", inferred=True))
    content = te.insert_column_block(
        content, _legacy_col_block("x-Selector 1 Is Cost Related", "Value6"))
    content = re.sub(r'("Performance_x")(\s*\)\s*,?)$',
                     r'\1, "Other", "No Cost"\2', content, flags=re.M)
    lines = content.split("\n")
    anchor = next(i for i, l in enumerate(lines) if '"Cost",' in l)
    lines.insert(anchor + 1, CUSTOM_ROW_LEGACY)
    lines.insert(anchor + 2, DISABLED_ROW)
    content = "\n".join(lines)
    te.assert_tmdl_valid(content)
    f.write_text(content, encoding="utf-8")
    return f


def _legacyize_y(growth_defn: Path) -> Path:
    """y flavor: +Value6 client-only column, tuples widened 5 -> 6 fields."""
    f = growth_defn / "tables" / "y-Selector 1.tmdl"
    content = f.read_text(encoding="utf-8")
    content = te.insert_column_block(
        content, _legacy_col_block("Value6", "Value6", inferred=True))
    content = re.sub(r'("(?:Other|Query Related)")(\)\s*,?)$',
                     r'\1, "No Cost"\2', content, flags=re.M)
    te.assert_tmdl_valid(content)
    f.write_text(content, encoding="utf-8")
    return f


def _overlay_hashes(growth_defn: Path) -> dict:
    return {rel: _sha(growth_defn / rel) for rel in OVERLAY_FILES}


def _run_schema_sync(fleet):
    """Port of collect + run_batch(allowed_categories=(modernize,)):
    detect, then remediate restricted to the modernize kind."""
    from tmdl_drift_doctor.remediate import remediate
    items = detect_all(fleet)
    result = remediate(fleet, items, kind=MODERNIZE)
    return items, result


def _report_only(finding) -> bool:
    from tmdl_drift_doctor.detect import REMEDIABLE_KINDS
    return finding.kind not in REMEDIABLE_KINDS


# ─────────────────────────── tests ───────────────────────────

def test_category_is_in_the_sync_allowlist():
    # Port: SYNC_AXIS_CATEGORIES -> the tool's derived (field-parameter)
    # phase kind list; the original "zmap" merge kind is
    # `mapping_category.missing` (G-zmap-merge).
    from tmdl_drift_doctor.remediate import DERIVED_PHASE_KINDS
    assert MODERNIZE in DERIVED_PHASE_KINDS
    assert (DERIVED_PHASE_KINDS.index(MODERNIZE)
            < DERIVED_PHASE_KINDS.index("mapping_category.missing"))


def test_x_client_custom_columns_preserved_not_stripped(tmp_path):
    """A client-only axis column ('x-Selector 1 Is Cost Related' / Value5 /
    Value6) is customization — the modernizer must NEVER strip it. The table
    is left byte-untouched and surfaced as a report-only schema_blocked
    finding."""
    uni_defn, growth_model = _copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    xfile = _legacyize_x(growth_defn)
    before = _sha(xfile)
    overlay_before = _overlay_hashes(growth_defn)
    pre_cols = _ordered_columns(xfile)
    assert "x-Selector 1 Is Cost Related" in pre_cols  # the client-only column

    fleet = _make_fleet(tmp_path, uni_defn, growth_model)
    items, result = _run_schema_sync(fleet)

    # NEVER a modernize finding for a client-customized axis table
    assert not [i for i in items
                if i.kind == MODERNIZE and i.ref.endswith("x-Selector 1")]
    # a report-only schema_blocked finding is surfaced instead (no action)
    blocked = [i for i in items
               if i.kind == BLOCKED and i.ref.endswith("x-Selector 1")]
    assert len(blocked) == 1 and _report_only(blocked[0])
    assert result.count("applied") == 0

    # the client-custom columns + tuples are byte-untouched
    assert _sha(xfile) == before
    assert "x-Selector 1 Is Cost Related" in _ordered_columns(xfile)
    _entries, fc = extract_entries_from_file(xfile)
    assert fc == 6, "client 6-field tuples must be preserved verbatim"
    # overlay untouched
    assert _overlay_hashes(growth_defn) == overlay_before


def test_client_custom_axis_is_a_noop_across_runs(tmp_path):
    """Blocked (client-customized) axis tables are stable — a second run
    changes nothing (no oscillation)."""
    uni_defn, growth_model = _copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    xfile = _legacyize_x(growth_defn)

    fleet = _make_fleet(tmp_path, uni_defn, growth_model)
    _run_schema_sync(fleet)
    after_first = _sha(xfile)
    _run_schema_sync(fleet)
    assert _sha(xfile) == after_first, "second run changed a blocked axis table"


def test_y_client_custom_columns_preserved(tmp_path):
    """y flavor: the client-only Value6 column is preserved, not stripped."""
    uni_defn, growth_model = _copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    yfile = _legacyize_y(growth_defn)
    before = _sha(yfile)
    _pre, pre_fc = extract_entries_from_file(yfile)
    assert pre_fc == 6

    fleet = _make_fleet(tmp_path, uni_defn, growth_model)
    items, result = _run_schema_sync(fleet)
    assert not [i for i in items
                if i.kind == MODERNIZE and i.ref.endswith("y-Selector 1")]
    # original object id "fixture_growth:y-Selector 1" = (model, ref)
    assert [f"{i.model}:{i.ref}" for i in items if i.kind == BLOCKED] \
        == ["fixture_growth:y-Selector 1"]
    assert result.count("applied") == 0
    assert _sha(yfile) == before
    _post, post_fc = extract_entries_from_file(yfile)
    assert post_fc == 6, "client Value6 column must survive"


def test_client_only_columns_block_loud_and_write_nothing(tmp_path):
    """The block is loud (the planner and the modernizer raise naming the
    derived-only columns) and writes NOTHING; through the engine it is a
    report-only schema_blocked finding (applied=0, failed=0)."""
    from tmdl_drift_doctor.fieldparams import (
        FieldParamSchemaError,
        build_schema_plan,
        modernize_schema,
    )
    uni_defn, growth_model = _copy_pair(tmp_path)
    growth_defn = growth_model / "definition"
    xfile = _legacyize_x(growth_defn)
    ufile = uni_defn / "tables" / "x-Selector 1.tmdl"
    before = _sha(xfile)

    # original needle "client-only axis column" -> the tool's own stable
    # wording (spec Test port notes)
    with pytest.raises(FieldParamSchemaError,
                       match="derived-only field-parameter column"):
        build_schema_plan(xfile, ufile)
    with pytest.raises(FieldParamSchemaError):
        modernize_schema(xfile, ufile)
    assert _sha(xfile) == before, "a blocked modernization WROTE to the client"

    fleet = _make_fleet(tmp_path, uni_defn, growth_model)
    _items, result = _run_schema_sync(fleet)
    assert result.count("applied") == 0 and result.count("failed") == 0
    assert _sha(xfile) == before


def test_pristine_pair_emits_no_schema_items(tmp_path):
    uni_defn, growth_model = _copy_pair(tmp_path)
    fleet = _make_fleet(tmp_path, uni_defn, growth_model)
    items = detect_all(fleet)
    assert not [i for i in items
                if i.kind.startswith("fieldparam.schema")], (
        "modern (template-identical) axis tables must not re-fire")

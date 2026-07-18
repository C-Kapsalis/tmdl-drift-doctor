"""Safe TMDL surgery — above all, the never-inject-into-DAX invariant.

The production failure mode this guards against: inserting a property line
right after ``measure X =`` when the expression is BARE MULTI-LINE places the
property INSIDE the DAX body. The engine then reads ``isHidden`` as the start
of the expression and the measure fails to compile. The insert must land
after the whole expression — and the write guard must refuse content where
it didn't.
"""

import pytest

from tmdl_drift_doctor import TmdlEditError
from tmdl_drift_doctor import tmdl_edit as te
from tmdl_drift_doctor.tmdl import parse_table_file

SINGLE = (
    "table T\n"
    "\tlineageTag: 00000000-0000-4000-8000-000000000001\n"
    "\n"
    "\tmeasure 'Simple' = COUNTROWS(T)\n"
    "\t\tformatString: #,0\n"
    "\t\tlineageTag: 00000000-0000-4000-8000-000000000002\n"
)

BARE_MULTILINE = (
    "table T\n"
    "\tlineageTag: 00000000-0000-4000-8000-000000000001\n"
    "\n"
    "\tmeasure 'Layered' =\n"
    "\n"
    "\t\t\tVAR _x = COUNTROWS(T)\n"
    "\t\t\tRETURN\n"
    "\t\t\t    _x + 1\n"
    "\t\tformatString: #,0\n"
    "\t\tlineageTag: 00000000-0000-4000-8000-000000000003\n"
)

FENCED = (
    "table T\n"
    "\tlineageTag: 00000000-0000-4000-8000-000000000001\n"
    "\n"
    "\tmeasure 'Fenced' = ```\n"
    "\t\t\tCALCULATE(\n"
    "\t\t\t    COUNTROWS(T)\n"
    "\t\t\t)\n"
    "\t\t\t```\n"
    "\t\tlineageTag: 00000000-0000-4000-8000-000000000004\n"
)


def _measure_expr(content: str, name: str, tmp_path) -> str:
    f = tmp_path / "T.tmdl"
    f.write_text(content, encoding="utf-8")
    return parse_table_file(f).measures[name].expression


class TestNeverInjectIntoDax:
    def test_single_line_inserts_after_declaration(self, tmp_path):
        out = te.insert_property_line(SINGLE, "measure", "Simple", "\t\tisHidden")
        lines = out.split("\n")
        decl = lines.index("\tmeasure 'Simple' = COUNTROWS(T)")
        assert lines[decl + 1] == "\t\tisHidden"
        te.assert_tmdl_valid(out)

    def test_bare_multiline_inserts_AFTER_the_expression(self, tmp_path):
        out = te.insert_property_line(BARE_MULTILINE, "measure", "Layered",
                                      "\t\tisHidden")
        lines = out.split("\n")
        # the property must come after the last DAX line, never after `=`
        assert lines.index("\t\tisHidden") > lines.index("\t\t\t    _x + 1")
        te.assert_tmdl_valid(out)
        # and the parsed expression is unchanged — the property is a property
        expr = _measure_expr(out, "Layered", tmp_path)
        assert "isHidden" not in expr
        assert _measure_expr(out, "Layered", tmp_path).strip() == \
            _measure_expr(BARE_MULTILINE, "Layered", tmp_path).strip()

    def test_fenced_inserts_after_the_closing_fence(self, tmp_path):
        out = te.insert_property_line(FENCED, "measure", "Fenced", "\t\tisHidden")
        lines = out.split("\n")
        assert lines.index("\t\tisHidden") > lines.index("\t\t\t```")
        te.assert_tmdl_valid(out)
        assert "isHidden" not in _measure_expr(out, "Fenced", tmp_path)

    def test_guard_refuses_injected_content(self):
        # simulate the historical bug: property spliced right after `=`,
        # DAX body following it
        injected = BARE_MULTILINE.replace(
            "\tmeasure 'Layered' =\n",
            "\tmeasure 'Layered' =\n\t\tisHidden\n")
        with pytest.raises(TmdlEditError, match="inside the expression"):
            te.assert_no_property_injection(injected)
        with pytest.raises(TmdlEditError):
            te.assert_tmdl_valid(injected)

    def test_guarded_write_never_persists_injected_content(self, tmp_path):
        injected = BARE_MULTILINE.replace(
            "\tmeasure 'Layered' =\n",
            "\tmeasure 'Layered' =\n\t\tisHidden\n")
        target = tmp_path / "T.tmdl"
        with pytest.raises(TmdlEditError):
            te.guarded_write(target, injected)
        assert not target.exists()

    def test_insert_is_idempotent(self):
        once = te.insert_property_line(BARE_MULTILINE, "measure", "Layered",
                                       "\t\tisHidden")
        twice = te.insert_property_line(once, "measure", "Layered",
                                        "\t\tisHidden")
        assert once == twice


class TestWriteGuards:
    def test_rejects_space_indentation(self):
        with pytest.raises(TmdlEditError, match="tabs"):
            te.assert_tmdl_valid("table T\n    column C\n")

    def test_rejects_unbalanced_fences(self):
        with pytest.raises(TmdlEditError, match="fence"):
            te.assert_tmdl_valid("table T\n\tmeasure 'M' = ```\n\t\t\tX\n")

    def test_rejects_duplicate_property(self):
        bad = SINGLE.replace("\t\tformatString: #,0",
                             "\t\tformatString: #,0\n\t\tformatString: 0.0")
        with pytest.raises(TmdlEditError, match="duplicate"):
            te.assert_tmdl_valid(bad)


class TestBlockSurgery:
    def test_extract_and_remove_roundtrip(self):
        block = te.extract_block(BARE_MULTILINE, "measure", "Layered")
        assert block.startswith("\tmeasure 'Layered' =")
        assert "formatString" in block
        removed = te.remove_block(BARE_MULTILINE, "measure", "Layered")
        assert "Layered" not in removed
        reinserted = te.insert_measure_block(removed, block)
        assert te.extract_block(reinserted, "measure", "Layered") == block

    def test_lineage_tag_regeneration(self):
        block = te.extract_block(SINGLE, "measure", "Simple")
        fresh = te.regenerate_lineage_tag(block)
        assert fresh != block
        assert te.first_lineage_tag(fresh) != te.first_lineage_tag(block)
        pinned = te.set_first_lineage_tag(fresh, "deadbeef-dead-4bad-8bad-000000000000")
        assert te.first_lineage_tag(pinned) == "deadbeef-dead-4bad-8bad-000000000000"

    def test_mapping_row_add_remove(self):
        src = ('\t\t\t\t    {\n'
               '\t\t\t\t        {"a", "A", 1},\n'
               '\t\t\t\t        {"b", "B", 2}\n'
               '\t\t\t\t    }\n')
        added = te.add_mapping_row(src, '{"c", "C", 3}')
        assert '{"c", "C", 3}' in added
        assert te.add_mapping_row(added, '{"c", "C", 3}') == added  # idempotent
        removed = te.remove_mapping_row(added, "b")
        assert '"b"' not in removed
        assert '"a"' in removed and '"c"' in removed
        # removing the first row swallows the FOLLOWING separator comma
        from tmdl_drift_doctor.tmdl import parse_mapping_rows
        removed_first = te.remove_mapping_row(src, "a")
        assert set(parse_mapping_rows(removed_first)) == {"b"}
        assert ",\n" not in removed_first  # no dangling separator comma

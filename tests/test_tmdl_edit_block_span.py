"""block_span must not run past a one-tab construct it doesn't know.

`block_span` ends a block at the first `_OBJECT_BOUNDARY` line. Any one-tab
construct MISSING from that pattern is invisible, so a block ending just before
it runs on to the next recognised keyword and swallows everything between —
in both directions, since extract_block and remove_block share block_span:

  extract  `remediate.py --client "Performance App Spend" --category
           column.missing` spliced universal's Conversions[value_monetary] (the
           LAST column, immediately followed by refreshPolicy) and carried the
           whole BigQuery refreshPolicy into the target as an unreported 14th
           change on a 13-item run. That model's partition is an OData feed, so
           the imported policy could never be byte-identical to it and
           test_existing_refresh_policies_are_structurally_valid went red —
           invisibly, because the client is `gating: advisory`.

  remove   deleting that same last column would have DELETED the table's
           refreshPolicy along with it.

The template Conversions case is asserted against a stand-in table at the
bottom (the original read the real template model).

Ported from the user's suite (batch b8-sync).
"""
from pathlib import Path

import pytest

from tmdl_drift_doctor import tmdl_edit as te

# Stand-in for the template's Conversions table (fictional; proven against the
# original test, see PORT-LOG batch b8-sync).
STANDIN_ROOT = Path(__file__).resolve().parent / "fixtures" / "b8-sync"
UNIVERSAL_CONVERSIONS = (
    STANDIN_ROOT / 'universal/Performance.SemanticModel/definition/tables'
    / 'Conversions.tmdl'
)

# A column immediately followed by refreshPolicy, then a partition, then the
# table-level annotation tail — the exact shape that broke.
FIXTURE = "\n".join([
    "table Conversions",
    "\tlineageTag: 11111111-1111-1111-1111-111111111111",
    "",
    "\tcolumn first_col",
    "\t\tdataType: string",
    "\t\tsourceColumn: first_col",
    "",
    "\tcolumn value_monetary",
    "\t\tdataType: double",
    "\t\tsourceColumn: value_monetary",
    "",
    "\t\tannotation SummarizationSetBy = Automatic",
    "",
    "\trefreshPolicy",
    "\t\tpolicyType: basic",
    "\t\tincrementalGranularity: month",
    "\t\tsourceExpression =",
    "\t\t\t\tlet",
    "\t\t\t\t    Source = BigQueryTheRangeStartThing",
    "\t\t\t\tin",
    "\t\t\t\t    Source",
    "",
    "\tpartition Conversions-abc = m",
    "\t\tmode: import",
    "\t\tsource =",
    "\t\t\t\tlet",
    "\t\t\t\t    Source = OData.Feed(\"https://api.example.com/\")",
    "\t\t\t\tin",
    "\t\t\t\t    Source",
    "",
    "\tannotation PBI_ResultType = Table",
    "",
])


def test_extract_stops_before_refresh_policy():
    block = te.extract_block(FIXTURE, "column", "value_monetary")
    assert block is not None
    assert "refreshPolicy" not in block, (
        "splicing this column would carry the table's refreshPolicy into the "
        f"target model. Extracted:\n{block}"
    )
    assert "policyType" not in block and "sourceExpression" not in block
    assert "partition" not in block
    # ...while still capturing the column's own two-tab properties.
    assert "sourceColumn: value_monetary" in block
    assert "annotation SummarizationSetBy" in block


def test_remove_does_not_delete_the_refresh_policy():
    updated = te.remove_block(FIXTURE, "column", "value_monetary")
    assert updated is not None
    assert "value_monetary" not in updated
    assert "refreshPolicy" in updated, "deleting the column ate the refreshPolicy"
    assert "policyType: basic" in updated
    assert "partition Conversions-abc" in updated


def test_extract_stops_before_the_table_annotation_tail():
    """A column that is the last object in the file must not swallow the
    one-tab `annotation` tail. Its own annotations are at two tabs."""
    tail_fixture = FIXTURE.replace(
        "\trefreshPolicy\n"
        "\t\tpolicyType: basic\n"
        "\t\tincrementalGranularity: month\n"
        "\t\tsourceExpression =\n"
        "\t\t\t\tlet\n"
        "\t\t\t\t    Source = BigQueryTheRangeStartThing\n"
        "\t\t\t\tin\n"
        "\t\t\t\t    Source\n"
        "\n"
        "\tpartition Conversions-abc = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\t\tlet\n"
        "\t\t\t\t    Source = OData.Feed(\"https://api.example.com/\")\n"
        "\t\t\t\tin\n"
        "\t\t\t\t    Source\n"
        "\n",
        "",
    )
    block = te.extract_block(tail_fixture, "column", "value_monetary")
    assert block is not None
    assert "PBI_ResultType" not in block, f"swallowed the table tail:\n{block}"
    assert "annotation SummarizationSetBy" in block


def test_real_universal_conversions_column_extracts_clean():
    """The production case, against a stand-in template table whose last
    column precedes a one-tab refreshPolicy."""
    content = UNIVERSAL_CONVERSIONS.read_text(encoding='utf-8-sig')
    # the stand-in always carries the policy (the original skipped otherwise)
    assert "\trefreshPolicy" in content, (
        "the Conversions stand-in must carry a refreshPolicy")
    # the column immediately preceding refreshPolicy is the risky one
    lines = content.split("\n")
    pol = next(i for i, l in enumerate(lines) if l.startswith("\trefreshPolicy"))
    last_col = None
    for i in range(pol - 1, -1, -1):
        if lines[i].startswith("\tcolumn "):
            last_col = lines[i].split("column ", 1)[1].strip().strip("'\"")
            break
    assert last_col, "no column found before refreshPolicy"
    block = te.extract_block(content, "column", last_col)
    assert block is not None, f"could not extract {last_col!r}"
    assert "refreshPolicy" not in block, (
        f"universal Conversions[{last_col}] still extracts its refreshPolicy"
    )

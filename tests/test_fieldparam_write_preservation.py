"""write_merged_entries must never lose comment-carried content (PBI#453).

Two loss classes surfaced while running the cross-level fixer fleet-wide on
2026-08-07 — both pre-existing writer bugs, both silent:

  1. a LIVE row's TRAILING inline comment after its separator comma
     (Spend App's `("Crawl Month", …),--NAMEOF('Crawl'[Crawl month]), …` —
     a parked alternative target) was dropped on every parse→re-emit cycle;
  2. a DISABLED row with prefix text between the comment marker and the
     tuple (insights's `--DQ-14 dup: ("Org Latest Sync Date", …`) failed
     the preservation regex, which anchored the tuple right after `--`.

Cousins of the 2026-06-04 incident class (disabled rows deleted / re-armed
on re-emit); same law: a rewrite may reorder to sort_order but must carry
every live row, disabled row, and comment payload through verbatim.

Ported from the original suite: the reader/writer, the batch kind list and
the comment masker are drift-doctor's (H-fieldparam-row-writer).
"""
from __future__ import annotations

import textwrap

import pytest


def extract_entries_from_file(path):
    """Port: the original reader -> drift-doctor's field-parameter row reader
    (H-fieldparam-row-writer): read_rows(path) -> (rows, arity)."""
    from tmdl_drift_doctor.fieldparams import read_rows
    return read_rows(path)


def write_merged_entries(path, rows, arity):
    """Port: the original writer -> fieldparams.write_rows."""
    from tmdl_drift_doctor.fieldparams import write_rows
    return write_rows(path, rows, arity)



def test_cross_level_categories_in_sync_allowlist():
    """Owner decision (PBI#453, 2026-08-10): the cross-level repairs are
    deterministic and additive-only, so the --sync-underlay batch path
    auto-applies them; only the interactive console prompts."""
    # Port: the original console's batch axis-kind list -> the tool's
    # batch-safe kind set.
    from tmdl_drift_doctor.remediate import BATCH_SAFE_KINDS
    assert "fieldparam.cross_level" in BATCH_SAFE_KINDS
    assert "fieldparam.plot_specific_extra" in BATCH_SAFE_KINDS


_AXIS = textwrap.dedent("""\
    table 'y-Selector 1'

    \tcolumn 'y-Selector 1'
    \t\tsourceColumn: [Value1]

    \tpartition 'y-Selector 1' = calculated
    \t\tmode: import
    \t\tsource = ```
    \t\t\t\t{
    \t\t\t\t-- Date_y
    \t\t\t\t("Month", NAMEOF('__Calendar'[Month]), 2, "Date_y", "Other"),
    \t\t\t\t("Crawl Month", NAMEOF('__Calendar'[Start Month]), 5, "Date_y", "Other"),--NAMEOF('Crawl'[Crawl month]), 5, "Date_y", "Other"),
    \t\t\t\t--DQ-14 dup: ("Org Latest Sync Date", NAMEOF('_m Organizations'[org_latest_sync_date]), 1535, "Date_y", "Other"),
    \t\t\t\t("Year", NAMEOF('__Calendar'[Year]), 9, "Date_y", "Other")
    \t\t\t\t}
    \t\t\t\t```
    """)


def _roundtrip(tmp_path):
    f = tmp_path / "y-Selector 1.tmdl"
    f.write_text(_AXIS, encoding="utf-8")
    entries, fc = extract_entries_from_file(f)
    write_merged_entries(f, entries, fc)
    return f.read_text(encoding="utf-8"), entries


def test_trailing_inline_comment_survives_rewrite(tmp_path):
    text, entries = _roundtrip(tmp_path)
    assert len(entries) == 3
    assert ("(\"Crawl Month\", NAMEOF('__Calendar'[Start Month]), 5, "
            "\"Date_y\", \"Other\"),--NAMEOF('Crawl'[Crawl month])") in text


def test_prefixed_disabled_row_survives_rewrite(tmp_path):
    text, _ = _roundtrip(tmp_path)
    assert '--DQ-14 dup: ("Org Latest Sync Date"' in text


def test_rewrite_is_idempotent_on_preserved_content(tmp_path):
    f = tmp_path / "y-Selector 1.tmdl"
    f.write_text(_AXIS, encoding="utf-8")
    for _ in range(2):
        entries, fc = extract_entries_from_file(f)
        write_merged_entries(f, entries, fc)
    once = f.read_text(encoding="utf-8")
    entries, fc = extract_entries_from_file(f)
    write_merged_entries(f, entries, fc)
    assert f.read_text(encoding="utf-8") == once


# ── adversarial-audit regressions (2026-08-10) ──────────────────────────────

def test_trailer_on_last_sorted_row_never_leaves_dangling_comma(tmp_path):
    """The trailer-carrying row sorting LAST used to defeat the
    last-live-row comma strip: the pass rstripped the COMMENT's commas and
    the real separator survived before `}` — the exact syntax error the
    pass exists to prevent. Trailers now attach after the strip."""
    axis = _AXIS.replace(
        '("Crawl Month", NAMEOF(\'__Calendar\'[Start Month]), 5, "Date_y", "Other"),'
        "--NAMEOF('Crawl'[Crawl month]), 5, \"Date_y\", \"Other\"),",
        '("Crawl Month", NAMEOF(\'__Calendar\'[Start Month]), 99, "Date_y", "Other"),'
        "--NAMEOF('Crawl'[Crawl month]), 99, \"Date_y\", \"Other\"),")
    f = tmp_path / "y-Selector 1.tmdl"
    f.write_text(axis, encoding="utf-8")
    entries, fc = extract_entries_from_file(f)
    write_merged_entries(f, entries, fc)
    text = f.read_text(encoding="utf-8")
    live = [l.strip() for l in text.splitlines()
            if l.strip().startswith("(") and "NAMEOF" in l]
    assert live[-1].startswith('("Crawl Month"')          # sorted last
    tuple_part = live[-1].split("--")[0].rstrip()
    assert not tuple_part.endswith(","), (
        "dangling separator before `}` — Power BI rejects the constructor")
    assert "--NAMEOF('Crawl'[Crawl month]), 99" in live[-1]  # trailer intact
    # and the rewrite stays idempotent in this shape too
    entries, fc = extract_entries_from_file(f)
    write_merged_entries(f, entries, fc)
    assert f.read_text(encoding="utf-8") == text


def test_trailer_never_duplicated_onto_identical_row(tmp_path):
    """Two rows normalizing to the same tuple, one carrying a trailer: the
    trailer must survive exactly once, never copied onto the twin."""
    axis = _AXIS.replace(
        '\t\t\t\t("Year", NAMEOF(\'__Calendar\'[Year]), 9, "Date_y", "Other")\n',
        '\t\t\t\t("Year", NAMEOF(\'__Calendar\'[Year]), 9, "Date_y", "Other"),--parked alt\n'
        '\t\t\t\t("Year",  NAMEOF(\'__Calendar\'[Year]), 9, "Date_y", "Other")\n')
    f = tmp_path / "y-Selector 1.tmdl"
    f.write_text(axis, encoding="utf-8")
    entries, fc = extract_entries_from_file(f)
    write_merged_entries(f, entries, fc)
    assert f.read_text(encoding="utf-8").count("--parked alt") == 1


def test_mask_line_comments_ignores_markers_inside_quotes():
    """A `--`/`//` inside a string literal or quoted table name is content:
    masking from it blinded every scanner to the rest of the row (the p0
    cross-level gate inherited the blind spot)."""
    # Port: the test-kernel masker -> the tool's shared comment masker.
    from tmdl_drift_doctor.fieldparams import mask_line_comments
    line = ('("Sessions -- Organic", NAMEOF(\'T -- X\'[c]), 1, "g_y", "Other"),'
            ' -- real comment\n'
            '("Visits https://app", NAMEOF(T[c2]), 2, "g_y", "Other"),\n')
    masked = mask_line_comments(line)
    assert '"Sessions -- Organic"' in masked
    assert "'T -- X'" in masked
    assert '"Visits https://app"' in masked
    assert "real comment" not in masked


# ---------------------------------------------------------------------------
# ANNOTATION COMMENTS + TUPLE ARITY (PBI#721 follow-up)
#
# The suite above exercises ONE tuple shape (4 fields + 1 extra). Production
# carries three across the live fleet, counting fields after the ordinal:
#   1 extra  — universal and most clients
#   2 extras — most clients
#   3 extras — spinout_05_db, on 3,161 rows
# and a STANDALONE annotation comment (marker, no tuple) matched none of the
# writer's three preserve-paths, so the group-header regeneration dropped it.
# Measured 2026-09-03 on spinout_05_db/Growth x-Selector 1: a run adding ONE unrelated
# row took the file from 47 comment lines to 43, deleting the only record of why
# four rows exist. Rows and extras were never lost — comments were.
# ---------------------------------------------------------------------------

_AXIS_WIDE = textwrap.dedent("""\
    table 'x-Selector 1'

    \tcolumn 'x-Selector 1'
    \t\tsourceColumn: [Value1]

    \tpartition 'x-Selector 1' = calculated
    \t\tmode: import
    \t\tsource = ```
    \t\t\t\t{
    \t\t\t\t-- Merchant Activity
    \t\t\t\t-- PBI#601 AUTO-ADDED: copied from x-Plot Specific 2 — this family must offer it.
    \t\t\t\t("First Cohort Size", NAMEOF('#B'[^^^^First Cohort Size]), 1100, "Merchant Activity", "Other", "No Cost"),
    \t\t\t\t("Cohort Churn %", NAMEOF('#B'[^^^^Total Churn/ Month]), 1106, "Merchant Activity", "Other", "No Cost"),
    \t\t\t\t-- import_log_x
    \t\t\t\t( "Latest Data Date", NAMEOF ( [Latest Data Date] ), 1024, "import_log_x", "Other", "Other" )
    \t\t\t\t}
    \t\t\t\t```
    """)


def _roundtrip_text(tmp_path, text, name="x-Selector 1.tmdl"):
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    entries, fc = extract_entries_from_file(f)
    write_merged_entries(f, entries, fc)
    return f.read_text(encoding="utf-8"), entries, fc


def test_standalone_annotation_comment_survives_rewrite(tmp_path):
    """A `--` line with no tuple is an annotation, not a group header."""
    text, _, _ = _roundtrip_text(tmp_path, _AXIS_WIDE)
    assert "PBI#601 AUTO-ADDED" in text, "annotation comment was dropped"


def test_annotation_stays_attached_to_its_row(tmp_path):
    """It must precede the row it documents, not drift to the end."""
    text, _, _ = _roundtrip_text(tmp_path, _AXIS_WIDE)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    i = next(k for k, l in enumerate(lines) if "PBI#601" in l)
    assert "First Cohort Size" in lines[i + 1], (
        f"annotation detached from its row; next line was {lines[i + 1]!r}")


def test_group_header_is_not_duplicated(tmp_path):
    """Headers are regenerated, so preserving them too would double them."""
    text, _, _ = _roundtrip_text(tmp_path, _AXIS_WIDE)
    assert text.count("-- Merchant Activity") == 1
    assert text.count("-- import_log_x") == 1


def test_three_extra_fields_survive_rewrite(tmp_path):
    """spinout-05's arity: 3 fields after the ordinal, on 3,161 live rows."""
    text, entries, fc = _roundtrip_text(tmp_path, _AXIS_WIDE)
    assert fc == 6, f"expected 6-field tuples, got {fc}"
    assert '"Merchant Activity", "Other", "No Cost"' in text
    assert '"import_log_x", "Other", "Other"' in text


def test_no_row_lost_on_wide_arity(tmp_path):
    text, entries, _ = _roundtrip_text(tmp_path, _AXIS_WIDE)
    assert len(entries) == 3
    for name in ("First Cohort Size", "Cohort Churn %", "Latest Data Date"):
        assert name in text, f"{name} lost in rewrite"

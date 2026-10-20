"""Unit-style checks for the encoded mapping-table codec + merge.

The codec's contract is CONTENT fidelity with loud failure: decode(encode(x))
== x, merges are strictly additive to model rows, and verification refuses
anything that does not decode back to exactly the intended rows. These tests
also decode the template z-Map stand-in so a codec/blob format drift (e.g.
Power BI changing the compression) fails here, not mid-sync.

Ported from the user's suite (G-zmap-codec, G-zmap-merge): the remediation
z-Map module of the original is replaced by drift-doctor's codec module
``tmdl_drift_doctor.mapping_codec`` (encode_rows / decode_rows /
extract_rows / replace_rows / verify_rows / categories / merge_categories /
add_category_rows) and its error class ``CodecError``; the asserted message
substrings are the error contract.
"""

from __future__ import annotations

import pytest
from port_helpers_b7_retire import MINIS, ZMAP_TABLE


_ROWS = [
    ["SEO", "", "seo_x"],
    ["SEO", "Rankings", "seo_rank_x"],
    ["Spend", "", "spend_x"],
]

@pytest.fixture()
def mc():
    """The codec module (imported per test so a missing module is a test
    error, not a collection error)."""
    import importlib
    return importlib.import_module("tmdl_drift_doctor.mapping_codec")


# Stand-in for the original's real template z-Map (universal_index): a
# fictional bike-shop entered-data table holding an SEO category.
ZMAP_STAND_IN = MINIS / "zmap_standin" / f"{ZMAP_TABLE}.tmdl"


def test_codec_roundtrip(mc):
    assert mc.decode_rows(mc.encode_rows(_ROWS)) == _ROWS


def test_real_universal_zmap_decodes(mc):
    f = ZMAP_STAND_IN
    rows = mc.extract_rows(f.read_text(encoding="utf-8"))
    assert rows and all(len(r) == len(rows[0]) for r in rows)
    assert "SEO" in mc.categories(rows)
    # full text round-trip: replace with identical rows, re-extract, compare
    new_text = mc.replace_rows(f.read_text(encoding="utf-8"), rows)
    mc.verify_rows(new_text, rows)


def test_merge_is_strictly_additive(mc):
    client = [["SEO", "", "seo_x"], ["Custom", "", "custom_x"]]
    universal = _ROWS
    merged = mc.merge_categories(client, universal, ["Spend"])
    # model rows first + untouched (incl. the model-only category)
    assert merged[: len(client)] == client
    # only the requested category's template rows appended
    assert merged[len(client):] == [["Spend", "", "spend_x"]]


def test_verify_content_rejects_mismatch(mc):
    text = 'x = Binary.FromText("%s")' % mc.encode_rows(_ROWS)
    with pytest.raises(mc.CodecError, match="intended rows"):
        mc.verify_rows(text, _ROWS + [["Extra", "", "extra_x"]])


def test_extract_refuses_zero_or_many_blobs(mc):
    with pytest.raises(mc.CodecError, match="exactly 1"):
        mc.extract_rows("no blob here")
    b = mc.encode_rows(_ROWS)
    two = f'Binary.FromText("{b}") Binary.FromText("{b}")'
    with pytest.raises(mc.CodecError, match="exactly 1"):
        mc.extract_rows(two)


def test_undecodable_blob_fails_loudly(mc):
    with pytest.raises(mc.CodecError, match="does not decode"):
        mc.decode_rows("QUJDRA==")  # valid b64, not deflate


def test_add_category_rows_is_additive_and_idempotent(mc):
    """The additive counterpart of row removal.

    merge_categories cannot do this: it can only copy rows that already exist
    in the TEMPLATE, so a model-only key (a retagged field-parameter group,
    say) has no source to copy from.
    """
    client = [["SEO", "", "seo_x"], ["Client-e LTV", "", "m_attr_y"]]
    rows, added, skipped = mc.add_category_rows(
        client, "Client-e LTV", ["m_attr_sub_y", "m_attr_y"])
    # existing rows untouched and still first
    assert rows[:len(client)] == client
    assert added == [["Client-e LTV", "", "m_attr_sub_y"]]
    assert skipped == ["m_attr_y"]          # already present -> no duplicate
    # re-running adds nothing
    again, added2, _ = mc.add_category_rows(rows, "Client-e LTV", ["m_attr_sub_y"])
    assert again == rows and added2 == []
    # and it round-trips through the codec unchanged
    assert mc.decode_rows(mc.encode_rows(rows)) == rows


def test_add_category_rows_refuses_a_non_three_column_zmap(mc):
    with pytest.raises(mc.CodecError, match="row width"):
        mc.add_category_rows([["SEO", "", "seo_x", "extra"]], "SEO", ["x"])
    with pytest.raises(mc.CodecError, match="empty z-Map"):
        mc.add_category_rows([], "SEO", ["x"])

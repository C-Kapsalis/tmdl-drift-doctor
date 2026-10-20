"""Encoded mapping tables in the pipeline: the read-only CLI, the template
merge kinds, the union, category retirement and the verified write."""

import pytest
from port_helpers_b7_retire import (ZMAP_TABLE, capture, copy_pair, detect,
                                    detect_and_apply, install_zmap, make_fleet,
                                    read_zmap_rows, zmap_file, zmap_tmdl)

from conftest import drift

from tmdl_drift_doctor import mapping_codec as mc
from tmdl_drift_doctor.remediate import remediate

MERGE_KINDS = ["mapping_category.missing", "mapping_row.missing"]


def _pair(tmp_path, t_rows, d_rows, kinds=MERGE_KINDS, **entry):
    uni, gro = copy_pair(tmp_path)
    install_zmap(uni, t_rows)
    install_zmap(gro, d_rows)
    fy = make_fleet(tmp_path, uni, {"fixture_growth": {"path": gro, **entry}},
                    kinds=kinds)
    capture(fy)
    return fy, uni, gro


# ───────────── read-only CLI ─────────────

def test_decode_and_diff(tmp_path):
    mine = tmp_path / "mine.tmdl"
    theirs = tmp_path / "theirs.tmdl"
    mine.write_text(zmap_tmdl([["Beans", "", "origin"], ["Beans", "", "roast"],
                               ["Brewing", "", "method"]]), encoding="utf-8")
    theirs.write_text(zmap_tmdl([["Beans", "", "origin"],
                                 ["Retail", "", "cups"]]), encoding="utf-8")
    r = drift("mapping decode", mine)
    assert r.exit_code == 0 and "3 row(s), 2 categories" in r.output
    r = drift("mapping diff", mine, theirs)
    assert r.exit_code == 0
    assert "- Retail" in r.output and "+ Brewing" in r.output
    assert drift("mapping diff", mine).exit_code == 2


# ───────────── merge kinds ─────────────

def test_row_missing_extra_and_two_cell_fix(tmp_path):
    fy, _uni, gro = _pair(
        tmp_path, [["Retail", "", "store_id"], ["Retail", "", "sku"]],
        [["Retail", "store_id"], ["Wholesale", "dealer_id"]])
    found = [(f.kind, f.ref) for f in detect(fy)
             if "mapping" in f.kind]
    assert ("mapping_row.missing", f"{ZMAP_TABLE}/Retail|sku") in found
    assert ("extra.mapping_row", f"{ZMAP_TABLE}/Wholesale|dealer_id") in found
    assert not [k for k, r in found if r.endswith("|store_id")]
    _f, res = detect_and_apply(fy, "mapping_row.missing")
    assert res.count("applied") == 1
    assert read_zmap_rows(gro) == [["Retail", "store_id"],
                                   ["Wholesale", "dealer_id"],
                                   ["Retail", "sku"]]
    assert not [f for f in detect(fy) if f.kind == "mapping_row.missing"]


def test_category_missing_appends_template_rows_after_model_rows(tmp_path):
    fy, _uni, gro = _pair(
        tmp_path, [["SEO", "", "seo_x"], ["Spend", "", "spend_x"]],
        [["SEO", "", "seo_x"], ["Custom", "", "custom_x"]])
    found = [f for f in detect(fy) if f.kind == "mapping_category.missing"]
    assert len(found) == 1 and found[0].data["categories"] == ["Spend"]
    _f, res = detect_and_apply(fy, "mapping_category.missing")
    assert res.count("applied") == 1
    assert read_zmap_rows(gro) == [["SEO", "", "seo_x"],
                                   ["Custom", "", "custom_x"],
                                   ["Spend", "", "spend_x"]]
    assert not [f for f in detect(fy) if f.kind == "mapping_category.missing"]


def test_declared_absent_category_is_not_offered(tmp_path):
    fy, _uni, _gro = _pair(
        tmp_path, [["SEO", "", "seo_x"], ["Spend", "", "spend_x"]],
        [["SEO", "", "seo_x"]],
        overrides={"mapping_categories": [
            {"category": "Spend", "reason": "no paid media here"}]})
    assert not [f for f in detect(fy) if f.kind == "mapping_category.missing"]


def test_undecodable_model_string_fails_and_writes_nothing(tmp_path):
    fy, _uni, gro = _pair(tmp_path, [["SEO", "", "seo_x"]],
                          [["SEO", "", "seo_x"]])
    path = zmap_file(gro)
    text = path.read_text(encoding="utf-8")
    blob = mc._blob(text).group(1)
    path.write_text(text.replace(blob, "QUJDRA=="), encoding="utf-8")
    before = path.read_bytes()
    _f, res = detect_and_apply(fy, "mapping_category.missing")
    assert res.count("failed") == 1
    assert path.read_bytes() == before


def test_failed_read_back_restores_the_file(tmp_path, monkeypatch):
    fy, _uni, gro = _pair(
        tmp_path, [["SEO", "", "seo_x"], ["Spend", "", "spend_x"]],
        [["SEO", "", "seo_x"]])
    path = zmap_file(gro)
    before = path.read_bytes()
    real = mc.verify_rows
    calls = {"n": 0}

    def flaky(text, rows):
        calls["n"] += 1
        if calls["n"] > 1:                     # the read-back after writing
            raise mc.CodecError("intended rows differ (simulated)")
        return real(text, rows)

    monkeypatch.setattr(mc, "verify_rows", flaky)
    _f, res = detect_and_apply(fy, "mapping_category.missing")
    assert res.count("failed") == 1
    assert path.read_bytes() == before


# ───────────── union ─────────────

def test_union_keeps_template_rows_then_new_model_rows():
    t = [["Retail", "", "store_id"], ["Retail", "", "sku"]]
    d = [["Wholesale", "dealer_id"], ["Retail", "store_id"],
         ["Wholesale", "dealer_id"]]
    rows, added = mc.union_rows(t, d)
    assert rows == t + [["Wholesale", "", "dealer_id"]] and added == 1


def test_union_command(tmp_path):
    t = tmp_path / "t.tmdl"
    d = tmp_path / "d.tmdl"
    out = tmp_path / "o.tmdl"
    t.write_text(zmap_tmdl([["Retail", "", "store_id"]]), encoding="utf-8")
    d.write_text(zmap_tmdl([["Events", "", "tickets"]]), encoding="utf-8")
    out.write_text(zmap_tmdl([]), encoding="utf-8")
    r = drift("mapping union", "--template", t, "--derived", d, "--out", out)
    assert r.exit_code == 0, r.output
    assert "total 2" in r.output
    assert mc.extract_rows(out.read_text(encoding="utf-8")) == [
        ["Retail", "", "store_id"], ["Events", "", "tickets"]]
    plain = tmp_path / "plain.tmdl"
    plain.write_text("table x\n", encoding="utf-8")
    r = drift("mapping union", "--template", t, "--derived", d, "--out", plain)
    assert r.exit_code == 2 and plain.read_text() == "table x\n"


# ───────────── category retirement ─────────────

def _retire_category(tmp_path, model_rows):
    t_rows = [["Events", "", "tickets"], ["Beans", "", "origin"]]
    fy, uni, gro = _pair(tmp_path, t_rows, model_rows,
                         kinds=["retire.mapping_category"])
    install_zmap(uni, [["Beans", "", "origin"]])
    capture(fy)
    return fy, gro


def test_retired_category_is_removed(tmp_path):
    fy, gro = _retire_category(tmp_path, [["Events", "", "tickets"],
                                          ["Beans", "", "origin"]])
    found = [f for f in detect(fy) if f.kind == "retire.mapping_category"]
    assert [f.ref for f in found] == [f"{ZMAP_TABLE}/Events"]
    _f, res = detect_and_apply(fy, "retire.mapping_category")
    assert res.count("applied") == 1
    assert read_zmap_rows(gro) == [["Beans", "", "origin"]]


def test_retired_category_with_model_keys_waits_for_a_person(tmp_path):
    fy, gro = _retire_category(tmp_path, [["Events", "", "tickets"],
                                          ["Events", "", "vip_passes"],
                                          ["Beans", "", "origin"]])
    found = [f for f in detect(fy) if f.kind == "retire.mapping_category"]
    assert len(found) == 1 and found[0].blocked
    assert "vip_passes" in found[0].blocked[0]
    _f, res = detect_and_apply(fy, "retire.mapping_category")
    assert res.count("blocked") == 1
    assert ["Events", "", "vip_passes"] in read_zmap_rows(gro)


def test_drop_groups_is_exact_and_trims():
    rows = [["Wholesale", "", "Beans_x"], ["Wholesale", "", "Cafe_y"],
            [" Retail ", "", "Cups_x"], ["Retail", "", "Cafe_y"],
            ["Events", "", "Tickets_x"]]
    kept, removed = mc.drop_groups(rows, {"Retail"})
    assert len(kept) == 3 and sorted(r[2] for r in removed) == ["Cafe_y",
                                                                "Cups_x"]
    assert mc.drop_groups(rows, {"retail"})[1] == []
    kept, removed = mc.drop_groups(rows, {"Wholesale"})
    assert removed[0] == ["Wholesale", "", "Beans_x"]   # row 0 is data

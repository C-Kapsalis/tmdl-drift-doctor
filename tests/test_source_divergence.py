"""Source divergence: detection on and off, declared tables, the
enrichment registry tools and the source-view ledger."""

import json

from port_helpers_b7_retire import (capture, copy_pair, detect_and_apply,
                                    make_fleet)

from conftest import drift, drift_json, edit_file, write_fleet

from tmdl_drift_doctor.enrichment import aggregate, markdown

EXTRA = ("\tcolumn 'Campaign Y'\n\t\tdataType: string\n"
         "\t\tlineageTag: 50000000-0000-4000-8000-000000000001\n"
         "\t\tsummarizeBy: none\n\t\tsourceColumn: campaign_y\n\n")


def _diverge(tmp_path, **keys):
    uni, gro = copy_pair(tmp_path)
    edit_file(gro / "definition" / "tables" / "_Campaigns.tmdl",
              '"_campaigns_n"', '"_campaigns_custom"')
    t = uni / "definition" / "tables" / "_Campaigns.tmdl"
    text = t.read_text(encoding="utf-8")
    t.write_text(text.replace("\tpartition", EXTRA + "\tpartition", 1),
                 encoding="utf-8")
    entry = {"path": gro, **keys.pop("entry", {})}
    fy = write_fleet(tmp_path, uni, {"north": entry},
                     allowlist={"kinds": ["column.missing"]}, **keys)
    capture(fy)
    return fy, gro


def test_divergence_is_reported_and_the_splice_queues_a_notice(tmp_path):
    fy, _gro = _diverge(tmp_path, source_identity={
        "function": "source_filter_dates", "db_parameter": "v-db"})
    _r, rep = drift_json("detect", fleet=fy)
    assert rep["models"]["north"]["source_divergent_tables"] == {
        "_Campaigns": {"template": "_campaigns_n",
                       "model": "_campaigns_custom"}}
    r = drift("remediate", "--yes", "--kind", "column.missing", fleet=fy)
    assert r.exit_code == 0, r.output
    reg = json.loads((tmp_path / ".drift-doctor" /
                      "view_enrichment_outstanding.json").read_text())
    assert reg["entries"][0]["columns"] == [{"name": "Campaign Y",
                                             "sourceColumn": "campaign_y"}]
    out = drift("enrichment report", fleet=fy).output
    assert "| north | _Campaigns | _campaigns_custom | `campaign_y` | - |" in out


def test_without_a_source_identity_block_nothing_is_divergent(tmp_path):
    fy, _gro = _diverge(tmp_path)
    _r, rep = drift_json("detect", fleet=fy)
    assert rep["models"]["north"]["source_divergent_tables"] == {}
    r = drift("remediate", "--yes", "--kind", "column.missing", fleet=fy)
    assert r.exit_code == 0
    assert not (tmp_path / ".drift-doctor" /
                "view_enrichment_outstanding.json").exists()


def test_a_declared_table_is_divergent_without_a_view_change(tmp_path):
    uni, gro = copy_pair(tmp_path)
    fy = write_fleet(tmp_path, uni, {"north": {
        "path": gro, "source_divergent": ["Ads"]}})
    capture(fy)
    _r, rep = drift_json("detect", fleet=fy)
    assert "Ads" in rep["models"]["north"]["source_divergent_tables"]


def test_rescan_flags_registry_columns_that_are_gone(tmp_path):
    fy, gro = _diverge(tmp_path, source_identity={
        "function": "source_filter_dates", "db_parameter": "v-db"})
    drift("remediate", "--yes", "--kind", "column.missing", fleet=fy)
    r = drift("enrichment rescan", fleet=fy)
    assert r.exit_code == 0, r.output
    f = gro / "definition" / "tables" / "_Campaigns.tmdl"
    f.write_text(f.read_text(encoding="utf-8").replace(
        "\tcolumn 'Campaign Y'", "\tcolumn 'Campaign Z'"), encoding="utf-8")
    r = drift("enrichment rescan", fleet=fy)
    assert r.exit_code == 5 and "Campaign Y" in r.output
    assert drift("enrichment rescan", "--model", "other",
                 fleet=fy).exit_code == 0


def test_markdown_digest_shape():
    entries = aggregate([
        {"model": "a", "table": "T", "template_view": "t", "model_view": "m",
         "column": {"name": "x", "sourceColumn": "x_src"}},
        {"model": "b", "table": "U", "template_view": "t", "model_view": "n",
         "measure": "Total"}])
    text = markdown(entries)
    assert text.count("\n| ") == 3          # header + two rows
    assert "| a | T | m | `x_src` | - |" in text
    assert "| b | U | n | - | Total |" in text
    assert markdown([]) == ""


def test_a_repointed_view_is_followed_once(tmp_path):
    uni, gro = copy_pair(tmp_path)
    fy = make_fleet(tmp_path, uni, {"north": gro},
                    kinds=["retire.source_view"])
    capture(fy)
    edit_file(uni / "definition" / "tables" / "Ads.tmdl", '"ads_n"',
              '"ads_v2"')
    r = drift("capture", fleet=fy)
    assert "source view of Ads: ads_n -> ads_v2" in r.output
    _f, res = detect_and_apply(fy)
    assert [o.finding.ref for o in res.outcomes if o.status == "applied"] == [
        "Ads: ads_n -> ads_v2"]
    assert '"ads_v2"' in (gro / "definition" / "tables" / "Ads.tmdl").read_text()
    edit_file(uni / "definition" / "tables" / "Ads.tmdl", '"ads_v2"',
              '"ads_v3"')
    capture(fy)
    _f, res = detect_and_apply(fy)
    assert [o.finding.ref for o in res.outcomes if o.status == "applied"] == [
        "Ads: ads_v2 -> ads_v3"]

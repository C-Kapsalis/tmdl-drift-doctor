"""A model committed from Windows has CRLF line endings. Every reader and
writer must treat it like the same model with LF endings, and give it back
with CRLF."""

from conftest import drift

from tmdl_drift_doctor import groups as gp
from tmdl_drift_doctor import tmdl_edit as te
from tmdl_drift_doctor.refresh import parse_policy, problems
from tmdl_drift_doctor.remediate import FileChange, _restore


def crlf(text: str) -> str:
    return text.replace("\n", "\r\n")


def test_a_crlf_refresh_policy_is_at_table_level():
    text = crlf("table Sales\n\tlineageTag: 1\n\n"
                "\trefreshPolicy\n\t\tpolicyType: basic\n"
                "\t\trollingWindowGranularity: year\n"
                "\t\trollingWindowPeriods: 6\n\n"
                "\tpartition Sales = m\n\t\tmode: import\n"
                "\t\tsource = sales\n")
    pol = parse_policy(text)
    assert pol.scalars["rollingWindowPeriods"] == "6"
    assert problems(text, pol) == []


def test_a_crlf_model_file_deregisters_a_table():
    text = crlf("model Model\n\n"
                "annotation PBI_QueryOrder = [\"Orders\",\"Workshop\"]\n\n"
                "ref table Orders\nref table Workshop\n")
    assert te.deregister_table(text, "Workshop") == crlf(
        "model Model\n\nannotation PBI_QueryOrder = [\"Orders\"]\n\n"
        "ref table Orders\n")


def test_a_crlf_report_json_round_trips():
    raw = crlf('{\n  "visual": {\n    "width": 372.66666666666669\n  }\n}\n')
    doc, dump = gp._load_exact(raw)
    assert doc is not None and dump(doc) == raw


def test_a_restored_crlf_file_keeps_its_bytes(tmp_path):
    path = tmp_path / "Sales.tmdl"
    old = crlf("table Sales\n\tlineageTag: 1\n")
    path.write_bytes(b"broken")
    _restore(FileChange(path, old, "x"))
    assert path.read_bytes() == old.encode("utf-8")


def test_ref_push_keeps_crlf_and_is_idempotent(captured, fleet_dir):
    fy = fleet_dir / "fleet.yml"
    vf = captured.model_definition("alpha") / "tables" / "Visits.tmdl"
    vf.write_bytes(vf.read_bytes().replace(b"\r\n", b"\n")
                   .replace(b"\n", b"\r\n"))
    r = drift("remediate", "--ref", "Total Visits", "--yes", fleet=fy)
    assert r.exit_code == 0, r.output
    raw = vf.read_bytes()
    assert b"COUNTROWS(Visits)" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")
    r = drift("remediate", "--ref", "Total Visits", "--yes", "--model",
              "alpha", fleet=fy)
    assert "applied=0" in r.output and "unchanged" in r.output, r.output

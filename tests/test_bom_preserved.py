"""A file's UTF-8 byte-order mark survives every write the tool makes.

Power BI Desktop and Windows editors may save a model file with a UTF-8
byte-order mark. The ordinary cascade kept it; `remediate --ref` dropped
it, and so did the restore after a failed read-back (the file came back
without its mark). Both now keep the file's bytes as they were, apart
from the change itself.
"""

from __future__ import annotations

from conftest import drift

from tmdl_drift_doctor import DriftDoctorError
from tmdl_drift_doctor import remediate as rem
from tmdl_drift_doctor import tmdl_edit as te

BOM = b"\xef\xbb\xbf"


def _crlf(raw: bytes) -> bytes:
    return raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def test_ref_push_keeps_the_bom(captured, fleet_dir):
    vf = captured.model_definition("alpha") / "tables" / "Visits.tmdl"
    vf.write_bytes(BOM + vf.read_bytes())
    r = drift("remediate", "--ref", "Total Visits", "--yes", "--model",
              "alpha", fleet=fleet_dir / "fleet.yml")
    assert r.exit_code == 0, r.output
    raw = vf.read_bytes()
    assert b"COUNTROWS(Visits)" in raw, r.output
    assert raw.startswith(BOM) and not raw.startswith(BOM + BOM)


def test_ref_push_keeps_the_bom_and_crlf(captured, fleet_dir):
    vf = captured.model_definition("alpha") / "tables" / "Visits.tmdl"
    vf.write_bytes(BOM + _crlf(vf.read_bytes()))
    r = drift("remediate", "--ref", "Total Visits", "--yes", "--model",
              "alpha", fleet=fleet_dir / "fleet.yml")
    assert r.exit_code == 0, r.output
    raw = vf.read_bytes()
    assert raw.startswith(BOM)
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_a_failed_read_back_restores_the_exact_bytes(captured, fleet_dir,
                                                      monkeypatch):
    """The restore puts back the bytes that were there: mark and endings."""
    vf = captured.model_definition("alpha") / "tables" / "Visits.tmdl"
    before = BOM + _crlf(vf.read_bytes())
    vf.write_bytes(before)
    real = rem._cascade_object

    def failing(fleet, f, keyword, insert):
        label, changes = real(fleet, f, keyword, insert)

        def verify(path):
            raise DriftDoctorError("read-back failed (test)")
        for c in changes:
            c.verify = verify
        return label, changes

    monkeypatch.setattr(rem, "_cascade_object", failing)
    r = drift("remediate", "--yes", "--kind", "measure.expression_drift",
              "--model", "alpha", fleet=fleet_dir / "fleet.yml")
    assert "read-back failed (test)" in r.output, r.output
    assert vf.read_bytes() == before


def test_a_text_change_read_without_its_bom_keeps_it(tmp_path):
    """A fixer that read the file with the mark stripped still writes the
    mark back."""
    path = tmp_path / "Map.tmdl"
    path.write_bytes(BOM + b"table Map\r\n\tlineageTag: 1\r\n")
    old = te.read_text(path)
    rem.write_change(rem.FileChange(path, old,
                                    old.replace("1", "2")))
    assert path.read_bytes() == BOM + b"table Map\r\n\tlineageTag: 2\r\n"


def test_tmdl_edit_write_text_keeps_an_existing_bom(tmp_path):
    path = tmp_path / "T.tmdl"
    path.write_bytes(BOM + b"table T\n")
    te.write_text(path, te.read_text(path) + "\tlineageTag: 1\n")
    assert path.read_bytes() == BOM + b"table T\n\tlineageTag: 1\n"

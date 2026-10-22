"""A file the cascade creates takes the line endings of the file it copies.

A fleet committed from Windows has CRLF files. A table the template gained
is copied into a derived model as a new file; that file must come out with
the template file's CRLF endings, not LF, or the next commit carries a
mixed-ending model and the reviewer sees a whole-file diff.
"""

from __future__ import annotations

from pathlib import Path

from conftest import drift

from tmdl_drift_doctor.baseline import capture


def _to_crlf(root: Path) -> None:
    for p in root.rglob("*.tmdl"):
        p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n")
                      .replace(b"\n", b"\r\n"))


def _only_crlf(raw: bytes) -> bool:
    return b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


def test_a_copied_table_keeps_the_crlf_of_a_crlf_fleet(fleet, fleet_dir):
    _to_crlf(fleet_dir)
    capture(fleet)
    r = drift("remediate", "--yes", "--kind", "table.missing",
              fleet=fleet_dir / "fleet.yml")
    assert r.exit_code == 0, r.output
    copy = fleet.model_definition("bravo") / "tables" / "Classes.tmdl"
    assert copy.exists(), r.output
    assert _only_crlf(copy.read_bytes())
    assert _only_crlf((fleet.model_definition("bravo") / "model.tmdl")
                      .read_bytes())


def test_a_copied_table_follows_the_template_file(fleet, fleet_dir):
    """Only the template is CRLF: the copy follows the file it copies."""
    _to_crlf(fleet.template_definition)
    capture(fleet)
    r = drift("remediate", "--yes", "--kind", "table.missing",
              fleet=fleet_dir / "fleet.yml")
    assert r.exit_code == 0, r.output
    copy = fleet.model_definition("bravo") / "tables" / "Classes.tmdl"
    assert _only_crlf(copy.read_bytes())


def test_a_copied_table_stays_lf_in_an_lf_fleet(captured, fleet_dir):
    r = drift("remediate", "--yes", "--kind", "table.missing",
              fleet=fleet_dir / "fleet.yml")
    assert r.exit_code == 0, r.output
    copy = captured.model_definition("bravo") / "tables" / "Classes.tmdl"
    assert b"\r\n" not in copy.read_bytes()


def test_write_change_uses_the_given_line_ending_for_a_new_file(tmp_path):
    from tmdl_drift_doctor.remediate import FileChange, write_change
    path = tmp_path / "New.tmdl"
    write_change(FileChange(path, None, "table New\n\tlineageTag: 1\n",
                            eol="\r\n"))
    assert path.read_bytes() == b"table New\r\n\tlineageTag: 1\r\n"


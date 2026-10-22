"""A saved detection report is read whatever encoding Windows wrote it in.

`detect --json > report.json` in Windows PowerShell 5.1 writes UTF-16 with
a byte-order mark, and `Out-File -Encoding utf8` writes UTF-8 with one. The
ratchet and the blast-radius diff read such a report like a plain UTF-8 one.
A crash there exits 1, which a CI job reads as drift.
"""

from __future__ import annotations

import json

import pytest
from conftest import drift

ENCODINGS = {
    "utf-8": lambda t: t.encode("utf-8"),
    "utf-8-bom": lambda t: t.encode("utf-8-sig"),
    "utf-16-le-bom": lambda t: t.encode("utf-16"),          # PowerShell 5.1 `>`
    "utf-16-be-bom": lambda t: b"\xfe\xff" + t.encode("utf-16-be"),
}


@pytest.fixture
def report_text(captured, fleet_dir):
    fy = fleet_dir / "fleet.yml"
    r = drift("detect", "--json", fleet=fy)
    json.loads(r.stdout)
    assert drift("ratchet tighten", fleet=fy).exit_code == 0
    return r.stdout


@pytest.mark.parametrize("enc", sorted(ENCODINGS))
def test_ratchet_check_reads_the_report(report_text, fleet_dir, tmp_path, enc):
    path = tmp_path / "report.json"
    path.write_bytes(ENCODINGS[enc](report_text))
    r = drift("ratchet check", "--from-json", path, fleet=fleet_dir / "fleet.yml")
    assert r.exit_code == 0, r.output
    assert "Traceback" not in r.output
    assert "at or below its floor" in r.output


@pytest.mark.parametrize("enc", sorted(ENCODINGS))
def test_ratchet_tighten_reads_the_report(report_text, fleet_dir, tmp_path, enc):
    path = tmp_path / "report.json"
    path.write_bytes(ENCODINGS[enc](report_text))
    r = drift("ratchet tighten", "--from-json", path,
              fleet=fleet_dir / "fleet.yml")
    assert r.exit_code == 0, r.output


@pytest.mark.parametrize("enc", sorted(ENCODINGS))
def test_diff_reads_the_report(report_text, tmp_path, enc):
    base = tmp_path / "base.json"
    base.write_bytes(ENCODINGS[enc](report_text))
    head = tmp_path / "head.json"
    head.write_text(report_text, encoding="utf-8")
    out = tmp_path / "out.md"
    r = drift("diff", "--base", base, "--head", head, "--out", out)
    assert r.exit_code == 0, r.output
    assert "adds or resolves no actionable drift" in out.read_text(
        encoding="utf-8")


def test_an_undecodable_report_is_refused_with_exit_2(tmp_path, captured,
                                                      fleet_dir):
    """Bytes that are no Unicode text at all are a usage error, never a
    traceback (exit 1 reads as drift in CI)."""
    path = tmp_path / "report.json"
    path.write_bytes(b"{\"models\": \xff\xfe\xfa}")
    r = drift("ratchet check", "--from-json", path,
              fleet=fleet_dir / "fleet.yml")
    assert r.exit_code == 2, r.output
    assert "Traceback" not in r.output
    assert "UTF-8" in r.output

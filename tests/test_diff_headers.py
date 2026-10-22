"""Dry-run diff headers are git-style: `a/<path>`, never `a//<abs path>`.

The headers named the file by its absolute path glued to `a/`, so a POSIX
path printed as `a//home/...`. A path under the working directory is now
shown relative to it, and any other absolute path without its leading
slash.
"""

from __future__ import annotations

from conftest import drift

from tmdl_drift_doctor import tmdl_edit as te


def _headers(output: str) -> list:
    return [line for line in output.splitlines()
            if line.startswith(("--- ", "+++ "))]


def test_remediate_dry_run_headers_are_relative(captured, fleet_dir,
                                                monkeypatch):
    monkeypatch.chdir(fleet_dir)
    r = drift("remediate", "--dry-run", fleet=fleet_dir / "fleet.yml")
    heads = _headers(r.output)
    assert heads, r.output
    assert "--- a/franchises/alpha/GymChain.SemanticModel/definition/" \
           "tables/Visits.tmdl" in heads
    assert not any(h[4:6] in ("a/", "b/") and h[6:7] == "/" for h in heads)


def test_ref_dry_run_headers_are_relative(captured, fleet_dir, monkeypatch):
    monkeypatch.chdir(fleet_dir)
    r = drift("remediate", "--ref", "Total Visits", "--dry-run",
              fleet=fleet_dir / "fleet.yml")
    heads = _headers(r.output)
    assert heads, r.output
    assert all(h[4:].startswith(("a/franchises/", "b/franchises/"))
               for h in heads), heads


def test_purge_dry_run_headers_are_relative(captured, fleet_dir,
                                            monkeypatch):
    monkeypatch.chdir(fleet_dir)
    r = drift("purge", "--measure", "Alpha Loyalty Score", "--model", "alpha",
              fleet=fleet_dir / "fleet.yml")
    heads = _headers(r.output)
    assert heads, r.output
    assert all(h[4:].startswith(("a/franchises/", "b/franchises/"))
               for h in heads), heads


def test_a_path_outside_the_working_directory_loses_its_leading_slash(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    label = te.diff_label("/elsewhere/Model/tables/T.tmdl")
    assert label == "elsewhere/Model/tables/T.tmdl"
    assert te.diff_label(tmp_path / "x" / "T.tmdl") == "x/T.tmdl"

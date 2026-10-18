"""
Client overlay structural validation (A-fleet-config-validation).

The original read the client registry and `tests/overlays/<client>/{policy,dq,mapping}.yml`.
In the port the registry is the stand-in `fleet.yml` (an entry's `path` replaces
`model_path`) and the overlays live beside it in `overlays/<model id>/` (fictional
stand-ins; the original overlay files are not copied). `drift-doctor fleet check` runs the
validation:

  - overlays are opt-in: a model without an overlay folder is skipped, and a folder may hold
    any subset of the three files;
  - each present file loads as YAML and carries `version`, `client` and `description`;
  - `client`, when set, equals the model id;
  - every model entry has a path whose `definition/` folder exists.
"""
from __future__ import annotations

import shutil

import pytest

from conftest import drift
from port_helpers_b1_l1a import copy_fleet, primary_models

OVERLAY_FILES = ("policy.yml", "dq.yml", "mapping.yml")


def _check(root):
    return drift("fleet check", fleet=root / "fleet.yml")


def test_overlay_structure_per_client(tmp_path):
    """Every overlay file declares the required fields + a matching client key."""
    root = copy_fleet(tmp_path)
    clients = [m for m in primary_models(root) if (root / "overlays" / m).is_dir()]
    assert clients, "stand-in has overlay folders"
    r = _check(root)
    assert r.exit_code == 0, r.output
    for cid in clients:
        for name in OVERLAY_FILES:
            assert f"{cid}/{name}:" not in r.output, r.output


def _edit(path, old, new):
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


@pytest.mark.parametrize("variant", ["missing-description", "client-mismatch", "invalid-yaml"])
def test_overlay_structure_problem_is_reported(tmp_path, variant):
    root = copy_fleet(tmp_path)
    od = root / "overlays" / "spinout-02"
    if variant == "missing-description":
        _edit(od / "policy.yml", "description:", "summary:")
        needles = ["spinout-02/policy.yml:", "missing required field(s)", "description"]
    elif variant == "client-mismatch":
        _edit(od / "policy.yml", "client: 'spinout-02'", "client: 'spinout-03'")
        needles = ["spinout-02/policy.yml:", "'spinout-03'", "'spinout-02'"]
    else:
        (od / "dq.yml").write_text((od / "dq.yml").read_text(encoding="utf-8")
                                   + "bad: [unclosed\n", encoding="utf-8")
        needles = ["spinout-02/dq.yml:"]
    r = _check(root)
    assert r.exit_code == 2, r.output
    for n in needles:
        assert n in r.output, (n, r.output)


def test_partial_overlay_folder_is_allowed(tmp_path):
    """A folder holding only dq.yml is not an error (partial overlays are allowed)."""
    root = copy_fleet(tmp_path)
    od = root / "overlays" / "spinout-02"
    (od / "policy.yml").unlink()
    (od / "mapping.yml").unlink()
    r = _check(root)
    assert r.exit_code == 0, r.output


def test_no_overlay_folders_is_not_an_error(tmp_path):
    """No overlay folders at all: nothing to validate (original: skip)."""
    root = copy_fleet(tmp_path)
    shutil.rmtree(root / "overlays")
    r = _check(root)
    assert r.exit_code == 0, r.output


def test_every_registered_client_resolves_on_disk(tmp_path):
    """Every fleet entry has a path whose definition/ exists."""
    root = copy_fleet(tmp_path)
    r = _check(root)
    assert r.exit_code == 0, r.output


def test_entry_whose_definition_is_missing_fails(tmp_path):
    root = copy_fleet(tmp_path)
    shutil.rmtree(root / "spinout_05_db" / "Performance.SemanticModel" / "definition")
    r = _check(root)
    assert r.exit_code == 2, r.output
    assert "spinout_05_db" in r.output or "spinout-05" in r.output


def test_entry_without_path_fails(tmp_path):
    root = copy_fleet(tmp_path)
    _edit(root / "fleet.yml", "  spinout-05: spinout_05_db/Performance.SemanticModel\n",
          "  spinout-05:\n    gating: enforced\n")
    r = _check(root)
    assert r.exit_code == 2, r.output
    assert "spinout-05" in r.output and "no model_path" in r.output, r.output

"""Shared fixtures: a throwaway copy of the fixture fleet per test.

The fixture fleet is a fictional gym chain — one template model
(``GymChain``) and two franchise models (``alpha``, ``bravo``) with seeded
drift of every kind the suite detects.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tmdl_drift_doctor import tmdl_edit as te
from tmdl_drift_doctor.baseline import capture
from tmdl_drift_doctor.fleet import load_fleet

FIXTURE = Path(__file__).parent / "fixtures" / "fleet"


@pytest.fixture
def fleet_dir(tmp_path):
    dst = tmp_path / "fleet"
    shutil.copytree(FIXTURE, dst)
    return dst


@pytest.fixture
def fleet(fleet_dir):
    return load_fleet(fleet_dir / "fleet.yml")


@pytest.fixture
def captured(fleet):
    """A fleet with a fresh baseline already captured."""
    capture(fleet)
    return fleet


def retire_everything_from_template(fleet) -> dict:
    """Simulate a template maintainer retiring one object of every kind:

    * measure      — Visits[Peak Hour Visits]
    * column       — Classes[Capacity]
    * table        — Promotions
    * mapping row  — Plan Map/legacy-gold
    * relationship — Visits[PromoId] -> Promotions[PromoId] (it touched the
      retired table)

    The template's 'Class Fill Rate %' stops using the retired column, as a
    maintainer would rewrite it. Returns the removed measure block (for
    revival tests)."""
    tdef = fleet.template_definition

    vf = tdef / "tables" / "Visits.tmdl"
    content = vf.read_text(encoding="utf-8")
    measure_block = te.extract_block(content, "measure", "Peak Hour Visits")
    vf.write_text(te.remove_block(content, "measure", "Peak Hour Visits"),
                  encoding="utf-8")

    cf = tdef / "tables" / "Classes.tmdl"
    cf.write_text(te.remove_block(cf.read_text(encoding="utf-8"),
                                  "column", "Capacity").replace(
        "SUM(Classes[Capacity])", "COUNTROWS(Classes)"), encoding="utf-8")

    rf = tdef / "relationships.tmdl"
    rf.write_text(te.remove_relationship_block(
        rf.read_text(encoding="utf-8"),
        "Visits[PromoId] -> Promotions[PromoId]"), encoding="utf-8")

    (tdef / "tables" / "Promotions.tmdl").unlink()
    mf = tdef / "model.tmdl"
    mf.write_text(te.deregister_table(mf.read_text(encoding="utf-8"),
                                      "Promotions"), encoding="utf-8")

    pf = tdef / "tables" / "Plan Map.tmdl"
    pf.write_text(te.remove_mapping_row(pf.read_text(encoding="utf-8"),
                                        "legacy-gold"), encoding="utf-8")
    return {"measure_block": measure_block}


def tree_state(root: Path) -> dict:
    """{relative_path: content} for every model file under `root` — used to
    assert dry-run touches nothing."""
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file() and ".drift-doctor" not in p.parts:
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


def edit_fleet_yml(fleet_dir: Path, mutate) -> None:
    """Load fleet.yml, let `mutate(data)` change it, write it back."""
    import yaml
    path = Path(fleet_dir) / "fleet.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def detect_all(fleet, **kw):
    from tmdl_drift_doctor.baseline import load_baseline
    from tmdl_drift_doctor.detect import detect_fleet
    from tmdl_drift_doctor.ledger import Ledger
    return detect_fleet(fleet, load_baseline(fleet), Ledger(fleet.ledger_path),
                        **kw)


def edit_file(path: Path, old: str, new: str) -> None:
    text = Path(path).read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path}"
    Path(path).write_text(text.replace(old, new, 1), encoding="utf-8")


# ---------------------------------------------------------------------------
# Ported-suite helpers (added with the test port of the user's original suite)
#
# * ``FIXTURES`` is ``tests/fixtures``. Each ported batch keeps its stand-in
#   models in its own sub-folder there.
# * ``stand_in(name)`` copies ``tests/fixtures/<name>`` into ``tmp_path`` and
#   returns the copy, so a test may mutate it.
# * ``write_fleet(root, template, models, **keys)`` writes ``root/fleet.yml``
#   (version 1) with paths relative to ``root`` and returns its path. ``models``
#   maps a name to a path or to a long-form mapping. Extra keys (allowlist,
#   mapping_tables, state_dir, overrides, ...) are written as given.
# * ``drift(cmd, *args, fleet=None, input=None)`` runs ``drift-doctor <cmd>``
#   through the click entry point. ``cmd`` is the command path as one string
#   ("detect", "ratchet check"); ``--fleet <fleet>`` is inserted after it when
#   ``fleet`` is given. Returns the click ``Result`` (``exit_code``,
#   ``output``, ``stdout``, ``stderr``).
# * ``drift_json(cmd, *args, fleet=None)`` runs with ``--json`` and parses
#   stdout.
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def stand_in(tmp_path):
    def _copy(name: str, dest_name: str | None = None) -> Path:
        src = FIXTURES / name
        dst = tmp_path / (dest_name or Path(name).name)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return dst
    return _copy


def write_fleet(root, template, models: dict, **keys) -> Path:
    import yaml
    root = Path(root)

    def rel(p):
        p = Path(p)
        try:
            return str(p.relative_to(root)) if p.is_absolute() else str(p)
        except ValueError:
            return str(p)

    out_models = {}
    for name, entry in models.items():
        if isinstance(entry, dict):
            entry = dict(entry)
            entry["path"] = rel(entry["path"])
            out_models[name] = entry
        else:
            out_models[name] = rel(entry)
    data = {"version": 1, "template": rel(template), "models": out_models}
    data.update(keys)
    path = root / "fleet.yml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def drift(cmd: str, *args, fleet=None, input=None, env=None):
    from click.testing import CliRunner
    from tmdl_drift_doctor.cli import main
    argv = cmd.split()
    if fleet is not None:
        argv += ["--fleet", str(fleet)]
    argv += [str(a) for a in args]
    return CliRunner().invoke(main, argv, input=input, env=env)


def drift_json(cmd: str, *args, fleet=None):
    import json
    r = drift(cmd, "--json", *args, fleet=fleet)
    try:
        return r, json.loads(r.stdout)
    except ValueError:
        return r, None

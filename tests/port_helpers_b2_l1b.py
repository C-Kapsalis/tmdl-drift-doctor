"""Helpers for the b2-l1b ported tests (fleet stand-ins under tests/fixtures/b2-l1b).

Each scenario folder holds stand-in model folders (laid out like the original
repo root: ``universal/`` for the template, ``<model>_db/`` per derived model)
and a ``scenario.json`` describing the fleet. The ported test copies a scenario
into ``tmp_path`` and writes a ``fleet.yml`` from ``scenario.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import FIXTURES, write_fleet

BATCH = "b2-l1b"


def load_scenario(stand_in, group: str, name: str):
    """Copy ``fixtures/b2-l1b/<group>/<name>`` to tmp; return (root, data)."""
    root = stand_in(f"{BATCH}/{group}/{name}", dest_name=f"{group}-{name}")
    data = json.loads((root / "scenario.json").read_text(encoding="utf-8"))
    return root, data


def scenario_data(group: str, name: str) -> dict:
    """Read a scenario.json in place (collection-time parametrization only)."""
    return json.loads((FIXTURES / BATCH / group / name / "scenario.json")
                      .read_text(encoding="utf-8"))


def ensure_model_dirs(root: Path, paths) -> None:
    """Create a minimal TMDL folder for every path that has none (the
    connection-contract tests only need the fleet to load)."""
    for rel in paths:
        d = Path(root) / rel / "definition"
        if not d.is_dir():
            d.mkdir(parents=True)
            (d / "model.tmdl").write_text(
                "model Model\n\tculture: en-US\n", encoding="utf-8")


def findings(report: dict, model: str | None = None, kind: str | None = None):
    """Non-overridden findings from a ``detect --json`` report."""
    out = []
    for name, entry in (report or {}).get("models", {}).items():
        if model and name != model:
            continue
        for f in entry.get("findings", []):
            k = f.get("kind", "")
            if kind is None or k == kind or k.startswith(kind + "."):
                out.append(dict(f, model=name))
    return out

"""Helpers for the b1-l1a port (layer-1 fleet gates on the roastery stand-in).

The stand-in fleet lives in ``tests/fixtures/b1-l1a/fleet``: a fictional coffee
roastery with the template ``universal/Performance.SemanticModel`` and one entry
per client of the original registry (ids renamed by gates/rename.py). Entry
``spinout-02`` has a secondary model ``spinout-02-performance``.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import zlib
from pathlib import Path

import yaml

from conftest import FIXTURES, drift

FLEET_SRC = FIXTURES / "b1-l1a" / "fleet"
ZMAP_FILE = "z-Map Dimension-Measure Groupings.tmdl"
TEMPLATE_REL = "universal/Performance.SemanticModel"


def fleet_doc(root: Path = FLEET_SRC) -> dict:
    return yaml.safe_load((Path(root) / "fleet.yml").read_text(encoding="utf-8"))


def model_paths(root: Path = FLEET_SRC) -> dict:
    """{model name: path relative to the fleet root}, secondary models included."""
    out = {}
    for name, entry in fleet_doc(root)["models"].items():
        if isinstance(entry, str):
            out[name] = entry
            continue
        out[name] = entry["path"]
        for sec in entry.get("secondary_models") or []:
            out[sec["name"]] = sec["path"]
    return out


def primary_models(root: Path = FLEET_SRC) -> list:
    return sorted(fleet_doc(root)["models"])


def model_key(rel: str) -> str:
    """`<parent folder>/<model folder>`, the category-annotation model key."""
    p = Path(rel)
    return f"{p.parent.name}/{p.name}"


def zmap_models(root: Path = FLEET_SRC) -> list:
    """[(name, rel)] of every non-template model that has a mapping table."""
    return sorted(
        (n, rel) for n, rel in model_paths(root).items()
        if (Path(root) / rel / "definition" / "tables" / ZMAP_FILE).is_file()
    )


def copy_fleet(dst: Path) -> Path:
    root = Path(dst) / "fleet"
    shutil.copytree(FLEET_SRC, root)
    return root


def definition(root: Path, name: str) -> Path:
    if name == "template":
        return Path(root) / TEMPLATE_REL / "definition"
    return Path(root) / model_paths(root)[name] / "definition"


def capture(root: Path):
    r = drift("capture", fleet=Path(root) / "fleet.yml")
    assert r.exit_code == 0, r.output
    return r


def detect(root: Path, *args):
    """Capture, then `detect --json`; returns (result, report)."""
    capture(root)
    r = drift("detect", "--json", *args, fleet=Path(root) / "fleet.yml")
    try:
        rep = json.loads(r.stdout)
    except ValueError:
        raise AssertionError(f"detect --json gave no JSON (exit {r.exit_code}):\n{r.output}")
    return r, rep


def kind_matches(kind: str, prefix: str) -> bool:
    """Dotted-prefix match at a segment boundary (drift-rulings R4)."""
    return kind == prefix or kind.startswith(prefix + ".")


def findings(rep: dict, model: str, prefix: str) -> list:
    m = (rep.get("models") or {}).get(model) or {}
    return [f for f in m.get("findings") or [] if kind_matches(f["kind"], prefix)]


def overridden(rep: dict, model: str, prefix: str) -> list:
    m = (rep.get("models") or {}).get(model) or {}
    return [f for f in m.get("overridden") or [] if kind_matches(f["kind"], prefix)]


# ── encoded mapping table (test-side writer for mutations) ──

_BLOB = re.compile(r'Binary\.FromText\(\s*"([A-Za-z0-9+/=]+)"')


def read_zmap_rows(table_file: Path) -> list:
    blob = _BLOB.search(Path(table_file).read_text(encoding="utf-8")).group(1)
    return json.loads(zlib.decompress(base64.b64decode(blob), -zlib.MAX_WBITS))


def write_zmap_rows(table_file: Path, rows: list) -> None:
    raw = json.dumps(rows, separators=(",", ":")).encode()
    c = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    blob = base64.b64encode(c.compress(raw) + c.flush()).decode()
    text = Path(table_file).read_text(encoding="utf-8")
    m = _BLOB.search(text)
    Path(table_file).write_text(text[:m.start(1)] + blob + text[m.end(1):], encoding="utf-8")

"""Batch b7-retire helpers: the retirement / receipt / encoded-mapping ports.

The original tests drove an internal sync engine on two mini models. The port
drives drift-doctor instead, on a fleet.yml generated per test in tmp_path:

* ``copy_pair(tmp_path)`` copies the minis (``tests/fixtures/b7-retire``):
  template ``universal_mini/Performance.SemanticModel`` and derived
  ``growth_mini/Growth.SemanticModel``. Both carry an ``Ads`` table read
  through ``source_filter_dates("ads_n", ...)``, an ``Import Log`` table
  (``report_import_log_data``) and a ``_Campaigns`` table (``_campaigns_n``).
* ``make_fleet(root, template, models, ...)`` writes fleet.yml with the source
  identity block (function ``source_filter_dates``, parameter ``v-db``), the
  encoded mapping table and an allowlist.
* ``encode_rows`` / ``decode_rows`` / ``zmap_tmdl`` are a test-side builder
  and reader for encoded entered-data tables (raw DEFLATE + base64 of compact
  JSON), written for the fixtures so the sync tests do not depend on the codec
  under test. The codec itself is exercised in test_zmap_codec.py.
* ``detect_and_apply(fleet_yml, kind)`` = capture-free detect + remediate
  through the Python API (``detect_fleet`` + ``remediate(..., sync=True)``),
  the engine layer below the interactive console (the originals called
  ``collect`` + ``run_batch`` the same way).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import zlib
from pathlib import Path

from conftest import FIXTURES, write_fleet

MINIS = FIXTURES / "b7-retire"
UNI_SRC = MINIS / "universal_mini" / "Performance.SemanticModel"
GRO_SRC = MINIS / "growth_mini" / "Growth.SemanticModel"
ZMAP_TABLE = "z-Map Dimension-Measure Groupings"

SOURCE_IDENTITY = {"function": "source_filter_dates", "db_parameter": "v-db"}


def copy_pair(tmp_path: Path, growth_dir: str = "growth_mini"):
    """(template model dir, derived model dir) — fresh copies of the minis."""
    uni = tmp_path / "universal_mini" / "Performance.SemanticModel"
    gro = tmp_path / growth_dir / "Growth.SemanticModel"
    if not uni.exists():
        shutil.copytree(UNI_SRC, uni)
    shutil.copytree(GRO_SRC, gro)
    return uni, gro


def make_fleet(root: Path, template: Path, models: dict, *,
               kinds=None, **extra) -> Path:
    keys = {
        "mapping_tables": [ZMAP_TABLE],
        "source_identity": dict(SOURCE_IDENTITY),
        "state_dir": ".drift-doctor",
        "allowlist": {"kinds": list(kinds or [])},
    }
    keys.update(extra)
    return write_fleet(root, template, models, **keys)


# ───────────────────────── hashing ─────────────────────────

def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_hashes(defn: Path) -> dict:
    defn = Path(defn)
    return {str(p.relative_to(defn)): _sha(p)
            for p in sorted(defn.rglob("*")) if p.is_file()}


def overlay_hashes(template_defn: Path, derived_defn: Path) -> dict:
    """Hashes of the derived-only files (files the template does not have):
    a template cascade must never touch them."""
    tdef, ddef = Path(template_defn), Path(derived_defn)
    return {rel: h for rel, h in tree_hashes(ddef).items()
            if not (tdef / rel).exists()}


def assert_overlay_untouched(before: dict, template_defn: Path,
                             derived_defn: Path) -> None:
    after = overlay_hashes(template_defn, derived_defn)
    assert after == before, (
        f"derived-only overlay files changed: "
        f"{sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))}")


# ───────────────────────── encoded mapping tables ─────────────────────────

def encode_rows(rows) -> str:
    raw = json.dumps(rows, separators=(",", ":")).encode("utf-8")
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return base64.b64encode(c.compress(raw) + c.flush()).decode("ascii")


def decode_rows(b64: str):
    packed = base64.b64decode(b64)
    plain = zlib.decompressobj(-zlib.MAX_WBITS).decompress(packed)
    return json.loads(plain.decode("utf-8"))


def read_zmap_rows(model_or_defn: Path):
    p = Path(model_or_defn)
    defn = p / "definition" if (p / "definition").is_dir() else p
    text = (defn / "tables" / f"{ZMAP_TABLE}.tmdl").read_text(encoding="utf-8")
    found = re.findall(r'Binary\.FromText\(\s*"([A-Za-z0-9+/=]+)"', text)
    assert len(found) == 1, f"expected one encoded string, found {len(found)}"
    return decode_rows(found[0])


def zmap_file(model_or_defn: Path) -> Path:
    p = Path(model_or_defn)
    defn = p / "definition" if (p / "definition").is_dir() else p
    return defn / "tables" / f"{ZMAP_TABLE}.tmdl"


_ZMAP = (
    "table '" + ZMAP_TABLE + "'\n"
    "\tlineageTag: 00000000-aaaa-bbbb-cccc-000000000001\n"
    "\n"
    "\tcolumn 'Analysis Category'\n"
    "\t\tdataType: string\n"
    "\t\tlineageTag: 00000000-aaaa-bbbb-cccc-000000000002\n"
    "\t\tsummarizeBy: none\n"
    "\t\tsourceColumn: Analysis Category\n"
    "\n"
    "\t\tannotation SummarizationSetBy = Automatic\n"
    "\n"
    "\tpartition '" + ZMAP_TABLE + "' = m\n"
    "\t\tmode: import\n"
    "\t\tsource =\n"
    "\t\t\t\tlet\n"
    "\t\t\t\t    Source = Table.FromRows(Json.Document(Binary.Decompress("
    "Binary.FromText(\"{b64}\", BinaryEncoding.Base64), Compression.Deflate)), "
    "let _t = ((type nullable text) meta [Serialized.Text = true]) in type "
    "table [#\"Analysis Category\" = _t, #\"Analysis Subcategory\" = _t, "
    "category_x_id = _t])\n"
    "\t\t\t\tin\n"
    "\t\t\t\t    Source\n"
    "\n"
    "\tannotation PBI_Id = 00000000000000000000000000000001\n"
)


def zmap_tmdl(rows) -> str:
    return _ZMAP.replace("{b64}", encode_rows(rows))


def install_zmap(model_dir: Path, rows) -> None:
    """Write the encoded mapping table into a model and register it in
    model.tmdl (once)."""
    defn = Path(model_dir) / "definition"
    (defn / "tables" / f"{ZMAP_TABLE}.tmdl").write_text(
        zmap_tmdl(rows), encoding="utf-8")
    m = defn / "model.tmdl"
    text = m.read_text(encoding="utf-8")
    ref = f"ref table '{ZMAP_TABLE}'"
    if ref not in text:
        m.write_text(text.rstrip("\n") + "\n" + ref + "\n", encoding="utf-8")


# ───────────────────────── drift-doctor API ─────────────────────────

def capture(fleet_yml: Path) -> dict:
    from tmdl_drift_doctor.baseline import capture as _capture
    from tmdl_drift_doctor.fleet import load_fleet
    return _capture(load_fleet(fleet_yml))


def ledger_entries(fleet_yml: Path) -> list:
    from tmdl_drift_doctor.fleet import load_fleet
    from tmdl_drift_doctor.ledger import Ledger
    return Ledger(load_fleet(fleet_yml).ledger_path).entries()


def detect(fleet_yml: Path, kind: str | None = None, model: str | None = None):
    """Findings from the Python API (no freshness check: the live template is
    read as it is on disk, like the original collect())."""
    from tmdl_drift_doctor.baseline import load_baseline
    from tmdl_drift_doctor.detect import detect_fleet
    from tmdl_drift_doctor.fleet import load_fleet
    from tmdl_drift_doctor.ledger import Ledger
    fleet = load_fleet(fleet_yml)
    findings = detect_fleet(fleet, load_baseline(fleet),
                            Ledger(fleet.ledger_path), model_filter=model)
    if kind:
        findings = [f for f in findings if f.kind == kind]
    return findings


def detect_and_apply(fleet_yml: Path, kind_prefix: str = "retire.",
                     model: str | None = None):
    """detect + remediate(sync=True) for every finding under `kind_prefix`.
    Returns (findings, RunResult)."""
    from tmdl_drift_doctor.baseline import load_baseline
    from tmdl_drift_doctor.detect import detect_fleet
    from tmdl_drift_doctor.fleet import load_fleet
    from tmdl_drift_doctor.ledger import Ledger
    from tmdl_drift_doctor.remediate import remediate
    fleet = load_fleet(fleet_yml)
    baseline = load_baseline(fleet)
    findings = detect_fleet(fleet, baseline, Ledger(fleet.ledger_path),
                            model_filter=model)
    result = remediate(fleet, findings, kind=kind_prefix, dry_run=False,
                       sync=True, baseline=baseline)
    return findings, result


def applied(result, kind: str) -> list:
    return [o for o in result.outcomes
            if o.status == "applied" and o.finding.kind == kind]


def digest(fleet_yml: Path, tmp_path: Path, kind_prefix: str, name="digest.json"):
    """The decision digest of a dry --sync run (`remediate --digest-json`)."""
    from conftest import drift
    out = Path(tmp_path) / name
    r = drift("remediate", "--sync", "--dry-run", "--kind", kind_prefix,
              "--digest-json", out, fleet=fleet_yml)
    assert r.exit_code in (0, 1), r.output
    assert out.exists(), f"no digest written: {r.output}"
    data = json.loads(out.read_text(encoding="utf-8"))
    return data["entries"] if isinstance(data, dict) else data


def git_commit_all(root: Path, message: str = "fixture") -> None:
    """Commit everything under `root` (initialising the repo once), so the
    tool's template-tree provenance has a value."""
    root = Path(root)
    git = ["git", "-C", str(root), "-c", "user.email=fixture@example.com",
           "-c", "user.name=fixture", "-c", "commit.gpgsign=false"]
    if not (root / ".git").exists():
        subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", message],
                   check=True)


def template_tree(template_dir: Path) -> str:
    r = subprocess.run(["git", "-C", str(template_dir), "rev-parse", "HEAD:./"],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()

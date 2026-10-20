"""Helpers for the batch b6-change ported tests (change detection, the
total-only regression ratchet and the pristine-category purge).

Everything here is test scaffolding written for the port; the behaviour under
test is reached only through drift-doctor's CLI and public API.

* ``ROASTERY`` is the fictional coffee-roastery stand-in fleet
  (``tests/fixtures/b6-change/roastery/<variant>/Roastery.SemanticModel``):
  ``template`` plus derived variants ``clean``, ``drift`` (one gap of each
  kind), ``divergent`` (Roasts reads another source view and carries one extra
  column), and the regression family ``regress`` / ``regress_more`` /
  ``regress_shift``.
* ``mini_model`` writes a tiny one-table model for the column-override tests.
* ``zmap_*`` encode / decode the encoded entered-data mapping table (base64 of
  a raw-DEFLATE JSON row list) so a test can install one and read it back.
* ``axis_entries`` lists ``(identifier, display, target)`` for each live row of
  a field-parameter table (commented rows skipped).
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import zlib
from pathlib import Path

from conftest import FIXTURES, write_fleet

ROASTERY = FIXTURES / "b6-change" / "roastery"
MODEL_DIR = "Roastery.SemanticModel"
SYNC_ENGINE = FIXTURES / "sync_engine"
UNI_SRC = SYNC_ENGINE / "universal_mini" / "Performance.SemanticModel"
GRO_SRC = SYNC_ENGINE / "growth_mini" / "Growth.SemanticModel"
EXTRAS = SYNC_ENGINE / "extras"

ZMAP_TABLE = "z-Map Dimension-Measure Groupings"

GAP_KINDS = ["measure.missing", "measure.expression_drift", "column.missing",
             "relationship.missing", "relationship.modified", "table.missing"]

# The template's source function, as the user's fleet configures it
# (G-source-divergence-guard parity defaults).
SOURCE_IDENTITY = {"function": "source_filter_dates", "db_parameter": "v-db"}


# ───────────────────────── roastery fleet ─────────────────────────

def roastery_fleet(tmp_path: Path, models: dict, **keys) -> Path:
    """Copy the template and the named variants into tmp_path and write a
    fleet.yml. ``models`` maps a fleet model name to a variant name (or to a
    long-form dict with ``variant`` plus fleet keys such as ``overrides``)."""
    root = tmp_path / "fleet"
    root.mkdir(exist_ok=True)
    shutil.copytree(ROASTERY / "template" / MODEL_DIR,
                    root / "template" / MODEL_DIR)
    entries = {}
    for name, spec in models.items():
        spec = dict(spec) if isinstance(spec, dict) else {"variant": spec}
        variant = spec.pop("variant")
        dst = root / "models" / name / MODEL_DIR
        shutil.copytree(ROASTERY / variant / MODEL_DIR, dst)
        entries[name] = dict(spec, path=dst) if spec else dst
    keys.setdefault("allowlist", {"kinds": GAP_KINDS})
    keys.setdefault("source_identity", SOURCE_IDENTITY)
    return write_fleet(root, root / "template" / MODEL_DIR, entries, **keys)


def swap_variant(fleet_yml: Path, model: str, variant: str) -> None:
    """Replace a derived model's files with another stand-in variant (the
    fleet entry keeps its name and path)."""
    dst = Path(fleet_yml).parent / "models" / model / MODEL_DIR
    shutil.rmtree(dst)
    shutil.copytree(ROASTERY / variant / MODEL_DIR, dst)


# ───────────────────────── tiny models ─────────────────────────

def mini_model(root: Path, table: str, columns: list) -> Path:
    """Write ``root/Mini.SemanticModel`` holding one import table."""
    model = Path(root) / "Mini.SemanticModel"
    tables = model / "definition" / "tables"
    tables.mkdir(parents=True, exist_ok=True)
    quoted = f"'{table}'" if (" " in table or table[0] in "#_-") \
        and not table.startswith("__") else table
    (model / "definition" / "model.tmdl").write_text(
        f"model Model\n\tculture: en-US\n\nref table {quoted}\n",
        encoding="utf-8")
    (model / "definition" / "database.tmdl").write_text(
        "database\n\tcompatibilityLevel: 1601\n", encoding="utf-8")
    lines = [f"table {quoted}", "\tlineageTag: 0b6c0000-0000-0000-0000-000000000001", ""]
    for i, col in enumerate(columns, start=2):
        cq = f"'{col}'" if " " in col else col
        lines += [f"\tcolumn {cq}", "\t\tdataType: string",
                  f"\t\tlineageTag: 0b6c0000-0000-0000-0000-{i:012d}",
                  "\t\tsummarizeBy: none", f"\t\tsourceColumn: {col}", "",
                  "\t\tannotation SummarizationSetBy = Automatic", ""]
    lines += [f"\tpartition {quoted} = m", "\t\tmode: import", "\t\tsource =",
              "\t\t\t\tlet", '\t\t\t\t    Source = #table({}, {})', "\t\t\t\tin",
              "\t\t\t\t    Source", ""]
    (tables / f"{table}.tmdl").write_text("\n".join(lines), encoding="utf-8")
    return model


# ───────────────────────── encoded mapping table ─────────────────────────

def zmap_encode(rows: list) -> str:
    raw = json.dumps(rows, separators=(",", ":"), ensure_ascii=True).encode()
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    return base64.b64encode(comp.compress(raw) + comp.flush()).decode("ascii")


def zmap_decode(b64: str) -> list:
    return json.loads(zlib.decompress(base64.b64decode(b64), -15).decode())


_BLOB = re.compile(r'Binary\.FromText\(\s*"([A-Za-z0-9+/=]+)"')


def zmap_rows(table_file: Path) -> list:
    blobs = _BLOB.findall(Path(table_file).read_text(encoding="utf-8"))
    assert len(blobs) == 1, f"expected one encoded blob, found {len(blobs)}"
    return zmap_decode(blobs[0])


def zmap_tmdl(rows: list) -> str:
    b64 = zmap_encode(rows)
    return (
        f"table '{ZMAP_TABLE}'\n"
        "\tlineageTag: 00000000-aaaa-bbbb-cccc-000000000001\n\n"
        "\tcolumn 'Analysis Category'\n"
        "\t\tdataType: string\n"
        "\t\tlineageTag: 00000000-aaaa-bbbb-cccc-000000000002\n"
        "\t\tsummarizeBy: none\n"
        "\t\tsourceColumn: Analysis Category\n\n"
        "\t\tannotation SummarizationSetBy = Automatic\n\n"
        f"\tpartition '{ZMAP_TABLE}' = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\t\tlet\n"
        "\t\t\t\t    Source = Table.FromRows(Json.Document(Binary.Decompress("
        f'Binary.FromText("{b64}", BinaryEncoding.Base64), Compression.Deflate)), '
        "let _t = ((type nullable text) meta [Serialized.Text = true]) in type "
        'table [#"Analysis Category" = _t, #"Analysis Subcategory" = _t, '
        "category_x_id = _t])\n"
        "\t\t\t\tin\n"
        "\t\t\t\t    Source\n\n"
        "\tannotation PBI_Id = 00000000000000000000000000000001\n"
    )


def install_zmap(definition: Path, rows: list) -> None:
    (Path(definition) / "tables" / f"{ZMAP_TABLE}.tmdl").write_text(
        zmap_tmdl(rows), encoding="utf-8")


# ───────────────────────── field-parameter rows ─────────────────────────

_ROW = re.compile(
    r"""^\s*\(\s*"(?P<display>[^"]*)"\s*,\s*NAMEOF\s*\(\s*
        (?:'(?P<qt>[^']*)'|(?P<bt>[A-Za-z_][\w ]*?))?\s*
        \[(?P<target>[^\]]*)\]\s*\)\s*,\s*[^,]*,\s*"(?P<ident>[^"]*)"
    """, re.X)


def axis_entries(axis_file: Path) -> list:
    out = []
    for line in Path(axis_file).read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("//"):
            continue
        m = _ROW.match(line)
        if m:
            out.append((m["ident"], m["display"], m["target"]))
    return out


def insert_axis_row(axis_file: Path, row: str, after_contains: str) -> None:
    from tmdl_drift_doctor import tmdl_edit as te
    lines = Path(axis_file).read_text(encoding="utf-8").split("\n")
    anchor = next(i for i, ln in enumerate(lines) if after_contains in ln)
    lines.insert(anchor + 1, row)
    content = "\n".join(lines)
    te.assert_tmdl_valid(content)
    Path(axis_file).write_text(content, encoding="utf-8")

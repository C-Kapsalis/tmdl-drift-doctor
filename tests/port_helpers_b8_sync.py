"""Helpers for the ported underlay-sync fixture suite (batch b8-sync).

The original suite drove an internal batch-sync engine (context builder,
provider collection, batch driver). The port drives drift-doctor instead:

* a ``fleet.yml`` is written per test in ``tmp_path`` over copies of the two
  fixture models (template = ``universal_mini``, derived model =
  ``growth_mini``, plus its ``Mini.Report``);
* ``drift-doctor capture`` records the (possibly mutated) template;
* findings come from ``detect`` (Python API, plus ``detect --json`` for the
  report fields);
* the batch apply is ``tmdl_drift_doctor.remediate.remediate`` gated by the
  fleet allowlist, and the counters are read from its ``RunResult``.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from conftest import FIXTURES, detect_all, drift, drift_json, write_fleet

FX = FIXTURES / "sync_engine"
UNI_SRC = FX / "universal_mini" / "Performance.SemanticModel"
GRO_SRC = FX / "growth_mini" / "Growth.SemanticModel"
REP_SRC = FX / "growth_mini" / "Mini.Report"
EXTRAS = FX / "extras"

MODEL = "fixture_growth"
OVERLAY_FILES = ("tables/Cohort Performance.tmdl", "tables/Growth Measures.tmdl")

# Kinds the batch sync applies. Original categories -> drift-doctor kinds:
# rename.drift -> measure.renamed, measure.missing.* -> measure.missing,
# expression.cascade -> expression.drift, fieldparam.x/y* -> fieldparam.
# Matching is dotted-prefix (drift-rulings R4).
SYNC_KINDS = (
    "measure.renamed",
    "table.missing",
    "measure.missing",
    "measure.expression_drift",
    "column.missing",
    "relationship.missing",
    "relationship.modified",
    "expression.drift",
    "fieldparam",
)

SOURCE_IDENTITY = {"function": "source_filter_dates", "db_parameter": "v-db"}


def kind_admitted(kind: str, allowed) -> bool:
    """Dotted-prefix match (drift-rulings R4)."""
    return any(kind == p.rstrip(".") or kind.startswith(p.rstrip(".") + ".")
               for p in allowed)


def is_sync_kind(kind: str) -> bool:
    # model-intrinsic heads are noise on the minis, never part of the
    # underlay-diff scenarios (same exclusion as the original).
    if kind.split(".")[0] in ("visibility", "ffilter", "ifilter"):
        return False
    return kind_admitted(kind, SYNC_KINDS)


def sync_kinds(findings) -> list:
    """Sorted kinds of the sync-relevant findings (advisory and overridden
    findings excluded)."""
    return sorted(f.kind for f in findings
                  if not f.advisory and not f.overridden
                  and is_sync_kind(f.kind))


# ─── fixture pair ───

def copy_pair(tmp_path: Path):
    """Copy both minis + report into tmp; return (uni_defn, growth_model)."""
    uni_model = tmp_path / "universal_mini" / "Performance.SemanticModel"
    growth_model = tmp_path / "growth_mini" / "Growth.SemanticModel"
    shutil.copytree(UNI_SRC, uni_model)
    shutil.copytree(GRO_SRC, growth_model)
    shutil.copytree(REP_SRC, growth_model.parent / "Mini.Report")
    return uni_model / "definition", growth_model


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def overlay_hashes(growth_defn: Path) -> dict:
    return {rel: sha(growth_defn / rel) for rel in OVERLAY_FILES}


def assert_overlay_untouched(before: dict, growth_defn: Path) -> None:
    after = overlay_hashes(growth_defn)
    changed = [rel for rel in before if before[rel] != after[rel]]
    assert not changed, (
        f"OVERLAY files were modified by the sync: {changed} — the engine "
        f"must never touch growth-only files")


def tree_hashes(root: Path) -> dict:
    return {str(p.relative_to(root)): sha(p)
            for p in sorted(Path(root).rglob("*")) if p.is_file()}


# ─── fleet + run ───

def pair_fleet(tmp_path: Path, allowed=SYNC_KINDS, overrides=None,
               source_divergent=None) -> Path:
    entry = {"path": tmp_path / "growth_mini" / "Growth.SemanticModel"}
    if overrides:
        entry["overrides"] = overrides
    if source_divergent:
        entry["source_divergent"] = list(source_divergent)
    return write_fleet(
        tmp_path,
        tmp_path / "universal_mini" / "Performance.SemanticModel",
        {MODEL: entry},
        state_dir=".drift-doctor",
        source_identity=dict(SOURCE_IDENTITY),
        allowlist={"kinds": list(allowed), "expressions": ["v-fromdate"]},
    )


def capture_and_detect(tmp_path: Path, allowed=SYNC_KINDS, overrides=None):
    """Write fleet.yml, capture the template, detect. Returns
    (fleet, findings, report) where report is the parsed `detect --json`."""
    from tmdl_drift_doctor.fleet import load_fleet
    fy = pair_fleet(tmp_path, allowed=allowed, overrides=overrides)
    r = drift("capture", fleet=fy)
    assert r.exit_code == 0, r.output
    _r, report = drift_json("detect", fleet=fy)
    assert report is not None, _r.output
    fleet = load_fleet(fy)
    return fleet, detect_all(fleet), report


@dataclass
class SyncRun:
    fleet: object
    findings: list
    report: dict
    result: object
    applied_items: list = field(default_factory=list)

    def count(self, *statuses) -> int:
        return sum(self.result.count(s) for s in statuses)

    @property
    def applied(self) -> int:
        return self.count("applied")

    @property
    def failed(self) -> int:
        return self.count("failed")

    def model_report(self) -> dict:
        return self.report["models"][MODEL]


def run_sync(tmp_path: Path, allowed=SYNC_KINDS, overrides=None) -> SyncRun:
    """capture + detect + batch remediate (allowlist = `allowed`)."""
    from tmdl_drift_doctor.baseline import load_baseline
    from tmdl_drift_doctor.remediate import remediate
    fleet, findings, report = capture_and_detect(
        tmp_path, allowed=allowed, overrides=overrides)
    result = remediate(fleet, findings, baseline=load_baseline(fleet))
    applied = [(o.finding.kind, o.finding.ref) for o in result.outcomes
               if o.status == "applied"]
    return SyncRun(fleet, findings, report, result, applied)


def count_report_refs(roots, measure: str) -> int:
    """The report reference counter drift-doctor shares with pbi-plot-styler
    (G-report-blast-radius A: one implementation, owned by the styler)."""
    from pbi_plot_styler.rename import count_measure_refs
    return count_measure_refs([Path(r) for r in roots], measure)

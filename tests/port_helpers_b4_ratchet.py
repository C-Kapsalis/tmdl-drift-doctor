"""Helpers for the ported ratchet / advisory-gating tests (batch b4-ratchet).

The original tests wrote a ``remediate.py --detect-json`` dump and a floors
file to tmp_path and ran the CI ratchet scripts as subprocesses. Here the
same synthetic data is written in drift-doctor's shapes and judged by
``drift-doctor ratchet check|tighten --from-json`` (H-ratchet-check,
H-ratchet-tighten-scoped port notes):

* report: ``{"schema_version", "generated_at", "template_tree", "scope":
  {fleet, models, alternate_model, targets, kind_filter, overrides},
  "errors": [...], "models": {name: {"gating", "findings": [{kind, ref,
  remediable, actionable}], "overridden": [], "errors": n}}}``.
  Original ``provider_errors: N`` -> the model's ``errors`` (plus one entry per
  model in the top-level ``errors`` list); ``scope.category`` ->
  ``kind_filter``; ``requested`` -> ``models``; ``model: true`` ->
  ``alternate_model``; ``tmdl_commit`` -> ``template_tree``.
* floors: ``<state_dir>/floors.json`` = ``{"version": 2, <header>, "models":
  {name: {"total": n, "by_category": {head: n}}}}``; the original
  ``clients`` key is ``models`` and ``_meta`` became the document header
  (``detect_tmdl_commit`` -> ``template_tree``).
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import drift, write_fleet

from tmdl_drift_doctor.detect import JSON_SCHEMA_VERSION

MODEL_TMDL = "model Model\n\tculture: en-US\n"


def item(kind: str, ref: str, *, remediable: bool = True) -> dict:
    """One finding (original: ``{"category", "object_id", ...}``)."""
    return {"kind": kind, "ref": ref, "detail": "", "remediable": remediable,
            "actionable": remediable, "advisory": False}


def fleet_scope(targets: list, **kw) -> dict:
    scope = {"fleet": True, "models": None, "alternate_model": None,
             "targets": list(targets), "kind_filter": None, "overrides": None}
    scope.update(kw)
    return scope


def scoped_scope(requested: str, targets: list, **kw) -> dict:
    scope = {"fleet": False, "models": [requested], "alternate_model": None,
             "targets": list(targets), "kind_filter": None, "overrides": None}
    scope.update(kw)
    return scope


def write_report(path: Path, items_by_model: dict, *, scope="fleet",
                 provider_errors: dict | None = None,
                 template_tree: str = "cafe1234",
                 generated_at: str = "2026-08-07T00:00:00+00:00") -> Path:
    """``items_by_model``: {model: [item, ...]}. ``scope="fleet"`` builds a
    fleet scope whose targets are the report's models; ``scope=None`` omits
    the block (pre-scope reports)."""
    provider_errors = provider_errors or {}
    doc = {
        "schema_version": JSON_SCHEMA_VERSION,
        "generated_at": generated_at,
        "template_tree": template_tree,
        "errors": [{"model": m, "error": f"{n} provider error(s)"}
                   for m, n in sorted(provider_errors.items()) if n],
        "models": {
            m: {"gating": "enforced", "findings": items, "overridden": [],
                "errors": provider_errors.get(m, 0)}
            for m, items in items_by_model.items()
        },
    }
    if scope == "fleet":
        doc["scope"] = fleet_scope(sorted(items_by_model))
    elif scope is not None:
        doc["scope"] = scope
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def counted_report(path: Path, counts: dict, *, kind: str = "measure.missing",
                   **kw) -> Path:
    """Original ``_dump(path, {cid: item_count})`` of ``measure.missing``."""
    return write_report(path, {m: [item(kind, f"m{i}") for i in range(n)]
                               for m, n in counts.items()}, **kw)


def floors_path(fleet_yml: Path) -> Path:
    return Path(fleet_yml).parent / ".drift-doctor" / "floors.json"


def write_floors(fleet_yml: Path, models: dict, *, header: dict | None = None,
                 version: int = 2) -> Path:
    """``models``: {name: {"total": n, "by_category": {...}}} written as-is."""
    doc = {"version": version}
    doc.update(header or {})
    doc["models"] = models
    p = floors_path(fleet_yml)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def measure_floors(fleet_yml: Path, totals: dict, *,
                   header: dict | None = None) -> Path:
    """Original scope-test ``_floors``: ``{total: n, by_category: {measure: n}}``
    (empty map when n is 0) with a policy/updated_at/commit header."""
    if header is None:
        header = {"policy": "test", "updated_at": "2026-01-01T00:00:00+00:00",
                  "template_tree": "0ldc0mm1t"}
    return write_floors(fleet_yml, {
        m: {"total": n, "by_category": ({"measure": n} if n else {})}
        for m, n in totals.items()}, header=header)


def read_floors(fleet_yml: Path) -> dict:
    return json.loads(floors_path(fleet_yml).read_text(encoding="utf-8"))


def make_fleet(root: Path, names, *, advisory: dict | None = None,
               allowlist_kinds=("measure.missing",)) -> Path:
    """A minimal fleet in ``root``: a template plus one tiny model per name.
    ``advisory``: {name: gating_reason}. Returns the fleet.yml path."""
    root = Path(root)
    advisory = advisory or {}

    def mk(rel: str) -> Path:
        d = root / rel / "definition"
        d.mkdir(parents=True, exist_ok=True)
        (d / "model.tmdl").write_text(MODEL_TMDL, encoding="utf-8")
        return root / rel

    template = mk("template/BikeShop.SemanticModel")
    models = {}
    for n in names:
        p = mk(f"stores/{n}/BikeShop.SemanticModel")
        if n in advisory:
            models[n] = {"path": p, "gating": "advisory",
                         "gating_reason": advisory[n]}
        else:
            models[n] = p
    return write_fleet(root, template, models, state_dir=".drift-doctor",
                       allowlist={"kinds": list(allowlist_kinds)})


def check(fleet_yml: Path, report: Path, *extra):
    return drift("ratchet check", "--from-json", report, *extra, fleet=fleet_yml)


def tighten(fleet_yml: Path, report: Path, *extra):
    return drift("ratchet tighten", "--from-json", report, *extra,
                 fleet=fleet_yml)

"""The drift ratchet: fleet drift may only go down.

``<state_dir>/floors.json`` records, per derived model, the number of
actionable findings (total and per kind) the fleet has agreed to carry.
``ratchet check`` fails when any model rises above its floor;
``ratchet tighten`` lowers each floor to ``min(floor, current)`` and never
raises one (raising a floor is a deliberate human edit of the file, made in
the change that justifies it).

Two rules are stricter than a floor:

* **resurrections are hard-zero.** A ``retire.*`` finding means an object
  the ledger proves was retired from the template is back in a derived
  model. That is not backlog that can be paid down later, so it is never
  counted into a floor and always fails the check, by name. If a model must
  keep an ex-template object, declare a ``retain`` override instead.
* **provenance is checked.** Floors are only ever written from a full-fleet,
  unfiltered, error-free detection run: a scoped or kind-filtered dump says
  nothing about the models or kinds it left out, and a run with parse errors
  undercounts.

Models with ``gating: advisory`` are reported in their own block and never
fail the check (resurrections included).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import DriftDoctorError
from .detect import JSON_SCHEMA_VERSION

HARD_ZERO_PREFIX = "retire."
FLOORS_VERSION = 1


def report_problems(report: dict, *, writing: bool) -> list:
    """Reasons a detect report cannot be trusted for the ratchet. A
    model-scoped report may be CHECKED (only its models are compared) but
    never used to WRITE floors."""
    problems = []
    if report.get("schema_version") != JSON_SCHEMA_VERSION:
        problems.append(
            f"report schema_version is {report.get('schema_version')!r}; the "
            f"ratchet needs {JSON_SCHEMA_VERSION} (re-run `drift-doctor "
            f"detect --json` with this version)")
        return problems
    scope = report.get("scope") or {}
    if scope.get("kind_filter"):
        problems.append(
            f"the report is kind-filtered (--kind {scope['kind_filter']!r}); "
            f"per-model floors can never be compared to a filtered count")
    if writing and not scope.get("fleet"):
        problems.append(
            "the report is model-scoped; floors are only written from a "
            "full-fleet run (silence about the other models means nothing)")
    if report.get("errors"):
        names = ", ".join(e.get("model", "?") for e in report["errors"])
        problems.append(
            f"the report carries detection errors ({names}); its counts are "
            f"silently reduced. Fix the model(s) and re-run")
    return problems


def counts(report: dict) -> dict:
    """{model: {"total": n, "by_kind": {kind: n}}} of actionable findings,
    resurrections excluded (they are never floored)."""
    out = {}
    for name, body in (report.get("models") or {}).items():
        by_kind: dict = {}
        for f in body.get("findings") or ():
            if not f.get("actionable") or f["kind"].startswith(HARD_ZERO_PREFIX):
                continue
            by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
        out[name] = {"total": sum(by_kind.values()),
                     "by_kind": dict(sorted(by_kind.items()))}
    return out


def resurrections(report: dict) -> dict:
    """{model: [finding]} for every actionable ``retire.*`` finding."""
    out = {}
    for name, body in (report.get("models") or {}).items():
        hits = [f for f in body.get("findings") or ()
                if f.get("actionable") and f["kind"].startswith(HARD_ZERO_PREFIX)]
        if hits:
            out[name] = hits
    return out


def load_floors(path: Path) -> Optional[dict]:
    path = Path(path)
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise DriftDoctorError(f"{path}: not valid JSON ({e})") from e
    if doc.get("version") != FLOORS_VERSION or not isinstance(
            doc.get("models"), dict):
        raise DriftDoctorError(
            f"{path}: not a drift-doctor floors file (expected version "
            f"{FLOORS_VERSION} with a `models` mapping)")
    return doc


def save_floors(path: Path, doc: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


@dataclass
class CheckResult:
    violations: list = field(default_factory=list)
    advisories: list = field(default_factory=list)   # advisory-gated models
    improved: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def check(report: dict, floors_doc: dict, gating: dict) -> CheckResult:
    res = CheckResult()
    floors = floors_doc.get("models") or {}
    current = counts(report)
    raised = resurrections(report)
    for name in sorted(current):
        found = []
        for f in raised.get(name, ()):
            found.append(f"{name}: resurrection {f['kind']} {f['ref']} (a "
                         f"ledger-retired object is back; hard zero, never "
                         f"floored)")
        cur = current[name]
        floor = floors.get(name)
        if floor is None:
            found.append(f"{name}: no floor recorded; record one with "
                         f"`drift-doctor ratchet tighten --allow-new`")
        else:
            if cur["total"] > floor.get("total", 0):
                found.append(f"{name}: total rose {floor.get('total', 0)} -> "
                             f"{cur['total']}")
            fk = floor.get("by_kind") or {}
            for kind, n in cur["by_kind"].items():
                if n > fk.get(kind, 0):
                    found.append(f"{name}: {kind} rose {fk.get(kind, 0)} -> {n}")
            if cur["total"] < floor.get("total", 0):
                res.improved.append(f"{name}: {floor['total']} -> {cur['total']}")
        if gating.get(name) == "advisory":
            res.advisories.extend(found)
        else:
            res.violations.extend(found)
    scope = report.get("scope") or {}
    unseen = sorted(set(floors) - set(current))
    if unseen and not scope.get("fleet"):
        res.notes.append(f"model-scoped report: {len(current)} of "
                         f"{len(floors)} floor(s) evaluated")
    elif unseen:
        res.notes.append("floor(s) for model(s) no longer in the fleet: "
                         + ", ".join(unseen)
                         + " (drop them with `ratchet tighten --prune`)")
    return res


def tighten(report: dict, floors_doc: Optional[dict], *, allow_new: bool,
            prune: bool, fleet_models) -> tuple:
    """(new_doc, changes). Floors only ever go down; a model without a
    floor is recorded at its current counts only when seeding a new file or
    with `allow_new`; `prune` drops floors of models no longer in the fleet."""
    seeding = floors_doc is None
    old = dict((floors_doc or {}).get("models") or {})
    new = {k: {"total": v.get("total", 0),
               "by_kind": dict(v.get("by_kind") or {})} for k, v in old.items()}
    changes = []
    for name, cur in sorted(counts(report).items()):
        floor = new.get(name)
        if floor is None:
            if seeding or allow_new:
                new[name] = cur
                changes.append(f"{name}: recorded at {cur['total']}")
            else:
                changes.append(f"{name}: no floor (pass --allow-new to record "
                               f"it at {cur['total']})")
            continue
        total = min(floor["total"], cur["total"])
        by_kind = {}
        for kind in sorted(set(floor["by_kind"]) | set(cur["by_kind"])):
            n = min(floor["by_kind"].get(kind, 0), cur["by_kind"].get(kind, 0))
            if n:
                by_kind[kind] = n
        if total != floor["total"] or by_kind != floor["by_kind"]:
            changes.append(f"{name}: {floor['total']} -> {total}")
        new[name] = {"total": total, "by_kind": by_kind}
    if prune:
        for name in sorted(set(new) - set(fleet_models)):
            del new[name]
            changes.append(f"{name}: pruned (no longer in the fleet)")
    doc = {
        "version": FLOORS_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "template_tree": report.get("template_tree"),
        "models": new,
    }
    return doc, changes

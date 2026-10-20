"""The drift ratchet: fleet drift may only go down.

``<state_dir>/floors.json`` records, per derived model, how much drift the
fleet has agreed to carry: a ``total`` and a count per kind *head* (the text
before the first dot: ``measure.missing`` and ``measure.expression_drift``
both roll up to ``measure``). ``ratchet check`` fails when a model rises
above its floor; ``ratchet tighten`` lowers each floor to
``min(floor, current)`` and never raises one (raising a floor is a
deliberate human edit, made in the change that justifies it).

What counts is every finding a sync could act on (a remediable kind),
whatever ``allowlist.kinds`` says, minus overridden findings, advisory and
report-only kinds, and model-intrinsic regenerations. The allowlist decides
what ``remediate`` writes, never what the ratchet counts.

Resurrections are hard zero: a ``retire.*`` finding means an object the
ledger proves was retired from the template is back. It is never counted
into a floor, always fails (in its own ``RESURRECTION`` block, by name), and
a floors file that grants a ``retire`` allowance is itself a violation.

Floors are only written from a report the ratchet can trust (a scope block,
no kind filter, no alternate model or overrides file, no detection errors);
a model-scoped report may lower its own models' floors but never prune.
Models with ``gating: advisory`` are reported in an ``ADVISORY`` block and
never fail the check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import DriftDoctorError
from .detect import JSON_SCHEMA_VERSION

HARD_ZERO = "retire"
INTRINSIC = ("ifilter", "visibility", "ffilter.coverage_gap")
FLOORS_VERSION = 2
TOTALS_FILE = "drift_totals.json"


def head(kind: str) -> str:
    return kind.split(".", 1)[0]


def _intrinsic(kind: str) -> bool:
    return any(kind == p or kind.startswith(p + ".") for p in INTRINSIC)


def _counted(f: dict) -> bool:
    kind = f.get("kind") or ""
    return (bool(f.get("remediable")) and not f.get("advisory")
            and not _intrinsic(kind) and head(kind) != HARD_ZERO
            and kind != "decision")


# ───────────────────────── report trust ─────────────────────────

def report_problems(report: dict, *, writing: bool, prune: bool = False) -> list:
    """Reasons a detect report cannot be trusted. A report without a scope
    block may be CHECKED (all its models are compared, no prune hint) but
    never used to WRITE floors."""
    if report.get("schema_version") != JSON_SCHEMA_VERSION:
        return [f"report schema_version is {report.get('schema_version')!r}; "
                f"the ratchet needs {JSON_SCHEMA_VERSION} (re-run `drift-doctor "
                f"detect --json` with this version)"]
    problems = []
    scope = report.get("scope")
    if scope is None:
        if writing:
            problems.append("the report has no scope block, so what it covered "
                            "is unknown; floors are never written from it")
        scope = {}
    if scope.get("kind_filter"):
        problems.append(f"the report is category-filtered (--kind "
                        f"{scope['kind_filter']!r}); floors are never compared "
                        f"with or written from a filtered count")
    if writing and scope.get("alternate_model"):
        problems.append(f"the report was run against an alternate model "
                        f"({scope['alternate_model']}): a --model-scoped "
                        f"secondary-model report never writes floors")
    if writing and scope.get("overrides"):
        problems.append(f"the report was produced with a non-default "
                        f"--overrides file ({scope['overrides']}); floors are "
                        f"only written from the fleet's own declarations")
    bad = sorted({e.get("model", "?") for e in report.get("errors") or []}
                 | {m for m, b in (report.get("models") or {}).items()
                    if (b or {}).get("errors")})
    if bad:
        problems.append(f"the report carries a provider error (detection "
                        f"failed) for {', '.join(bad)}; its counts are "
                        f"silently reduced. Fix the model(s) and re-run")
    if prune and not scope.get("fleet"):
        problems.append("--prune requires a FLEET dump: a scoped report says "
                        "nothing about the models it left out")
    return problems


# ───────────────────────── counts ─────────────────────────

def counts(report: dict) -> dict:
    """{model: {"total", "by_category": {head: n}, "by_kind": {kind: n}}}."""
    out = {}
    for name, body in (report.get("models") or {}).items():
        by_kind: dict = {}
        for f in (body or {}).get("findings") or ():
            if _counted(f):
                by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
        by_head: dict = {}
        for k, n in by_kind.items():
            by_head[head(k)] = by_head.get(head(k), 0) + n
        out[name] = {"total": sum(by_kind.values()),
                     "by_category": dict(sorted(by_head.items())),
                     "by_kind": dict(sorted(by_kind.items()))}
    return out


def resurrections(report: dict) -> dict:
    """{model: [finding]} for every remediable ``retire.*`` finding."""
    out = {}
    for name, body in (report.get("models") or {}).items():
        hits = [f for f in (body or {}).get("findings") or ()
                if head(f.get("kind") or "") == HARD_ZERO
                and f.get("remediable", True)]
        if hits:
            out[name] = hits
    return out


# ───────────────────────── floors file ─────────────────────────

def _normalize(entry: dict) -> dict:
    cats = dict(entry.get("by_category") or {})
    if not cats and entry.get("by_kind"):
        for k, n in (entry.get("by_kind") or {}).items():
            cats[head(k)] = cats.get(head(k), 0) + int(n)
    return {"total": int(entry.get("total", 0)),
            "by_category": {k: int(v) for k, v in sorted(cats.items())}}


def load_floors(path: Path) -> Optional[dict]:
    """The floors document with every model in head form (a legacy file
    keyed ``by_kind`` is rolled up on read), or None when absent."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise DriftDoctorError(f"{path}: not valid JSON ({e})") from e
    if not isinstance(doc, dict) or doc.get("version") not in (1, 2) or \
            not isinstance(doc.get("models"), dict):
        raise DriftDoctorError(
            f"{path}: not a drift-doctor floors file (expected version "
            f"{FLOORS_VERSION} with a `models` mapping)")
    doc = dict(doc)
    doc["models"] = {m: _normalize(e or {}) for m, e in doc["models"].items()}
    return doc


def allowances(doc: dict) -> list:
    """(model, value) for every floor that grants a retire allowance."""
    return [(m, e["by_category"][HARD_ZERO])
            for m, e in sorted((doc.get("models") or {}).items())
            if e["by_category"].get(HARD_ZERO)]


def save_floors(path: Path, doc: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


# ───────────────────────── check ─────────────────────────

@dataclass
class CheckResult:
    violations: list = field(default_factory=list)
    resurrected: list = field(default_factory=list)   # enforced, by line
    advisories: list = field(default_factory=list)
    improved: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    info: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.violations or self.resurrected)


def _gating(report: dict, gating: dict, name: str) -> str:
    if name in gating:
        return gating[name]
    return ((report.get("models") or {}).get(name) or {}).get(
        "gating", "enforced")


def check(report: dict, floors_doc: dict, gating: dict) -> CheckResult:
    res = CheckResult()
    floors = floors_doc.get("models") or {}
    current = counts(report)
    raised = resurrections(report)
    for name, value in allowances(floors_doc):
        line = (f"{name}: the floors file grants a retire allowance "
                f"({HARD_ZERO}: {value}); resurrections are hard zero. Remove "
                f"the key and remediate the model instead (`drift-doctor "
                f"remediate --model {name} --kind retire --sync`)")
        if _gating(report, gating, name) == "advisory":
            res.advisories.append(line)
        else:
            res.violations.append(line)
    for name in sorted(current):
        advisory = _gating(report, gating, name) == "advisory"
        block = []
        for f in raised.get(name, ()):
            block.append(f"{name}: {f['kind']} {f['ref']}")
        if block:
            block.append(f"{name}: cascade it with `drift-doctor remediate "
                         f"--model {name} --kind retire --sync`, or declare a "
                         f"`retain` override")
        found = []
        cur = current[name]
        floor = floors.get(name)
        if floor is None:
            found.append(f"{name}: no floor recorded; record one with "
                         f"`drift-doctor ratchet tighten --allow-new`")
        else:
            if cur["total"] > floor["total"]:
                found.append(f"{name}: TOTAL rose {floor['total']} -> "
                             f"{cur['total']}")
            fc = floor["by_category"]
            for h, n in cur["by_category"].items():
                if n > fc.get(h, 0):
                    found.append(f"{name}: category '{h}' rose "
                                 f"{fc.get(h, 0)} -> {n}")
            if cur["total"] < floor["total"]:
                res.improved.append(f"{name}: {floor['total']} -> "
                                    f"{cur['total']}")
        for h, n in cur["by_category"].items():
            kinds = ", ".join(f"{k} {v}" for k, v in cur["by_kind"].items()
                              if head(k) == h)
            res.info.append(f"{name}: {h} {n} ({kinds})")
        if advisory:
            res.advisories.extend(("RESURRECTION " + b) for b in block)
            res.advisories.extend(found)
        else:
            res.resurrected.extend(block)
            res.violations.extend(found)
    scope = report.get("scope")
    unseen = sorted(set(floors) - set(current))
    if scope is not None and not scope.get("fleet"):
        res.notes.append(f"model-scoped report: {len(current)} of "
                         f"{len(floors)} floors evaluated")
    elif scope is not None:
        targets = set(scope.get("targets") or [])
        for name in unseen:
            if name in targets:
                res.notes.append(f"{name}: detection FAILED; not a prune "
                                 f"candidate")
            else:
                res.notes.append(f"{name}: no longer in the fleet; prune it "
                                 f"with `ratchet tighten --prune`")
    return res


# ───────────────────────── tighten ─────────────────────────

def tighten(report: dict, floors_doc: Optional[dict], *, allow_new: bool,
            prune: bool, fleet_models) -> tuple:
    """(new doc or None when nothing changed, change lines, notes)."""
    seeding = floors_doc is None
    old = {m: _normalize(e) for m, e in
           ((floors_doc or {}).get("models") or {}).items()}
    new = {m: {"total": e["total"], "by_category": dict(e["by_category"])}
           for m, e in old.items()}
    changes, notes = [], []
    current = counts(report)
    for name, cur in sorted(current.items()):
        cur = {"total": cur["total"], "by_category": cur["by_category"]}
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
        cats = {}
        for h in sorted(set(floor["by_category"]) | set(cur["by_category"])):
            if h == HARD_ZERO:
                continue
            n = min(floor["by_category"].get(h, 0),
                    cur["by_category"].get(h, 0))
            if n:
                cats[h] = n
        if total != floor["total"] or cats != floor["by_category"]:
            changes.append(f"{name}: {floor['total']} -> {total}")
        new[name] = {"total": total, "by_category": cats}
    scope = report.get("scope") or {}
    fleet = bool(scope.get("fleet"))
    if prune:
        targets = set(scope.get("targets") or fleet_models)
        for name in sorted(set(new) - set(current)):
            if name in targets:
                notes.append(f"{name}: detection FAILED; kept (not a prune "
                             f"candidate)")
                continue
            del new[name]
            changes.append(f"{name}: pruned (no longer in the fleet)")
    if not seeding and new == old:
        return None, changes, notes
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    doc = {k: v for k, v in (floors_doc or {}).items()
           if k not in ("models", "version")}
    doc["version"] = FLOORS_VERSION
    if fleet or seeding:
        doc["updated_at"] = now
        doc["template_tree"] = report.get("template_tree")
    doc["last_tighten"] = {"scope": "fleet" if fleet else "scoped",
                           "models": sorted(current),
                           "template_tree": report.get("template_tree"),
                           "at": now}
    doc["models"] = new
    return doc, changes, notes


# ───────────────────────── total-only mode ─────────────────────────

COMPONENTS = {
    "cascade_gaps": ("table.missing", "measure.missing", "measure.wrong_host"),
    "expr_drift": ("measure.expression_drift",),
    "col_missing": ("column.missing",),
    "rel_missing": ("relationship.missing",),
    "rel_modified": ("relationship.modified",),
}


def totals(report: dict) -> dict:
    """{model: {component: n, "total": n}} of the five superset buckets."""
    out = {}
    for name, body in (report.get("models") or {}).items():
        row = {c: 0 for c in COMPONENTS}
        for f in (body or {}).get("findings") or ():
            for comp, kinds in COMPONENTS.items():
                if f.get("kind") in kinds:
                    row[comp] += 1
        row["total"] = sum(row[c] for c in COMPONENTS)
        out[name] = row
    return out

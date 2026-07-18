"""Drift detection — typed findings, one per divergence.

Detection is read-only: it compares each derived model (parsed live from
disk) against the committed template baseline plus the retirement record and
emits ``Finding`` objects. It never mutates a model; remediation is a
separate, opt-in step that consumes these findings.

The doctrine that shapes the kinds:

* the template is the canonical CORE — every template object must exist in
  every derived model, and shared objects must not silently diverge;
* a derived model may legitimately EXTEND beyond the template — extras on
  shared tables are advisory (``extra.*``), and derived-only tables are not
  drift at all;
* the only deletions ever proposed are of objects PROVABLY retired from the
  template (the ledger's ``retire.*`` channel).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .fleet import Fleet
from .ledger import Ledger
from .tmdl import Model, normalize_expression, parse_mapping_rows, parse_model

# Kinds remediation can apply (subject to the allowlist).
REMEDIABLE_KINDS = (
    "table.missing",
    "column.missing",
    "measure.missing",
    "measure.expression_drift",
    "measure.property_drift",
    "column.property_drift",
    "expression.drift",
    "mapping_row.missing",
    "retire.table",
    "retire.column",
    "retire.measure",
    "retire.mapping_row",
)

# Kinds that only inform — never remediated (deleting a derived model's own
# work is a human decision, full stop).
ADVISORY_KINDS = ("extra.measure", "extra.column")

# Deletions: only ever applied in --sync mode, only from the ledger.
RETIREMENT_FINDING_KINDS = ("retire.table", "retire.column",
                            "retire.measure", "retire.mapping_row")


@dataclass
class Finding:
    kind: str                 # e.g. "measure.expression_drift"
    model: str                # derived-model name from fleet.yml
    ref: str                  # object reference, e.g. "Visits[Total Visits]"
    template_value: Optional[str] = None
    derived_value: Optional[str] = None
    detail: str = ""
    data: dict = field(default_factory=dict)   # kind-specific extras

    @property
    def advisory(self) -> bool:
        return self.kind in ADVISORY_KINDS

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "model": self.model,
            "ref": self.ref,
            "template_value": self.template_value,
            "derived_value": self.derived_value,
            "detail": self.detail,
            "advisory": self.advisory,
        }


def _clip(value, n: int = 120) -> str:
    s = "" if value is None else str(value)
    return s if len(s) <= n else s[: n - 1] + "…"


def detect_model(name: str, fleet: Fleet, baseline: dict,
                 ledger: Ledger, model: Optional[Model] = None) -> list:
    """All findings for one derived model, ordered: structural gaps, drift,
    advisories, retirements."""
    model = model or parse_model(fleet.model_definition(name))
    live = _live_view(model, fleet.mapping_tables)
    retire = ledger.active_retirements(model=name)
    findings: list = []

    b_tables = baseline.get("tables") or {}

    # ── missing objects (template -> derived) ──
    for tname, body in b_tables.items():
        if tname not in live["tables"]:
            findings.append(Finding(
                kind="table.missing", model=name, ref=tname,
                detail=f"template table '{tname}' is absent from '{name}'"))
            continue
        derived = live["tables"][tname]
        for cname, props in (body.get("columns") or {}).items():
            ref = f"{tname}[{cname}]"
            if cname not in derived["columns"]:
                findings.append(Finding(
                    kind="column.missing", model=name, ref=ref,
                    detail=f"template column {ref} is absent from '{name}'"))
            elif derived["columns"][cname] != props:
                findings.append(Finding(
                    kind="column.property_drift", model=name, ref=ref,
                    template_value=_fmt_props(props),
                    derived_value=_fmt_props(derived["columns"][cname]),
                    detail=f"column {ref} properties differ from the template"))
        for mname, m in (body.get("measures") or {}).items():
            ref = f"{tname}[{mname}]"
            if mname not in derived["measures"]:
                findings.append(Finding(
                    kind="measure.missing", model=name, ref=ref,
                    detail=f"template measure {ref} is absent from '{name}'"))
                continue
            d = derived["measures"][mname]
            if d["expression"] != m["expression"]:
                findings.append(Finding(
                    kind="measure.expression_drift", model=name, ref=ref,
                    template_value=_clip(m["expression"]),
                    derived_value=_clip(d["expression"]),
                    detail=f"measure {ref} DAX differs from the template"))
            elif d["properties"] != m["properties"]:
                findings.append(Finding(
                    kind="measure.property_drift", model=name, ref=ref,
                    template_value=_fmt_props(m["properties"]),
                    derived_value=_fmt_props(d["properties"]),
                    detail=f"measure {ref} properties differ from the template"))

    # ── shared expressions (allowlist-named ONLY) ──
    b_exprs = baseline.get("expressions") or {}
    for ename in sorted(fleet.allowlist_expressions):
        if ename not in b_exprs:
            continue  # the template doesn't define it — nothing canonical
        t_norm = normalize_expression(b_exprs[ename])
        d_raw = live["expressions"].get(ename)
        if d_raw is None or normalize_expression(d_raw) != t_norm:
            findings.append(Finding(
                kind="expression.drift", model=name, ref=ename,
                template_value=_clip(t_norm),
                derived_value=_clip(normalize_expression(d_raw)) if d_raw else "(absent)",
                detail=(f"shared expression '{ename}' differs from the "
                        f"template (allowlisted cascade set)")))

    # ── mapping rows (template -> derived) ──
    for tname, rows in (baseline.get("mapping_rows") or {}).items():
        if tname not in live["tables"]:
            continue  # covered by table.missing
        d_rows = live["mapping_rows"].get(tname, {})
        for key, tuple_text in rows.items():
            if key not in d_rows:
                findings.append(Finding(
                    kind="mapping_row.missing", model=name, ref=f"{tname}/{key}",
                    template_value=_clip(tuple_text),
                    detail=(f"template mapping row '{key}' is absent from "
                            f"'{name}' table '{tname}'"),
                    data={"table": tname, "row_key": key, "row": tuple_text}))

    # ── extras + retirements (derived -> template) ──
    for tname, derived in live["tables"].items():
        if tname not in b_tables:
            if ("table", tname) in retire:
                findings.append(Finding(
                    kind="retire.table", model=name, ref=tname,
                    detail=(f"table '{tname}' was retired from the template "
                            f"(ledger) but '{name}' still carries it")))
            # else: derived-only table — legitimate extension, not drift
            continue
        for mname in derived["measures"]:
            ref = f"{tname}[{mname}]"
            if mname in (b_tables[tname].get("measures") or {}):
                continue
            if ("measure", ref) in retire:
                findings.append(Finding(
                    kind="retire.measure", model=name, ref=ref,
                    detail=(f"measure {ref} was retired from the template "
                            f"(ledger) but '{name}' still carries it")))
            else:
                findings.append(Finding(
                    kind="extra.measure", model=name, ref=ref,
                    detail=(f"'{name}' carries measure {ref} on a shared table "
                            f"with no template counterpart — advisory only "
                            f"(never auto-deleted)")))
        for cname in derived["columns"]:
            ref = f"{tname}[{cname}]"
            if cname in (b_tables[tname].get("columns") or {}):
                continue
            if ("column", ref) in retire:
                findings.append(Finding(
                    kind="retire.column", model=name, ref=ref,
                    detail=(f"column {ref} was retired from the template "
                            f"(ledger) but '{name}' still carries it")))
            else:
                findings.append(Finding(
                    kind="extra.column", model=name, ref=ref,
                    detail=(f"'{name}' carries column {ref} on a shared table "
                            f"with no template counterpart — advisory only "
                            f"(never auto-deleted)")))

    # retired mapping rows still present in the derived model
    for (kind, ref), _event in retire.items():
        if kind != "mapping_row":
            continue
        tname, key = ref.rsplit("/", 1)
        if key in live["mapping_rows"].get(tname, {}):
            findings.append(Finding(
                kind="retire.mapping_row", model=name, ref=ref,
                detail=(f"mapping row '{key}' of '{tname}' was retired from "
                        f"the template (ledger) but '{name}' still carries it"),
                data={"table": tname, "row_key": key}))

    return findings


def detect_fleet(fleet: Fleet, baseline: dict, ledger: Ledger,
                 model_filter: Optional[str] = None,
                 kind_filter: Optional[str] = None) -> list:
    findings: list = []
    for name in fleet.models:
        if model_filter and name != model_filter:
            continue
        findings.extend(detect_model(name, fleet, baseline, ledger))
    if kind_filter:
        findings = [f for f in findings if f.kind.startswith(kind_filter)]
    return findings


# ───────────────────────── helpers ─────────────────────────

def _fmt_props(props: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(props.items())) or "(none)"


def _live_view(model: Model, mapping_tables: list) -> dict:
    """The same comparable shape build_snapshot produces, from a live parse."""
    from .baseline import build_snapshot
    snap = build_snapshot(model, mapping_tables)
    return {
        "tables": snap["tables"],
        "expressions": model.expressions,
        "mapping_rows": snap["mapping_rows"],
    }

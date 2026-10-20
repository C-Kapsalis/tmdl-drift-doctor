"""Drift detection: typed findings, one per divergence.

Detection is read-only: it compares each derived model (parsed live from
disk) against the committed template baseline plus the retirement record and
emits ``Finding`` objects. It never mutates a model; remediation is a
separate, opt-in step that consumes these findings.

The doctrine that shapes the kinds:

* the template is the canonical CORE: every template object must exist in
  every derived model, and shared objects must not silently diverge;
* a derived model may legitimately EXTEND beyond it: extras on shared tables
  are advisory (``extra.*``, ``relationship.extra``), and derived-only tables
  are not drift at all;
* measure names are model-global, so a template measure is looked up by
  name across the whole derived model before it is declared missing (the
  ``measure.wrong_host`` / ``measure.duplicate_host`` kinds);
* the only deletions ever proposed are of objects PROVABLY retired from the
  template (the ledger's ``retire.*`` channel), and a retirement that derived
  work still depends on is ``blocked`` for a human, never forced.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import DriftDoctorError
from .fleet import Fleet, Override, kind_matches
from .ledger import Ledger
from .tmdl import (
    Model,
    blank_strings,
    dax_references,
    normalize_expression,
    parse_model,
    unquote,
)

# Kinds remediation can apply (subject to the allowlist).
REMEDIABLE_KINDS = (
    "table.missing",
    "column.missing",
    "measure.missing",
    "measure.renamed",
    "measure.expression_drift",
    "measure.property_drift",
    "measure.wrong_host",
    "measure.duplicate_host",
    "column.property_drift",
    "expression.drift",
    "expression.missing",
    "mapping_row.missing",
    "mapping_category.missing",
    "category_annotation.undeclared",
    "relationship.missing",
    "relationship.modified",
    "relationship.casing_mismatch",
    "fieldparam.schema_modernize",
    "fieldparam.blank_row",
    "fieldparam.row_missing",
    "fieldparam.row_drift",
    "fieldparam.dangling_row",
    "refresh_policy.missing",
    "refresh_policy.window_drift",
    "refresh_policy.invalid",
    "retire.table",
    "retire.column",
    "retire.measure",
    "retire.mapping_row",
    "retire.mapping_category",
    "retire.relationship",
    "retire.source_view",
    "purge.fieldparam_entry",
    "purge.measure",
    "purge.column",
    "purge.relationship",
    "purge.zmap_row",
    "purge.table",
    "column.purge_listed",
    "fieldparam.dangling_selector",
)

# Kind families whose findings carry a per-category suffix
# (``fieldparam.cross_level.<category>``).
REMEDIABLE_PREFIXES = ("fieldparam.cross_level", "fieldparam.plot_specific_extra")


def is_remediable(kind: str) -> bool:
    return kind in REMEDIABLE_KINDS or any(
        kind == p or kind.startswith(p + ".") for p in REMEDIABLE_PREFIXES)


# Kinds that only inform, never remediated: deleting or relocating a derived
# model's own work is a human decision, full stop.
ADVISORY_KINDS = ("extra.measure", "extra.column", "relationship.extra",
                  "extra.mapping_row")

# Kinds detection offers only in --sync mode (the purge channel).
SYNC_ONLY_KINDS = ("purge.fieldparam_entry", "purge.measure", "purge.column",
                   "purge.relationship", "purge.zmap_row", "purge.table",
                   "column.purge_listed")

# Deletions: only ever applied in --sync mode, only from the ledger.
RETIREMENT_FINDING_KINDS = ("retire.table", "retire.column", "retire.measure",
                            "retire.mapping_row", "retire.mapping_category",
                            "retire.relationship", "retire.source_view")

# Kinds that fail a gate whatever `allowlist.kinds` says: a model that lost
# a whole axis table, or live DAX naming a retired table, is never backlog.
ALWAYS_GATING = ("model.count_below_template", "retire.stranded_ref")

JSON_SCHEMA_VERSION = 2


@dataclass
class Finding:
    kind: str                 # e.g. "measure.expression_drift"
    model: str                # derived-model name from fleet.yml
    ref: str                  # object reference, e.g. "Visits[Total Visits]"
    template_value: Optional[str] = None
    derived_value: Optional[str] = None
    detail: str = ""
    data: dict = field(default_factory=dict)   # kind-specific extras
    override: Optional[Override] = None        # set when acknowledged

    @property
    def advisory(self) -> bool:
        return self.kind in ADVISORY_KINDS

    @property
    def overridden(self) -> bool:
        return self.override is not None

    @property
    def blocked(self) -> list:
        """Reasons a retirement must not run (derived work depends on it)."""
        return list(self.data.get("blocked") or [])

    def to_dict(self) -> dict:
        d = {
            "kind": self.kind,
            "model": self.model,
            "ref": self.ref,
            "template_value": self.template_value,
            "derived_value": self.derived_value,
            "detail": self.detail,
            "advisory": self.advisory,
            "remediable": is_remediable(self.kind),
        }
        data = {k: v for k, v in (self.data or {}).items()
                if k not in ("blocked", "row")}
        if data:
            d["data"] = data
        if self.blocked:
            d["blocked"] = self.blocked
        if self.override is not None:
            d["override"] = self.override.to_dict()
        return d


def decision(model: str, kind: str, name: str, note: str, *,
             for_kind: str = "", report_refs=None) -> Finding:
    """A call routed to a person: never a fix, never counted; the engine
    turns it into a digest entry (``RunResult.digest``)."""
    return Finding(kind="decision", model=model, ref=name, detail=note,
                   data={"kind": kind, "name": name, "note": note,
                         "report_refs": report_refs,
                         "for_kind": for_kind or kind})


def is_actionable(f: Finding, fleet: Fleet) -> bool:
    """THE predicate shared by detect's exit code, the ratchet and review:
    a finding is actionable when its kind is allowlisted, it is not
    advisory, and no override acknowledges it."""
    if f.kind == "decision":
        return False
    return ((fleet.allows(f.kind) or f.kind in ALWAYS_GATING
             or _missing_axis_table(f, fleet))
            and not f.advisory and not f.overridden)


def _missing_axis_table(f: Finding, fleet: Fleet) -> bool:
    """A missing table of an axis family gates whatever the allowlist says
    (remediation still needs the allowlist or an interactive yes)."""
    if f.kind != "table.missing":
        return False
    from .metrics import family_of, family_prefixes
    return family_of(f.ref, family_prefixes(fleet.raw)) is not None


def is_gating(f: Finding, fleet: Fleet) -> bool:
    """Actionable AND on a model whose gating is not ``advisory``."""
    return is_actionable(f, fleet) and not fleet.is_advisory(f.model)


def _clip(value, n: int = 120) -> str:
    s = "" if value is None else str(value)
    return s if len(s) <= n else s[: n - 1] + "…"


def split_ref(ref: str) -> tuple:
    """``Table[Name]`` -> (Table, Name)."""
    i = ref.rindex("[")
    return ref[:i], ref[i + 1:-1]


# ───────────────────── dependency (coupling) analysis ─────────────────────

def _definitions(model: Model):
    """Yield (kind, table, name, dax) for every measure and calculated
    column in a model."""
    for tname, table in model.tables.items():
        for m in table.measures.values():
            yield "measure", tname, m.name, m.expression
        for c in table.columns.values():
            if c.expression:
                yield "column", tname, c.name, c.expression


def references(model: Model, dax: str, host: str, target_kind: str,
               target: str) -> bool:
    """Does `dax` (an object hosted on table `host`) refer to `target`?

    * column ``T[C]``: a qualified ``T[C]``, or a bare ``[C]`` written on T
      itself that is not also a measure name;
    * measure ``T[M]``: a qualified ``T[M]`` or a bare ``[M]``;
    * table ``T``: the table by name, any ``T[...]``, or a bare ``[M]`` for
      a measure hosted only on T.
    """
    refs = dax_references(dax)
    if target_kind == "column":
        t, c = split_ref(target)
        if (t, c) in refs["qualified"]:
            return True
        return (host == t and c in refs["bare"]
                and c not in model.measure_hosts())
    if target_kind == "measure":
        t, m = split_ref(target)
        return (t, m) in refs["qualified"] or m in refs["bare"]
    if target_kind == "table":
        if target in refs["tables"]:
            return True
        hosts = model.measure_hosts()
        return any(hosts.get(b) == [target] for b in refs["bare"])
    return False


def dependents(model: Model, target_kind: str, target: str) -> dict:
    """Everything in `model` that would dangle if `target` were removed.

    Returns {"definitions": [(kind, Table[Name])], "relationships": [sig],
    "sort_by": [Table[Column]], "hierarchies": [Table/Hierarchy]}. Objects
    hosted on a table that is itself the target go with it and are not
    listed here."""
    out = {"definitions": [], "relationships": [], "sort_by": [],
           "hierarchies": []}
    for kind, tname, name, dax in _definitions(model):
        if target_kind == "table" and tname == target:
            continue
        if f"{tname}[{name}]" == target:
            continue
        if references(model, dax, tname, target_kind, target):
            out["definitions"].append((kind, f"{tname}[{name}]"))
    for sig, rel in model.relationships.items():
        if target_kind == "table" and rel.touches_table(target):
            out["relationships"].append(sig)
        elif target_kind == "column" and rel.touches_column(*split_ref(target)):
            out["relationships"].append(sig)
    if target_kind == "column":
        t, c = split_ref(target)
        table = model.tables.get(t)
        if table is not None:
            for col in table.columns.values():
                if col.name != c and unquote(
                        col.properties.get("sortByColumn", "")) == c:
                    out["sort_by"].append(f"{t}[{col.name}]")
            for hname, levels in table.hierarchies.items():
                if c in levels:
                    out["hierarchies"].append(f"{t}/{hname}")
    return out


def _owned(baseline: dict, kind: str, ref: str) -> bool:
    """Is a measure/column ref template-owned (present in the baseline)?"""
    t, n = split_ref(ref)
    body = (baseline.get("tables") or {}).get(t) or {}
    return n in (body.get("measures" if kind == "measure" else "columns") or {})


def _pending_removal(retire: dict, kind: str, ref: str) -> bool:
    """Is this object itself ledger-retired (so its removal is pending)?"""
    if (kind, ref) in retire:
        return True
    return ("table", split_ref(ref)[0]) in retire


def coupling_blockers(model: Model, baseline: dict, retire: dict,
                      target_kind: str, target: str) -> tuple:
    """(blockers, sweep): human-readable reasons the retirement of `target`
    must be refused, and the relationship signatures the retirement removes
    with it (template-owned or ledger-retired ones).

    Template-owned and ledger-retired dependents are NOT blockers: their own
    fix in the same run resolves them. Whether that fix has landed is
    re-checked when the retirement is applied (its precondition)."""
    deps = dependents(model, target_kind, target)
    b_rels = baseline.get("relationships") or {}
    blockers, sweep = [], []
    derived_only = [ref for kind, ref in deps["definitions"]
                    if not _owned(baseline, kind, ref)
                    and not _pending_removal(retire, kind, ref)]
    if derived_only:
        blockers.append("derived-only definition(s) reference it: "
                        + ", ".join(sorted(derived_only)))
    custom = []
    for sig in deps["relationships"]:
        if sig in b_rels or ("relationship", sig) in retire:
            sweep.append(sig)
        else:
            custom.append(sig)
    if custom:
        blockers.append("derived-only relationship(s) use it: "
                        + ", ".join(sorted(custom)))
    if deps["sort_by"]:
        blockers.append("column(s) sort by it: "
                        + ", ".join(sorted(deps["sort_by"])))
    if deps["hierarchies"]:
        blockers.append("hierarchy level(s) use it: "
                        + ", ".join(sorted(deps["hierarchies"])))
    return blockers, sorted(sweep)


# ───────────────────────────── detection ─────────────────────────────

def detect_model(name: str, fleet: Fleet, baseline: dict,
                 ledger: Ledger, model: Optional[Model] = None) -> list:
    """All findings for one derived model, ordered: structural gaps, drift,
    relationships, advisories, retirements."""
    model = model or parse_model(fleet.model_definition(name))
    live = _live_view(model, fleet.mapping_tables)
    retire = ledger.active_retirements(model=name)
    findings: list = []

    b_tables = baseline.get("tables") or {}
    hosts = model.measure_hosts()
    claimed: set = set()   # (table, measure) explained by a host finding

    # ── missing objects (template -> derived) ──
    for tname, body in b_tables.items():
        if tname not in live["tables"]:
            findings.append(Finding(
                kind="table.missing", model=name, ref=tname,
                detail=f"template table '{tname}' is absent from '{name}'"))
            for mname, m in (body.get("measures") or {}).items():
                others = sorted(hosts.get(mname) or [])
                if others:
                    for t in others:
                        claimed.add((t, mname))
                    findings.append(_wrong_host(
                        name, tname, others[0], mname,
                        _same_body(model, others[0], mname, m)))
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
            others = [t for t in (hosts.get(mname) or []) if t != tname]
            if mname not in derived["measures"]:
                if others:
                    others = sorted(others)
                    for t in others:
                        claimed.add((t, mname))
                    findings.append(_wrong_host(
                        name, tname, others[0], mname,
                        _same_body(model, others[0], mname, m)))
                else:
                    findings.append(Finding(
                        kind="measure.missing", model=name, ref=ref,
                        detail=f"template measure {ref} is absent from '{name}'"))
                continue
            for t in others:
                claimed.add((t, mname))
                findings.append(_duplicate_host(name, tname, t, mname))
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

    # ── template-driven renames (one missing + one extra, same core) ──
    findings = _pair_renames(name, fleet, baseline, model, findings)

    # ── shared expressions ──
    findings.extend(_expression_findings(name, fleet, baseline, model))

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

    # ── encoded mapping tables ──
    from .mappings import merge_findings
    findings.extend(merge_findings(name, fleet, baseline,
                                   fleet.model_definition(name)))

    # ── relationships (template -> derived), matched on field signature ──
    b_rels = baseline.get("relationships") or {}
    by_folded = {sig.casefold(): sig for sig in model.relationships}
    seen_derived: set = set()
    for sig, props in b_rels.items():
        rel = model.relationships.get(sig)
        if rel is None and sig.casefold() in by_folded:
            # same relationship spelled with another casing: one casing
            # finding per mismatching endpoint part, never a "missing" one
            rel = model.relationships[by_folded[sig.casefold()]]
        if rel is not None:
            seen_derived.add(rel.signature)
        if rel is None:
            findings.append(Finding(
                kind="relationship.missing", model=name, ref=sig,
                template_value=_fmt_rel(props),
                detail=(f"template relationship {sig} is absent from "
                        f"'{name}' (appended only once both endpoints "
                        f"exist)")))
        elif rel.properties != props:
            findings.append(Finding(
                kind="relationship.modified", model=name, ref=sig,
                template_value=_fmt_rel(props),
                derived_value=_fmt_rel(rel.properties),
                detail=(f"relationship {sig} properties differ from the "
                        f"template (overwrite keeps '{name}''s block name)")))
    findings.extend(_casing_findings(name, model, baseline))

    # ── extras (derived -> template) ──
    for tname, derived in live["tables"].items():
        if tname not in b_tables:
            continue  # derived-only or retired table: handled below
        for mname in derived["measures"]:
            ref = f"{tname}[{mname}]"
            if mname in (b_tables[tname].get("measures") or {}):
                continue
            if (tname, mname) in claimed:
                continue  # explained by a wrong/duplicate-host finding
            if ("measure", ref) in retire:
                continue  # retire.measure, below
            findings.append(Finding(
                kind="extra.measure", model=name, ref=ref,
                detail=(f"'{name}' carries measure {ref} on a shared table "
                        f"with no template counterpart; advisory only "
                        f"(never auto-deleted)")))
        for cname in derived["columns"]:
            ref = f"{tname}[{cname}]"
            if cname in (b_tables[tname].get("columns") or {}):
                continue
            if ("column", ref) in retire:
                continue
            findings.append(Finding(
                kind="extra.column", model=name, ref=ref,
                detail=(f"'{name}' carries column {ref} on a shared table "
                        f"with no template counterpart; advisory only "
                        f"(never auto-deleted)")))
    for sig, rel in model.relationships.items():
        if sig in b_rels or sig in seen_derived \
                or ("relationship", sig) in retire:
            continue
        if rel.from_table in b_tables and rel.to_table in b_tables:
            findings.append(Finding(
                kind="relationship.extra", model=name, ref=sig,
                detail=(f"'{name}' carries relationship {sig} between two "
                        f"shared tables with no template counterpart; "
                        f"advisory only (never auto-deleted)")))

    # ── retirements (ledger-proven), with the coupling guard ──
    findings.extend(_retirements(name, model, live, baseline, retire))
    findings.extend(_retired_aliases(name, model, baseline, retire, findings))
    _protect(fleet, findings)
    findings.extend(_revived_decisions(name, model, ledger))
    from .mappings import retirement_findings
    findings.extend(retirement_findings(name, fleet, model.definition,
                                        retire))
    findings.extend(_stranded_refs(name, model, ledger))
    findings = drop_paired_extras(findings)

    # ── field-parameter tables ──
    from .fieldparams import fieldparam_findings
    findings.extend(fieldparam_findings(name, fleet, model))

    # ── count floors against the template's captured metrics ──
    from .metrics import COUNT_KIND, count_floor_findings
    for data, detail in count_floor_findings(
            name, model, baseline.get("metrics"), fleet.raw,
            fleet.overrides.get(name)):
        ref = data["metric"] + (f":{data['table']}" if data.get("table")
                                else f":{data['prefix']}" if data.get("prefix")
                                else "")
        findings.append(Finding(kind=COUNT_KIND, model=name, ref=ref,
                                template_value=_none_or_str(data["floor"]),
                                derived_value=str(data["client"]),
                                detail=detail, data=data))
    return findings


def _declared_divergent(fleet: Fleet, model: str, measure: str) -> bool:
    for o in fleet.overrides.get(model) or []:
        if o.kind in ("absent", "expression") and (
                o.object == measure or o.object.endswith(f"[{measure}]")):
            return True
    return False


def _pair_renames(name: str, fleet: Fleet, baseline: dict, model: Model,
                  findings: list) -> list:
    """Turn one ``measure.missing`` plus one derived-only measure sharing
    its core name into a single ``measure.renamed`` (``old -> new``).
    Ambiguous cores (two candidates on either side) are left as they are."""
    from .renames import DEFAULT_EXCLUDE, DEFAULT_SIGILS, core_name
    conf = ((fleet.setting("renames") or {}).get("pairing") or {}) \
        if isinstance(fleet.setting("renames"), dict) else {}
    sigils = tuple(conf.get("prefix_sigils") or DEFAULT_SIGILS)
    exclude = tuple(conf.get("exclude_prefixes") or DEFAULT_EXCLUDE)
    template_names = {m for body in (baseline.get("tables") or {}).values()
                      for m in (body.get("measures") or {})}
    missing = {split_ref(f.ref)[1]: f for f in findings
               if f.kind == "measure.missing"}
    hosts = model.measure_hosts()
    extra = [m for m in hosts if m not in template_names
             and len(hosts[m]) == 1]

    def usable(n):
        return not any(n.startswith(p) for p in exclude)

    by_core: dict = {}
    for n in missing:
        if usable(n):
            by_core.setdefault(core_name(n, sigils), [[], []])[0].append(n)
    for n in extra:
        if usable(n):
            by_core.setdefault(core_name(n, sigils), [[], []])[1].append(n)
    drop_missing, drop_extra, out = set(), set(), []
    for core, (miss, ext) in sorted(by_core.items()):
        if len(miss) != 1 or len(ext) != 1 or miss[0] == ext[0]:
            continue
        new, old = miss[0], ext[0]
        if _declared_divergent(fleet, name, new):
            continue
        tmpl = split_ref(missing[new].ref)[0]
        host = hosts[old][0]
        drop_missing.add(new)
        drop_extra.add(f"{host}[{old}]")
        out.append(Finding(
            kind="measure.renamed", model=name, ref=f"{old} -> {new}",
            template_value=new, derived_value=old,
            detail=(f"the template renamed measure '{old}' to '{new}' "
                    f"(same core name); the fix renames '{name}''s "
                    f"declaration on '{host}' and rewrites its DAX and "
                    f"report references"),
            data={"old": old, "new": new, "host": host,
                  "template_table": tmpl}))
    if not out:
        return findings
    kept = [f for f in findings
            if not (f.kind == "measure.missing"
                    and split_ref(f.ref)[1] in drop_missing)]
    return kept + out


def drop_paired_extras(findings: list) -> list:
    """Remove the ``extra.measure`` (and ``retire.measure``) findings a
    rename explains: the old name is renamed, never removed."""
    explained = {(f.model, f.data.get("host"), f.data.get("old"))
                 for f in findings if f.kind == "measure.renamed"}
    if not explained:
        return findings
    return [f for f in findings if not (
        f.kind in ("extra.measure", "retire.measure")
        and (f.model, *split_ref(f.ref)) in explained)]


def _none_or_str(v):
    return None if v is None else str(v)


def _casing_findings(name: str, model: Model, baseline: dict) -> list:
    """``relationship.casing_mismatch``: an endpoint whose table or column
    exists in the template under another casing. One finding per
    mismatching part; endpoints on derived-only tables are ignored."""
    b_tables = baseline.get("tables") or {}
    tables = {t.casefold(): t for t in b_tables}
    out = []
    for sig, rel in sorted(model.relationships.items()):
        for end, table, col in (("from", rel.from_table, rel.from_column),
                                ("to", rel.to_table, rel.to_column)):
            canon_t = tables.get(table.casefold())
            if canon_t is None:
                continue
            cols = {c.casefold(): c for c in
                    (b_tables[canon_t].get("columns") or {})}
            canon_c = cols.get(col.casefold(), col)
            for part, found, canon in (("table", table, canon_t),
                                       ("column", col, canon_c)):
                if found != canon:
                    out.append(Finding(
                        kind="relationship.casing_mismatch", model=name,
                        ref=f"{sig} ({end} {part})",
                        template_value=canon, derived_value=found,
                        detail=(f"relationship {sig}: {end} {part} is "
                                f"spelled '{found}' but the template spells "
                                f"it '{canon}' ({canon_t}[{canon_c}])"),
                        data={"signature": sig, "end": end, "part": part,
                              "table": canon_t, "column": canon_c}))
    return out


_SENSORS = ("ISFILTERED", "ISCROSSFILTERED", "HASONEVALUE", "HASONEFILTER",
            "ISEMPTY", "ISINSCOPE")


def _stranded_refs(name: str, model: Model, ledger: Ledger) -> list:
    """``retire.stranded_ref``: live DAX that still names a table the ledger
    retired and the model no longer declares."""
    gone = sorted({ref for (kind, ref) in ledger.active_retirements(name)
                   if kind == "table" and ref not in model.tables})
    if not gone:
        return []
    out = []
    for tname, table in model.tables.items():
        objs = [("measure", m.name, m.expression) for m in table.measures.values()]
        objs += [("column", c.name, c.expression) for c in table.columns.values()
                 if c.expression]
        for kind, oname, expr in objs:
            live = blank_strings(expr or "")
            for dead in gone:
                quoted = "'" + dead.replace("'", "''") + "'"
                bare = re.escape(dead)
                if quoted not in live and not re.search(
                        rf"(?<![\w']){bare}\[", live):
                    continue
                sensor = re.compile(r"\b(?:" + "|".join(_SENSORS)
                                    + r")\s*\(\s*$", re.IGNORECASE)
                sensor_only = all(sensor.search(live[:m.start()])
                                  for m in re.finditer(re.escape(quoted), live))
                out.append(Finding(
                    kind="retire.stranded_ref", model=name,
                    ref=f"{tname}[{oname}]",
                    detail=(f"{kind} {tname}[{oname}] still references the "
                            f"ledger-retired table '{dead}', which '{name}' "
                            f"no longer declares (unresolvable DAX)"),
                    data={"table": tname, "object": oname, "object_kind": kind,
                          "retired_table": dead,
                          "sensor_only": bool(sensor_only)}))
    return out


def _one_line(form: str) -> str:
    return _clip(" | ".join(ln.strip() for ln in form.split("\n") if ln.strip()))


def _expression_findings(name: str, fleet: Fleet, baseline: dict,
                         model: Model) -> list:
    """``expression.drift`` for allowlisted names whose declaration (any of
    them, when a name is declared more than once) differs from the
    template's in comparison form; ``expression.missing`` for every template
    name the model does not declare at all."""
    out = []
    b_exprs = baseline.get("expressions") or {}
    for ename in sorted(fleet.allowlist_expressions):
        if ename not in b_exprs or ename not in model.expression_forms:
            continue
        wanted = b_exprs[ename]
        differing = [f for f in model.expression_forms[ename] if f != wanted]
        if differing:
            out.append(Finding(
                kind="expression.drift", model=name, ref=ename,
                template_value=_one_line(wanted),
                derived_value=_one_line(differing[0]),
                detail=(f"shared expression '{ename}' differs from the "
                        f"template (allowlisted cascade set)")))
    if not model.has_expressions_file:
        return out
    for ename in sorted(b_exprs):
        if ename in model.expression_forms:
            continue
        detail = f"template expression '{ename}' is not declared in '{name}'"
        if ename in fleet.connection_parameters:
            detail += ("; it is a connection parameter, so remediation never "
                       "writes it: copy the declaration from the template and "
                       "set this model's own value")
        out.append(Finding(kind="expression.missing", model=name, ref=ename,
                           template_value=_one_line(b_exprs[ename]),
                           detail=detail))
    return out


def _retirements(name: str, model: Model, live: dict, baseline: dict,
                 retire: dict) -> list:
    b_tables = baseline.get("tables") or {}
    out: list = []
    retiring_tables, retiring_columns = set(), set()

    def guarded(kind, ref, detail, data=None):
        target_kind = kind.split(".", 1)[1]
        blockers, sweep = coupling_blockers(model, baseline, retire,
                                            target_kind, ref)
        if kind == "retire.table":
            blockers += _own_work_on_table(model, baseline, retire, ref)
        data = dict(data or {})
        if sweep:
            data["sweep_relationships"] = sweep
        if blockers:
            data["blocked"] = blockers
            detail += "; BLOCKED: " + "; ".join(blockers)
        out.append(Finding(kind=kind, model=name, ref=ref, detail=detail,
                           data=data))

    for tname in live["tables"]:
        if tname not in b_tables and ("table", tname) in retire:
            retiring_tables.add(tname)
            guarded("retire.table", tname,
                    f"table '{tname}' was retired from the template (ledger) "
                    f"but '{name}' still carries it")
    for tname, derived in live["tables"].items():
        if tname not in b_tables:
            continue
        for mname in derived["measures"]:
            ref = f"{tname}[{mname}]"
            if (mname not in (b_tables[tname].get("measures") or {})
                    and ("measure", ref) in retire):
                guarded("retire.measure", ref,
                        f"measure {ref} was retired from the template "
                        f"(ledger) but '{name}' still carries it")
        for cname in derived["columns"]:
            ref = f"{tname}[{cname}]"
            if (cname not in (b_tables[tname].get("columns") or {})
                    and ("column", ref) in retire):
                retiring_columns.add((tname, cname))
                guarded("retire.column", ref,
                        f"column {ref} was retired from the template "
                        f"(ledger) but '{name}' still carries it")

    # retired mapping rows still present in the derived model
    for (kind, ref), _event in retire.items():
        if kind != "mapping_row" or _event.get("category"):
            continue
        tname, key = ref.rsplit("/", 1)
        if key in live["mapping_rows"].get(tname, {}):
            out.append(Finding(
                kind="retire.mapping_row", model=name, ref=ref,
                detail=(f"mapping row '{key}' of '{tname}' was retired from "
                        f"the template (ledger) but '{name}' still carries it"),
                data={"table": tname, "row_key": key}))

    # retired relationships, unless a table/column retirement sweeps them
    b_rels = baseline.get("relationships") or {}
    for sig, rel in model.relationships.items():
        if sig in b_rels or ("relationship", sig) not in retire:
            continue
        if (rel.from_table in retiring_tables or rel.to_table in retiring_tables
                or (rel.from_table, rel.from_column) in retiring_columns
                or (rel.to_table, rel.to_column) in retiring_columns):
            continue  # removed together with its endpoint's retirement
        out.append(Finding(
            kind="retire.relationship", model=name, ref=sig,
            detail=(f"relationship {sig} was retired from the template "
                    f"(ledger) but '{name}' still carries it")))
    return out


def _retired_aliases(name: str, model: Model, baseline: dict, retire: dict,
                     found: list) -> list:
    """A model measure whose core name (one prefix sigil stripped) equals a
    retired measure's, and which is no live template name, is retired too."""
    from .renames import core_name
    cores = {core_name(split_ref(ref)[1]) for (k, ref) in retire
             if k == "measure"}
    if not cores:
        return []
    live_names = {m for body in (baseline.get("tables") or {}).values()
                  for m in (body.get("measures") or {})}
    already = {f.ref for f in found if f.kind == "retire.measure"}
    retired_refs = {ref for (k, ref) in retire if k == "measure"}
    out = []
    for tname, table in sorted(model.tables.items()):
        for mname in sorted(table.measures):
            ref = f"{tname}[{mname}]"
            if mname in live_names or ref in already or ref in retired_refs:
                continue
            if core_name(mname) not in cores:
                continue
            blockers, _s = coupling_blockers(model, baseline, retire,
                                             "measure", ref)
            f = Finding(kind="retire.measure", model=name, ref=ref,
                        detail=(f"measure {ref} is an alias of a measure "
                                f"retired from the template (same core name)"),
                        data={"alias": True})
            if blockers:
                f.data["blocked"] = blockers
                f.detail += "; BLOCKED: " + "; ".join(blockers)
            out.append(f)
    return out


def _protect(fleet: Fleet, findings: list) -> None:
    """Removal paths never touch protected names: such retirements are
    blocked for a person."""
    from .purge import is_protected
    for f in findings:
        if f.kind.startswith("retire.") and f.kind != "retire.stranded_ref" \
                and is_protected(fleet, f.ref):
            f.data.setdefault("blocked", []).append(
                "the name matches protected_name_pattern")


def _revived_decisions(name: str, model: Model, ledger: Ledger) -> list:
    """A retirement that was revived while the model still carries the
    object: no removal, one digest line."""
    out = []
    seen = set()
    for e in ledger.entries():
        if e.get("event") != "revived" or e.get("model") not in (None, name):
            continue
        kind, ref = e.get("kind"), e.get("ref") or ""
        if (kind, ref) in seen:
            continue
        present = False
        if kind == "table":
            present = ref in model.tables
        elif kind in ("measure", "column") and ref.endswith("]"):
            t, n = split_ref(ref)
            present = (model.measure(t, n) if kind == "measure"
                       else model.column(t, n)) is not None
        if present:
            seen.add((kind, ref))
            out.append(decision(name, kind, ref,
                                f"revived ({e.get('source') or 'manual'}); "
                                f"retirement skipped",
                                for_kind=f"retire.{kind}"))
    return out


def _own_work_on_table(model: Model, baseline: dict, retire: dict,
                       table: str) -> list:
    """A retired table that hosts objects the ledger does NOT cover (never
    in the template as far as the ledger knows) carries derived work; the
    table removal would delete it, so it blocks."""
    t = model.tables.get(table)
    if t is None:
        return []
    known = {ref for (kind, ref) in retire if kind in ("measure", "column")}
    # capture records the objects the table held when it left the template;
    # a ledger written before that field existed is trusted as is
    event = retire.get(("table", table)) or {}
    template_era = set(event.get("objects") or [])
    if not template_era:
        return []
    own = sorted(f"{table}[{n}]" for n in list(t.measures) + list(t.columns)
                 if f"{table}[{n}]" not in template_era
                 and f"{table}[{n}]" not in known)
    if own:
        return ["the table hosts derived-only object(s): " + ", ".join(own)]
    return []


def _same_body(model: Model, table: str, mname: str, template: dict) -> bool:
    m = model.measure(table, mname)
    return m is not None and normalize_expression(m.expression) == \
        (template or {}).get("expression")


def _wrong_host(model: str, host: str, found_on: str, mname: str,
                same_body: bool = True) -> Finding:
    body = ("the body matches the template" if same_body
            else "the body also differs from the template")
    return Finding(
        kind="measure.wrong_host", model=model, ref=f"{host}[{mname}]",
        template_value=host, derived_value=found_on,
        detail=(f"'{model}' carries measure '{mname}' on '{found_on}', but "
                f"the template hosts it on '{host}'; {body}. The fix moves "
                f"the block verbatim (DAX and lineage tag untouched); measure "
                f"names are model-global, so it is never inserted a second "
                f"time"),
        data={"from_table": found_on, "to_table": host, "measure": mname})


def _duplicate_host(model: str, host: str, extra: str, mname: str) -> Finding:
    return Finding(
        kind="measure.duplicate_host", model=model, ref=f"{extra}[{mname}]",
        template_value=host, derived_value=extra,
        detail=(f"'{model}' carries measure '{mname}' on '{extra}' as well "
                f"as on '{host}', the template's host. Measure names are "
                f"model-global, so the model will not load; the fix removes "
                f"the stale copy on '{extra}'"),
        data={"template_host": host, "measure": mname})


def finding_files(fleet: Fleet, f: Finding) -> list:
    """The files (relative to the model root) a finding's fix would write,
    as far as the kind tells: the table file for table-scoped kinds,
    relationships.tmdl and expressions.tmdl for theirs."""
    defn = f"{fleet.models[f.model].name}/definition"
    head = f.kind.split(".", 1)[0]
    if head in ("relationship",) or f.kind == "retire.relationship":
        return [f"{defn}/relationships.tmdl"]
    if head == "expression":
        return [f"{defn}/expressions.tmdl"]
    table = f.data.get("table") if isinstance(f.data, dict) else None
    if not table:
        ref = f.ref
        if "->" in ref:
            return [f"{defn}/relationships.tmdl"]
        table = ref[:ref.rindex("[")] if ref.endswith("]") and "[" in ref \
            else ref.split("/", 1)[0]
    out = [f"{defn}/tables/{table}.tmdl"]
    if f.kind == "measure.wrong_host":
        out.append(f"{defn}/tables/{f.data.get('from_table')}.tmdl")
    return out


def apply_overrides(fleet: Fleet, findings: list) -> list:
    from .overrides import match_override
    for f in findings:
        f.override = match_override(fleet, f)
        if f.override is None and f.model in fleet.models \
                and fleet.exempt_patterns(f.model):
            for rel in finding_files(fleet, f):
                hit = fleet.file_override(f.model, rel)
                if hit is not None:
                    f.override = hit
                    break
    return findings


def detect_fleet(fleet: Fleet, baseline: dict, ledger: Ledger,
                 model_filter: Optional[str] = None,
                 kind_filter: Optional[str] = None,
                 errors: Optional[list] = None,
                 sync: bool = False) -> list:
    """Findings for every (or one) derived model, overrides applied.

    When `errors` is a list, a model that cannot be parsed is recorded there
    as {"model", "error"} and detection continues; otherwise it raises."""
    from .overrides import resolve_overrides
    resolve_overrides(fleet, baseline, ledger)
    if model_filter and model_filter not in fleet.models:
        raise DriftDoctorError(
            f"unknown model '{model_filter}'; fleet.yml defines: "
            f"{', '.join(fleet.models)}")
    findings: list = []
    from .annotations import findings as annotation_findings
    from .refresh import refresh_findings
    from .sources import source_view_findings
    cache: dict = {}
    policies: dict = {}
    for name in fleet.models:
        if model_filter and name != model_filter:
            continue
        try:
            model = parse_model(fleet.model_definition(name))
            findings.extend(detect_model(name, fleet, baseline, ledger,
                                         model))
            findings.extend(annotation_findings(name, fleet, cache))
            findings.extend(refresh_findings(name, fleet, model, policies))
            findings.extend(source_view_findings(name, fleet, model, ledger))
            if sync:
                from .purge import column_purge_findings, purge_findings
                findings.extend(purge_findings(name, fleet, baseline, model,
                                               ledger))
                findings.extend(column_purge_findings(name, fleet, findings))
            from .selectors import findings as selector_findings
            findings.extend(selector_findings(name, model))
        except DriftDoctorError as e:
            if errors is None:
                raise
            errors.append({"model": name, "error": str(e)})
    if kind_filter:
        findings = [f for f in findings
                    if kind_matches(f.data.get("for_kind", f.kind)
                                    if f.kind == "decision" else f.kind,
                                    kind_filter)]
    return apply_overrides(fleet, findings)


# ───────────────────────── the JSON report (v2) ─────────────────────────

def git_tree_hash(path: Path) -> Optional[str]:
    """The git tree hash of `path` at HEAD, or None outside git."""
    try:
        r = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD:./"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def model_counts(fleet: Fleet, items: list) -> dict:
    by_kind: dict = {}
    for f in items:
        if is_actionable(f, fleet):
            by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
    return {
        "actionable": sum(by_kind.values()),
        "advisory": sum(1 for f in items if f.advisory),
        "overridden": sum(1 for f in items if f.overridden),
        "not_allowlisted": sum(1 for f in items if not f.advisory
                               and not f.overridden
                               and not fleet.allows(f.kind)),
        "by_kind": dict(sorted(by_kind.items())),
    }


def divergence_report(fleet: Fleet, name: str, baseline: Optional[dict]):
    from .sources import divergent_tables
    if baseline is None:
        return {}
    try:
        model = parse_model(fleet.model_definition(name))
    except DriftDoctorError:
        return {}
    return {t: {"template": a, "model": b} for t, (a, b) in
            divergent_tables(fleet, baseline, name, model).items()}


def build_report(fleet: Fleet, findings: list, errors: list,
                 model_filter: Optional[str] = None,
                 kind_filter: Optional[str] = None,
                 baseline: Optional[dict] = None) -> dict:
    names = [model_filter] if model_filter else list(fleet.models)
    models = {}
    for name in names:
        items = [f for f in findings if f.model == name]
        decisions = [f for f in items if f.kind == "decision"]
        items = [f for f in items if f.kind != "decision"]
        models[name] = {
            "source_divergent_tables": divergence_report(fleet, name,
                                                         baseline),
            "gating": fleet.gating.get(name, "enforced"),
            "decisions": [dict(f.data, model=name) for f in decisions],
            "findings": [dict(f.to_dict(), actionable=is_actionable(f, fleet))
                         for f in items if not f.overridden],
            "overridden": [f.to_dict() for f in items if f.overridden],
            "counts": model_counts(fleet, items),
        }
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "template_tree": git_tree_hash(fleet.template),
        "scope": {"fleet": model_filter is None, "models": names,
                  "kind_filter": kind_filter},
        "errors": list(errors),
        "models": models,
    }


# ───────────────────────── helpers ─────────────────────────

def _fmt_props(props: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(props.items())) or "(none)"


def _fmt_rel(props: dict) -> str:
    """Relationship properties that differ from the engine defaults."""
    from .tmdl import RELATIONSHIP_DEFAULTS
    shown = {k: v for k, v in props.items() if RELATIONSHIP_DEFAULTS.get(k) != v}
    return _fmt_props(shown) if shown else "(engine defaults)"


def _live_view(model: Model, mapping_tables: list) -> dict:
    """The same comparable shape build_snapshot produces, from a live parse."""
    from .baseline import build_snapshot
    snap = build_snapshot(model, mapping_tables, with_metrics=False,
                          encoded=False)
    return {
        "tables": snap["tables"],
        "expressions": model.expressions,
        "mapping_rows": snap["mapping_rows"],
    }

"""Incremental-refresh parity for fact tables.

The template decides whether a fact table has an incremental-refresh policy
and what its window is. A model's own partition source is never replaced by
the template's (that would repoint it at the template's data); only the
policy and its window follow the template. Every change here owes a manual
full refresh of the archive, so the kinds are never batch-safe.

Kinds:

* ``refresh_policy.missing``: the template has a policy, the model does not
  (report-only when the table cannot take one: not a single import-mode M
  partition, entered data, or no readable source);
* ``refresh_policy.window_drift``: both have a policy, the windows differ;
* ``refresh_policy.invalid``: the model's policy is malformed (its
  ``sourceExpression`` differs from the partition source, the block is
  duplicated, or a scalar is out of its domain);
* ``refresh_policy.client_only`` (report-only): the model has a policy the
  template's table does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import DriftDoctorError
from . import tmdl_edit as te

SCALARS = ("rollingWindowGranularity", "rollingWindowPeriods",
           "incrementalGranularity", "incrementalPeriods",
           "incrementalPeriodsOffset")
GRANULARITIES = ("day", "month", "quarter", "year")


@dataclass
class Policy:
    start: int                      # line index of `\trefreshPolicy`
    end: int                        # exclusive
    scalars: dict = field(default_factory=dict)
    policy_type: str = ""
    source: str = ""                # sourceExpression body (dedented text)
    count: int = 1                  # how many policy blocks the file has


def _body(lines: list, i: int, depth: int) -> tuple:
    """(body lines, next index) of an expression whose `=` ends line i:
    following lines that are blank or indented deeper than `depth` tabs."""
    j = i + 1
    while j < len(lines) and (not lines[j].strip()
                              or len(lines[j]) - len(lines[j].lstrip("\t"))
                              > depth):
        j += 1
    end = j
    while end > i + 1 and not lines[end - 1].strip():
        end -= 1
    return lines[i + 1:end], j


def _norm_body(body: list) -> str:
    return "\n".join(ln.strip() for ln in body).strip()


def _lines(text: str) -> list:
    """The lines of a table file, whatever its line endings (a model
    committed from Windows has CRLF)."""
    return text.replace("\r\n", "\n").split("\n")


def parse_policy(text: str) -> Optional[Policy]:
    lines = _lines(text)
    starts = [i for i, ln in enumerate(lines) if ln.strip() == "refreshPolicy"]
    if not starts:
        return None
    i = starts[0]
    j = i + 1
    pol = Policy(start=i, end=i + 1, count=len(starts))
    while j < len(lines):
        ln = lines[j]
        if ln.strip() and not ln.startswith("\t\t"):
            break
        m = re.match(r"^\t\t(\w+)\s*:\s*(.*?)\s*$", ln)
        if m:
            if m.group(1) == "policyType":
                pol.policy_type = m.group(2)
            elif m.group(1) in SCALARS:
                pol.scalars[m.group(1)] = m.group(2)
            j += 1
            continue
        if re.match(r"^\t\tsourceExpression\s*=\s*$", ln):
            body, j = _body(lines, j, 2)
            pol.source = _norm_body(body)
            continue
        j += 1
    while j > i + 1 and not lines[j - 1].strip():
        j -= 1
    pol.end = j
    return pol


@dataclass
class PartitionInfo:
    name: str
    kind: str          # the word after `=`: m, calculated, entity ...
    mode: str
    source: str        # normalized body
    raw_body: list     # source body lines as written


def partitions(text: str) -> list:
    lines = _lines(text)
    out = []
    for i, ln in enumerate(lines):
        m = re.match(r"^\tpartition\s+(.+?)\s*=\s*(\w+)\s*$", ln)
        if not m:
            continue
        info = PartitionInfo(m.group(1), m.group(2), "", "", [])
        j = i + 1
        while j < len(lines) and (not lines[j].strip()
                                  or lines[j].startswith("\t\t")):
            mm = re.match(r"^\t\tmode\s*:\s*(\S+)", lines[j])
            if mm:
                info.mode = mm.group(1)
            sm = re.match(r"^\t\tsource\s*=\s*(.*)$", lines[j])
            if sm:
                if sm.group(1).strip():
                    info.raw_body = ["\t\t\t\t" + sm.group(1).strip()]
                    info.source = sm.group(1).strip()
                    j += 1
                    continue
                body, j = _body(lines, j, 2)
                info.raw_body = body
                info.source = _norm_body(body)
                continue
            j += 1
        out.append(info)
    return out


def ineligible(text: str) -> Optional[str]:
    parts = partitions(text)
    if len(parts) != 1:
        return f"it has {len(parts)} partitions (incremental refresh needs one)"
    p = parts[0]
    if p.kind != "m" or (p.mode and p.mode != "import"):
        return "its partition is not an import-mode Power Query partition"
    if "Binary.FromText" in p.source:
        return "it is entered data"
    if not p.source:
        return "its partition source cannot be read"
    return None


def problems(text: str, pol: Policy) -> list:
    out = []
    if pol.count > 1:
        out.append(f"{pol.count} refreshPolicy blocks")
    lines = _lines(text)
    if lines[pol.start] != "\trefreshPolicy":
        out.append("the refreshPolicy block is not at table level")
    for key in ("rollingWindowGranularity", "incrementalGranularity"):
        v = pol.scalars.get(key)
        if v is not None and v not in GRANULARITIES:
            out.append(f"{key} '{v}' is not one of {', '.join(GRANULARITIES)}")
    for key in ("rollingWindowPeriods", "incrementalPeriods"):
        v = pol.scalars.get(key)
        if v is not None and not re.fullmatch(r"[1-9]\d*", v):
            out.append(f"{key} '{v}' is not a positive whole number")
    v = pol.scalars.get("incrementalPeriodsOffset")
    if v is not None and not re.fullmatch(r"-?\d+", v):
        out.append(f"incrementalPeriodsOffset '{v}' is not a whole number")
    parts = partitions(text)
    if len(parts) == 1 and pol.source and parts[0].source != pol.source:
        out.append("its sourceExpression differs from the partition source")
    return out


# ───────────────────────── configuration ─────────────────────────

def settings(fleet) -> dict:
    conf = fleet.setting("incremental_refresh") or {}
    return conf if isinstance(conf, dict) else {}


def _window_override(fleet, model: str, table: str) -> Optional[dict]:
    for d in (fleet.sections.get(model) or {}).get("refresh_windows", ()):
        if d.get("table") == table:
            return d
    return None


def _is_fact(fleet, table, roles) -> bool:
    conf = settings(fleet)
    if table.name in set(conf.get("exempt") or []):
        return False
    pat = conf.get("fact_pattern")
    if pat:
        return bool(re.search(pat, table.name))
    return roles.is_fact(table)


# ───────────────────────── detection ─────────────────────────

def _f(kind, model, ref, detail, **data):
    from .detect import Finding
    return Finding(kind=kind, model=model, ref=ref, detail=detail, data=data)


def refresh_findings(name: str, fleet, model, cache: dict) -> list:
    if settings(fleet).get("enabled") is False:
        return []
    from .roles import Roles
    roles = Roles(fleet.raw)
    tdef = fleet.template_definition
    out = []
    for tname, table in sorted(model.tables.items()):
        if not _is_fact(fleet, table, roles):
            continue
        text = te.read_text(table.file)
        mine = parse_policy(text)
        t_file = tdef / "tables" / table.file.name
        if t_file not in cache:
            cache[t_file] = parse_policy(te.read_text(t_file)) \
                if t_file.is_file() else None
        theirs = cache[t_file]
        if mine is not None:
            bad = problems(text, mine)
            if bad:
                out.append(_f("refresh_policy.invalid", name, tname,
                              f"the refresh policy of '{tname}' is malformed: "
                              + "; ".join(bad), table=tname, problems=bad))
        if theirs is None:
            if mine is not None and t_file.is_file():
                out.append(_f("refresh_policy.client_only", name, tname,
                              f"'{name}' gives '{tname}' a refresh policy the "
                              f"template's table does not have (report-only)",
                              table=tname))
            continue
        override = _window_override(fleet, name, tname)
        if override is not None and override.get("kind") == "absent":
            continue
        expected = dict(theirs.scalars)
        source = "template"
        if override is not None:
            expected.update({k: str(override[k]) for k in SCALARS
                             if k in override})
            source = "override"
        if mine is None:
            why = ineligible(text)
            windowed = "RangeStart" in (partitions(text) or [
                PartitionInfo("", "", "", "", [])])[0].source
            detail = (f"the template gives '{tname}' an incremental-refresh "
                      f"policy and '{name}' has none")
            if why:
                detail += f"; it cannot take one as is: {why} (report-only)"
            elif windowed:
                detail += ("; the source is already windowed, so only the "
                           "policy metadata is added")
            out.append(_f("refresh_policy.missing", name, tname, detail,
                          table=tname, expected=expected,
                          policy_type=theirs.policy_type or "basic",
                          ineligible=why or "", windowed=windowed))
            continue
        diffs = {k: (mine.scalars.get(k), v) for k, v in expected.items()
                 if mine.scalars.get(k) != v}
        if diffs:
            out.append(_f(
                "refresh_policy.window_drift", name, tname,
                f"the refresh window of '{tname}' differs from the {source}: "
                + "; ".join(f"{k} {a!r} -> {b!r}" for k, (a, b)
                            in sorted(diffs.items())),
                table=tname, expected=expected, source=source))
    return out


# ───────────────────────── fixes ─────────────────────────

def _table_path(fleet, f) -> Path:
    path = fleet.model_definition(f.model) / "tables" / f"{f.data['table']}.tmdl"
    if not path.is_file():
        tables = fleet.model_definition(f.model) / "tables"
        from .tmdl import parse_table_file
        for p in tables.glob("*.tmdl"):
            if parse_table_file(p).name == f.data["table"]:
                return p
        raise DriftDoctorError(f"no table file for '{f.data['table']}'")
    return path


def apply_missing(fleet, f) -> tuple:
    from .remediate import FileChange, _Skip
    if f.data.get("ineligible"):
        raise _Skip(f"'{f.data['table']}' cannot take a refresh policy: "
                    f"{f.data['ineligible']}")
    path = _table_path(fleet, f)
    old = te.read_text(path)
    if parse_policy(old) is not None:
        return ("refresh policy already present", [])
    part = partitions(old)[0]
    block = ["\trefreshPolicy", f"\t\tpolicyType: {f.data['policy_type']}"]
    block += [f"\t\t{k}: {f.data['expected'][k]}" for k in SCALARS
              if k in f.data["expected"]]
    block.append("\t\tsourceExpression =")
    block += part.raw_body
    block.append("")
    lines = old.split("\n")
    at = next(i for i, ln in enumerate(lines)
              if re.match(r"^\tpartition\b", ln))
    new = "\n".join(lines[:at] + block + lines[at:])
    return (f"add the template's incremental-refresh policy to "
            f"'{f.data['table']}' built from the model's own partition source"
            f" (a full refresh is owed)", [FileChange(path, old, new)])


def apply_window(fleet, f) -> tuple:
    from .remediate import FileChange
    path = _table_path(fleet, f)
    old = te.read_text(path)
    pol = parse_policy(old)
    if pol is None:
        raise DriftDoctorError(f"'{f.data['table']}' has no refresh policy "
                               f"any more; re-run detect")
    lines = old.split("\n")
    missing = []
    for key, value in f.data["expected"].items():
        for i in range(pol.start + 1, pol.end):
            if re.match(rf"^\t\t{key}\s*:", lines[i]):
                lines[i] = f"\t\t{key}: {value}"
                break
        else:
            missing.append(f"\t\t{key}: {value}")
    if missing:
        lines[pol.start + 1:pol.start + 1] = missing
    new = "\n".join(lines)
    return (f"rewrite the refresh window of '{f.data['table']}' "
            f"(a full refresh is owed)", [FileChange(path, old, new)])


def apply_realign(fleet, f) -> tuple:
    from .remediate import FileChange
    path = _table_path(fleet, f)
    old = te.read_text(path)
    pol = parse_policy(old)
    parts = partitions(old)
    if pol is None or len(parts) != 1:
        raise DriftDoctorError(f"'{f.data['table']}' cannot be realigned "
                               f"automatically; fix it by hand")
    lines = old.split("\n")
    i = next((k for k in range(pol.start, pol.end)
              if re.match(r"^\t\tsourceExpression\s*=", lines[k])), None)
    if i is None:
        raise DriftDoctorError("the policy has no sourceExpression")
    _b, j = _body(lines, i, 2)
    end = j
    while end > i + 1 and not lines[end - 1].strip():
        end -= 1
    lines[i + 1:end] = parts[0].raw_body
    new = "\n".join(lines)
    return (f"copy the partition source of '{f.data['table']}' into its "
            f"refresh policy (a full refresh is owed)",
            [FileChange(path, old, new)])

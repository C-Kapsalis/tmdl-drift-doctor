"""Named measure removals outside the template-retirement channel.

* ``plan_purge``: remove explicitly named measures from chosen models, on
  whatever table hosts them (``drift-doctor purge --measure``). A measure
  something else still calls is blocked, and two hosts need ``--table``.
* ``plan_retire``: retire a measure that was superseded by another
  (``drift-doctor retire OLD --new NEW``): report bindings move to NEW
  first, and the old declaration goes only once nothing points at it;
  ``--drop`` deletes a declaration nothing in DAX calls.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import tmdl_edit as te
from .renames import declares
from .tmdl import parse_model, strip_comments


@dataclass
class Item:
    model: str
    measure: str
    status: str                 # would-remove | removed | skipped | blocked
    reason: str = ""
    path: Optional[Path] = None
    old: Optional[str] = None
    new: Optional[str] = None
    report_refs: Optional[int] = None

    def diff(self) -> str:
        if self.path is None or self.new is None:
            return ""
        return "".join(difflib.unified_diff(
            self.old.splitlines(True), self.new.splitlines(True),
            fromfile=f"a/{self.path}", tofile=f"b/{self.path}"))


def _unqualified_refs(expr: str, name: str) -> bool:
    from .tmdl import blank_strings
    text = re.sub(r"(?:'(?:[^']|'')*'|\b[A-Za-z_]\w*)\s*\[[^\]]*\]", " ",
                  blank_strings(expr or ""))
    return f"[{name}]" in text


def plan_purge(model_name: str, model_dir: Path, measure: str,
               table: Optional[str] = None) -> Item:
    defn = Path(model_dir) / "definition"
    model = parse_model(defn)
    hosts = sorted(model.measure_hosts().get(measure) or [])
    item = Item(model_name, measure, "skipped")
    if not hosts:
        item.reason = "nothing to remove"
        return item
    if table:
        if table not in hosts:
            item.reason = f"not hosted on '{table}' (hosts: {', '.join(hosts)})"
            return item
        host = table
    elif len(hosts) > 1:
        item.status = "blocked"
        item.reason = (f"hosted on {', '.join(hosts)}; pick one with --table")
        return item
    else:
        host = hosts[0]
    users = []
    for t in model.tables.values():
        for m in t.measures.values():
            if not (t.name == host and m.name == measure) and \
                    _unqualified_refs(m.expression, measure):
                users.append(f"{t.name}[{m.name}]")
        for c in t.columns.values():
            if c.expression and _unqualified_refs(c.expression, measure):
                users.append(f"{t.name}[{c.name}]")
    if users:
        item.status = "blocked"
        item.reason = "still referenced by " + ", ".join(sorted(users))
        return item
    path = model.tables[host].file
    old = path.read_bytes().decode("utf-8")
    eol = "\r\n" if "\r\n" in old else "\n"
    new = te.remove_block(old.replace("\r\n", "\n"), "measure", measure)
    te.assert_tmdl_valid(new)
    from .renames import count_report_refs
    item.status = "would-remove"
    item.path, item.old, item.new = path, old, new.replace("\n", eol)
    item.report_refs = count_report_refs(model_dir, measure)
    item.reason = f"remove from '{host}'"
    if item.report_refs:
        item.reason += (f"; WARNING: {item.report_refs} report reference(s) "
                        f"bind it and will break")
    return item


def dax_reference_count(defn: Path, name: str) -> int:
    n = 0
    for f in sorted((Path(defn) / "tables").glob("*.tmdl")):
        text = strip_comments(f.read_text(encoding="utf-8"))
        n += text.count(f"[{name}]")
    return n


def _declaring_file(defn: Path, name: str) -> Optional[Path]:
    for f in sorted((Path(defn) / "tables").glob("*.tmdl")):
        if declares(f.read_text(encoding="utf-8"), name):
            return f
    return None


@dataclass
class RetireResult:
    model: str
    status: str
    detail: str = ""


def plan_retire(model_name: str, model_dir: Path, old: str,
                new: Optional[str], apply: bool) -> Optional[RetireResult]:
    from .renames import count_report_refs, report_changes
    defn = Path(model_dir) / "definition"
    host = _declaring_file(defn, old)
    refs = dax_reference_count(defn, old)
    # the declaration line itself is not a reference
    reports = count_report_refs(model_dir, old) or 0
    if refs:
        return RetireResult(model_name, "REFUSED",
                            f"{refs} DAX reference(s) to [{old}] remain")
    if host is None and not reports:
        return None
    if new is None:                      # --drop
        if host is None:
            return RetireResult(model_name, "nothing to do",
                                f"{reports} report binding(s) to '{old}' "
                                f"stay (harmless)")
        if not apply:
            return RetireResult(model_name, "dry run",
                                f"drop the declaration from {host.name}; "
                                f"{reports} report binding(s) stay")
        return _remove_decl(model_name, host, old,
                            f"dropped; {reports} report binding(s) stay")
    if _declaring_file(defn, new) is None:
        return RetireResult(model_name, "SKIPPED",
                            f"'{new}' is not declared in this model")
    if not apply:
        where = f"; remove the declaration from {host.name}" if host else ""
        return RetireResult(model_name, "dry run",
                            f"move {reports} report binding(s) to '{new}'"
                            f"{where}")
    changes, _n = report_changes(model_dir, old, new)
    for path, _raw, data in changes:
        Path(path).write_bytes(data)
    left = count_report_refs(model_dir, old) or 0
    if left:
        return RetireResult(model_name, "ABORTED",
                            f"{left} report binding(s) to '{old}' remain; "
                            f"the declaration stays")
    if host is None:
        return RetireResult(model_name, "retired",
                            f"moved {reports} binding(s); no declaration")
    return _remove_decl(model_name, host, old,
                        f"moved {reports} binding(s) and removed the "
                        f"declaration")


def _remove_decl(model_name, host: Path, old: str, ok: str) -> RetireResult:
    text = host.read_text(encoding="utf-8")
    pat = re.compile(r"^\tmeasure\s+(?:'(?:[^']|'')*'|\"[^\"]*\"|[^=]+?)\s*=",
                     re.MULTILINE)
    actual = None
    for m in pat.finditer(text):
        raw = m.group(0)[len("\tmeasure"):].rstrip("=").strip()
        name = raw[1:-1].replace("''", "'") if raw[:1] in "'\"" else raw
        if name == old:
            actual = name
            break
    new = te.remove_block(text, "measure", actual) if actual else None
    if new is None or declares(new, old):
        return RetireResult(model_name, "ABORTED",
                            f"the declaration of '{old}' could not be removed")
    te.assert_tmdl_valid(new)
    host.write_bytes(new.encode("utf-8"))
    return RetireResult(model_name, "retired", ok)

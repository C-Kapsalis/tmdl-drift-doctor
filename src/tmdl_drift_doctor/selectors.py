"""Selector measures: SWITCH arms that route a field-parameter pick to a
measure call.

* ``fieldparam.dangling_selector``: an arm whose called measure no longer
  exists makes the whole selector measure fail to process. Only the called
  names decide; the quoted key is merely compared with the slicer value.
  The fix removes those arm lines (and a separator left before a closing
  parenthesis).
* ``add_branch``: clone the arm of a sibling measure for a new measure, one
  line per SWITCH, so a new field-parameter row is not routed to BLANK().
"""

from __future__ import annotations

import re
from pathlib import Path

from . import tmdl_edit as te

_KEY = re.compile(r"(?:^|(?<=[=,(]))\s*\"(?:'(?:[^']|'')+'|[A-Za-z_][\w ]*)"
                  r"\[[^\]]+\]\"")
_BARE = re.compile(r"(?<![\w'\]])\[([^\]]+)\]")


def _norm(name: str) -> str:
    return name.replace("''", "'").casefold()


def dangling_arms(definition, measures: set) -> list:
    """[(file name, line number, [unresolved names])] of SWITCH arms that
    call a measure the model does not have."""
    known = {_norm(m) for m in measures}
    out = []
    tables = Path(definition) / "tables"
    for f in sorted(tables.glob("*.tmdl")) if tables.is_dir() else []:
        for i, line in enumerate(te.read_text(f).split("\n"), 1):
            s = line.strip()
            if s.startswith(("--", "//")):
                continue
            m = _KEY.search(line)
            if not m:
                continue
            gone = [n for n in _BARE.findall(line[m.end():])
                    if _norm(n) not in known]
            if gone:
                out.append((f.name, i, gone))
    return out


def findings(name: str, model) -> list:
    from .detect import Finding
    arms = dangling_arms(model.definition, set(model.measure_hosts()))
    if not arms:
        return []
    listed = "; ".join(f"{f}:{n} [{', '.join(g)}]" for f, n, g in arms)
    return [Finding(kind="fieldparam.dangling_selector", model=name,
                    ref="selector measures",
                    detail=(f"{len(arms)} selector arm(s) call a measure "
                            f"'{name}' does not have: {listed}"),
                    data={"arms": [[f, n, g] for f, n, g in arms]})]


def remove_lines(text: str, numbers: set) -> str:
    """Delete the given 1-based lines; a comma left before a closing ``)``
    by the deletion goes too. Line endings are kept."""
    crlf = "\r\n" in text
    lines = text.split("\n")
    keep, cut_at = [], []
    for i, ln in enumerate(lines, 1):
        if i in numbers:
            cut_at.append(len(keep))
            continue
        keep.append(ln)

    def blankish(ln):
        s = ln.strip()
        return not s or s.startswith(("--", "//"))

    for pos in cut_at:
        before = next((k for k in range(pos - 1, -1, -1)
                       if not blankish(keep[k])), None)
        after = next((k for k in range(pos, len(keep))
                      if not blankish(keep[k])), None)
        if before is not None and after is not None and \
                keep[before].rstrip("\r").rstrip().endswith(",") and \
                keep[after].lstrip().startswith(")"):
            ln = keep[before]
            cr = "\r" if ln.endswith("\r") else ""
            body = ln.rstrip("\r").rstrip()
            keep[before] = body[:-1] + cr
    out = "\n".join(keep)
    return out if crlf or "\r" not in out else out


def apply_dangling(fleet, f) -> tuple:
    from .remediate import FileChange
    defn = fleet.model_definition(f.model)
    by_file: dict = {}
    for fname, n, _g in f.data.get("arms") or []:
        by_file.setdefault(fname, set()).add(int(n))
    changes = []
    current = set(__import__("tmdl_drift_doctor.tmdl", fromlist=["x"])
                  .parse_model(defn).measure_hosts())
    live = {(a, b) for a, b, _g in dangling_arms(defn, current)}
    for fname, nums in sorted(by_file.items()):
        nums = {n for n in nums if (fname, n) in live}
        if not nums:
            continue
        path = defn / "tables" / fname
        old = te.read_text(path)
        changes.append(FileChange(path, old, remove_lines(old, nums)))
    n = sum(len(v) for v in by_file.values())
    return (f"remove {n} dangling selector branch(es)", changes)


# ───────────────────────── add-branch ─────────────────────────

def plan_branch(text: str, measure: str, sibling: str) -> tuple:
    """(status, new text or None, inserted count)."""
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(eol)

    def branch(ln, name):
        return f"[{name}]\"" in ln and "=" in ln

    sib = [i for i, ln in enumerate(lines) if branch(ln, sibling)]
    mine = [i for i, ln in enumerate(lines) if branch(ln, measure)]
    if not sib:
        return ("REFUSED: no branch of the sibling to copy", None, 0)
    if mine:
        if len(mine) == len(sib):
            return (f"already present ({len(mine)} branch(es))", None, 0)
        return (f"REFUSED: partial state ({len(mine)} branch(es) of "
                f"'{measure}' for {len(sib)} of '{sibling}')", None, 0)
    out = []
    for i, ln in enumerate(lines):
        out.append(ln)
        if i in sib:
            copy = ln.replace(f"[{sibling}]", f"[{measure}]")
            if copy == ln:
                return (f"REFUSED: line {i + 1} has no bracketed "
                        f"[{sibling}] to replace", None, 0)
            out.append(copy)
    return (f"insert {len(sib)} branch(es)", eol.join(out), len(sib))


def add_branch(path: Path, measure: str, sibling: str,
               write: bool) -> tuple:
    raw = path.read_bytes().decode("utf-8")
    status, new, n = plan_branch(raw, measure, sibling)
    if new is not None:
        te.assert_tmdl_valid(new.replace("\r\n", "\n"))
        if write:
            path.write_bytes(new.encode("utf-8"))
    return status, n

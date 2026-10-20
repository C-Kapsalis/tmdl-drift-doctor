"""Branch freshness: ``drift-doctor freshness``.

The ratchet catches a retired object after it is back in a derived model.
The cause is earlier: a branch cut before a retirement still carries the
object, and when the main line is merged into it the conflict resolver sees
two plausible sides and may keep the stale one.

This command warns the author of such a branch *before* the merge, by
comparing the retirement ledger the branch was cut from with the ledger on
the main line. It is advisory by construction: the ratchet is the gate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

MARKER = "<!-- drift-doctor:freshness -->"
CHECKABLE = ("table", "relationship", "measure", "column")
MAX_MODELS = 6


class FreshnessError(Exception):
    """A ledger or a model file could not be read."""


def _events(path) -> list:
    p = Path(path)
    if not p.exists():
        raise FreshnessError(f"no ledger at {p}")
    out = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FreshnessError(
                f"{p}:{i + 1}: malformed ledger line ({exc}). Repair that "
                f"line or restore the file from version control, then "
                f"retry.") from exc
        if not isinstance(event, dict):
            # Valid JSON, wrong shape: the line is not an event, so the
            # ledger cannot be read. Refuse it like a malformed line.
            raise FreshnessError(
                f"{p}:{i + 1}: the ledger line is a "
                f"{type(event).__name__}, not a ledger event object. Repair "
                f"that line or restore the file from version control, then "
                f"retry.")
        out.append(event)
    return out


def in_force(path) -> dict:
    """{(kind, ref): event} still retired in this ledger.

    A ``retired`` event opens a retirement; a later ``revived`` event with
    no model closes it fleet-wide.
    """
    state: dict = {}
    for e in _events(path):
        key = (e.get("kind"), e.get("ref"))
        if e.get("event") == "retired":
            state[key] = e
        elif e.get("event") == "revived" and e.get("model") is None:
            state.pop(key, None)
    return state


def unseen(base_path, head_path) -> list:
    """The retirements in force in the head ledger that are not in force
    in the base ledger, sorted by kind then ref."""
    base = in_force(base_path)
    head = in_force(head_path)
    return sorted((k for k in head if k not in base), key=lambda k: (k[0] or "",
                                                                  k[1] or ""))


def _named(text: str, name: str) -> bool:
    """Is `name` declared in this TMDL text, bare or single-quoted?"""
    if not name:
        return False
    esc = re.escape(name)
    squote = re.escape(name.replace("'", "''"))
    return re.search(rf"(?:'{squote}'|{esc})\b", text) is not None


def _table_text(model_dir: Path, table: str) -> str:
    """The TMDL text of one table: its file under ``tables/``, or every
    file that declares a table with that name."""
    out = []
    tables_dir = model_dir / "tables"
    for path in sorted(tables_dir.glob("*.tmdl")) if tables_dir.is_dir() else []:
        if path.stem.strip("'") == table:
            out.append(path.read_text(encoding="utf-8", errors="ignore"))
    if not out:
        for path in sorted(model_dir.rglob("*.tmdl")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if re.search(rf"^\s*table\s+(?:'{re.escape(table)}'|{re.escape(table)})\s*$",
                         text, re.M):
                out.append(text)
    return "\n".join(out)


def _all_text(model_dir: Path) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in sorted(model_dir.rglob("*.tmdl")))


def _relationship_present(text: str, ref: str) -> bool:
    """Judged on the FROM side only: a relationship whose from-column is
    ``From.c``. A ref that does not parse is absent."""
    m = re.match(r"^\s*([^\[]+)\[([^\]]+)\]\s*->\s*([^\[]+)\[([^\]]+)\]\s*$",
                 ref)
    if not m:
        return False
    from_table, from_col = m.group(1), m.group(2)
    if not _named(text, from_table):
        return False
    esc = re.escape(from_col)
    squote = re.escape(from_col.replace("'", "''"))
    return re.search(rf"(?:'{squote}'|{esc})\b", text) is not None


def _measure_present(model_dir: Path, ref: str) -> bool:
    """Table ``T`` declares measure ``M`` (bare or single-quoted name, at
    the start of a line after indentation, followed by ``=``). A ref with
    no host table counts any table."""
    m = re.match(r"^\s*([^\[]+)\[([^\]]+)\]\s*$", ref)
    host, measure = (m.group(1), m.group(2)) if m else (None, ref)
    esc = re.escape(measure)
    squote = re.escape(measure.replace("'", "''"))
    pattern = rf"^\s*measure\s+(?:'{squote}'|{esc})\s*="
    if host:
        text = _table_text(model_dir, host)
        if not text:
            return False
        return re.search(pattern, text, re.M) is not None
    for path in sorted((model_dir / "tables").glob("*.tmdl")) \
            if (model_dir / "tables").is_dir() else []:
        if re.search(pattern, path.read_text(encoding="utf-8", errors="ignore"),
                     re.M):
            return True
    return False


def _column_present(model_dir: Path, ref: str) -> bool:
    m = re.match(r"^\s*([^\[]+)\[([^\]]+)\]\s*$", ref)
    if not m:
        return False
    table, column = m.group(1), m.group(2)
    text = _table_text(model_dir, table)
    if not text:
        return False
    esc = re.escape(column)
    squote = re.escape(column.replace("'", "''"))
    return re.search(rf"^\s*column\s+(?:'{squote}'|{esc})\b", text, re.M) is not None


def _table_present(model_dir: Path, ref: str) -> bool:
    tables_dir = model_dir / "tables"
    if tables_dir.is_dir():
        for path in tables_dir.glob("*.tmdl"):
            if path.stem.strip("'") == ref:
                return True
    text = _all_text(model_dir)
    return re.search(rf"^\s*table\s+(?:'{re.escape(ref)}'|{re.escape(ref)})\s*$",
                     text, re.M) is not None


def at_risk(fleet, unseen_pairs) -> tuple:
    """(at_risk, not_checked).

    ``at_risk``: [((kind, ref), [model, ...])] for a checkable retirement
    that is still present in at least one derived model.
    ``not_checked``: [(kind, ref)] for a kind the tool does not check.
    """
    at: list = []
    unchecked: list = []
    for kind, ref in unseen_pairs:
        if kind not in CHECKABLE:
            unchecked.append((kind, ref))
            continue
        models = []
        for name in fleet.models:
            model_dir = fleet.model_definition(name)
            try:
                if kind == "table":
                    present = _table_present(model_dir, ref)
                elif kind == "relationship":
                    present = _relationship_present(_all_text(model_dir), ref)
                elif kind == "measure":
                    present = _measure_present(model_dir, ref)
                else:
                    present = _column_present(model_dir, ref)
            except OSError:
                present = False
            if present:
                models.append(name)
        if models:
            at.append(((kind, ref), sorted(models)))
    return at, unchecked


def _model_list(models: list) -> str:
    if len(models) <= MAX_MODELS:
        return ", ".join(models)
    return (", ".join(models[:MAX_MODELS]) +
            f"... (+{len(models) - MAX_MODELS})")


def render(base: dict, head: dict, unseen_pairs, at, unchecked, *,
           base_label: str = "") -> str:
    """The Markdown body written to ``--body-file``."""
    lines = [MARKER, ""]
    if not unseen_pairs:
        lines += [f"The branch base is current with the main line's ledger "
                  f"(`{base_label}`): there is nothing the main line has "
                  f"retired that this branch has not seen.", ""]
        return "\n".join(lines)

    n = len(head)
    lines += [f"This branch base predates **{n} ledger entr"
              f"{'y' if n == 1 else 'ies'}** on the main line.", ""]
    if at:
        lines += ["| Object | Kind | Models |", "|---|---|---|"]
        for (kind, ref), models in at:
            lines.append(f"| `{ref}` | {kind} | {_model_list(models)} |")
        lines += ["", "Keep the deletions when you merge, then let "
                  "`drift-doctor remediate --sync` settle the cascade.", ""]
    if unchecked:
        lines += ["The check is **incomplete**: the kind of "
                  f"{len(unchecked)} retirement(s) below could not be "
                  "checked, so this merge is not known to be safe.", ""]
    elif not at:
        lines += ["None of them is present in any derived model, so nothing "
                  "can be resurrected by this merge.", ""]
    if unchecked:
        lines += ["<details>", "<summary>Not checked "
                  f"({len(unchecked)}) — the check could not be performed "
                  "for this kind</summary>", ""]
        for kind, ref in unchecked:
            lines.append(f"- `{ref}` ({kind})")
        lines += ["", "</details>", ""]
    lines += ["The ratchet is the blocking gate.", ""]
    return "\n".join(lines)


def run(fleet, base_ledger, head_ledger=None, *, base_label: str = "",
        body_file=None, stale_flag_file=None) -> dict:
    """Compare two ledgers and report what the main line has retired that
    this branch has not seen."""
    head_ledger = head_ledger or fleet.ledger_path
    base = in_force(base_ledger)
    head = in_force(head_ledger)
    unseen_pairs = unseen(base_ledger, head_ledger)
    at, unchecked = at_risk(fleet, unseen_pairs)
    body = render(base, head, unseen_pairs, at, unchecked,
                  base_label=base_label)
    if body_file:
        path = Path(body_file)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    if stale_flag_file:
        path = Path(stale_flag_file)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1" if at else "", encoding="utf-8")
    return {"base": len(base), "head": len(head), "unseen": unseen_pairs,
            "at_risk": at, "not_checked": unchecked, "body": body}

"""The blast radius of a change: ``drift-doctor diff``.

Two ``detect --json`` reports in, one Markdown file out: what a template
change (or a merge) adds to the fleet's drift, what it resolves, and how
far each object reaches.

The report is written for a CI comment: a fixed hidden marker line first,
so a later run can edit the same sticky comment in place, and the footer
names the two reports it compared.

Nothing here writes a model. The report is read-only evidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import StaleBaselineError
from .detect import JSON_SCHEMA_VERSION
from .report_io import read_report_text

MARKER = "<!-- drift-doctor:blast-radius -->"
HEADING = "## Blast radius of this change"
MAX_OBJECTS = 15


class DiffError(Exception):
    """A report could not be read or parsed."""


def _load(path) -> dict:
    p = Path(path)
    try:
        doc = json.loads(read_report_text(p))
    except FileNotFoundError as exc:
        raise DiffError(f"no report at {p}") from exc
    except (OSError, ValueError) as exc:
        raise DiffError(f"the report at {p} could not be read: {exc}") from exc
    if not isinstance(doc, dict):
        raise DiffError(f"the report at {p} is not a report object")
    return doc


def _readable(path, doc) -> dict:
    """A report whose shape is wrong cannot be read.

    A report is an artifact: it may be hand-edited, truncated by a pipeline
    or written by another version of the tool. Whatever it is, the command
    must refuse it as a configuration problem, never crash on it.
    """
    p = Path(path)
    version = doc.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) \
            or version < JSON_SCHEMA_VERSION:
        raise DiffError(f"the report at {p} is not a `drift-doctor detect "
                        f"--json` report this version can compare "
                        f"(schema_version is {version!r}, it needs "
                        f"{JSON_SCHEMA_VERSION}); re-run `drift-doctor "
                        f"detect --json` to write one")
    if "models" not in doc:
        raise DiffError(f"the report at {p} has no models block, so it is not "
                        f"a `drift-doctor detect --json` report; re-run "
                        f"`drift-doctor detect --json` to write one")
    models = doc.get("models")
    if models is not None and not isinstance(models, dict):
        raise DiffError(f"the report at {p} has a models block that is a "
                        f"{type(models).__name__}, not an object; it cannot "
                        f"be read")
    for name, body in (models or {}).items():
        if not isinstance(body, dict):
            raise DiffError(f"the report at {p} has an entry for {name!r} "
                            f"that is a {type(body).__name__}, not an "
                            f"object; it cannot be read")
        findings = body.get("findings")
        if findings is None:
            continue
        if not isinstance(findings, list):
            raise DiffError(f"the report at {p} has findings for {name!r} "
                            f"that are a {type(findings).__name__}, not a "
                            f"list; they cannot be read")
        for f in findings:
            if not isinstance(f, dict):
                raise DiffError(f"the report at {p} has a finding for "
                                f"{name!r} that is a {type(f).__name__}, not "
                                f"an object; it cannot be read")
    return doc


def _items(doc: dict) -> dict:
    """{model: {(kind, ref)}} of the report's listed (not overridden)
    findings. A finding without a kind or a ref counts as ``?``."""
    out: dict = {}
    for model, body in (doc.get("models") or {}).items():
        pairs = set()
        for f in (body or {}).get("findings") or ():
            kind = f.get("kind") or "?"
            ref = f.get("ref") or "?"
            pairs.add((kind, ref))
        out[model] = pairs
    return out


def _head(kind: str) -> str:
    return kind.split(".", 1)[0]


def _scope_note(doc: dict, side: str) -> Optional[str]:
    """A warning when the report is not a full-fleet, unfiltered run."""
    scope = doc.get("scope")
    if scope is None:
        return (f"the {side} report carries no scope block, so what it "
                f"covered is unknown; models missing from it appear as "
                f"phantom deltas")
    if scope.get("kind_filter"):
        return (f"the {side} report is kind-filtered "
                f"({scope['kind_filter']!r}); models it left out appear as "
                f"phantom deltas")
    if not scope.get("fleet"):
        models = scope.get("models") or scope.get("targets") or []
        return (f"the {side} report is model-scoped ({', '.join(models) or '?'}"
                f"); the models it left out appear as phantom deltas")
    return None


def _tree_id(doc: dict) -> str:
    tree = doc.get("template_tree")
    return (str(tree)[:9] if tree else "?")


def _signed(n: int) -> str:
    return f"+{n}" if n > 0 else f"-{abs(n)}" if n < 0 else "+0"


def _breakdown(added: list, removed: list) -> str:
    """`measure +2, column -1`: the heads that moved, by reach (the
    biggest first), and within a head the added part before the removed."""
    heads: dict = {}
    for kind, _ref in added:
        heads.setdefault(_head(kind), [0, 0])[0] += 1
    for kind, _ref in removed:
        heads.setdefault(_head(kind), [0, 0])[1] += 1
    order = sorted(heads, key=lambda h: (-(heads[h][0] + heads[h][1]), h))
    parts = []
    for head in order:
        plus, minus = heads[head]
        if plus and minus:
            parts.append(f"{head} +{plus}, -{minus}")
        elif plus:
            parts.append(f"{head} +{plus}")
        else:
            parts.append(f"{head} -{minus}")
    return ", ".join(parts)


def render(base: dict, head: dict, *, stale: bool = False) -> str:
    """The Markdown body of the blast-radius report."""
    base_items = _items(base)
    head_items = _items(head)
    models = sorted(set(base_items) | set(head_items))

    added_by: dict = {}
    removed_by: dict = {}
    rows: list = []
    for model in models:
        b = base_items.get(model, set())
        h = head_items.get(model, set())
        added = sorted(h - b)
        removed = sorted(b - h)
        for pair in added:
            added_by.setdefault(pair, []).append(model)
        for pair in removed:
            removed_by.setdefault(pair, []).append(model)
        if added or removed:
            rows.append((model, added, removed))

    lines: list = [MARKER, "", HEADING, ""]

    for side, doc in (("base", base), ("head", head)):
        note = _scope_note(doc, side)
        if note:
            lines += [f"> **Warning:** {note}.", ""]

    if stale:
        lines += ["> **Warning:** the fleet's committed baseline no longer "
                  "matches the live template, so the comparison below is "
                  "against an older picture of the template.", ""]

    if not rows:
        lines += ["This change adds or resolves no actionable drift in any "
                  "model.", ""]
    else:
        objects = []
        seen = set()
        for model, added, removed in rows:
            for pair in added + removed:
                if pair not in seen:
                    seen.add(pair)
                    objects.append(pair)
        reach = {pair: len(added_by.get(pair, ())) +
                 len(removed_by.get(pair, ())) for pair in objects}
        objects.sort(key=lambda p: -reach[p])
        distinct = len(objects)
        lines.append(f"**{distinct} object(s) in {len(rows)} model(s).**")
        lines.append("")
        for pair in objects[:MAX_OBJECTS]:
            kind, ref = pair
            bits = []
            if added_by.get(pair):
                bits.append(f"new drift in {len(added_by[pair])} model(s)")
            if removed_by.get(pair):
                bits.append(f"resolves drift in {len(removed_by[pair])} "
                            f"model(s)")
            lines.append(f"- **{_head(kind)}** `{ref}` ({kind}) - "
                         f"{' and '.join(bits)}")
        if distinct > MAX_OBJECTS:
            lines.append(f"- ...and {distinct - MAX_OBJECTS} more object(s)")
        lines.append("")
        lines.append("| Model | Added | Removed | Net | By kind |")
        lines.append("|---|--:|--:|--:|---|")
        total_added = total_removed = 0
        for model, added, removed in rows:
            total_added += len(added)
            total_removed += len(removed)
            lines.append(f"| {model} | {len(added)} | {len(removed)} | "
                         f"{_signed(len(added) - len(removed))} | "
                         f"{_breakdown(added, removed)} |")
        lines.append("")
        lines.append(f"**+{total_added} / -{total_removed}** actionable "
                     f"item(s) across {len(rows)} of {len(models)} model(s).")
        lines.append("")

    lines += [f"_Compared base `{_tree_id(base)}` with head "
               f"`{_tree_id(head)}`._", ""]
    return "\n".join(lines)


def write_diff(base_path, head_path, out_path, *, fleet=None) -> dict:
    """Compare two reports and write the Markdown file.

    Returns a summary: ``{"path", "lines", "stale", "objects", "models"}``.
    """
    base = _readable(base_path, _load(base_path))
    head = _readable(head_path, _load(head_path))
    stale = False
    if fleet is not None:
        stale = _stale_baseline(fleet)
    body = render(base, head, stale=stale)
    out = Path(out_path)
    if out.parent and not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    base_items = _items(base)
    head_items = _items(head)
    models = sorted(set(base_items) | set(head_items))
    changed = 0
    for model in models:
        if base_items.get(model, set()) != head_items.get(model, set()):
            changed += 1
    return {"path": str(out), "lines": body.count("\n"), "stale": stale,
            "objects": changed, "models": len(models)}


def _stale_baseline(fleet) -> bool:
    """The same test detection uses: does the committed baseline still
    match the live template?"""
    from .baseline import ensure_baseline_fresh, load_baseline
    try:
        ensure_baseline_fresh(fleet, load_baseline(fleet))
    except StaleBaselineError:
        return True
    except Exception:
        return False
    return False

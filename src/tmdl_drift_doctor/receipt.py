"""The sync receipt: ``<state_dir>/last_sync.json``.

After a remediate run that applied something, the receipt records per model
what was applied (``kind`` and ``ref``) with that model's own stamps
(``generated_at``, ``template_tree``). Models the run did not touch keep
their entries verbatim, so a one-model run never re-attests the rest of the
fleet. A schema-1 receipt kept its stamps at file level only: an entry
carried over from one inherits those stamps once. A run that applied nothing
leaves the file byte-identical.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 2
RECEIPT = "last_sync.json"


def _tree(template: Optional[Path]) -> Optional[str]:
    from .detect import git_tree_hash
    return git_tree_hash(template) if template is not None else None


def write_sync_receipt(state_dir, result, template: Optional[Path] = None,
                       template_tree: Optional[str] = None) -> Optional[Path]:
    """Merge this run's applied fixes into the receipt. Returns the path
    when the file was written, None when nothing was applied."""
    applied: dict = {}
    for o in result.outcomes:
        if o.status == "applied":
            applied.setdefault(o.finding.model, []).append(
                {"kind": o.finding.kind, "ref": o.finding.ref})
    if not applied:
        return None
    path = Path(state_dir) / RECEIPT
    old: dict = {}
    if path.is_file():
        try:
            old = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            old = {}
    models = {}
    legacy = old.get("schema_version", SCHEMA_VERSION) < SCHEMA_VERSION
    for name, entry in (old.get("models") or {}).items():
        entry = dict(entry)
        if legacy:
            entry.setdefault("generated_at", old.get("generated_at"))
            entry.setdefault("template_tree", old.get("template_tree"))
        models[name] = entry
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tree = template_tree if template_tree is not None else _tree(template)
    for name, items in applied.items():
        models[name] = {"applied": items, "generated_at": stamp,
                        "template_tree": tree}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": SCHEMA_VERSION,
                                "models": dict(sorted(models.items()))},
                               indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path

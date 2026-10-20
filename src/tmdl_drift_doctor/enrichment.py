"""View-enrichment notices and their registry.

When a fix splices a template column (or measure) onto a table whose model
partition reads its own source view, that view must gain the source column
or the next refresh fails. Each such splice yields a notice; a run
aggregates its notices per (model, table), merges them into the persistent
registry ``<state_dir>/view_enrichment_outstanding.json`` (this run's keys
replace their own entries; every other entry passes through verbatim) and
reports and notifies only this run's entries.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REGISTRY = "view_enrichment_outstanding.json"
REPORT = "view_enrichment_report.json"


def notice(fleet, baseline: dict, f, template_view, model_view) -> dict:
    """The payload of one splice onto a source-divergent table."""
    if f.kind == "column.missing":
        table, col = f.ref[:f.ref.rindex("[")], f.ref[f.ref.rindex("[") + 1:-1]
        props = (((baseline.get("tables") or {}).get(table) or {})
                 .get("columns") or {}).get(col) or {}
        source = (props.get("sourceColumn") or col).strip().strip("[]")
        item = {"column": {"name": col, "sourceColumn": source}}
    else:
        table, name = f.ref[:f.ref.rindex("[")], f.ref[f.ref.rindex("[") + 1:-1]
        item = {"measure": name}
    rel = _rel(fleet, f.model)
    return {"model": f.model, "model_path": rel, "table": table,
            "template_view": template_view, "model_view": model_view, **item,
            "message": (f"{f.model}: '{table}' reads its own view "
                        f"'{model_view}', which must gain the source column "
                        f"of the spliced {f.ref} (the template reads "
                        f"'{template_view}')")}


def _rel(fleet, model: str) -> str:
    try:
        return fleet.models[model].relative_to(fleet.root).as_posix()
    except ValueError:
        return str(fleet.models[model])


def aggregate(notices: list) -> list:
    """Registry entries, one per (model, table), columns and measures
    de-duplicated and sorted."""
    by_key: dict = {}
    for n in notices:
        if not isinstance(n, dict) or "table" not in n:
            continue
        key = (n["model"], n["table"])
        e = by_key.setdefault(key, {
            "model": n["model"], "model_path": n.get("model_path"),
            "table": n["table"], "template_view": n.get("template_view"),
            "model_view": n.get("model_view"), "columns": [],
            "measures_applied": []})
        if n.get("column") and n["column"] not in e["columns"]:
            e["columns"].append(n["column"])
        if n.get("measure") and n["measure"] not in e["measures_applied"]:
            e["measures_applied"].append(n["measure"])
    out = []
    for e in by_key.values():
        e["columns"].sort(key=lambda c: c["name"])
        e["measures_applied"].sort()
        out.append(e)
    return out


def load_registry(path: Path) -> list:
    path = Path(path)
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8")) or {}
    return list(data.get("entries") or [])


def merge_registry(state_dir: Path, entries: list) -> list:
    """This run's entries replace their own (model, table) keys; every other
    entry is kept as it was, in order. Returns the merged list."""
    path = Path(state_dir) / REGISTRY
    old = load_registry(path)
    keys = {(e["model"], e["table"]) for e in entries}
    merged = [e for e in old if (e.get("model"), e.get("table")) not in keys]
    merged += entries
    if entries:
        path.write_text(json.dumps({"schema_version": 1, "entries": merged},
                                   indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    return merged


def write_report(state_dir: Path, entries: list) -> Path:
    path = Path(state_dir) / REPORT
    path.write_text(json.dumps({"entries": entries}, indent=2,
                               ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def notify(fleet, entries: list) -> str:
    """Run fleet.yml's ``notify_command`` once with this run's entries as
    JSON on stdin. Returns "" or a warning."""
    cmd = fleet.setting("notify_command")
    if not cmd or not entries:
        return ""
    try:
        r = subprocess.run(cmd, shell=True, input=json.dumps(entries),
                           text=True, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return f"notify_command failed: {e}"
    return "" if r.returncode == 0 else (f"notify_command exited "
                                         f"{r.returncode}: {r.stderr.strip()}")


def markdown(entries: list) -> str:
    if not entries:
        return ""
    lines = ["## Source views to enrich", "",
             "The model views below must gain the listed source columns, or "
             "the next refresh of the table fails.", "",
             "| model | table | model view | columns needed | measures tied |",
             "|---|---|---|---|---|"]
    for e in entries:
        cols = ", ".join(f"`{c.get('sourceColumn') or c.get('name')}`"
                         for c in e.get("columns") or []) or "-"
        meas = ", ".join(e.get("measures_applied") or []) or "-"
        lines.append(f"| {e.get('model')} | {e.get('table')} | "
                     f"{e.get('model_view')} | {cols} | {meas} |")
    return "\n".join(lines) + "\n"


def rescan(fleet, entries: list, models=None) -> list:
    """Warnings for registry entries whose recorded columns are gone from
    the model's table file."""
    warnings = []
    for e in entries:
        if models and e.get("model") not in models:
            continue
        name = e.get("model")
        if name not in fleet.models:
            continue
        path = fleet.model_definition(name) / "tables" / f"{e['table']}.tmdl"
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        gone = []
        for c in e.get("columns") or []:
            col = c.get("name")
            pat = (r"^\tcolumn\s+(?:" + re.escape(col) + r"|'"
                   + re.escape(col.replace("'", "''")) + r"'|\""
                   + re.escape(col) + r"\")(?:\s|$)")
            if not re.search(pat, text, re.MULTILINE):
                gone.append(col)
        if gone:
            warnings.append(f"{name}/{e['table']}: recorded column(s) "
                            f"{', '.join(gone)} are no longer in the model")
    return warnings

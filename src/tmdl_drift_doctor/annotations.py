"""Category annotations: model.tmdl ``annotation Category_<Key>`` lines must
match the categories the model's encoded mapping table adds on top of the
template's.

* ``category_annotation.orphaned`` (report-only): an annotation naming a
  category the model's mapping does not contain;
* ``category_annotation.undeclared`` (fixable): a model-only mapping
  category with no annotation. The fix adds one, valued from version
  control when the annotation existed before, else ``"<Category> metrics"``;
  a category whose name cannot round-trip to a key is left for a person.

A shrink-only baseline file (``category_annotations.baseline``) accepts
known exceptions per model key ``<parent folder>/<model folder>``.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

from . import mapping_codec as mc

PREFIX = "Category_"
PROPERTY_SUFFIXES = ("_RequiredServices", "_HealthPolicy")
_ANNOT = re.compile(r"^annotation\s+(Category_[^\s=]+)\s*=\s*(.*?)\s*$",
                    re.MULTILINE)


def _conf(fleet) -> dict:
    conf = fleet.setting("category_annotations") or {}
    return conf if isinstance(conf, dict) else {}


def model_key(path: Path) -> str:
    path = Path(path)
    return f"{path.parent.name}/{path.name}"


def key_to_name(key: str) -> str:
    return key[len(PREFIX):].replace("_", " ")


def annotated(text: str, suffixes=PROPERTY_SUFFIXES) -> dict:
    """{category name: value} of the category annotations in model.tmdl."""
    out = {}
    for m in _ANNOT.finditer(text):
        key = m.group(1)
        if key.endswith(tuple(suffixes)):
            continue
        out[key_to_name(key)] = m.group(2)
    return out


def mapping_categories(definition: Path, table: str) -> Optional[list]:
    path = Path(definition) / "tables" / f"{table}.tmdl"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    blobs = re.findall(r'Binary\.FromText\(\s*"([A-Za-z0-9+/=]*)"', text)
    if not blobs:
        return None
    rows = mc.decode_rows(blobs[0])
    return list(dict.fromkeys(r[0] for r in rows
                              if isinstance(r, list) and r
                              and isinstance(r[0], str)))


def load_baseline(fleet) -> dict:
    rel = _conf(fleet).get("baseline")
    if not rel:
        return {"orphaned": {}, "undeclared": {}}
    path = (fleet.root / rel)
    if not path.is_file():
        return {"orphaned": {}, "undeclared": {}}
    data = json.loads(path.read_text(encoding="utf-8")) or {}
    return {"orphaned": data.get("orphaned") or {},
            "undeclared": data.get("undeclared") or {}}


def _table(fleet) -> Optional[str]:
    return _conf(fleet).get("table") or (fleet.mapping_tables[0]
                                         if fleet.mapping_tables else None)


def findings(name: str, fleet, cache: dict) -> list:
    from .detect import Finding
    table = _table(fleet)
    if table is None:
        return []
    if "template" not in cache:
        cache["template"] = mapping_categories(fleet.template_definition,
                                               table)
        cache["baseline"] = load_baseline(fleet)
    template = cache["template"]
    if not template:
        return []
    definition = fleet.model_definition(name)
    try:
        mine = mapping_categories(definition, table)
    except mc.CodecError:
        return []
    if mine is None:
        return []
    model_file = definition / "model.tmdl"
    text = model_file.read_text(encoding="utf-8") if model_file.is_file() \
        else ""
    suffixes = tuple(_conf(fleet).get("property_suffixes")
                     or PROPERTY_SUFFIXES)
    notes = annotated(text, suffixes)
    key = model_key(fleet.models[name])
    base = cache["baseline"]
    out = []
    for cat in sorted(set(notes) - set(mine)
                      - set(base["orphaned"].get(key) or [])):
        out.append(Finding(
            kind="category_annotation.orphaned", model=name, ref=cat,
            detail=(f"model.tmdl annotates category '{cat}' but the mapping "
                    f"table has no such category; retire or repoint the "
                    f"annotation by hand"),
            data={"category": cat}))
    for cat in [c for c in mine if c not in template and c not in notes
                and c not in (base["undeclared"].get(key) or [])]:
        ok = bool(re.fullmatch(r"[A-Za-z0-9]+(?: [A-Za-z0-9]+)*", cat))
        out.append(Finding(
            kind="category_annotation.undeclared", model=name, ref=cat,
            detail=(f"the mapping table adds category '{cat}', which no "
                    f"`Category_*` annotation declares"
                    + ("" if ok else "; its name cannot become an annotation "
                       "key, so it needs a manual decision")),
            data={"category": cat, "manual": not ok}))
    return out


def _history_value(model_file: Path, key: str) -> Optional[str]:
    """The newest value of `key` in version control: the HEAD version of
    the file first, else the newest commit that added or removed it."""
    folder = model_file.parent

    def git(*args):
        try:
            r = subprocess.run(["git", "-C", str(folder), *args],
                               capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout if r.returncode == 0 else None

    pat = re.compile(r"^[+-]?annotation\s+" + re.escape(key)
                     + r"\s*=\s*(.*?)\s*$")
    head = git("show", f"HEAD:./{model_file.name}")
    for line in (head or "").split("\n"):
        m = pat.match(line)
        if m:
            return m.group(1)
    log = git("log", "-p", "--format=commit %H", "--", model_file.name)
    if not log:
        return None
    added = removed = None
    for line in log.split("\n"):
        if line.startswith("commit "):
            if added or removed:
                break
            continue
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            m = pat.match(line)
            if m:
                if line.startswith("+"):
                    added = added or m.group(1)
                else:
                    removed = removed or m.group(1)
    return added or removed


def apply_undeclared(fleet, f) -> tuple:
    from .remediate import FileChange, _Skip
    if f.data.get("manual"):
        raise _Skip(f"manual: category '{f.ref}' cannot become an annotation "
                    f"key; declare it by hand")
    path = fleet.model_definition(f.model) / "model.tmdl"
    old = path.read_text(encoding="utf-8")
    cat = f.data["category"]
    key = PREFIX + cat.replace(" ", "_")
    if re.search(r"^annotation\s+" + re.escape(key) + r"\s*=", old,
                 re.MULTILINE):
        return ("annotation already present", [])
    value = _history_value(path, key) or json.dumps(f"{cat} metrics")
    line = f"annotation {key} = {value}"
    lines = old.split("\n")
    anchor = None
    for i, ln in enumerate(lines):
        if ln.startswith("annotation Category_"):
            anchor = i
    if anchor is None:
        for i, ln in enumerate(lines):
            if ln.startswith("annotation "):
                anchor = i
    if anchor is None:
        new = old.rstrip("\n") + "\n\n" + line + "\n"
    else:
        new = "\n".join(lines[:anchor + 1] + ["", line] + lines[anchor + 1:])
    return (f"declare category '{cat}' in model.tmdl",
            [FileChange(path, old, new)])

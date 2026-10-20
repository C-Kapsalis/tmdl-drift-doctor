"""Fleet configuration — ``fleet.yml``.

A fleet is one template model plus N derived models. All paths are resolved
relative to the fleet file's directory. A fuller example lives in the
documentation (docs/fleet-yml.md); the essentials::

    version: 1
    template: template/GymChain.SemanticModel
    models:
      alpha: franchises/alpha/GymChain.SemanticModel
      bravo:                                   # long form
        path: franchises/bravo/GymChain.SemanticModel
        gating: advisory                       # findings never fail CI
        gating_reason: still being aligned to the template
        secondary_models:                      # more models of the same entry
          - name: bravo-outlet
            path: franchises/bravo/Outlet.SemanticModel
            deviations: []
        overrides:                             # declared, reviewed divergence
          measures:
            - object: Visits[Total Visits]
              kind: expression
              reason: bravo counts zero-minute visits differently
    state_dir: .drift-doctor
    allowlist:
      kinds: [measure.missing, measure.expression_drift]
      expressions: [Reporting Start Date]

Nothing cascades by default: a drift kind the allowlist does not name is
detected and reported but never remediated, and only the NAMED shared
expressions are ever compared for content. Allowlist entries match kinds as
dotted prefixes at segment boundaries (``measure`` matches every
``measure.*`` kind; ``measure.miss`` matches nothing).

A model entry is a bare path or a mapping with ``path`` plus optional keys.
Overrides are either the flat list form (``object``/``kind``/``reason``) or
typed sections (``measures``, ``columns``, ``source_views``, ``tables``,
``relationships``, ``fieldparam_entries``, ``mapping_rows``, ``files``).
Every problem is collected and reported at once; nothing is half-loaded.
Whether each override ``object`` resolves is checked against the baseline
and ledger at detect time (``overrides.resolve_overrides``).
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from . import DriftDoctorError

GATING_MODES = ("enforced", "advisory")

# flat-list override kinds (the 1.x form) and the kinds a typed section takes
FLAT_KINDS = ("expression", "absent", "retain", "extension")

# section -> (required keys, optional keys, allowed `kind` values or None)
SECTIONS = {
    "measures": ({"object", "reason"}, {"kind", "owner", "added"},
                 ("expression", "absent")),
    "columns": ({"object", "reason"}, {"kind", "owner", "added"},
                ("absent", "extension")),
    "expressions": ({"object", "reason"}, {"kind", "owner", "added"},
                    ("expression", "absent")),
    "source_views": ({"table", "view", "reason"}, {"owner", "added"}, None),
    "tables": ({"name", "reason"}, {"owner", "added"}, None),
    "relationships": ({"signature", "reason"}, {"owner", "added"}, None),
    "fieldparam_entries": ({"category", "entry", "reason"},
                           {"owner", "added"}, None),
    "mapping_rows": ({"category", "key", "reason"}, {"owner", "added"}, None),
    "files": ({"path", "reason"}, {"owner", "added"}, None),
}

_ENTRY_KEYS = {"path", "gating", "gating_reason", "overrides",
               "secondary_models", "connection", "family", "source_divergent",
               "workspace", "rename_map", "visibility", "snapshots",
               "rename_profile"}
_SECONDARY_KEYS = {"name", "path", "deviations", "gating", "gating_reason",
                   "connection", "family"}
_CONNECTION_KEYS = {"project", "billing_project", "dataset"}
_FLAT_KEYS = {"object", "kind", "reason", "owner", "added"}
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

DEFAULT_GROWTH_SUFFIXES = ("Growth.SemanticModel", "insights.SemanticModel")


@dataclass(frozen=True)
class Override:
    """One declared, reviewed divergence for one derived model.

    * ``expression``: the model keeps its own definition of a shared object
      (drift on it is acknowledged, never cascaded);
    * ``absent``: the model deliberately lacks a template object;
    * ``retain``: the model deliberately keeps an object retired from the
      template (the retirement channel never removes it);
    * ``extension``: a derived-only column on a shared table is a declared
      extension (acknowledged instead of advisory);
    * ``file``: a whole file the template cascade must never write.
    """
    model: str
    object: str
    kind: str
    reason: str
    owner: Optional[str] = None
    added: Optional[str] = None
    section: Optional[str] = None
    added_raw: object = field(default=None, compare=False)

    def to_dict(self) -> dict:
        return {"object": self.object, "kind": self.kind,
                "reason": self.reason, "owner": self.owner,
                "added": self.added}

    def meta(self) -> dict:
        return {"reason": self.reason, "owner": self.owner,
                "added": self.added_raw if self.added_raw is not None
                else self.added}


@dataclass
class Fleet:
    root: Path
    template: Path                       # <model>.SemanticModel dir
    models: dict                         # name -> <model>.SemanticModel dir
    mapping_tables: list = field(default_factory=list)
    state_dir: Path = None
    allowlist_kinds: set = field(default_factory=set)
    allowlist_expressions: set = field(default_factory=set)
    gating: dict = field(default_factory=dict)      # name -> enforced|advisory
    gating_reasons: dict = field(default_factory=dict)
    overrides: dict = field(default_factory=dict)   # name -> [Override]
    sections: dict = field(default_factory=dict)    # name -> {section: [entry]}
    connection_parameters: set = field(default_factory=set)
    connections: dict = field(default_factory=dict)  # name -> {project, ...}
    default_project: str = ""
    entry_of: dict = field(default_factory=dict)    # model -> fleet entry
    families: dict = field(default_factory=dict)    # model -> family name
    growth_suffixes: tuple = DEFAULT_GROWTH_SUFFIXES
    source_divergent: dict = field(default_factory=dict)  # model -> {tables}
    raw: dict = field(default_factory=dict)          # the parsed fleet.yml
    path: Optional[Path] = None

    # ── gating / scope ──
    def is_advisory(self, name: str) -> bool:
        return self.gating.get(name) == "advisory"

    def allows(self, kind: str) -> bool:
        """Is `kind` named by the allowlist (dotted prefix match)?"""
        return any(kind_matches(kind, p) for p in self.allowlist_kinds)

    @property
    def primary_models(self) -> list:
        return [n for n in self.models if self.entry_of.get(n, n) == n]

    def setting(self, key: str, default=None):
        return self.raw.get(key, default)

    # ── paths ──
    @property
    def template_definition(self) -> Path:
        return self.template / "definition"

    def model_definition(self, name: str) -> Path:
        return self.models[name] / "definition"

    def model_root(self, name: str) -> Path:
        """The folder holding the model's ``*.SemanticModel`` (file
        exemptions are relative to it)."""
        return self.models[name].parent

    @property
    def template_root(self) -> Path:
        return self.template.parent

    @property
    def baseline_path(self) -> Path:
        return self.state_dir / "baseline.json"

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / "ledger.jsonl"

    @property
    def floors_path(self) -> Path:
        return self.state_dir / "floors.json"

    # ── connection contract ──
    def connection(self, name: str) -> tuple:
        """(project, billing project, dataset) the fleet declares for a
        model. Project is empty unless declared (leave the template's
        default); dataset falls back to ``<model id>_db``."""
        conn = self.connections.get(name) or {}
        project = conn.get("project") or ""
        billing = conn.get("billing_project") or project
        dataset = conn.get("dataset") or f"{self.entry_of.get(name, name)}_db"
        return project, billing, dataset

    # ── file exemptions ──
    def exempt_patterns(self, name: str) -> list:
        return [e.get("path") or "" for e in
                (self.sections.get(name) or {}).get("files") or []]

    def file_override(self, name: str, rel: str) -> Optional[Override]:
        """The ``files`` override covering `rel` (a path relative to the
        model root), or None."""
        rel = rel.replace("\\", "/")
        for e in (self.sections.get(name) or {}).get("files") or []:
            pat = (e.get("path") or "").strip()
            if pat and (rel == pat or fnmatch.fnmatchcase(rel, pat)):
                return _override(name, pat, "file", e, "files")
        return None


def kind_matches(kind: str, prefix: str) -> bool:
    """Dotted-prefix match at a segment boundary: ``measure.missing``
    matches ``measure.missing`` and ``measure.missing.f``; ``measure``
    matches every ``measure.*``; ``measure.miss`` matches nothing."""
    prefix = (prefix or "").rstrip(".")
    return bool(prefix) and (kind == prefix or kind.startswith(prefix + "."))


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _override(model, obj, kind, entry, section) -> Override:
    added = entry.get("added")
    owner = entry.get("owner")
    return Override(model=model, object=obj, kind=kind,
                    reason=" ".join(str(entry.get("reason") or "").split()),
                    owner=str(owner) if owner is not None else None,
                    added=str(added) if added is not None else None,
                    section=section, added_raw=added)


def _mappings(prefix, items, errors):
    """Yield (location, entry) for each mapping in `items`; anything else
    is recorded as an error."""
    for n, item in enumerate(items):
        here = "%s[%d]" % (prefix, n)
        if isinstance(item, dict):
            yield here, item
        else:
            errors.append(here + " must be a mapping")


def _flat_overrides(model, raw, errors) -> list:
    out = []
    for where, entry in _mappings(f"models.{model}.overrides", raw, errors):
        unknown = sorted(set(entry) - _FLAT_KEYS)
        if unknown:
            errors.append(f"{where}: unknown key(s) {', '.join(unknown)} "
                          f"(allowed: {', '.join(sorted(_FLAT_KEYS))})")
        obj, kind = _text(entry.get("object")), entry.get("kind")
        ok = True
        if not obj:
            errors.append(f"{where}: `object` must be a non-empty string")
            ok = False
        if kind not in FLAT_KINDS:
            errors.append(f"{where}: `kind` must be one of "
                          f"{', '.join(FLAT_KINDS)} (got {kind!r})")
            ok = False
        if not _text(entry.get("reason")):
            errors.append(f"{where}: `reason` must be a non-empty string; an "
                          f"override without a reason is not reviewable")
            ok = False
        if ok:
            out.append(_override(model, obj, kind, entry, None))
    return out


def _typed_overrides(model, raw, errors) -> tuple:
    """(flat Override list, {section: [entries]}) from the typed form."""
    flat, kept = [], {}
    for section, entries in raw.items():
        if section not in SECTIONS:
            errors.append(f"models.{model}.overrides: unknown section "
                          f"`{section}` (sections: {', '.join(SECTIONS)})")
            continue
        if entries is None:
            continue
        if not isinstance(entries, list):
            errors.append(f"models.{model}.overrides.{section} must be a list")
            continue
        required, optional, kinds = SECTIONS[section]
        good = []
        for where, entry in _mappings(f"models.{model}.overrides.{section}",
                                      entries, errors):
            bad = False
            for key in sorted(set(entry) - required - optional):
                errors.append(f"{where}: `{key}` is not a key of section "
                              f"`{section}` (keys: "
                              f"{', '.join(sorted(required | optional))})")
                bad = True
            if not _text(entry.get("reason")):
                errors.append(f"{where}: `reason` must be a non-empty string; "
                              f"an override without a reason is not "
                              f"reviewable")
                bad = True
            for key in sorted(required - {"reason", "path"}):
                if not _text(entry.get(key)):
                    if section == "columns" and key == "object":
                        continue    # reported by the column-shape check
                    errors.append(f"{where}: `{key}` must be a non-empty "
                                  f"string")
                    bad = True
            if section == "files" and not isinstance(entry.get("path"), str):
                errors.append(f"{where}: `path` must be a string")
                bad = True
            kind = entry.get("kind")
            if kinds is not None:
                kind = kind or kinds[0]
                if kind not in kinds:
                    errors.append(f"{where}: `kind` must be one of "
                                  f"{', '.join(kinds)} (got {kind!r})")
                    bad = True
            obj = _text(entry.get("object"))
            if section == "measures" and kind == "expression" and obj \
                    and not re.match(r"^.+\[[^\]]+\]$", obj):
                errors.append(f"{where}: `{obj}` is a bare name; an "
                              f"`expression` override needs the qualified "
                              f"Table[Measure] form")
                bad = True
            if section == "columns":
                m = re.match(r"^(.+)\[([^\]]*)\]$", obj)
                if not m or not m.group(2).strip():
                    errors.append(f"{where}: `{obj}`: a column is required "
                                  f"(write it as Table[column])")
                    bad = True
            if bad:
                continue
            good.append(entry)
            if section in ("measures", "columns", "expressions"):
                flat.append(_override(model, obj, kind, entry, section))
            elif section == "tables":
                flat.append(_override(model, entry["name"].strip(), "retain",
                                      entry, section))
            elif section == "relationships":
                flat.append(_override(model, entry["signature"].strip(),
                                      "retain", entry, section))
        kept[section] = good
    return flat, kept


def _parse_overrides(model, raw, errors) -> tuple:
    if raw is None:
        return [], {}
    if isinstance(raw, list):
        return _flat_overrides(model, raw, errors), {}
    if isinstance(raw, dict):
        return _typed_overrides(model, raw, errors)
    errors.append(f"models.{model}.overrides must be a list or a mapping "
                  f"of typed sections")
    return [], {}


def _check_connection(where, conn, errors) -> dict:
    if conn is None:
        return {}
    if not isinstance(conn, dict):
        errors.append(f"{where}.connection must be a mapping")
        return {}
    for key in sorted(set(conn) - _CONNECTION_KEYS):
        errors.append(f"{where}.connection: unknown key `{key}` (keys: "
                      f"{', '.join(sorted(_CONNECTION_KEYS))})")
    return {k: str(v) for k, v in conn.items() if v not in (None, "")}


def _check_workspace(where, ws, errors, seen_guids) -> None:
    if ws is None:
        return
    if not isinstance(ws, dict):
        errors.append(f"{where}.workspace must be a mapping")
        return
    if "name" in ws and not _text(ws.get("name")):
        errors.append(f"{where}.workspace.name must be a non-empty string")
    for key in ("guid", "tenant_id"):
        if key in ws and not _GUID.match(str(ws.get(key) or "")):
            errors.append(f"{where}.workspace.{key} is not a GUID: "
                          f"{ws.get(key)!r}")
    guid = str(ws.get("guid") or "").lower()
    if guid:
        if guid in seen_guids:
            errors.append(f"{where}.workspace.guid {guid} is also the "
                          f"workspace of {seen_guids[guid]}")
        seen_guids.setdefault(guid, where)
    if "verified" in ws:
        v = ws.get("verified")
        if not (v is False or re.match(r"^\d{4}-\d{2}-\d{2}$", str(v))):
            errors.append(f"{where}.workspace.verified must be a "
                          f"YYYY-MM-DD date or false (got {v!r})")


def read_fleet_yaml(path) -> dict:
    path = Path(path)
    if not path.exists():
        raise DriftDoctorError(
            f"fleet file not found: {path}. Create one (see the "
            f"configuration reference in the documentation) or point "
            f"--fleet at an existing fleet.yml.")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise DriftDoctorError(f"{path}: not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise DriftDoctorError(f"{path}: the top level must be a mapping")
    return data


def load_fleet(path) -> Fleet:
    path = Path(path)
    data = read_fleet_yaml(path)

    shape = (
        (data.get("version") == 1,
         "version must be 1 (got %r)" % (data.get("version"),)),
        (bool(data.get("template")), "`template` is required"),
        (isinstance(data.get("models"), dict) and bool(data.get("models")),
         "`models` must be a non-empty mapping of name -> path"),
    )
    errors = [msg for ok, msg in shape if not ok]
    if errors:
        raise DriftDoctorError(f"{path}: " + "; ".join(errors))

    root = path.parent.resolve()
    template = (root / str(data["template"])).resolve()
    fleet = Fleet(root=root, template=template, models={}, raw=data,
                  path=path.resolve())
    seen_paths: dict = {}
    seen_guids: dict = {}

    def add_model(name, rel, entry_name, where):
        target = (root / str(rel)).resolve()
        if name in fleet.models:
            errors.append(f"{where}: the model name '{name}' is used twice")
        if target in seen_paths:
            errors.append(f"{where}: path {rel} is also the path of "
                          f"{seen_paths[target]}")
        seen_paths.setdefault(target, name)
        fleet.models[name] = target
        fleet.entry_of[name] = entry_name

    for name, entry in data["models"].items():
        name = str(name)
        where = f"models.{name}"
        if isinstance(entry, str):
            entry = {"path": entry}
        if not isinstance(entry, dict) or not entry.get("path"):
            errors.append(f"{where}: no model_path (the entry must be a path "
                          f"or a mapping with a `path` key)")
            continue
        for key in sorted(set(entry) - _ENTRY_KEYS):
            errors.append(f"{where}: unknown key `{key}` (allowed: "
                          f"{', '.join(sorted(_ENTRY_KEYS))})")
        mode = entry.get("gating") or "enforced"
        if mode not in GATING_MODES:
            errors.append(f"{where}.gating must be one of "
                          f"{', '.join(GATING_MODES)} (got {mode!r})")
        add_model(name, entry["path"], name, where)
        fleet.gating[name] = mode
        if entry.get("gating_reason"):
            fleet.gating_reasons[name] = str(entry["gating_reason"])
        flat, sections = _parse_overrides(name, entry.get("overrides"), errors)
        fleet.overrides[name] = flat
        fleet.sections[name] = sections
        fleet.connections[name] = _check_connection(where,
                                                    entry.get("connection"),
                                                    errors)
        if entry.get("family"):
            fleet.families[name] = str(entry["family"])
        if entry.get("source_divergent"):
            fleet.source_divergent[name] = set(entry["source_divergent"])
        _check_workspace(where, entry.get("workspace"), errors, seen_guids)

        secondaries = entry.get("secondary_models") or []
        if not isinstance(secondaries, list):
            errors.append(f"{where}.secondary_models must be a list")
            secondaries = []
        for i, sec in enumerate(secondaries):
            swhere = f"{where}.secondary_models[{i}]"
            if not isinstance(sec, dict) or not sec.get("name") \
                    or not sec.get("path"):
                errors.append(f"{swhere} needs `name` and `path`")
                continue
            for key in sorted(set(sec) - _SECONDARY_KEYS):
                errors.append(f"{swhere}: unknown key `{key}`")
            if not isinstance(sec.get("deviations"), list):
                errors.append(f"{swhere}: `deviations` must be declared (a "
                              f"list; [] means an exact clone of the entry)")
            sname = str(sec["name"])
            add_model(sname, sec["path"], name, swhere)
            fleet.gating[sname] = sec.get("gating") or mode
            if sec.get("gating") and sec["gating"] not in GATING_MODES:
                errors.append(f"{swhere}.gating must be one of "
                              f"{', '.join(GATING_MODES)}")
            reason = sec.get("gating_reason") or entry.get("gating_reason")
            if reason:
                fleet.gating_reasons[sname] = str(reason)
            # an entry's declarations cover every model of the entry
            fleet.overrides[sname] = [
                Override(model=sname, object=o.object, kind=o.kind,
                         reason=o.reason, owner=o.owner, added=o.added,
                         section=o.section, added_raw=o.added_raw)
                for o in flat]
            fleet.sections[sname] = sections
            fleet.connections[sname] = dict(fleet.connections[name])
            fleet.connections[sname].update(_check_connection(
                swhere, sec.get("connection"), errors))
            if sec.get("family"):
                fleet.families[sname] = str(sec["family"])

    meta = data.get("metadata")
    if isinstance(meta, dict) and "total" in meta \
            and meta["total"] != len(data["models"]):
        errors.append(f"metadata.total is {meta['total']} but fleet.yml "
                      f"defines {len(data['models'])} model entries")

    families = data.get("families") or {}
    growth = (families.get("growth") or {}) if isinstance(families, dict) else {}
    suffixes = growth.get("suffixes")
    if suffixes is not None:
        if not isinstance(suffixes, list) or len(suffixes) < 2:
            errors.append("families.growth.suffixes needs at least two "
                          "suffixes (a single suffix is how growth models "
                          "named otherwise went missing)")
        else:
            fleet.growth_suffixes = tuple(str(s) for s in suffixes)

    allow = data.get("allowlist") or {}
    fleet.connection_parameters = set(data.get("connection_parameters") or [])
    clash = sorted(set(allow.get("expressions") or [])
                   & fleet.connection_parameters)
    if clash:
        errors.append("allowlist.expressions names connection parameter(s) "
                      + ", ".join(clash) + "; a connection parameter is never "
                      "cascaded from the template")
    default_conn = data.get("connection") or {}
    if isinstance(default_conn, dict):
        fleet.default_project = str(default_conn.get("project") or "")

    if errors:
        raise DriftDoctorError(f"{path}: " + "; ".join(errors))

    missing = [str(p) for p in [template, *fleet.models.values()]
               if not (p / "definition").is_dir()]
    if missing:
        raise DriftDoctorError(
            f"{path}: model definition dir(s) not found:\n  "
            + "\n  ".join(f"{m}/definition" for m in missing)
            + "\nEach template/model entry must point at a TMDL folder-format "
              "model (a *.SemanticModel folder containing definition/). "
              "Check the paths — they resolve relative to the fleet file.")

    fleet.mapping_tables = list(data.get("mapping_tables") or [])
    fleet.state_dir = (root / (data.get("state_dir") or ".drift-doctor")).resolve()
    fleet.allowlist_kinds = set(allow.get("kinds") or [])
    fleet.allowlist_expressions = set(allow.get("expressions") or [])
    fleet.state_dir.mkdir(parents=True, exist_ok=True)
    return fleet


# ───────────────────────── fleet check helpers ─────────────────────────

OVERLAY_FILES = ("policy.yml", "dq.yml", "mapping.yml")
OVERLAY_REQUIRED = ("version", "client", "description")


def overlay_problems(fleet: Fleet) -> list:
    """``<model>/<file>: <problem>`` for every malformed overlay file.
    Overlays are opt-in: a missing folder, or a folder holding any subset
    of the three files, is fine."""
    conf = fleet.setting("overlays") or {}
    base = fleet.root / (conf.get("dir") if isinstance(conf, dict)
                         and conf.get("dir") else "overlays")
    out = []
    for name in fleet.primary_models:
        folder = base / name
        if not folder.is_dir():
            continue
        for fname in OVERLAY_FILES:
            f = folder / fname
            if not f.is_file():
                continue
            try:
                doc = yaml.safe_load(f.read_text(encoding="utf-8"))
            except yaml.YAMLError as e:
                out.append(f"{name}/{fname}: not valid YAML ({e})".replace(
                    "\n", " "))
                continue
            if not isinstance(doc, dict):
                out.append(f"{name}/{fname}: the top level must be a mapping")
                continue
            absent = [k for k in OVERLAY_REQUIRED if k not in doc]
            if absent:
                out.append(f"{name}/{fname}: missing required field(s): "
                           + ", ".join(absent))
            client = doc.get("client")
            if client is not None and str(client) != name:
                out.append(f"{name}/{fname}: client '{client}' does not "
                           f"match the model id '{name}'")
    return out


def merge_overlay(base: dict, overlay: dict) -> dict:
    """Deep merge: mappings merge per key; scalars and lists replace."""
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_overlay(out[k], v)
        else:
            out[k] = v
    return out


def stamp_problems(fleet: Fleet, stamps: dict) -> list:
    """Problems of a connection-parameter stamping table (keyed by model
    folder, values quoted as in TMDL) against the fleet declaration."""
    by_folder = {}
    for name in fleet.models:
        by_folder.setdefault(fleet.models[name].parent.name, name)
    out = []
    for folder, params in (stamps or {}).items():
        model = by_folder.get(folder)
        if model is None:
            out.append(f"{folder}: stamping entry is unmapped (no fleet "
                       f"model lives in that folder)")
            continue
        project, billing, dataset = fleet.connection(model)
        expected = {"v-BQ_project": project or fleet.default_project,
                    "v-BQ_billing_project": billing or fleet.default_project,
                    "v-db": dataset, "v-BQ_db": dataset}
        values = {k: str(v).strip().strip('"') for k, v in
                  (params or {}).items()}
        for param, want in expected.items():
            if param in values and values[param] != want:
                out.append(f"{folder}: {param} is \"{values[param]}\", "
                           f"expected \"{want}\" (fleet model {model})")
        if "v-db" in values and "v-BQ_db" in values \
                and values["v-db"] != values["v-BQ_db"]:
            out.append(f"{folder}: v-db (\"{values['v-db']}\") and v-BQ_db "
                       f"(\"{values['v-BQ_db']}\") disagree")
    return out

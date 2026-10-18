"""Fleet configuration — ``fleet.yml``.

A fleet is one template model plus N derived models. All paths are resolved
relative to the fleet file's directory. Example::

    version: 1
    template: template/GymChain.SemanticModel
    models:
      alpha: franchises/alpha/GymChain.SemanticModel
      bravo:                                   # long form
        path: franchises/bravo/GymChain.SemanticModel
        gating: advisory                       # findings never fail CI
        overrides:                             # declared, reviewed divergence
          - object: Visits[Total Visits]
            kind: expression                   # expression | absent | retain
            reason: bravo counts zero-minute visits differently
            owner: data-team
            added: 2026-10-01
    mapping_tables:
      - Plan Map
    state_dir: .drift-doctor
    allowlist:
      kinds:
        - measure.missing
        - measure.expression_drift
        # ...
      expressions:
        - Reporting Start Date

Nothing cascades by default: a drift kind the allowlist does not name is
detected and reported but never remediated, and only the NAMED shared
expressions are ever compared or cascaded (a model's connection parameters
are shared expressions too — cascading those would repoint every tenant's
data source).

A model entry is either a bare path or a mapping with ``path`` plus the
optional ``gating`` (``enforced``, the default, or ``advisory``) and
``overrides`` keys. Override entries are schema-checked here; whether each
``object`` resolves is checked against the baseline and ledger at detect
time (``overrides.resolve_overrides``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from . import DriftDoctorError


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
    overrides: dict = field(default_factory=dict)   # name -> [Override]
    connection_parameters: set = field(default_factory=set)

    def is_advisory(self, name: str) -> bool:
        return self.gating.get(name) == "advisory"

    @property
    def template_definition(self) -> Path:
        return self.template / "definition"

    def model_definition(self, name: str) -> Path:
        return self.models[name] / "definition"

    @property
    def baseline_path(self) -> Path:
        return self.state_dir / "baseline.json"

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / "ledger.jsonl"

    @property
    def floors_path(self) -> Path:
        return self.state_dir / "floors.json"


GATING_MODES = ("enforced", "advisory")
OVERRIDE_KINDS = ("expression", "absent", "retain")
_OVERRIDE_KEYS = {"object", "kind", "reason", "owner", "added"}
_MODEL_KEYS = {"path", "gating", "overrides"}


@dataclass(frozen=True)
class Override:
    """One declared, reviewed divergence for one derived model.

    * ``expression``: the model keeps its own definition of a shared object
      (drift on it is acknowledged, never cascaded);
    * ``absent``: the model deliberately lacks a template object;
    * ``retain``: the model deliberately keeps an object retired from the
      template (the retirement channel never removes it).
    """
    model: str
    object: str
    kind: str
    reason: str
    owner: Optional[str] = None
    added: Optional[str] = None

    def to_dict(self) -> dict:
        return {"object": self.object, "kind": self.kind,
                "reason": self.reason, "owner": self.owner,
                "added": self.added}


def _parse_overrides(model: str, raw, errors: list) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append(f"models.{model}.overrides must be a list")
        return []
    out = []
    for i, entry in enumerate(raw):
        where = f"models.{model}.overrides[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} must be a mapping")
            continue
        unknown = sorted(set(entry) - _OVERRIDE_KEYS)
        if unknown:
            errors.append(f"{where}: unknown key(s) {', '.join(unknown)} "
                          f"(allowed: {', '.join(sorted(_OVERRIDE_KEYS))})")
        obj = entry.get("object")
        if not (isinstance(obj, str) and obj.strip()):
            errors.append(f"{where}: `object` must be a non-empty string")
        kind = entry.get("kind")
        if kind not in OVERRIDE_KINDS:
            errors.append(f"{where}: `kind` must be one of "
                          f"{', '.join(OVERRIDE_KINDS)} (got {kind!r})")
        reason = entry.get("reason")
        if not (isinstance(reason, str) and reason.strip()):
            errors.append(f"{where}: `reason` must be a non-empty string; "
                          f"an override without a reason is not reviewable")
        if isinstance(obj, str) and obj.strip() and kind in OVERRIDE_KINDS \
                and isinstance(reason, str) and reason.strip():
            owner, added = entry.get("owner"), entry.get("added")
            out.append(Override(model=model, object=obj.strip(), kind=kind,
                                reason=" ".join(reason.split()),
                                owner=str(owner) if owner is not None else None,
                                added=str(added) if added is not None else None))
    return out


def load_fleet(path) -> Fleet:
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

    errors = []
    if data.get("version") != 1:
        errors.append(f"version must be 1 (got {data.get('version')!r})")
    if not data.get("template"):
        errors.append("`template` is required")
    if not isinstance(data.get("models"), dict) or not data.get("models"):
        errors.append("`models` must be a non-empty mapping of name -> path")
    if errors:
        raise DriftDoctorError(f"{path}: " + "; ".join(errors))

    root = path.parent.resolve()
    template = (root / data["template"]).resolve()
    models, gating, overrides = {}, {}, {}
    for name, entry in data["models"].items():
        if isinstance(entry, str):
            entry = {"path": entry}
        if not isinstance(entry, dict) or not entry.get("path"):
            errors.append(f"models.{name} must be a path or a mapping with "
                          f"a `path` key")
            continue
        unknown = sorted(set(entry) - _MODEL_KEYS)
        if unknown:
            errors.append(f"models.{name}: unknown key(s) {', '.join(unknown)}"
                          f" (allowed: {', '.join(sorted(_MODEL_KEYS))})")
        mode = entry.get("gating") or "enforced"
        if mode not in GATING_MODES:
            errors.append(f"models.{name}.gating must be one of "
                          f"{', '.join(GATING_MODES)} (got {mode!r})")
        models[name] = (root / entry["path"]).resolve()
        gating[name] = mode
        overrides[name] = _parse_overrides(name, entry.get("overrides"), errors)
    if errors:
        raise DriftDoctorError(f"{path}: " + "; ".join(errors))

    missing = [str(p) for p in [template, *models.values()]
               if not (p / "definition").is_dir()]
    if missing:
        raise DriftDoctorError(
            f"{path}: model definition dir(s) not found:\n  "
            + "\n  ".join(f"{m}/definition" for m in missing)
            + "\nEach template/model entry must point at a TMDL folder-format "
              "model (a *.SemanticModel folder containing definition/). "
              "Check the paths — they resolve relative to the fleet file.")

    allow = data.get("allowlist") or {}
    fleet = Fleet(
        root=root,
        template=template,
        models=models,
        mapping_tables=list(data.get("mapping_tables") or []),
        state_dir=(root / (data.get("state_dir") or ".drift-doctor")).resolve(),
        allowlist_kinds=set(allow.get("kinds") or []),
        allowlist_expressions=set(allow.get("expressions") or []),
        gating=gating,
        overrides=overrides,
        connection_parameters=set(data.get("connection_parameters") or []),
    )
    fleet.state_dir.mkdir(parents=True, exist_ok=True)
    return fleet

"""Fleet configuration — ``fleet.yml``.

A fleet is one template model plus N derived models. All paths are resolved
relative to the fleet file's directory. Example::

    version: 1
    template: template/GymChain.SemanticModel
    models:
      alpha: franchises/alpha/GymChain.SemanticModel
      bravo: franchises/bravo/GymChain.SemanticModel
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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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


def load_fleet(path) -> Fleet:
    path = Path(path)
    if not path.exists():
        raise DriftDoctorError(
            f"fleet file not found: {path}. Create one (see "
            f"docs/reference/configuration.md for the schema) or point "
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
    models = {name: (root / p).resolve() for name, p in data["models"].items()}

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
    )
    fleet.state_dir.mkdir(parents=True, exist_ok=True)
    return fleet
